"""Explicit real-model smoke on generated, non-personal printed documents.

Not a blind OCR benchmark or a PDF security certification. Generated inputs and
previews stay under .tmp; the report contains only measurements and reason codes.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from core.document_parser import DocumentError, parse_document_result
from core.knowledge import KnowledgeBaseService
from core.types import DocumentParseOptions, KnowledgeImportRequest, KnowledgeSearchRequest

LINES = ["扫描资料导入测试", "此文档仅用于本地功能验收。", "退款期限为7天，金额为12.50元。",
         "没有审核，不得发布。", "QuartzToken OCR evidence."]


def cer(reference: str, hypothesis: str) -> float:
    reference, hypothesis = [re.sub(r"\s+", "", text) for text in (reference, hypothesis)]
    row = list(range(len(hypothesis) + 1))
    for i, left in enumerate(reference, 1):
        previous, row = row, [i]
        for j, right in enumerate(hypothesis, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
    return round(row[-1] / max(1, len(reference)), 4)


def fixture_image(font_path: str):
    image = Image.new("RGB", (1100, 1500), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(LINES):
        draw.text((70, 80 + index * 90), line, font=ImageFont.truetype(font_path, 38), fill="black")
    return image


def native_pdf() -> bytes:
    writer = PdfWriter(); page = writer.add_blank_page(width=528, height=720)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    contents = DecodedStreamObject()
    contents.set_data(b"BT /F1 18 Tf 40 600 Td (NativeQuartz source evidence.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(contents)
    out = io.BytesIO(); writer.write(out)
    return out.getvalue()


def box_hits_ink(image, unit) -> bool:
    x0, y0, x1, y1 = unit.bounding_box
    rectangle = (int(x0 * image.width), int(y0 * image.height),
                 max(int(x0 * image.width) + 1, int(x1 * image.width)),
                 max(int(y0 * image.height) + 1, int(y1 * image.height)))
    with image.crop(rectangle).convert("L") as crop:
        return crop.getextrema()[0] < 100


def evaluate(args) -> dict:
    created_at = datetime.now(timezone.utc).isoformat()
    directory = ROOT / ".tmp" / "ocr-acceptance" / str(time.time_ns())
    source = directory / "sources"; source.mkdir(parents=True)
    options = DocumentParseOptions(ocr_enabled=True, model_root=args.model_root,
        worker_python=args.ocr_python, timeout_seconds=120)
    image = fixture_image(args.font)
    image.save(source / "scanned.pdf", "PDF", resolution=150)
    image.save(source / "scanned.png", "PNG")
    image.save(source / "scanned.jpg", "JPEG", quality=90)
    rotated = image.rotate(90, expand=True)
    rotated.save(source / "rotated.pdf", "PDF", resolution=150)
    writer = PdfWriter(); writer.append(PdfReader(io.BytesIO(native_pdf())))
    writer.append(PdfReader(source / "scanned.pdf")); writer.add_blank_page(width=528, height=720)
    with (source / "mixed.pdf").open("wb") as handle:
        writer.write(handle)
    rows = []
    details = {}
    for name in ("scanned.pdf", "mixed.pdf", "rotated.pdf", "scanned.png", "scanned.jpg"):
        started = time.perf_counter()
        try:
            parsed = parse_document_result((source / name).read_bytes(), Path(name).suffix, options=options)
            ocr_units = [u for u in parsed.units if u.extraction_method == "ocr"]
            recognized = "\n".join(u.text for u in ocr_units)
            error_rate = cer("\n".join(LINES), recognized)
            boxes = all(box_hits_ink(rotated if name == "rotated.pdf" else image, u) for u in ocr_units)
            passed = (error_rate <= 0.20 and boxes and bool(ocr_units) and parsed.requires_review
                      and all(u.ocr_engine.startswith("paddleocr-") for u in ocr_units))
            if name == "mixed.pdf":
                passed = passed and [(p.page, p.method) for p in parsed.pages] == [(1, "native"), (2, "ocr"), (3, "blank")]
                passed = passed and "NativeQuartz" in parsed.units[0].text
            rows.append({"case": name.replace(".", "_"), "passed": passed,
                "cer_whitespace_normalized": error_rate, "boxes_overlap_source_ink": boxes,
                "ocr_blocks": len(ocr_units), "duration_seconds": round(time.perf_counter() - started, 3),
                "pages": [p.model_dump() for p in parsed.pages]})
            if ocr_units:
                details = {"engine": ocr_units[0].ocr_engine,
                    "model_fingerprint": ocr_units[0].ocr_model_fingerprint}
            # Private preview for visual debugging; not copied to the public report.
            (directory / (name + ".preview.json")).write_text(parsed.model_dump_json(indent=2), encoding="utf-8")
        except DocumentError as exc:
            rows.append({"case": name.replace(".", "_"), "passed": False, "reason": str(exc),
                "duration_seconds": round(time.perf_counter() - started, 3)})
    service = KnowledgeBaseService(directory / "catalog.sqlite3", source, parse_options=options)
    (source / "update.pdf").write_bytes(native_pdf())
    first = service.ingest(KnowledgeImportRequest(source_path="update.pdf"))
    (source / "update.pdf").write_bytes((source / "scanned.pdf").read_bytes())
    denied = service.ingest(KnowledgeImportRequest(source_path="update.pdf"))
    preserved = service.store.get("ai-engineering", first.document_id).document_version == first.document_version
    rows.append({"case": "unreviewed_update_preserves_previous", "passed": denied.reason == "ocr_review_required" and preserved})
    accepted = service.ingest(KnowledgeImportRequest(source_path="update.pdf", accept_ocr_review=True))
    result = service.search(KnowledgeSearchRequest(query="QuartzToken", min_score=0), retrieval_mode="bm25")
    cited = result.sources[0] if result.sources else None
    rows.append({"case": "reviewed_update_search_provenance", "passed": bool(
        accepted.status == "updated" and cited and cited.extraction_method == "ocr" and cited.ocr_reviewed
        and cited.page == 1 and cited.bounding_box and cited.ocr_model_fingerprint)})
    repeated = service.ingest(KnowledgeImportRequest(source_path="update.pdf", accept_ocr_review=True))
    rows.append({"case": "repeat_import_unchanged", "passed": repeated.status == "unchanged"})
    for name, overrides, expected in [
        ("real_worker_timeout", {"timeout_seconds": 1}, "ocr_timeout"),
        ("real_worker_pixel_limit", {"max_page_pixels": 100_000}, "ocr_pixel_limit"),
    ]:
        started = time.perf_counter()
        reason = "unexpected_success"
        try:
            parse_document_result((source / "scanned.pdf").read_bytes(), ".pdf",
                options=options.model_copy(update=overrides))
        except DocumentError as exc:
            reason = str(exc)
        rows.append({"case": name, "passed": reason == expected, "reason": reason,
            "duration_seconds": round(time.perf_counter() - started, 3)})
    image.close(); rotated.close()
    return {"created_at_utc": created_at, "scope": "self_authored_printed_document_engineering_smoke",
        "independent_quality_benchmark": False, "cloud_ocr": False, "device": "cpu",
        "source_text_in_report": False, "cer_limit_for_this_fixture_only": 0.20,
        "passed": all(row["passed"] for row in rows), "cases": len(rows), "runs": rows, **details}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-root", default=".rag_cache/ocr-models")
    parser.add_argument("--ocr-python", default=".venv-ocr/Scripts/python.exe")
    parser.add_argument("--font", default="C:/Windows/Fonts/msyh.ttc")
    parser.add_argument("--output", default=".tmp/ocr-p0-report.json")
    args = parser.parse_args()
    report = evaluate(args)
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
