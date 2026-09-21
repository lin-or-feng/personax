"""文档 1-8 模块对应的 Agent 路由、工具网关与 RAG 降级回归。"""
from __future__ import annotations

from core.assistant import AssistantOrchestrator
from core.assistant_tools import KnowledgeSearchTool
from core.harness import Harness, RuleConfig
from core.rag import Chunk, RAGPipeline, VectorStore
from core.tool_gateway import AssistantToolGateway
from core.types import AssistantRequest, ToolCallRequest


def _pipeline() -> RAGPipeline:
    store = VectorStore()
    store.add(Chunk(
        id="rag",
        text="PersonaX 使用 BM25 关键词检索、dense kNN 语义检索和 RRF 融合。",
        summary="PersonaX RAG",
        metadata={"topic": "PersonaX RAG", "source": "docs/rag.md", "keywords": ["BM25", "RRF"]},
    ))
    return RAGPipeline(store)


def test_structured_llm_router_uses_valid_json():
    calls: list[str] = []

    def fake_complete(prompt: str, *, system: str = "", **kwargs) -> str:
        calls.append(system)
        if "只读路由器" in system:
            return '```json\n{"route":"direct","reason":"只是问候"}\n```'
        return "你好，我是 PersonaX 助手。"

    service = AssistantOrchestrator(
        {"assistant": {"router": "llm"}},
        rag_pipeline=_pipeline(),
        complete_fn=fake_complete,
    )
    response = service.reply(AssistantRequest(question="你好呀", thread_id="llm-router"))
    assert response.route == "direct"
    assert "router=llm" in response.trace[0].detail
    assert len(calls) == 2


def test_invalid_llm_router_falls_back_to_rule():
    def fake_complete(prompt: str, *, system: str = "", **kwargs) -> str:
        if "只读路由器" in system:
            return "这不是 JSON"
        return "根据本地资料，BM25 与 RRF 可以配合 [1]。"

    service = AssistantOrchestrator(
        {
            "assistant": {"router": "llm"},
            "rag": {"min_score": 0.0, "assistant_top_k": 1},
        },
        rag_pipeline=_pipeline(),
        complete_fn=fake_complete,
    )
    response = service.reply(AssistantRequest(question="请解释 BM25 和 RRF", thread_id="fallback"))
    assert response.route == "knowledge"
    assert response.sources
    assert "router=fallback" in response.trace[0].detail


def test_manual_knowledge_switch_bypasses_llm_router():
    router_calls = 0

    def fake_complete(prompt: str, *, system: str = "", **kwargs) -> str:
        nonlocal router_calls
        if "只读路由器" in system:
            router_calls += 1
        return "直接回答。"

    service = AssistantOrchestrator(
        {"assistant": {"router": "llm"}},
        rag_pipeline=_pipeline(),
        complete_fn=fake_complete,
    )
    response = service.reply(AssistantRequest(
        question="请解释 BM25", use_knowledge=False, thread_id="manual-off"))
    assert response.route == "direct"
    assert "router=manual" in response.trace[0].detail
    assert router_calls == 0


def test_read_only_tool_gateway_validates_and_audits_calls():
    harness = Harness(RuleConfig(rate_limit=5))
    gateway = AssistantToolGateway(KnowledgeSearchTool(_pipeline()), harness)
    result = gateway.call(ToolCallRequest(
        name="assistant_knowledge_search",
        arguments={"query": "BM25 RRF", "top_k": 1, "min_score": 0.0},
        user_id="gateway-user",
    ))
    assert result.status == "ok" and result.output and result.output.sources
    assert {item.name for item in gateway.list_tools()} == {"assistant_knowledge_search"}
    assert any(item["event"] == "tool_completed" for item in harness.audit.entries)

    langchain_tool = gateway.as_langchain_tool("langchain-user")
    langchain_result = langchain_tool.invoke(
        {"query": "BM25 RRF", "top_k": 1, "min_score": 0.0})
    assert langchain_tool.name == "assistant_knowledge_search"
    assert langchain_result["status"] == "ok"
    assert langchain_result["output"]["sources"]


def test_read_only_tool_gateway_blocks_unknown_and_invalid_calls():
    harness = Harness(RuleConfig(rate_limit=5))
    gateway = AssistantToolGateway(KnowledgeSearchTool(_pipeline()), harness)
    unknown = gateway.call(ToolCallRequest(
        name="xhs_publish", arguments={}, user_id="gateway-user"))
    invalid = gateway.call(ToolCallRequest(
        name="assistant_knowledge_search",
        arguments={"query": "", "top_k": 999},
        user_id="gateway-user",
    ))
    assert unknown.status == "blocked"
    assert invalid.status == "error" and "参数校验失败" in invalid.error


def test_dense_failure_degrades_to_bm25_rrf():
    pipeline = _pipeline()

    class FailingEmbedder:
        name = "failing-embedder"
        dimension = 8

        def encode(self, texts):
            raise RuntimeError("embedding unavailable")

    pipeline.store.embedder = FailingEmbedder()
    hits = pipeline.retrieve_hits("PersonaX BM25 RRF", top_k=1)
    assert hits and hits[0].chunk.id == "rag"
    assert pipeline.last_trace["dense_mode"] == "unavailable"
    assert pipeline.last_trace["degradation_reasons"]["dense_search"] == "RuntimeError"


def test_document_embedding_index_failure_keeps_bm25_available(tmp_path, monkeypatch):
    from core.rag import build_rag_from_dir

    class FailingEmbedder:
        name = "ollama:test"
        dimension = 0

        def encode(self, texts):
            raise RuntimeError("embedding service offline")

    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "rag.md").write_text(
        "---\ntopic: PersonaX RAG\nkeywords: [BM25, RRF]\n---\n"
        "PersonaX 使用 BM25 关键词检索和 RRF 融合。",
        encoding="utf-8",
    )
    monkeypatch.setattr("core.rag._make_embedder", lambda backend, model: FailingEmbedder())

    pipeline = build_rag_from_dir(
        knowledge,
        embedding_backend="ollama",
        environment_overrides=False,
    )
    hits = pipeline.retrieve_hits("PersonaX BM25 RRF", top_k=1)

    assert hits and hits[0].chunk.metadata["source"] == "rag.md"
    assert pipeline.last_trace["dense_mode"] == "unavailable"
    assert "dense_index" in pipeline.last_trace["degraded_components"]


def test_reranker_failure_preserves_rrf_results():
    pipeline = _pipeline()

    class FailingReranker:
        model_name = "failing-reranker"

        def rerank(self, query, chunks, top_k):
            raise RuntimeError("reranker unavailable")

    pipeline.reranker = FailingReranker()
    hits = pipeline.retrieve_hits("PersonaX BM25 RRF", top_k=1)
    assert hits and hits[0].chunk.id == "rag"
    assert "reranker" in pipeline.last_trace["degraded_components"]


def test_query_enhancer_and_hyde_failure_fall_back_to_rules():
    pipeline = _pipeline()

    class FailingEnhancer:
        def rewrite(self, query, n=3):
            raise RuntimeError("rewrite unavailable")

        def hyde(self, query):
            raise RuntimeError("hyde unavailable")

    pipeline.enhancer = FailingEnhancer()
    pipeline.enable_hyde = True
    hits = pipeline.retrieve_hits("PersonaX BM25", top_k=1)
    assert hits
    assert pipeline.last_trace["degraded_components"] == ["query_enhancer", "hyde"]


def test_assistant_uses_langchain_structured_tool_by_default():
    service = AssistantOrchestrator(
        {"rag": {"min_score": 0.0, "assistant_top_k": 1}},
        rag_pipeline=_pipeline(),
        complete_fn=lambda prompt, **kwargs: "根据资料，这是混合检索 [1]。",
    )
    response = service.reply(AssistantRequest(
        question="PersonaX 怎么做 BM25 和 RRF？",
        thread_id="langchain-runtime",
    ))
    retriever = next(step for step in response.trace if step.worker == "retriever")
    assert retriever.status == "ok"
    assert "runtime=langchain" in retriever.detail
