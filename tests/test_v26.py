"""PersonaX 2.6 事实支持度与可观测闭环回归。"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

from core.assistant import AssistantOrchestrator
from core.assistant_tools import KnowledgeSearchTool
from core.grounding import claim_support, evaluate_answer_grounding
from core.harness import Harness, RuleConfig
from core.llm import _record_usage
from core.observability import (
    aggregate_usage,
    load_usage_events,
    safe_event_rows,
    trace_events,
)
from core.rag import Chunk, RAGPipeline, VectorStore
from core.tool_gateway import AssistantToolGateway
from core.types import AssistantRequest, AssistantSource, ToolCallRequest, UsageEvent
from eval.grounding_scorer import evaluate


def _source(excerpt: str, ref_id: str = "1") -> AssistantSource:
    return AssistantSource(
        ref_id=ref_id,
        title="PersonaX 工程说明",
        source="README.md",
        excerpt=excerpt,
    )


def _pipeline() -> RAGPipeline:
    store = VectorStore()
    store.add(Chunk(
        id="rag",
        text="PersonaX 使用 BM25 和 RRF 完成混合检索。",
        summary="PersonaX RAG",
        metadata={"topic": "RAG", "source": "README.md"},
    ))
    return RAGPipeline(store)


def test_grounding_release_dataset_passes():
    report = evaluate()
    assert report["passed"] is True
    assert report["cases"] == 16
    assert report["accuracy"] >= 0.90
    assert report["unsupported_recall"] >= 0.85


def test_grounding_rejects_invalid_and_contradictory_citations():
    sources = [_source("ANN 尚未引入，目前使用精确 kNN。")]
    invalid = claim_support("系统已经启用 ANN [2]。", sources)
    contradicted = claim_support("系统已经启用 ANN [1]。", sources)
    report = evaluate_answer_grounding(
        "系统已经启用 ANN [1]。第二句没有引用。", sources,
    )

    assert invalid.supported is False and "无效引用" in invalid.reason
    assert contradicted.supported is False and "否定冲突" in contradicted.reason
    assert report.supported_claims == 0
    assert report.cited_claims == 1


def test_grounding_accepts_citation_after_sentence_punctuation():
    report = evaluate_answer_grounding(
        "PersonaX 使用 BM25 和 RRF 完成混合检索。[1]",
        [_source("PersonaX 使用 BM25 和 RRF 完成混合检索。")],
    )

    assert report.cited_claims == 1
    assert report.supported_claims == 1


def test_usage_loader_skips_malformed_and_oversized_lines(tmp_path):
    path = tmp_path / "usage.jsonl"
    path.write_text(
        json.dumps({"event": "llm_call", "trace_id": "trace-1"})
        + "\nnot-json\n"
        + json.dumps({"event": "tool_call", "tool": "x"})
        + "x" * 200
        + "\n",
        encoding="utf-8",
    )

    events, malformed = load_usage_events(path, max_line_bytes=128)

    assert [item.event for item in events] == ["llm_call"]
    assert malformed == 2


def test_observability_aggregates_quality_latency_tokens_and_cost():
    events = [
        UsageEvent(event="llm_call", trace_id="t1", latency_ms=100,
                   prompt_tokens=100, completion_tokens=20),
        UsageEvent(event="llm_call", trace_id="t1", latency_ms=200,
                   prompt_tokens=200, completion_tokens=30),
        UsageEvent(event="tool_call", trace_id="t1", status="ok", latency_ms=20),
        UsageEvent(event="tool_call", trace_id="t2", status="error", latency_ms=40),
        UsageEvent(event="assistant_reply", trace_id="t1", degraded=False, wall_ms=300),
        UsageEvent(event="assistant_reply", trace_id="t2", degraded=True, wall_ms=500),
    ]

    summary = aggregate_usage(
        events,
        malformed_lines=1,
        input_cny_per_million=10,
        output_cny_per_million=20,
    )

    assert summary.trace_count == 2
    assert summary.tool_success_rate == 0.5
    assert summary.assistant_degraded_rate == 0.5
    assert summary.llm_p95_ms == 195.0
    assert summary.assistant_p95_ms == 490.0
    assert summary.prompt_tokens == 300 and summary.completion_tokens == 50
    assert summary.estimated_cost_cny == 0.004
    assert summary.pricing_configured is True

    selected = trace_events(events, "t1")
    safe_rows = safe_event_rows(selected)
    assert len(selected) == 4
    assert all("prompt" not in str(row).lower() for row in safe_rows)


def test_observability_ignores_invalid_or_negative_price_config(monkeypatch):
    monkeypatch.setenv("LLM_INPUT_COST_CNY_PER_MILLION", "not-a-number")
    monkeypatch.setenv("LLM_OUTPUT_COST_CNY_PER_MILLION", "-20")

    summary = aggregate_usage([
        UsageEvent(event="llm_call", prompt_tokens=100, completion_tokens=50),
    ])

    assert summary.pricing_configured is False
    assert summary.estimated_cost_cny == 0.0


def test_tool_and_llm_telemetry_share_trace_id(tmp_path, monkeypatch):
    from core import usage

    usage_path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(usage, "_PATH", usage_path)
    gateway = AssistantToolGateway(
        KnowledgeSearchTool(_pipeline()),
        Harness(RuleConfig(rate_limit=5)),
    )
    request = ToolCallRequest(
        name="assistant_knowledge_search",
        arguments={"query": "BM25 RRF", "top_k": 1, "min_score": 0.0},
        user_id="test-user",
        trace_id="trace-shared",
    )
    assert gateway.call(request).status == "ok"

    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4),
    )
    _record_usage("fake-model", time.time(), response, trace_id="trace-shared")
    events, malformed = load_usage_events(usage_path)

    assert malformed == 0
    assert {item.event for item in events} == {"tool_call", "llm_call"}
    assert {item.trace_id for item in events} == {"trace-shared"}


def test_assistant_passes_trace_id_to_router_and_answer_model():
    received: list[str] = []

    def fake_complete(prompt: str, *, system: str = "", **kwargs) -> str:
        received.append(kwargs.get("trace_id", ""))
        if "只读路由器" in system:
            return '{"route":"direct","reason":"无需检索"}'
        return "这是直接回答。"

    service = AssistantOrchestrator(
        {"assistant": {"router": "llm"}},
        rag_pipeline=_pipeline(),
        complete_fn=fake_complete,
    )
    response = service.reply(AssistantRequest(
        question="你好", thread_id="trace-test", trace_id="trace-e2e-001",
    ))

    assert response.trace_id == "trace-e2e-001"
    assert received == ["trace-e2e-001", "trace-e2e-001"]


def test_assistant_marks_cited_but_unsupported_claim_as_degraded():
    def fake_complete(prompt: str, **kwargs) -> str:
        return "PersonaX 已经用 ANN 完全替代 BM25 [1]。"

    service = AssistantOrchestrator(
        {"rag": {"min_score": 0.0, "assistant_top_k": 1}},
        rag_pipeline=_pipeline(),
        complete_fn=fake_complete,
    )
    response = service.reply(AssistantRequest(
        question="PersonaX 的检索方案是什么？",
        thread_id="grounding-runtime",
    ))

    assert response.degraded is True
    assert "未通过本地词法支持度检查" in response.answer
    assert "答案支持度降级" in response.trace[-1].detail
