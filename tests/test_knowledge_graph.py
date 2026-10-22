"""Source validation, bounded paths, version invalidation and catalog isolation."""
from __future__ import annotations

import pytest

from core.knowledge import KnowledgeBaseService
from core.knowledge_graph import KnowledgeGraphService
from core.types import KnowledgeGraphQuery, KnowledgeImportRequest, KnowledgeRelationInput


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    (root / "guide.md").write_text("# Graph\nAlpha references Beta. Beta references Gamma. TaskGroup is distinct.", encoding="utf-8")
    service = KnowledgeBaseService(tmp_path / "catalog.db", root)
    imported = service.ingest(KnowledgeImportRequest(kb_id="test", source_path="guide.md"))
    document = service.store.get("test", imported.document_id)
    graph = KnowledgeGraphService(service.store.path)
    base = {"kb_id": "test", "document_id": document.document_id,
        "document_version": document.document_version, "chunk_id": document.chunks[0].chunk_id,
        "evidence": document.chunks[0].text}
    return root, service, graph, base


def test_graph_builds_idempotent_evidence_paths_and_is_bounded(setup):
    _, _, graph, base = setup
    first = graph.add(KnowledgeRelationInput(**base, subject="Alpha", object="Beta"))
    assert graph.add(KnowledgeRelationInput(**base, subject="alpha", object="beta")).relation_id == first.relation_id
    graph.add(KnowledgeRelationInput(**base, subject="Beta", object="Gamma"))
    result = graph.query(KnowledgeGraphQuery(kb_id="test", entity="ALPHA"))
    assert len(result.paths) == 2 and len(result.paths[1].relations) == 2
    assert all(path.relations[0].document_version == base["document_version"] for path in result.paths)
    assert len(graph.query(KnowledgeGraphQuery(kb_id="test", entity="Alpha", max_hops=1)).paths) == 1
    assert len(graph.query(KnowledgeGraphQuery(kb_id="test", entity="Alpha", limit=1)).paths) == 1
    assert not graph.query(KnowledgeGraphQuery(kb_id="other", entity="Alpha")).paths


@pytest.mark.parametrize("change", [{"evidence": "Alpha and Beta fabricated evidence."},
    {"subject": "Task", "object": "Beta"}, {"document_version": "wrong"}, {"chunk_id": "wrong"}])
def test_graph_rejects_fabricated_or_stale_evidence(setup, change):
    _, _, graph, base = setup
    with pytest.raises(ValueError):
        graph.add(KnowledgeRelationInput(**(base | {"subject": "Alpha", "object": "Beta"} | change)))


def test_graph_relations_disappear_after_document_update_or_delete(setup):
    root, service, graph, base = setup
    graph.add(KnowledgeRelationInput(**base, subject="Alpha", object="Beta"))
    (root / "guide.md").write_text("# Graph\nNewAlpha does not retain the previous relation.", encoding="utf-8")
    service.ingest(KnowledgeImportRequest(kb_id="test", source_path="guide.md"))
    result = graph.query(KnowledgeGraphQuery(kb_id="test", entity="Alpha"))
    assert not result.paths and result.excluded_stale_relations == 1
    service.delete("test", base["document_id"])
    assert not graph.query(KnowledgeGraphQuery(kb_id="test", entity="Alpha")).paths
