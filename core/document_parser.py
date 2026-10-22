"""Bounded local document parsing. No URLs, JavaScript, OCR or model calls."""
from __future__ import annotations

import io
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

from .types import KnowledgeUnit

PARSER_VERSION = "local-text-v2"
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


def parse_document(data: bytes, suffix: str) -> list[KnowledgeUnit]:
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError("file_too_large")
    suffix = suffix.lower()
    if suffix not in {".md", ".txt", ".html", ".htm", ".pdf"}:
        raise DocumentError("unsupported_format")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise DocumentError("pdf_dependency_missing") from exc
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise DocumentError("encrypted_pdf")
            if len(reader.pages) > MAX_PDF_PAGES:
                raise DocumentError("too_many_pages")
            units = []
            total = 0
            for number, page in enumerate(reader.pages, 1):
                text = clean_text(page.extract_text() or "")
                if not text.strip():
                    raise DocumentError("pdf_page_requires_ocr")
                total += len(text)
                if total > MAX_TEXT_CHARS:
                    raise DocumentError("text_too_large")
                units.append(KnowledgeUnit(text=text, page=number, section=f"第 {number} 页"))
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError("invalid_pdf") from exc
    else:
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
