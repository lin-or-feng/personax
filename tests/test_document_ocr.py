"""Deterministic OCR contracts/safety. Real model quality is a separate smoke."""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
from pydantic import ValidationError
pytest.importorskip("pypdf")
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from core.document_parser import DocumentError, clean_text, parse_document_result
from core.knowledge import KnowledgeBaseService
from core.local_ocr import LocalPaddleOCR, MODEL_NAMES, RUNTIME_PACKAGES, verified_manifest
from core.types import DocumentParseOptions, KnowledgeImportRequest, KnowledgeSearchRequest, OCRBlock, OCRPageResult
from scripts.prepare_ocr import unpack_model, prepare


def pdf_bytes(*kinds):
    writer = PdfWriter()
    for kind in kinds:
        page = writer.add_blank_page(width=300, height=400)
        if kind == "blank":
            continue
        if kind == "text":
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
                DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
            content = b"BT /F1 12 Tf 20 250 Td (NativeQuartz is not a refund approval.) Tj ET"
        else:
            # Valid vector content with no text: forces visual interpretation rather than silent skip.
            content = b"0 0 m 100 100 l S"
        stream = DecodedStreamObject(); stream.set_data(content)
        page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO(); writer.write(out)
    return out.getvalue()


class FakeOCR:
    fingerprint = "a" * 64

    def __init__(self, *, confidence=0.99, fail="", blank=False):
        self.confidence, self.fail, self.blank = confidence, fail, blank
        self.calls = []

    def recognize(self, data, suffix, pages):
        self.calls.append(pages)
        if self.fail:
            raise DocumentError(self.fail)
        return [OCRPageResult(page=p, blank=self.blank,
            blocks=[] if self.blank else [OCRBlock(text="ScannedQuartz 不可退款，金额 12.50 元。",
                bounding_box=(0.1, 0.2, 0.8, 0.3), confidence=self.confidence)],
            engine="fake-contract-test", model_fingerprint=self.fingerprint) for p in pages]


@pytest.fixture
def manifest_root(tmp_path):
    root = tmp_path / "models"; root.mkdir()
    manifest = {"schema": 1, "models": MODEL_NAMES,
        "runtime_versions": {name: "fixture" for name in RUNTIME_PACKAGES}, "files": {}}
    for key in MODEL_NAMES:
        folder = root / key; folder.mkdir()
        for name in ("inference.json", "inference.pdiparams", "inference.yml"):
            data = (key + name).encode()
            (folder / name).write_bytes(data)
            manifest["files"][f"{key}/{name}"] = hashlib.sha256(data).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_mixed_pdf_only_routes_needed_pages_and_records_blank():
    backend = FakeOCR()
    parsed = parse_document_result(pdf_bytes("text", "scan", "blank"), ".pdf",
        options=DocumentParseOptions(ocr_enabled=True), ocr_backend=backend)
    assert backend.calls == [[2]]
    assert [(p.page, p.method) for p in parsed.pages] == [(1, "native"), (2, "ocr"), (3, "blank")]
    assert [u.page for u in parsed.units] == [1, 2]
    assert "not a refund" in parsed.units[0].text
    assert parsed.units[1].bounding_box == (0.1, 0.2, 0.8, 0.3)
    assert parsed.requires_review


def test_short_native_text_is_not_routed_by_character_count():
    data = pdf_bytes("text").replace(b"NativeQuartz is not a refund approval.", b"OK")
    # Rebuild xref offsets through the reader; avoids depending on malformed offsets.
    reader = PdfReader(io.BytesIO(data), strict=False)
    writer = PdfWriter(); writer.add_page(reader.pages[0]); out = io.BytesIO(); writer.write(out)
    backend = FakeOCR()
    parsed = parse_document_result(out.getvalue(), ".pdf", options=DocumentParseOptions(ocr_enabled=True), ocr_backend=backend)
    assert not backend.calls and parsed.units[0].text.strip() == "OK"


def test_force_ocr_is_explicit_and_includes_otherwise_blank_pages():
    backend = FakeOCR()
    parse_document_result(pdf_bytes("text", "blank"), ".pdf",
        options=DocumentParseOptions(ocr_enabled=True, force_ocr=True), ocr_backend=backend)
    assert backend.calls == [[1, 2]]
    with pytest.raises(ValidationError):
        DocumentParseOptions(force_ocr=True)


def test_disabled_ocr_does_not_call_backend_or_drop_scanned_page():
    backend = FakeOCR()
    with pytest.raises(DocumentError, match="pdf_page_requires_ocr"):
        parse_document_result(pdf_bytes("text", "scan"), ".pdf", ocr_backend=backend)
    assert backend.calls == []


def test_image_signatures_and_explicit_opt_in():
    with pytest.raises(DocumentError, match="ocr_invalid_image"):
        parse_document_result(b"not a png", ".png")
    with pytest.raises(DocumentError, match="image_requires_ocr"):
        parse_document_result(b"\x89PNG\r\n\x1a\n", ".png")


def test_low_confidence_is_visible_and_review_is_always_required():
    parsed = parse_document_result(pdf_bytes("scan"), ".pdf",
        options=DocumentParseOptions(ocr_enabled=True), ocr_backend=FakeOCR(confidence=0.2))
    assert "low_confidence" in parsed.units[0].quality_flags
    assert "ocr_unverified" in parsed.pages[0].quality_flags
    assert parsed.requires_review


def test_cleaning_preserves_negation_numbers_units_and_code_indentation():
    text = "不可退款，金额 12.50 元。\r\n    if not allowed:\r\n        return False\r\n"
    assert clean_text(text) == text.replace("\r\n", "\n")


def test_failed_or_unreviewed_ocr_update_keeps_old_version_and_safe_journal(tmp_path):
    source = tmp_path / "sources"; source.mkdir()
    path = source / "guide.pdf"; path.write_bytes(pdf_bytes("text"))
    service = KnowledgeBaseService(tmp_path / "catalog.sqlite3", source,
        parse_options=DocumentParseOptions(ocr_enabled=True), ocr_backend=FakeOCR(confidence=0.2))
    first = service.ingest(KnowledgeImportRequest(source_path="guide.pdf"))
    path.write_bytes(pdf_bytes("scan"))
    denied = service.ingest(KnowledgeImportRequest(source_path="guide.pdf"))
    assert denied.reason == "ocr_review_required"
    assert service.store.get("ai-engineering", first.document_id).document_version == first.document_version
    assert "ScannedQuartz" not in json.dumps([j.model_dump() for j in service.store.jobs("ai-engineering")])
    service.ocr_backend = FakeOCR(fail="ocr_timeout")
    assert service.ingest(KnowledgeImportRequest(source_path="guide.pdf", accept_ocr_review=True)).reason == "ocr_timeout"
    assert service.store.get("ai-engineering", first.document_id).document_version == first.document_version


def test_reviewed_ingest_preserves_ocr_provenance_through_search_and_restart(tmp_path):
    source = tmp_path / "sources"; source.mkdir()
    (source / "guide.pdf").write_bytes(pdf_bytes("scan"))
    backend = FakeOCR(confidence=0.2)
    service = KnowledgeBaseService(tmp_path / "catalog.sqlite3", source,
        parse_options=DocumentParseOptions(ocr_enabled=True), ocr_backend=backend)
    request = KnowledgeImportRequest(source_path="guide.pdf", accept_ocr_review=True)
    result = service.ingest(request)
    assert result.status == "created"
    assert service.ingest(request).status == "unchanged" and len(backend.calls) == 1
    restarted = KnowledgeBaseService(service.store.path, source)
    output = restarted.search(KnowledgeSearchRequest(query="ScannedQuartz", min_score=0), retrieval_mode="bm25")
    cited = output.sources[0]
    assert cited.page == 1 and cited.ocr_reviewed and cited.extraction_method == "ocr"
    assert cited.ocr_engine == "fake-contract-test" and cited.ocr_model_fingerprint == backend.fingerprint
    assert cited.bounding_box == (0.1, 0.2, 0.8, 0.3) and cited.ocr_confidence == 0.2
    assert "low_confidence" in cited.quality_flags and "不可退款" in cited.excerpt
    assert service.store.get("ai-engineering", result.document_id).page_reports[0].method == "ocr"
    backend.fingerprint = "b" * 64
    assert service.ingest(request).document_version != result.document_version


@pytest.mark.parametrize("box", [(0, 0, 2, 1), (0.5, 0, 0.2, 1), (0, 0, float("nan"), 1)])
def test_invalid_worker_boxes_are_rejected(box):
    with pytest.raises(ValidationError):
        OCRBlock(text="not empty", confidence=0.9, bounding_box=box)


def test_blank_flag_cannot_discard_real_blocks():
    with pytest.raises(ValidationError):
        OCRPageResult(page=1, blank=True, blocks=[OCRBlock(text="x", confidence=1, bounding_box=(0, 0, 1, 1))],
            engine="fake", model_fingerprint="a" * 64)


def test_empty_ocr_does_not_turn_nonblank_page_into_success():
    backend = FakeOCR()
    backend.recognize = lambda *args: [OCRPageResult(page=1, engine="fake", model_fingerprint="a" * 64)]
    with pytest.raises(DocumentError, match="ocr_empty_page"):
        parse_document_result(pdf_bytes("scan"), ".pdf", options=DocumentParseOptions(ocr_enabled=True), ocr_backend=backend)


def test_verified_models_detect_tampering_and_traversal(manifest_root):
    _, original = verified_manifest(str(manifest_root))
    assert len(original) == 64
    (manifest_root / "det" / "inference.json").write_text("modified")
    with pytest.raises(DocumentError, match="ocr_models_invalid"):
        verified_manifest(str(manifest_root))
    manifest = json.loads((manifest_root / "manifest.json").read_text())
    manifest["files"]["../private"] = "a" * 64
    (manifest_root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DocumentError, match="ocr_models_invalid"):
        verified_manifest(str(manifest_root))


def test_subprocess_has_timeout_no_shell_and_no_api_secrets(manifest_root, monkeypatch):
    backend = LocalPaddleOCR(DocumentParseOptions(ocr_enabled=True, model_root=str(manifest_root)))
    monkeypatch.setenv("PRIVATE_API_KEY", "must-not-reach-child")
    def timeout(command, **kwargs):
        assert command[1:3] == ["-m", "core.ocr_worker"]
        assert "PRIVATE_API_KEY" not in kwargs["env"]
        assert not kwargs.get("shell") and kwargs["timeout"] == 120
        assert kwargs["stdout"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.DEVNULL
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr("core.local_ocr._run_bounded_process", timeout)
    with pytest.raises(DocumentError, match="ocr_timeout"):
        backend.recognize(pdf_bytes("scan"), ".pdf", [1])


@pytest.mark.parametrize("response,reason", [
    ({"error": "ocr_pixel_limit"}, "ocr_pixel_limit"),
    ({"error": "private-source-text"}, "ocr_worker_failed"),
    ({"pages": []}, "ocr_invalid_result"),
])
def test_worker_failures_are_fixed_codes_and_page_coverage_is_checked(manifest_root, monkeypatch, response, reason):
    backend = LocalPaddleOCR(DocumentParseOptions(ocr_enabled=True, model_root=str(manifest_root)))
    def respond(command, **kwargs):
        Path(command[-1]).write_text(json.dumps(response), encoding="utf-8")
    monkeypatch.setattr("core.local_ocr._run_bounded_process", respond)
    with pytest.raises(DocumentError, match=f"^{reason}$"):
        backend.recognize(pdf_bytes("scan"), ".pdf", [1])


def test_encrypted_pdf_rejected_without_ocr_execution():
    writer = PdfWriter(); writer.add_blank_page(width=100, height=100); writer.encrypt("fixture")
    stream = io.BytesIO(); writer.write(stream)
    backend = FakeOCR()
    with pytest.raises(DocumentError, match="encrypted_pdf"):
        parse_document_result(stream.getvalue(), ".pdf", options=DocumentParseOptions(ocr_enabled=True), ocr_backend=backend)
    assert not backend.calls


def test_page_and_text_limits_cover_native_plus_ocr(monkeypatch):
    import core.document_parser as parser
    monkeypatch.setattr(parser, "MAX_PDF_PAGES", 1)
    with pytest.raises(DocumentError, match="too_many_pages"):
        parse_document_result(pdf_bytes("text", "scan"), ".pdf")
    monkeypatch.setattr(parser, "MAX_PDF_PAGES", 100)
    monkeypatch.setattr(parser, "MAX_TEXT_CHARS", 5)
    with pytest.raises(DocumentError, match="text_too_large"):
        parse_document_result(pdf_bytes("scan"), ".pdf", options=DocumentParseOptions(ocr_enabled=True), ocr_backend=FakeOCR())


@pytest.mark.parametrize("name,kind", [("../escape", "file"), ("model/inference.json", "symlink")])
def test_model_archive_cannot_write_links_or_traversal(tmp_path, name, kind):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        member = tarfile.TarInfo(name)
        if kind == "symlink":
            member.type = tarfile.SYMTYPE; member.linkname = "../escape"
            archive.addfile(member)
        else:
            member.size = 1; archive.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="model_archive_invalid"):
        unpack_model(stream.getvalue(), tmp_path)
    assert not (tmp_path.parent / "escape").exists()


def test_model_download_requires_explicit_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.prepare_ocr.importlib.metadata.version", lambda _: "fixture")
    with pytest.raises(ValueError, match="explicit_download_flag_required"):
        prepare(tmp_path / "not-created", allow_download=False)
    assert not (tmp_path / "not-created").exists()


def test_timeout_cleans_up_only_the_owned_process(monkeypatch):
    from core.local_ocr import _run_bounded_process
    stopped = []
    class Child:
        pid = 12345
        attempts = 0
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def communicate(self, data=None, timeout=None):
            self.attempts += 1
            if self.attempts == 1:
                raise subprocess.TimeoutExpired("owned-worker", timeout)
            return None, None
    child = Child()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: child)
    monkeypatch.setattr("core.local_ocr._terminate_owned_process", lambda item: stopped.append(item.pid))
    with pytest.raises(subprocess.TimeoutExpired):
        _run_bounded_process(["owned-worker"], input=b"{}", timeout=1)
    assert stopped == [12345] and child.attempts == 2


def test_inline_image_with_text_requires_ocr():
    reader = PdfReader(io.BytesIO(pdf_bytes("text")))
    writer = PdfWriter(); page = writer.add_page(reader.pages[0])
    old = page.get_contents().get_data()
    stream = DecodedStreamObject()
    stream.set_data(old + b"\nq 10 0 0 10 20 20 cm BI /W 1 /H 1 /CS /RGB /BPC 8 ID \xff\xff\xff EI Q")
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO(); writer.write(out)
    with pytest.raises(DocumentError, match="pdf_page_requires_ocr"):
        parse_document_result(out.getvalue(), ".pdf")


def test_extra_model_file_cannot_bypass_manifest(manifest_root):
    (manifest_root / "det" / "extra.py").write_text("not executed")
    with pytest.raises(DocumentError, match="ocr_models_invalid"):
        verified_manifest(str(manifest_root))
