"""Bounded local text parsing with explicit optional subprocess OCR. No URLs."""
from __future__ import annotations

import io
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

from .types import DocumentPageReport, DocumentParseOptions, DocumentParseResult, KnowledgeUnit

PARSER_VERSION = "local-pages-v3"
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 500_000
MAX_PDF_PAGES = 100


class DocumentError(ValueError):
    """Only fixed safe reason codes cross the CLI boundary."""


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = "".join(c for c in text if c in "\n\t" or unicodedata.category(c) != "Cc")
    if "\ufffd" in text or "\x00" in text:
        raise DocumentError("invalid_text")
    return text


class _HTMLText(HTMLParser):
    ignored = {"head", "script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "template"}
    void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    blocks = {"p", "div", "li", "pre", "tr", "article", "section", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden: list[str] = []
        self.units: list[KnowledgeUnit] = []
        self.parts: list[str] = []
        self.section = ""
        self.heading = False
        self.start = 1

    def flush(self):
        text = " ".join(" ".join(self.parts).split())
        if text:
            if self.heading:
                self.section = text[:300]
            self.units.append(KnowledgeUnit(text=text, section=self.section,
                line_start=self.start, line_end=max(self.start, self.getpos()[0])))
        self.parts.clear()

    def handle_starttag(self, tag, attrs):
        if self.hidden:
            if tag not in self.void:
                self.hidden.append(tag)
            return
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        navigation = bool(classes & {"sphinxsidebar", "sphinxsidebarwrapper", "related", "footer", "headerlink"})
        if tag in self.ignored or navigation or attributes.get("role") in {"navigation", "search"}:
            self.flush()
            if tag not in self.void:
                self.hidden.append(tag)
        elif not self.hidden:
            if tag in self.blocks or re.fullmatch(r"h[1-6]", tag):
                self.flush()
            if re.fullmatch(r"h[1-6]", tag):
                self.heading = True

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag in self.blocks or re.fullmatch(r"h[1-6]", tag):
            self.flush()
        if re.fullmatch(r"h[1-6]", tag):
            self.heading = False

    def handle_data(self, data):
        if not self.hidden and data.strip():
            if not self.parts:
                self.start = self.getpos()[0]
            self.parts.append(data)


def _coalesce_html_units(units: list[KnowledgeUnit], max_chars: int = 1200) -> list[KnowledgeUnit]:
    """Keep nearby same-section paragraphs together, retaining original HTML line spans."""
    merged: list[KnowledgeUnit] = []
    for unit in units:
        if (merged and merged[-1].section == unit.section
                and len(merged[-1].text) + len(unit.text) + 1 <= max_chars):
            previous = merged[-1]
            merged[-1] = previous.model_copy(update={"text": previous.text + "\n" + unit.text,
                "line_end": unit.line_end})
        else:
            merged.append(unit)
    return merged


def _plain_units(text: str, markdown: bool) -> list[KnowledgeUnit]:
    lines = text.splitlines()
    units: list[KnowledgeUnit] = []
    start = 0
    section = ""
    parts: list[str] = []
    in_frontmatter = markdown and bool(lines) and lines[0].strip() == "---"
    def flush():
        if parts and "\n".join(parts).strip():
            units.append(KnowledgeUnit(text="\n".join(parts), section=section,
                line_start=start + 1, line_end=start + len(parts)))
        parts.clear()
    for i, line in enumerate(lines):
        if in_frontmatter:
            if i > 0 and line.strip() == "---":
                in_frontmatter = False
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line) if markdown else None
        if heading:
            flush()
            section = heading.group(1)[:300]
        if not parts:
            if not line.strip():
                continue
            start = i
        parts.append(line)
    if in_frontmatter:
        raise DocumentError("invalid_frontmatter")
    flush()
    return units


def _parse_text_document(data: bytes, suffix: str) -> list[KnowledgeUnit]:
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError("file_too_large")
    suffix = suffix.lower()
    if suffix not in {".md", ".txt", ".html", ".htm"}:
        raise DocumentError("unsupported_format")
    try:
        raw = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise DocumentError("invalid_encoding") from exc
    if "\x00" in raw:
        raise DocumentError("invalid_text")
    text = clean_text(raw)
    if len(text) > MAX_TEXT_CHARS:
        raise DocumentError("text_too_large")
    if suffix in {".html", ".htm"}:
        parser = _HTMLText()
        parser.feed(text); parser.close(); parser.flush()
        units = _coalesce_html_units(parser.units)
    else:
        units = _plain_units(text, markdown=suffix == ".md")
    if not units or not any(re.search(r"[\w\u4e00-\u9fff]", unit.text) for unit in units):
        raise DocumentError("empty_document")
    return units


def _has_raster(resources, seen=None, depth=0) -> bool:
    """Inspect resource types only; never decode embedded image bytes here."""
    if not resources or depth > 6:
        return depth > 6  # uncertain resource graph requires review/OCR
    seen = set() if seen is None else seen
    resources = resources.get_object()
    objects = resources.get("/XObject", {})
    objects = objects.get_object() if hasattr(objects, "get_object") else objects
    if len(objects) > 2_000:
        raise DocumentError("pdf_resource_limit")
    for item in objects.values():
        key = (getattr(item, "idnum", None), getattr(item, "generation", None))
        if key[0] is not None and key in seen:
            continue
        seen.add(key)
        obj = item.get_object()
        if obj.get("/Subtype") == "/Image":
            return True
        if obj.get("/Subtype") == "/Form" and _has_raster(obj.get("/Resources"), seen, depth + 1):
            return True
    return False


def _page_has_raster(page) -> bool:
    if _has_raster(page.get("/Resources")):
        return True
    contents = page.get_contents()
    return contents is not None and any(operator == b"INLINE IMAGE" for _, operator in contents.operations)


def _suspicious_text(text: str) -> bool:
    compact = "".join(text.split())
    if not compact or not any(c.isalnum() for c in compact):
        return True
    bad = sum(unicodedata.category(c) in {"Co", "Cn", "Cs"} or c == "\ufffd" for c in compact)
    return bad / len(compact) > 0.02


def parse_document_result(data: bytes, suffix: str, *, options: DocumentParseOptions | None = None,
                          ocr_backend=None) -> DocumentParseResult:
    """Atomic page routing. Unknown/failed pages never silently disappear."""
    options = options or DocumentParseOptions()
    suffix = suffix.lower()
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError("file_too_large")
    if suffix not in {".pdf", ".png", ".jpg", ".jpeg"}:
        return DocumentParseResult(units=_parse_text_document(data, suffix))
    units, reports, needed = [], [], []
    if suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise DocumentError("invalid_pdf")
        try:
            from pypdf import PdfReader
        except ImportError:
            raise DocumentError("pdf_dependency_missing") from None
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise DocumentError("encrypted_pdf")
            if len(reader.pages) > MAX_PDF_PAGES:
                raise DocumentError("too_many_pages")
            total = 0
            for number, page in enumerate(reader.pages, 1):
                # Only structurally empty pages are skipped without visual recognition.
                if not page.get("/Contents") and not page.get("/Annots") and not options.force_ocr:
                    reports.append(DocumentPageReport(page=number, method="blank"))
                    continue
                try:
                    text = clean_text(page.extract_text() or "")
                except (DocumentError, UnicodeError):
                    text = ""
                total += len(text)
                if total > MAX_TEXT_CHARS:
                    raise DocumentError("text_too_large")
                if options.force_ocr or _suspicious_text(text) or _page_has_raster(page):
                    needed.append(number)
                else:
                    units.append(KnowledgeUnit(text=text, page=number, section=f"第 {number} 页"))
                    reports.append(DocumentPageReport(page=number, method="native", block_count=1))
        except DocumentError:
            raise
        except Exception:
            raise DocumentError("invalid_pdf") from None
    else:
        signature_ok = (data.startswith(b"\x89PNG\r\n\x1a\n") if suffix == ".png"
                        else data.startswith(b"\xff\xd8\xff"))
        if not signature_ok:
            raise DocumentError("ocr_invalid_image")
        needed = [1]
    if needed:
        if not options.ocr_enabled:
            raise DocumentError("pdf_page_requires_ocr" if suffix == ".pdf" else "image_requires_ocr")
        if ocr_backend is None:
            from .local_ocr import LocalPaddleOCR
            ocr_backend = LocalPaddleOCR(options)
        rows = ocr_backend.recognize(data, suffix, needed)
        if [row.page for row in rows] != needed:
            raise DocumentError("ocr_invalid_result")
        for row in rows:
            if row.blank:
                reports.append(DocumentPageReport(page=row.page, method="blank"))
                continue
            if not row.blocks:
                raise DocumentError("ocr_empty_page")
            flags = set()
            for block in row.blocks:
                text = clean_text(block.text)
                quality = ["ocr_unverified", "layout_unverified"]
                if block.confidence < options.min_confidence:
                    quality.append("low_confidence")
                if _suspicious_text(text):
                    quality.append("suspicious_text")
                flags.update(quality)
                units.append(KnowledgeUnit(text=text, page=row.page, section=f"第 {row.page} 页",
                    extraction_method="ocr", bounding_box=block.bounding_box,
                    ocr_confidence=block.confidence, quality_flags=quality,
                    ocr_engine=row.engine, ocr_model_fingerprint=row.model_fingerprint))
            reports.append(DocumentPageReport(page=row.page, method="ocr", block_count=len(row.blocks),
                quality_flags=sorted(flags)))
    if sum(len(unit.text) for unit in units) > MAX_TEXT_CHARS:
        raise DocumentError("text_too_large")
    if not units:
        raise DocumentError("empty_document")
    units.sort(key=lambda unit: unit.page or 0)
    reports.sort(key=lambda report: report.page)
    return DocumentParseResult(units=units, pages=reports)


def parse_document(data: bytes, suffix: str, *, options: DocumentParseOptions | None = None,
                   ocr_backend=None) -> list[KnowledgeUnit]:
    """Backward-compatible unit-only entry; catalog uses the richer page report."""
    return parse_document_result(data, suffix, options=options, ocr_backend=ocr_backend).units


def read_local_document(root: Path, relative: str) -> tuple[str, bytes]:
    """Reject traversal/links and bound bytes; local operator, not a file sandbox."""
    from pathlib import PureWindowsPath
    candidate = Path(relative)
    win = PureWindowsPath(relative)
    if candidate.is_absolute() or candidate.anchor or win.is_absolute() or win.root or win.drive or ".." in candidate.parts or ".." in win.parts:
        raise DocumentError("unsafe_path")
    if ":" in relative or "\\" in relative:
        raise DocumentError("unsafe_path")
    base = root.resolve(strict=True)
    target = base / candidate
    for part in [target, *target.parents]:
        if part == base:
            break
        if part.is_symlink():
            raise DocumentError("unsafe_path")
    resolved = target.resolve(strict=True)
    if not resolved.is_relative_to(base) or not resolved.is_file():
        raise DocumentError("unsafe_path")
    if resolved.stat().st_nlink > 1:
        raise DocumentError("unsafe_path")
    with resolved.open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError("file_too_large")
    return resolved.relative_to(base).as_posix(), data
