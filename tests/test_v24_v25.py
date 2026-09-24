"""PersonaX 2.4/2.5：可靠性、HITL、父子检索与记忆治理回归。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.agent_runtime import AgentRunBudget, RetryPolicy
from core.approval import PublishApprovalStore
from core.assistant import AssistantMemoryStore
from core.assistant_tools import KnowledgeSearchTool
from core.harness import Harness, RuleConfig
from core.memory import propose_memory_summary, sanitize_memory_text
from core.rag import HashingEmbedder, RAGPipeline, VectorStore, build_rag_from_dir
from core.tool_gateway import AssistantToolGateway
from core.types import (
    AssistantSource,
    ChatMessage,
    Draft,
    KnowledgeSearchOutput,
    ToolCallRequest,
)
from eval.security_scorer import evaluate as evaluate_security
from eval.rag_scorer import compare_rerankers
from publishers.xhs import ApprovalDenied, XhsPlaywrightPublisher


def _draft(body: str = "正文") -> Draft:
    return Draft(topic="测试", title="审批测试", body=body, tags=["#测试"])


def test_agent_budget_blocks_loops_steps_and_token_overflow():
    budget = AgentRunBudget(
        max_steps=2, max_estimated_tokens=8, repeated_state_limit=1
    )
    budget.consume_step("route", {"next": "answer"})
    with pytest.raises(RuntimeError, match="死循环"):
        budget.consume_step("route", {"next": "answer"})
    with pytest.raises(RuntimeError, match="Token"):
        budget.reserve_text("这是一段明显超过八个估算 token 的文本" * 3)


def test_tool_gateway_retries_transient_error_and_bounds_output():
    class FlakyKnowledgeTool(KnowledgeSearchTool):
        def __init__(self):
            super().__init__(RAGPipeline(VectorStore()))
            self.calls = 0

        def run(self, inp):
            self.calls += 1
            if self.calls < 3:
                raise ConnectionError("temporary")
            return KnowledgeSearchOutput(
                sources=[
                    AssistantSource(ref_id="1", title="a", excerpt="a" * 400),
                    AssistantSource(ref_id="2", title="b", excerpt="b" * 400),
                ],
                trace={},
            )

    tool = FlakyKnowledgeTool()
    gateway = AssistantToolGateway(
        tool,
        Harness(RuleConfig(rate_limit=20)),
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0),
        max_output_chars=500,
    )
    result = gateway.call(ToolCallRequest(
        name=tool.name,
        arguments={"query": "test", "top_k": 2, "min_score": 0.0},
        user_id="tester",
        trace_id="trace-retry-test",
    ))
    assert result.status == "ok" and result.attempts == 3
    assert result.trace_id == "trace-retry-test"
    assert result.output is not None
    assert sum(len(item.excerpt) for item in result.output.sources) == 500
    assert result.output.trace["tool_output_truncated"] is True


def test_publish_approval_survives_restart_invalidates_edits_and_is_consumed(tmp_path: Path):
    db_path = tmp_path / "workflow.sqlite3"
    draft = _draft()
    requested = PublishApprovalStore(db_path).request(draft, "web_user")
    assert requested.status == "pending" and requested.interrupt_id
    duplicate = PublishApprovalStore(db_path).request(draft, "web_user")
    assert duplicate.request_id == requested.request_id

    # 新实例模拟应用重启；Command(resume) 从 SQLite checkpoint 继续。
    approved = PublishApprovalStore(db_path).decide(
        requested.request_id, "approved", reason="人工确认"
    )
    assert approved.status == "approved"
    with pytest.raises(PermissionError, match="草稿已在审批后修改"):
        PublishApprovalStore(db_path).require_approved(
            requested.request_id, _draft("被修改的正文"), "web_user"
        )

    publisher = XhsPlaywrightPublisher(
        max_retries=1,
        auto_approve=True,
        user_id="web_user",
        approval_store=PublishApprovalStore(db_path),
        approval_request_id=requested.request_id,
        require_persisted_approval=True,
    )
    publisher._login_state_missing = lambda: None  # type: ignore[method-assign]
    publisher._do_publish = lambda _draft: "https://example.test/published"  # type: ignore[method-assign]
    first = publisher.publish(draft)
    second = publisher.publish(_draft("即使调用方换了正文也不会再次触发平台操作"))
    assert first.success and first.url == "https://example.test/published"
    assert second.success and "IDEMPOTENT" in second.message
    assert PublishApprovalStore(db_path).get(requested.request_id).status == "consumed"  # type: ignore[union-attr]


def test_real_publish_rejects_missing_persisted_approval():
    publisher = XhsPlaywrightPublisher(
        auto_approve=True,
        user_id="web_user",
        require_persisted_approval=True,
    )
    with pytest.raises(ApprovalDenied, match="缺少持久化审批"):
        publisher.publish(_draft())


def test_memory_governance_masks_pii_supports_ttl_export_and_delete(tmp_path: Path):
    db_path = tmp_path / "assistant.sqlite3"
    store = AssistantMemoryStore(db_path)
    memory = store.add(
        "u",
        "我的手机号是 13800138000，邮箱 test.user@example.com",
        kind="summary",
        source_thread_id="thread-1",
        ttl_days=30,
    )
    assert memory.kind == "summary" and memory.redacted
    assert "13800138000" not in memory.content and "test.user" not in memory.content
    assert store.export("u")[0]["source_thread_id"] == "thread-1"

    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE assistant_memories SET expires_at = 1 WHERE memory_id = ?", (memory.memory_id,))
        db.commit()
    assert store.list("u") == []
    assert store.delete_all("u") == 1


def test_summary_is_only_a_candidate_and_is_sanitized():
    calls = []

    def fake_complete(prompt: str, **kwargs) -> str:
        calls.append((prompt, kwargs))
        return "用户准备 Agent 岗，手机号 13800138000"

    candidate = propose_memory_summary(
        [ChatMessage(role="user", content="我准备 Agent 岗")], fake_complete
    )
    assert "Agent 岗" in candidate and "13800138000" not in candidate
    assert len(calls) == 1
    sanitized, redacted = sanitize_memory_text("身份证 110101199001011234")
    assert redacted and "110101199001011234" not in sanitized


def test_heading_parent_child_chunks_keep_versions_and_expand_context(tmp_path: Path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    body = (
        "---\ntopic: Agent 架构\n---\n"
        "# 检索层\n" + "BM25 与稠密向量通过 RRF 融合。" * 20
        + "\n# 安全层\n发布前必须人工审批。"
    )
    (knowledge / "agent.md").write_text(body, encoding="utf-8")
    pipeline = build_rag_from_dir(
        knowledge, chunk_size=80, embedding_backend="hashing",
        environment_overrides=False,
    )
    chunks = list(pipeline.store.chunks.values())
    assert pipeline.chunk_strategy == "heading_parent_child" and pipeline.index_version
    assert len(chunks) > 2
    assert all(chunk.metadata["document_version"] for chunk in chunks)
    assert all(chunk.metadata["index_version"] == pipeline.index_version for chunk in chunks)
    assert any(len(chunk.context_text) > len(chunk.text) for chunk in chunks)

    (knowledge / "agent.md").write_text(body + "\n新增版本内容。", encoding="utf-8")
    refreshed = build_rag_from_dir(
        knowledge, chunk_size=80, embedding_backend="hashing",
        environment_overrides=False,
    )
    assert refreshed is not pipeline
    assert refreshed.index_version != pipeline.index_version


def test_failed_index_is_not_cached_and_can_recover(tmp_path: Path, monkeypatch):
    import core.rag as rag

    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "doc.md").write_text("# RAG\nBM25 RRF dense", encoding="utf-8")

    class BrokenEmbedder:
        name = "broken"
        dimension = 0

        def encode(self, texts):
            raise OSError("offline")

    monkeypatch.setattr(rag, "_make_embedder", lambda backend, model: BrokenEmbedder())
    failed = build_rag_from_dir(
        knowledge, embedding_backend="ollama", environment_overrides=False
    )
    assert failed.store.last_index_error

    monkeypatch.setattr(rag, "_make_embedder", lambda backend, model: HashingEmbedder())
    recovered = build_rag_from_dir(
        knowledge, embedding_backend="ollama", environment_overrides=False
    )
    assert recovered is not failed and not recovered.store.last_index_error


def test_security_red_team_dataset_passes():
    report = evaluate_security()
    assert report["cases"] >= 16
    assert report["passed"] is True


def test_cross_encoder_ablation_selects_metrics_winner(monkeypatch):
    def fake_evaluate(*, reranker, **kwargs):
        cross = reranker == "cross-encoder"
        return {
            "gated_recall_at_k": 0.9 if cross else 0.8,
            "gated_ndcg_at_k": 0.88 if cross else 0.72,
            "mrr": 0.85 if cross else 0.75,
            "no_hit_accuracy": 1.0,
            "latency_ms": {"p95": 40.0 if cross else 10.0},
            "trace": {"degraded_components": []},
            "passed": True,
            "details": [],
        }

    monkeypatch.setattr("eval.rag_scorer.evaluate", fake_evaluate)
    report = compare_rerankers()
    assert report["winner"] == "cross_encoder"
    assert report["cross_encoder_available"] is True
    assert report["passed"] is True
