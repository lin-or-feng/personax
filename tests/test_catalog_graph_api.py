"""Graph API uses the same server-owned catalog and existing HTTP guards."""
from fastapi.testclient import TestClient

from api import create_app
from core.assistant import AssistantOrchestrator
from core.knowledge import KnowledgeBaseService
from core.knowledge_graph import KnowledgeGraphService
from core.types import KnowledgeImportRequest, KnowledgeRelationInput


def test_graph_api_auth_allowlist_and_source_versions(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "guide.md").write_text("# Relations\nAlpha references Beta.", encoding="utf-8")
    store = KnowledgeBaseService(tmp_path / "catalog.db", root)
    imported = store.ingest(KnowledgeImportRequest(kb_id="test", source_path="guide.md"))
    doc = store.store.get("test", imported.document_id)
    KnowledgeGraphService(store.store.path).add(KnowledgeRelationInput(kb_id="test", subject="Alpha", object="Beta",
        document_id=doc.document_id, document_version=doc.document_version,
        chunk_id=doc.chunks[0].chunk_id, evidence=doc.chunks[0].text))
    cfg = {"knowledge_catalog": {"db_path": str(store.store.path), "allowed_kb_ids": ["test"]}}
    monkeypatch.setattr("api._load_persona", lambda: cfg)
    monkeypatch.setenv("PERSONAX_API_TOKEN", "graph-test-token")
    with TestClient(create_app(lambda: AssistantOrchestrator(cfg))) as client:
        payload = {"kb_id": "test", "entity": "Alpha"}
        assert client.post("/v1/knowledge/graph", json=payload).status_code == 401
        headers = {"Authorization": "Bearer graph-test-token"}
        result = client.post("/v1/knowledge/graph", json=payload, headers=headers)
        assert result.status_code == 200 and result.json()["paths"]
        assert result.json()["paths"][0]["relations"][0]["document_version"] == doc.document_version
        assert client.post("/v1/knowledge/graph", json=payload | {"kb_id": "other"}, headers=headers).status_code == 403
        assert client.post("/v1/knowledge/graph", json=payload | {"max_hops": 100}, headers=headers).status_code == 422
        store.delete("test", doc.document_id)
        assert not client.post("/v1/knowledge/graph", json=payload, headers=headers).json()["paths"]
