"""Integration, refusal and current-version guarantees; no live model calls."""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from api import create_app
from core.assistant import AssistantOrchestrator
from core.knowledge import KnowledgeBaseService
from core.types import AssistantAPIRequest, AssistantRequest, KnowledgeImportRequest


@pytest.fixture
def catalog(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    service = KnowledgeBaseService(tmp_path / "catalog.sqlite3", root)
    (root / "guide.md").write_text("# QuartzToken\nQuartzToken uses SQLite transactions for document versions.", encoding="utf-8")
    result = service.ingest(KnowledgeImportRequest(kb_id="test", source_path="guide.md"))
    cfg = {"knowledge_catalog": {"db_path": str(service.store.path), "source_root": str(root),
        "allowed_kb_ids": ["test", "other"], "min_score": 0.1},
        "assistant": {"tool_runtime": "native"}}
    return root, service, cfg, result


def request(**kwargs):
    return AssistantRequest(question=kwargs.pop("question", "QuartzToken"),
        knowledge_source="catalog", kb_id="test", **kwargs)


def no_model(*args, **kwargs):
    raise AssertionError("a model must not be called")


def test_hashing_catalog_needs_real_lexical_anchor_not_hash_collision():
    from core.assistant_tools import _hashing_lexical_anchor
    assert not _hashing_lexical_anchor("UnobtainiumHyperdrive", "Python JSON dumps Unicode documentation")
    assert not _hashing_lexical_anchor("请给出北极星幻想王国的官方税率", "Python 官方文档说明字符串编码和路径操作")
    assert _hashing_lexical_anchor("Semaphore acquire 信号量", "asyncio Semaphore 同步原语")


def test_assistant_checkpoint_connections_close_explicitly(tmp_path, monkeypatch):
    from core.assistant import AssistantCheckpointStore
    from core.types import ChatMessage
    original_connect = sqlite3.connect
    connections = []
    class TrackedConnection(sqlite3.Connection):
        closed = False
        def close(self):
            self.closed = True
            super().close()
    def connect(*args, **kwargs):
        connection = original_connect(*args, factory=TrackedConnection, **kwargs)
        connections.append(connection)
        return connection
    monkeypatch.setattr("core.assistant.sqlite3.connect", connect)
    store = AssistantCheckpointStore(tmp_path / "checkpoints.db")
    store.save("test", [ChatMessage(role="user", content="question")], "knowledge")
    store.load("test")
    store.list_threads()
    store.delete("test")
    assert connections and all(connection.closed for connection in connections)


def test_catalog_request_fields_are_strict_and_preserved():
    source = AssistantAPIRequest(question="guide", knowledge_source="catalog",
        kb_id="test", task="summary", answer_mode="llm")
    core = source.to_core_request()
    assert (core.knowledge_source, core.kb_id, core.task, core.answer_mode) == ("catalog", "test", "summary", "llm")
    for values in ({"db_path": "private.db"}, {"knowledge_source": "arbitrary"},
                   {"kb_id": "../private"}, {"answer_mode": "shell"}):
        with pytest.raises(ValueError):
            AssistantAPIRequest(question="test", **values)


def test_extractive_reply_has_source_versions_without_calling_model(catalog):
    _, _, cfg, imported = catalog
    response = AssistantOrchestrator(cfg, complete_fn=no_model).reply(request())
    assert response.answer_mode == "extractive_baseline"
    assert response.knowledge_source == "catalog" and response.kb_id == "test"
    assert response.index_version and response.sources[0].document_version == imported.document_version
    assert response.sources[0].line_start and "[1]" in response.answer
    assert [step.worker for step in response.trace] == ["supervisor", "retriever", "answerer", "reviewer"]


@pytest.mark.parametrize("kwargs", [{"question": "UnobtainiumHyperdrive"}, {"use_knowledge": False}])
def test_no_evidence_refuses_without_model_even_if_llm_requested(catalog, kwargs):
    response = AssistantOrchestrator(catalog[2], complete_fn=no_model).reply(request(answer_mode="llm", **kwargs))
    assert response.answer_mode == "refused" and not response.sources
    assert response.degraded and "未生成事实结论" in response.answer


def test_disallowed_kb_is_not_read_and_exception_details_are_not_returned(catalog):
    cfg = catalog[2] | {"knowledge_catalog": catalog[2]["knowledge_catalog"] | {"allowed_kb_ids": ["other"]}}
    response = AssistantOrchestrator(cfg, complete_fn=no_model).reply(request())
    assert response.answer_mode == "refused" and not response.sources
    assert str(catalog[0]) not in response.model_dump_json()


def test_shared_service_observes_update_delete_and_reimport_without_restart(catalog):
    root, store, cfg, imported = catalog
    assistant = AssistantOrchestrator(cfg, complete_fn=no_model)
    before = assistant.reply(request())
    (root / "guide.md").write_text("# QuartzToken\nQuartzToken now uses monotonic revisions.", encoding="utf-8")
    updated = store.ingest(KnowledgeImportRequest(kb_id="test", source_path="guide.md"))
    after = assistant.reply(request())
    assert updated.document_version != imported.document_version
    assert after.index_version != before.index_version
    assert "monotonic" in after.answer and "transactions" not in after.answer
    assert store.delete("test", imported.document_id)
    assert assistant.reply(request()).answer_mode == "refused"
    store.ingest(KnowledgeImportRequest(kb_id="test", source_path="guide.md"))
    assert assistant.reply(request()).sources


@pytest.mark.parametrize("answer", ["Unrelated fabricated assertion [1].", "SecretUnsupportedPhrase [999].", "No citations."])
def test_invalid_model_claims_are_replaced_not_merely_warned(catalog, answer):
    response = AssistantOrchestrator(catalog[2], complete_fn=lambda *a, **k: answer).reply(request(answer_mode="llm"))
    assert response.answer_mode == "extractive_fallback" and response.degraded
    assert answer not in response.answer and "SQLite transactions" in response.answer


def test_valid_model_answer_and_model_error_paths(catalog):
    valid = "QuartzToken uses SQLite transactions for document versions [1]."
    response = AssistantOrchestrator(catalog[2], complete_fn=lambda *a, **k: valid).reply(request(answer_mode="llm"))
    assert response.answer_mode == "llm_grounded" and response.answer == valid
    def broken(*args, **kwargs):
        raise ConnectionError("private-error-contents")
    response = AssistantOrchestrator(catalog[2], complete_fn=broken).reply(request(answer_mode="llm"))
    assert response.answer_mode == "extractive_fallback" and "private-error" not in response.model_dump_json()


def test_stream_never_emits_unreviewed_model_claim(catalog):
    stream = AssistantOrchestrator(catalog[2], complete_fn=lambda *a, **k: "UnreviewedSecret [999].").reply_stream(request(answer_mode="llm"))
    emitted = "".join(stream)
    assert "UnreviewedSecret" not in emitted
    assert stream.response.answer == emitted and stream.response.answer_mode == "extractive_fallback"


@pytest.mark.parametrize("answer", [
    "UnreviewedSecret [999].", "Unrelated fabricated assertion [1].", "No citations.",
])
def test_async_stream_reviews_catalog_claims_before_emitting(catalog, answer):
    import asyncio

    async def model_stream(*args, **kwargs):
        yield answer

    async def run():
        service = AssistantOrchestrator(
            catalog[2], complete_fn=no_model, astream_complete_fn=model_stream,
        )
        stream = service.areply_stream(request(answer_mode="llm"))
        emitted = "".join([part async for part in stream])
        assert answer not in emitted
        assert "SQLite transactions" in emitted
        assert stream.response.answer == emitted
        assert stream.response.answer_mode == "extractive_fallback"
    asyncio.run(run())


def test_compare_requires_distinct_documents_and_summary_is_honest(catalog):
    root, store, cfg, _ = catalog
    assistant = AssistantOrchestrator(cfg, complete_fn=no_model)
    assert assistant.reply(request(task="compare")).answer_mode == "refused"
    (root / "second.md").write_text("# QuartzToken\nQuartzToken supports revision conflict detection.", encoding="utf-8")
    store.ingest(KnowledgeImportRequest(kb_id="test", source_path="second.md"))
    compared = assistant.reply(request(task="compare"))
    assert compared.answer_mode == "extractive_baseline" and len(compared.sources) == 2
    summary = assistant.reply(request(task="summary"))
    assert "不是模型综合生成" in summary.answer


def test_json_and_sse_share_same_catalog_and_auth_boundaries(catalog, monkeypatch):
    monkeypatch.setenv("PERSONAX_API_TOKEN", "catalog-test-token")
    assistant = AssistantOrchestrator(catalog[2], complete_fn=no_model)
    with TestClient(create_app(lambda: assistant)) as client:
        payload = {"question": "QuartzToken", "knowledge_source": "catalog", "kb_id": "test"}
        assert client.post("/v1/assistant/reply", json=payload).status_code == 401
        headers = {"Authorization": "Bearer catalog-test-token"}
        response = client.post("/v1/assistant/reply", json=payload, headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["answer_mode"] == "extractive_baseline" and body["index_version"]
        streamed = client.post("/v1/assistant/stream", json=payload, headers=headers)
        assert streamed.status_code == 200 and "event: done" in streamed.text
        done = [json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith("data: ") and '"sources"' in line][0]
        assert done["index_version"] == body["index_version"]
        assert done["sources"][0]["document_id"] == body["sources"][0]["document_id"]


def test_parallel_kbs_do_not_share_mutable_pipeline(catalog):
    root, store, cfg, _ = catalog
    (root / "other.md").write_text("# ZephyrToken\nZephyrToken belongs exclusively to the other knowledge base.", encoding="utf-8")
    store.ingest(KnowledgeImportRequest(kb_id="other", source_path="other.md"))
    assistant = AssistantOrchestrator(cfg, complete_fn=no_model)
    queries = [request(), request().model_copy(update={"kb_id": "other", "question": "ZephyrToken"})] * 3
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(assistant.reply, queries))
    for q, result in zip(queries, responses):
        assert result.sources
        assert all(s.document_id == store.store.documents(q.kb_id)[0].document_id for s in result.sources)
