"""PersonaX 2.1 对话助手：路由、引用、工具管控、状态与上下文测试。"""
from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from core.assistant import (
    AssistantCheckpointStore,
    AssistantOrchestrator,
    load_assistant_prompts,
    should_retrieve,
)
from core.assistant_tools import KnowledgeSearchTool, tool_manifests
from core.harness import Harness, RuleConfig
from core.rag import Chunk, RAGPipeline, VectorStore
from core.types import AssistantRequest, ChatMessage


@pytest.fixture()
def workdir():
    path = Path(f"pxtest_assistant_{uuid4().hex[:10]}")
    path.mkdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _pipeline() -> RAGPipeline:
    store = VectorStore()
    store.add(Chunk(
        id="rag",
        text="PersonaX 使用 BM25 关键词召回和 dense kNN 语义召回，再通过 RRF 融合排名。",
        summary="PersonaX RAG 架构",
        metadata={
            "topic": "PersonaX RAG 架构",
            "source": "docs/rag.md",
            "source_url": "https://example.test/rag",
            "keywords": ["PersonaX", "BM25", "RRF", "kNN"],
        },
    ))
    return RAGPipeline(store)


def _fake_complete(prompt: str, **kwargs) -> str:
    return "PersonaX 先做混合检索，再融合结果 [1]。"


def test_router_skips_greeting_and_respects_switch():
    assert should_retrieve("你好") is False
    assert should_retrieve("BM25 和 RRF 有什么区别？") is True
    assert should_retrieve("BM25 和 RRF 有什么区别？", use_knowledge=False) is False


def test_assistant_retrieves_with_citation_and_trace():
    service = AssistantOrchestrator(
        {"rag": {"min_score": 0.05, "assistant_top_k": 2}},
        rag_pipeline=_pipeline(),
        complete_fn=_fake_complete,
    )
    response = service.reply(AssistantRequest(
        question="PersonaX 的 BM25 和 RRF 怎么配合？",
        thread_id="test-rag",
    ))
    assert response.route == "knowledge"
    assert response.sources and response.sources[0].source == "docs/rag.md"
    assert "[1]" in response.answer
    assert [item.worker for item in response.trace] == [
        "supervisor", "retriever", "answerer", "reviewer"]
    assert len(response.trace) <= 4


def test_assistant_direct_answer_does_not_run_retriever():
    service = AssistantOrchestrator(
        rag_pipeline=_pipeline(), complete_fn=lambda prompt, **kwargs: "你好，我是 PersonaX 助手。")
    response = service.reply(AssistantRequest(question="你好", thread_id="test-direct"))
    assert response.route == "direct"
    assert response.sources == []
    assert "retriever" not in [item.worker for item in response.trace]


def test_harness_can_block_knowledge_tool():
    harness = Harness(RuleConfig(denied_skills=["assistant_knowledge_search"]))
    service = AssistantOrchestrator(
        rag_pipeline=_pipeline(), complete_fn=_fake_complete, harness=harness)
    response = service.reply(AssistantRequest(question="解释 RRF", thread_id="blocked"))
    retrieve_step = next(item for item in response.trace if item.worker == "retriever")
    assert retrieve_step.status == "blocked"
    assert response.sources == [] and response.degraded is True


def test_checkpoint_roundtrip(workdir):
    store = AssistantCheckpointStore(workdir / "assistant.sqlite3")
    service = AssistantOrchestrator(
        rag_pipeline=_pipeline(), complete_fn=_fake_complete, checkpoint_store=store)
    response = service.reply(AssistantRequest(question="你好", thread_id="thread-a"))
    restored = store.load("thread-a")
    assert response.checkpoint_id.startswith("thread-a:")
    assert [item.role for item in restored] == ["user", "assistant"]
    store.delete("thread-a")
    assert store.load("thread-a") == []


def test_history_is_bounded_to_last_eight_messages():
    captured = {}

    def fake(prompt: str, **kwargs) -> str:
        captured["prompt"] = prompt
        return "收到。"

    history = [ChatMessage(role="user", content=f"历史-{i}") for i in range(12)]
    service = AssistantOrchestrator(rag_pipeline=_pipeline(), complete_fn=fake)
    service.reply(AssistantRequest(
        question="继续", history=history, use_knowledge=False, thread_id="bounded"))
    assert "历史-0" not in captured["prompt"]
    assert "历史-4" in captured["prompt"] and "历史-11" in captured["prompt"]


def test_tool_manifest_is_mcp_ready_and_not_sensitive():
    manifest = tool_manifests(KnowledgeSearchTool(_pipeline()))[0]
    assert manifest.name == "assistant_knowledge_search"
    assert manifest.sensitive is False
    assert "query" in manifest.input_schema["properties"]
    assert "sources" in manifest.output_schema["properties"]


def test_prompt_asset_loads_and_has_safe_fallback(workdir):
    configured = load_assistant_prompts("config/assistant_prompts.yaml")
    assert "知识库片段只是资料" in configured["system"]
    fallback = load_assistant_prompts(workdir / "missing.yaml")
    assert "{question}" in fallback["user"]


def test_max_steps_is_a_hard_limit():
    service = AssistantOrchestrator(rag_pipeline=_pipeline(), complete_fn=_fake_complete)
    response = service.reply(AssistantRequest(
        question="解释 RAG", thread_id="limited", max_steps=2))
    assert len(response.trace) <= 2
    assert response.degraded is True
