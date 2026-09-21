"""PersonaX 2.2 对话助手：路由、引用、工具管控、状态与上下文测试。"""
from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from core.assistant import (
    AssistantCheckpointStore,
    AssistantMemoryStore,
    AssistantOrchestrator,
    load_assistant_prompts,
    should_retrieve,
)
from core.llm import complete_stream
from core.assistant_tools import KnowledgeSearchTool, tool_manifests
from core.harness import Harness, RuleConfig
from core.rag import Chunk, RAGPipeline, VectorStore
from core.types import AssistantRequest, ChatMessage, KnowledgeSearchInput


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
    assert should_retrieve("你能做什么") is False
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


def test_missing_evidence_is_disclosed_instead_of_presented_as_grounded():
    service = AssistantOrchestrator(
        {"rag": {"min_score": 0.95, "assistant_top_k": 2}},
        rag_pipeline=_pipeline(),
        complete_fn=lambda prompt, **kwargs: "这里给出一个通用回答。",
    )
    response = service.reply(AssistantRequest(
        question="解释一个知识库没有覆盖的主题",
        thread_id="missing-evidence",
    ))

    assert response.route == "knowledge"
    assert response.sources == []
    assert response.answer.startswith("本地知识库未检索到可核验资料")
    assert response.degraded is True
    assert response.trace[-1].worker == "reviewer"


def test_invalid_citation_number_is_visible_in_reviewer_trace():
    service = AssistantOrchestrator(
        {"rag": {"min_score": 0.0, "assistant_top_k": 1}},
        rag_pipeline=_pipeline(),
        complete_fn=lambda prompt, **kwargs: "混合检索结合关键词和向量 [9]。",
    )
    response = service.reply(AssistantRequest(
        question="PersonaX 的 BM25 和 RRF 怎么配合？",
        thread_id="invalid-citation",
    ))

    assert len(response.sources) == 1
    assert "无法对应当前来源列表" in response.answer
    assert "无效引用编号" in response.trace[-1].detail
    assert response.degraded is True


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


def test_checkpoint_lists_recent_threads(workdir):
    store = AssistantCheckpointStore(workdir / "assistant.sqlite3")
    store.save("older", [ChatMessage(role="user", content="旧问题")], "direct")
    store.save("newer", [ChatMessage(role="user", content="新的 RAG 问题")], "knowledge")

    summaries = store.list_threads(limit=10)

    assert [item.thread_id for item in summaries] == ["newer", "older"]
    assert summaries[0].preview == "新的 RAG 问题"
    assert summaries[0].message_count == 1


def test_long_term_memory_is_explicit_and_deletable(workdir):
    store = AssistantMemoryStore(workdir / "assistant.sqlite3")
    saved = store.add("local_user", " 我正在准备武汉 Agent 岗。 ")
    duplicate = store.add("local_user", "我正在准备武汉 Agent 岗。")

    memories = store.list("local_user")

    assert saved.memory_id == duplicate.memory_id
    assert [item.content for item in memories] == ["我正在准备武汉 Agent 岗。"]
    store.delete("local_user", saved.memory_id)
    assert store.list("local_user") == []


def test_streaming_reply_persists_completed_answer(workdir):
    store = AssistantCheckpointStore(workdir / "assistant.sqlite3")
    service = AssistantOrchestrator(
        rag_pipeline=_pipeline(),
        complete_fn=_fake_complete,
        stream_complete_fn=lambda prompt, **kwargs: iter(["流式", "回答"]),
        checkpoint_store=store,
    )

    response_stream = service.reply_stream(AssistantRequest(
        question="你好", thread_id="stream-thread", use_knowledge=False,
    ))
    rendered = "".join(response_stream)

    assert rendered == "流式回答"
    assert response_stream.response is not None
    assert response_stream.response.answer == rendered
    assert store.load("stream-thread")[-1].content == rendered
    answer_step = next(
        item for item in response_stream.response.trace if item.worker == "answerer")
    assert "mode=stream" in answer_step.detail


def test_streaming_prompt_includes_explicit_memory():
    captured = {}

    def fake_stream(prompt: str, **kwargs):
        captured["prompt"] = prompt
        yield "收到。"

    service = AssistantOrchestrator(
        rag_pipeline=_pipeline(), stream_complete_fn=fake_stream,
    )
    response_stream = service.reply_stream(AssistantRequest(
        question="继续建议", use_knowledge=False,
        memories=["目标岗位是武汉 Agent 工程师"],
    ))
    assert "".join(response_stream) == "收到。"
    assert "<untrusted_user_memory_json>" in captured["prompt"]
    assert "武汉 Agent 工程师" in captured["prompt"]


def test_llm_stream_has_offline_fallback():
    rendered = "".join(complete_stream("普通问题", max_retries=1))
    assert rendered


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


def test_history_window_settings_are_visible_in_trace():
    def fake(prompt: str, **kwargs) -> str:
        return "收到。"

    history = [
        ChatMessage(role="user", content=f"消息-{index}-" + "长内容" * 80)
        for index in range(6)
    ]
    service = AssistantOrchestrator(
        persona={"assistant": {
            "context_window_messages": 3,
            "context_window_tokens": 220,
            "context_message_tokens": 120,
        }},
        rag_pipeline=_pipeline(), complete_fn=fake,
    )
    response = service.reply(AssistantRequest(
        question="继续", history=history, use_knowledge=False,
    ))
    answer_trace = next(step for step in response.trace if step.worker == "answerer")

    assert "history=" in answer_trace.detail
    assert "history_tokens~" in answer_trace.detail
    assert "history_dropped=" in answer_trace.detail


def test_tool_manifest_is_mcp_ready_and_not_sensitive():
    manifest = tool_manifests(KnowledgeSearchTool(_pipeline()))[0]
    assert manifest.name == "assistant_knowledge_search"
    assert manifest.sensitive is False
    assert "query" in manifest.input_schema["properties"]
    assert "sources" in manifest.output_schema["properties"]


def test_knowledge_tool_returns_distinct_sources():
    store = VectorStore()
    store.add(Chunk(
        id="a-1",
        text="BM25 关键词召回第一段",
        metadata={"source": "same.md", "topic": "BM25"},
    ))
    store.add(Chunk(
        id="a-2",
        text="BM25 关键词召回第二段",
        metadata={"source": "same.md", "topic": "BM25"},
    ))
    store.add(Chunk(
        id="b-1",
        text="RRF 融合多路排名",
        metadata={"source": "other.md", "topic": "RRF"},
    ))

    result = KnowledgeSearchTool(RAGPipeline(store)).run(
        KnowledgeSearchInput(query="BM25 RRF", top_k=3, min_score=0.0)
    )

    assert len({source.source for source in result.sources}) == len(result.sources)
    assert {source.source for source in result.sources} == {"same.md", "other.md"}


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
