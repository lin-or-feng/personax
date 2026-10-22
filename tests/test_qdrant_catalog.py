"""Real optional Qdrant local-mode operations; not an HTTP/ANN benchmark."""
from __future__ import annotations

import pytest

pytest.importorskip("qdrant_client")

from core.knowledge import KnowledgeBaseService
from core.types import KnowledgeImportRequest, KnowledgeSearchRequest


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    service = KnowledgeBaseService(tmp_path / "catalog.db", root)
    return root, service, str(tmp_path / "qdrant")


def ingest(root, service, content, kb_id="test"):
    (root / "guide.md").write_text(content, encoding="utf-8")
    return service.ingest(KnowledgeImportRequest(kb_id=kb_id, source_path="guide.md"))


def search(service, path, query="QuartzToken", kb_id="test"):
    return service.search(KnowledgeSearchRequest(kb_id=kb_id, query=query, min_score=0.05),
        vector_store="qdrant", qdrant_path=path)


def test_qdrant_real_persistence_and_closed_client_can_reopen(setup):
    root, service, path = setup
    doc = ingest(root, service, "# QuartzToken\nQuartzToken uses transactions.")
    first = search(service, path)
    assert first.sources[0].document_version == doc.document_version
    second = search(KnowledgeBaseService(service.store.path, root), path)
    assert second.sources[0].chunk_id == first.sources[0].chunk_id


def test_qdrant_old_points_never_leak_into_updated_or_deleted_snapshot(setup):
    root, service, path = setup
    doc = ingest(root, service, "# QuartzToken\nQuartzToken old RedQuartz detail.")
    before = search(service, path)
    ingest(root, service, "# QuartzToken\nQuartzToken current BluQuartz detail.")
    after = search(service, path)
    assert "BluQuartz" in after.answer and "RedQuartz" not in after.answer
    assert after.index_version != before.index_version
    service.delete("test", doc.document_id)
    assert not search(service, path).sources


def test_qdrant_same_collection_does_not_mix_catalogs(setup):
    root, service, path = setup
    ingest(root, service, "# QuartzToken\nQuartzToken test library only.")
    ingest(root, service, "# ZephyrToken\nZephyrToken other library only.", kb_id="other")
    assert search(service, path).sources
    other = search(service, path, query="ZephyrToken", kb_id="other")
    assert other.sources and "QuartzToken" not in other.answer


def test_qdrant_empty_snapshot_releases_storage_lock(setup):
    root, service, path = setup
    assert not search(service, path).sources
    ingest(root, service, "# QuartzToken\nQuartzToken returns after an empty query.")
    assert search(service, path).sources
