"""Offline lifecycle/safety regressions, not a real-domain answer-quality benchmark."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from core.document_parser import DocumentError, parse_document
from core.harness import Harness, RuleConfig
from core.knowledge import KnowledgeBaseService
from core.rag import build_rag_from_documents
from core.types import KnowledgeImportRequest, KnowledgeSearchRequest

ROOT = Path(__file__).resolve().parents[1]


def test_html_coalesces_paragraphs_and_drops_nested_navigation():
    html = b'<head><title>NoiseTitle</title></head><div class="sphinxsidebar"><div>SideNoise</div></div><h2>Guide</h2><p>Alpha detail.</p><p>Beta detail.</p><script>DangerNoise</script>'
    units = parse_document(html, ".html")
    text = "\n".join(unit.text for unit in units)
    assert "Noise" not in text and "Alpha detail." in text and "Beta detail." in text
    assert any("Alpha detail." in u.text and "Beta detail." in u.text for u in units)
    assert all(unit.line_start <= unit.line_end for unit in units)


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    return root, KnowledgeBaseService(tmp_path / "catalog.sqlite3", root, chunk_size=100)


def ingest(service, path="guide.md", kb_id="test", **kwargs):
    return service.ingest(KnowledgeImportRequest(source_path=path, kb_id=kb_id, **kwargs))


def search(service, query="QuartzToken", kb_id="test", **kwargs):
    return service.search(KnowledgeSearchRequest(kb_id=kb_id, query=query,
        min_score=kwargs.pop("min_score", 0.1), **kwargs))


def test_import_is_idempotent_stable_and_persistent(setup):
    root, service = setup
    (root / "guide.md").write_text("# QuartzToken\nQuartzToken 使用内容哈希验证版本。", encoding="utf-8")
    created = ingest(service, source_url="https://example.test/guide", license="teaching-fixture")
    same = ingest(service, source_url="https://example.test/guide", license="teaching-fixture")
    assert created.status == "created" and same.status == "unchanged"
    assert same.document_id == created.document_id
    assert same.document_version == created.document_version
    before = service.store.get("test", created.document_id)
    (root / "aaa.md").write_text("# Another\nAnother different file.", encoding="utf-8")
    ingest(service, "aaa.md")
    restarted = KnowledgeBaseService(service.store.path, root)
    after = restarted.store.get("test", created.document_id)
    assert [c.chunk_id for c in before.chunks] == [c.chunk_id for c in after.chunks]
    output = search(restarted)
    assert output.mode == "extractive_baseline"
    source = output.sources[0]
    assert source.document_id == created.document_id
    assert source.document_version == created.document_version
    assert source.source_url == "https://example.test/guide"
    assert source.section == "QuartzToken" and source.line_start == 1
    assert source.line_end >= source.line_start
    assert source.chunk_id and "QuartzToken" in output.answer


def test_content_hash_catches_same_size_same_mtime_and_replaces_old_chunks(setup):
    root, service = setup
    path = root / "guide.md"
    path.write_text("# Colors\nRedQuartz is active.", encoding="utf-8")
    first = ingest(service)
    stat = path.stat()
    path.write_text("# Colors\nBluQuartz is active.", encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert path.stat().st_size == stat.st_size
    second = ingest(service)
    assert second.status == "updated" and second.document_id == first.document_id
    assert second.document_version != first.document_version
    doc = service.store.get("test", first.document_id)
    assert "RedQuartz" not in " ".join(c.text for c in doc.chunks)
    assert "BluQuartz" in search(service, "BluQuartz").answer
    assert "RedQuartz" not in search(service, "RedQuartz").answer


def test_delete_is_idempotent_scoped_and_reimportable(setup):
    root, service = setup
    path = root / "guide.md"
    path.write_text("# QuartzToken\nQuartzToken 独有资料。", encoding="utf-8")
    first = ingest(service)
    other = ingest(service, kb_id="other")
    assert other.document_id != first.document_id
    before = search(service).index_version
    assert service.delete("test", first.document_id)
    assert not service.delete("test", first.document_id)
    assert path.exists()
    assert not search(service).sources
    assert search(service).index_version != before
    assert search(service, kb_id="other").sources
    assert ingest(service).status == "updated"
    assert search(service).sources


def test_failed_update_keeps_last_good_version_and_safe_reason(setup):
    root, service = setup
    path = root / "guide.md"
    path.write_text("# QuartzToken\nQuartzToken intact.", encoding="utf-8")
    first = ingest(service)
    path.write_bytes(b"\xff\xfe private-token-should-not-be-logged")
    result = ingest(service)
    assert result.status == "failed" and result.reason == "invalid_encoding"
    assert service.store.get("test", first.document_id).document_version == first.document_version
    assert search(service).sources
    assert "private-token" not in json.dumps([j.model_dump() for j in service.store.jobs("test")])


@pytest.mark.parametrize("path", ["../secret.txt", "D:/secret.txt", "/tmp/secret.txt", "guide.md:secret", "folder\\guide.md"])
def test_unsafe_paths_fail_closed(setup, path):
    _, service = setup
    result = ingest(service, path)
    assert result.status == "failed" and result.reason == "unsafe_path"


def test_hard_link_is_rejected(setup):
    root, service = setup
    outside = root.parent / "private.txt"
    outside.write_text("secret", encoding="utf-8")
    os.link(outside, root / "guide.md")
    assert ingest(service).reason == "unsafe_path"


def test_symlink_is_rejected_if_platform_allows(setup):
    root, service = setup
    outside = root.parent / "private.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        (root / "guide.md").symlink_to(outside)
    except OSError:
        pytest.skip("OS does not permit creating symlinks")
    assert ingest(service).reason == "unsafe_path"


@pytest.mark.parametrize("name,data,reason", [
    ("empty.md", b" \n", "empty_document"),
    ("binary.txt", b"hello\0there", "invalid_text"),
    ("code.exe", b"not executed", "unsupported_format"),
    ("broken.md", b"---\nno close", "invalid_frontmatter"),
])
def test_invalid_inputs_have_fixed_reason(setup, name, data, reason):
    root, service = setup
    (root / name).write_bytes(data)
    result = ingest(service, name)
    assert result.reason == reason
    assert service.store.documents("test") == []


def test_html_strips_navigation_script_and_preserves_heading_and_source_lines(setup):
    root, service = setup
    (root / "guide.html").write_text(
        "<html><body><nav>SecretNavigation</nav>\n<h1>QuartzToken</h1>\n"
        "<p>QuartzToken works <b>with evidence</b>.</p>\n"
        "<script>SecretScript()</script><style>SecretStyle</style></body></html>", encoding="utf-8")
    result = ingest(service, "guide.html")
    assert result.status == "created"
    doc = service.store.get("test", result.document_id)
    text = " ".join(c.text for c in doc.chunks)
    assert "with evidence" in text
    assert not any(s in text for s in ["SecretNavigation", "SecretScript", "SecretStyle"])
    assert all(c.line_start is not None for c in doc.chunks)
    assert any(c.section == "QuartzToken" and "with evidence" in c.text
        and c.line_start <= 3 <= c.line_end for c in doc.chunks)


def test_late_chunk_excerpt_contains_hit_not_only_parent_beginning(setup):
    root, service = setup
    (root / "guide.md").write_text("# A guide\n" + "Unrelated background. " * 40 +
        "\nQuartzTokenCheckpoint has an exact recovery boundary.", encoding="utf-8")
    ingest(service)
    output = search(service, "QuartzTokenCheckpoint")
    assert output.sources and "QuartzTokenCheckpoint" in output.sources[0].excerpt
    assert output.sources[0].line_start >= 2


def test_excerpt_keeps_late_query_inside_one_large_child(setup):
    root, service = setup
    service = KnowledgeBaseService(service.store.path, root, chunk_size=1_000)
    (root / "guide.md").write_text("# A guide\n" + "Background " * 60 +
        "QuartzTokenCheckpoint is evidence.", encoding="utf-8")
    ingest(service)
    # This is an excerpt-location unit test, not a hashing semantic-recall benchmark.
    output = search(service, "QuartzTokenCheckpoint", min_score=0.0)
    assert "QuartzTokenCheckpoint" in output.sources[0].excerpt


def test_source_metadata_changes_are_versioned_and_bad_urls_rejected(setup):
    root, service = setup
    (root / "guide.md").write_text("# QuartzToken\nText.", encoding="utf-8")
    first = ingest(service)
    second = ingest(service, title="New title", source_url="https://example.test/source", license="test")
    assert second.status == "updated" and first.document_version != second.document_version
    bad = ingest(service, source_url="https://user:password@example.test/source")
    assert bad.status == "failed" and bad.reason == "invalid_source_url"
    assert service.store.get("test", first.document_id).document_version == second.document_version


def test_parser_and_chunk_configuration_invalidate_version(setup):
    root, service = setup
    (root / "guide.md").write_text("# QuartzToken\n" + "Data " * 80, encoding="utf-8")
    first = ingest(service)
    changed = KnowledgeBaseService(service.store.path, root, chunk_size=200)
    second = ingest(changed)
    assert first.document_version != second.document_version
    assert first.chunk_count > second.chunk_count


def test_parent_context_is_bounded_not_duplicated_whole_document(setup):
    root, service = setup
    (root / "guide.md").write_text("# QuartzToken\n" + "Long source paragraph. " * 500, encoding="utf-8")
    result = ingest(service)
    document = service.store.get("test", result.document_id)
    assert result.chunk_count > 50
    assert all(len(c.parent_text) <= service.chunk_size + 600 for c in document.chunks)
    assert len(document.model_dump_json()) < 300_000


def test_transaction_failure_rolls_back_document_and_completion(setup, monkeypatch):
    root, service = setup
    path = root / "guide.md"
    path.write_text("# QuartzToken\nGood version.", encoding="utf-8")
    first = ingest(service)
    path.write_text("# QuartzToken\nNew uncommitted version.", encoding="utf-8")
    original = service.store._event
    def fail_completion(db, job_id, event):
        if event == "document_active":
            raise sqlite3.OperationalError("injected audit failure")
        return original(db, job_id, event)
    monkeypatch.setattr(service.store, "_event", fail_completion)
    with pytest.raises(sqlite3.OperationalError):
        ingest(service)
    assert service.store.get("test", first.document_id).document_version == first.document_version
    assert service.store.jobs("test")[0].status == "failed"


def test_stale_writer_cannot_resurrect_deleted_document(setup):
    root, service = setup
    (root / "guide.md").write_text("# QuartzToken\nOriginal.", encoding="utf-8")
    first = ingest(service)
    stale = service.store.get("test", first.document_id)
    job = service.store.start("test", first.document_id, "ingest")
    service.delete("test", first.document_id)
    with pytest.raises(DocumentError, match="concurrent_update"):
        service.store.publish(job, stale.model_copy(), stale)
    with pytest.raises(DocumentError, match="concurrent_update"):
        service.store.confirm_unchanged(job, stale)
    assert not search(service).sources


def test_revision_rejects_stale_writer_after_delete_reimport_with_same_timestamp(setup, monkeypatch):
    import core.knowledge_store as storage
    monkeypatch.setattr(storage.time, "time", lambda: 1_000.0)
    root, service = setup
    (root / "guide.md").write_text("# QuartzToken\nOriginal.", encoding="utf-8")
    first = ingest(service)
    old = service.store.get("test", first.document_id)
    job = service.store.start("test", first.document_id, "ingest")
    service.delete("test", first.document_id)
    ingest(service)
    current = service.store.get("test", first.document_id)
    assert current.updated_at == old.updated_at
    assert current.document_version == old.document_version
    assert current.revision > old.revision
    with pytest.raises(DocumentError, match="concurrent_update"):
        service.store.publish(job, old.model_copy(), old)


def test_explicit_recovery_abandons_jobs_without_changing_catalog(setup):
    root, service = setup
    (root / "guide.md").write_text("# QuartzToken\nOriginal.", encoding="utf-8")
    first = ingest(service)
    job = service.store.start("test", first.document_id, "ingest")
    assert service.store.recover("other") == 0
    assert service.store.recover("test") == 1
    assert service.store.jobs("test")[0].status == "abandoned"
    doc = service.store.get("test", first.document_id)
    with pytest.raises(DocumentError, match="job_not_running"):
        service.store.publish(job, doc.model_copy(), doc)
    assert search(service).sources


def test_actual_process_exit_leaves_no_partial_update(setup):
    root, service = setup
    (root / "guide.md").write_text("# QuartzToken\nCommitted original.", encoding="utf-8")
    first = ingest(service)
    script = (
        "import os,sys; from core.knowledge_store import KnowledgeStore; "
        "store=KnowledgeStore(sys.argv[1]); store.start('test',sys.argv[2],'ingest'); os._exit(7)"
    )
    child = subprocess.run([sys.executable, "-c", script, str(service.store.path), first.document_id],
        cwd=ROOT, capture_output=True, timeout=15)
    assert child.returncode == 7
    restarted = KnowledgeBaseService(service.store.path, root)
    assert restarted.store.jobs("test")[0].status == "running"
    assert restarted.store.get("test", first.document_id).document_version == first.document_version
    assert restarted.store.recover("test") == 1
    assert search(restarted).sources


def test_job_identity_cannot_publish_another_document(setup):
    root, service = setup
    (root / "guide.md").write_text("# QuartzToken\nOriginal.", encoding="utf-8")
    first = ingest(service)
    previous = service.store.get("test", first.document_id)
    job = service.store.start("other", first.document_id, "ingest")
    with pytest.raises(DocumentError, match="job_identity_mismatch"):
        service.store.publish(job, previous.model_copy(), previous)
    assert service.store.get("test", first.document_id).document_version == first.document_version


def test_hundred_synthetic_documents_are_idempotent_not_a_quality_benchmark(setup):
    root, service = setup
    for number in range(100):
        name = f"fixture_{number:03d}.txt"
        (root / name).write_text(f"Synthetic engineering fixture {number}. QuartzUnique{number:03d} evidence.", encoding="utf-8")
        # One service's quota is a real guard: create a new local invocation per document.
        local = KnowledgeBaseService(service.store.path, root)
        assert ingest(local, name).status == "created"
        assert ingest(local, name).status == "unchanged"
    assert len(service.store.documents("test")) == 100
    assert search(service, "QuartzUnique099").sources[0].source == "fixture_099.txt"


def test_different_knowledge_bases_cannot_build_one_snapshot(setup):
    root, service = setup
    (root / "guide.md").write_text("# QuartzToken\nText.", encoding="utf-8")
    ingest(service); ingest(service, kb_id="other")
    with pytest.raises(ValueError, match="one knowledge base"):
        build_rag_from_documents(service.store.documents("test") + service.store.documents("other"))


def test_harness_denial_blocks_import_before_journal_write(setup):
    root, service = setup
    denied = KnowledgeBaseService(service.store.path, root,
        harness=Harness(RuleConfig(denied_skills=["knowledge_ingest"])))
    with pytest.raises(DocumentError, match="operation_blocked"):
        ingest(denied)
    assert denied.store.jobs("test") == []


def test_file_text_and_chunk_limits(setup, monkeypatch):
    root, service = setup
    import core.document_parser as parser
    monkeypatch.setattr(parser, "MAX_FILE_BYTES", 20)
    (root / "guide.md").write_bytes(b"x" * 21)
    assert ingest(service).reason == "file_too_large"
    monkeypatch.setattr(parser, "MAX_FILE_BYTES", 100)
    monkeypatch.setattr(parser, "MAX_TEXT_CHARS", 10)
    assert ingest(service).reason == "text_too_large"


def test_pdf_blank_page_is_not_reported_as_success():
    pypdf = pytest.importorskip("pypdf")
    import io
    writer = pypdf.PdfWriter(); writer.add_blank_page(width=100, height=100)
    stream = io.BytesIO(); writer.write(stream)
    with pytest.raises(DocumentError, match="empty_document"):
        parse_document(stream.getvalue(), ".pdf")


def test_text_pdf_retains_page_source():
    pypdf = pytest.importorskip("pypdf")
    import io
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = pypdf.PdfWriter(); page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    contents = DecodedStreamObject(); contents.set_data(b"BT /F1 12 Tf 20 250 Td (QuartzToken PDF evidence) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(contents)
    output = io.BytesIO(); writer.write(output)
    units = parse_document(output.getvalue(), ".pdf")
    assert units[0].page == 1 and "QuartzToken" in units[0].text


def test_cli_ingest_search_delete_and_exit_codes(tmp_path):
    root = tmp_path / "sources"; root.mkdir()
    (root / "guide.md").write_text("# QuartzToken\nQuartzToken is source evidence.", encoding="utf-8")
    base = [sys.executable, "main.py", "knowledge", "--db", str(tmp_path / "catalog.sqlite3"),
        "--source-root", str(root)]
    def run(*args):
        completed = subprocess.run(base + list(args), cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=30)
        return completed, json.loads(completed.stdout)
    result, created = run("ingest", "--file", "guide.md")
    assert result.returncode == 0
    result, output = run("search", "--query", "QuartzToken")
    assert result.returncode == 0 and output["sources"][0]["document_id"] == created["document_id"]
    denied, _ = run("delete", "--document-id", created["document_id"])
    assert denied.returncode == 1
    result, deleted = run("delete", "--document-id", created["document_id"], "--yes")
    assert result.returncode == 0 and deleted["deleted"] and not deleted["source_file_removed"]
    result, output = run("search", "--query", "QuartzToken")
    assert result.returncode == 0 and output["sources"] == []
    result, output = run("ingest", "--file", "../secret.txt")
    assert result.returncode == 1 and output["reason"] == "unsafe_path"


def test_authored_fixture_smoke_is_explicitly_not_independent_domain_quality():
    from eval.knowledge_scorer import evaluate
    report = evaluate()
    assert report["passed"] is True
    assert report["cases"] == 6
    assert report["independent_domain_evaluation"] is False
