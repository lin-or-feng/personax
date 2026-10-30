"""Bounded local OCR subprocess; never imported by the API/assistant.

Python sockets are denied during inference. This is not an OS security sandbox.
Only the preparation command may download models. No PDF/image files are saved.
"""
from __future__ import annotations

import base64
import contextlib
import importlib.metadata
import io
import json
import math
import socket
import sys
from pathlib import Path

from .document_parser import DocumentError, MAX_FILE_BYTES, MAX_TEXT_CHARS
from .local_ocr import MODEL_NAMES, RUNTIME_PACKAGES, verified_manifest
from .types import DocumentParseOptions, OCRBlock, OCRPageResult


def _deny_network(*args, **kwargs):
    raise OSError("ocr_network_disabled")


def run(payload: dict) -> list[OCRPageResult]:
    options = DocumentParseOptions.model_validate(payload["options"])
    manifest, fingerprint = verified_manifest(options.model_root)
    if fingerprint != payload["fingerprint"]:
        raise DocumentError("ocr_models_invalid")
    try:
        versions = {name: importlib.metadata.version(name) for name in RUNTIME_PACKAGES}
    except importlib.metadata.PackageNotFoundError:
        raise DocumentError("ocr_dependency_missing") from None
    if versions != manifest["runtime_versions"]:
        raise DocumentError("ocr_runtime_mismatch")
    socket.socket.connect = _deny_network
    socket.socket.connect_ex = _deny_network
    socket.create_connection = _deny_network
    try:
        import numpy as np
        import pypdfium2 as pdfium
        from PIL import Image, ImageOps
        from paddleocr import PaddleOCR
    except ImportError:
        raise DocumentError("ocr_dependency_missing") from None
    data = base64.b64decode(payload["data"], validate=True)
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError("ocr_input_limit")
    pages = payload["pages"]
    if not pages or len(pages) > 100 or pages != sorted(set(pages)) or any(type(p) is not int or not 1 <= p <= 100 for p in pages):
        raise DocumentError("ocr_page_limit")
    base = Path(options.model_root).resolve()
    engine = None
    used_pixels, block_count, text_count = 0, 0, 0
    results = []
    engine_version = "paddleocr-" + versions["paddleocr"] + "/paddle-" + versions["paddlepaddle"]

    def recognize(image, number):
        nonlocal engine, used_pixels, block_count, text_count
        width, height = image.size
        pixels = width * height
        used_pixels += pixels
        if pixels > options.max_page_pixels or used_pixels > options.max_total_pixels:
            raise DocumentError("ocr_pixel_limit")
        # Conservative uniform-white detection. Gray/noisy/illustrated pages are not blank.
        gray = image.convert("L")
        lo, hi = gray.getextrema()
        blank = lo >= 248 and hi - lo <= 3
        gray.close()
        if blank:
            return OCRPageResult(page=number, blank=True, engine=engine_version, model_fingerprint=fingerprint)
        if engine is None:
            engine = PaddleOCR(
                text_detection_model_name=MODEL_NAMES["det"], text_detection_model_dir=str(base / "det"),
                text_recognition_model_name=MODEL_NAMES["rec"], text_recognition_model_dir=str(base / "rec"),
                doc_orientation_classify_model_name=MODEL_NAMES["orientation"],
                doc_orientation_classify_model_dir=str(base / "orientation"),
                textline_orientation_model_name=MODEL_NAMES["line"],
                textline_orientation_model_dir=str(base / "line"),
                use_doc_orientation_classify=True, use_doc_unwarping=False,
                use_textline_orientation=True, device="cpu", cpu_threads=2,
                enable_mkldnn=False, text_rec_score_thresh=0.0,
            )
        rgb = image.convert("RGB")
        try:
            output = list(engine.predict(np.asarray(rgb)[:, :, ::-1]))  # Paddle arrays are BGR.
        finally:
            rgb.close()
        if len(output) != 1:
            raise DocumentError("ocr_invalid_result")
        record = output[0]
        texts, scores, polys = record["rec_texts"], record["rec_scores"], record["rec_polys"]
        if not len(texts) == len(scores) == len(polys):
            raise DocumentError("ocr_invalid_result")
        # OCR may rotate a page for recognition; convert boxes back to original displayed page.
        angle = int(record.get("doc_preprocessor_res", {}).get("angle", 0)) % 360
        blocks = []
        for text, score, poly in zip(texts, scores, polys):
            if not str(text).strip():
                continue
            points = np.asarray(poly, dtype=float).reshape(-1, 2)
            rotated_w, rotated_h = (height, width) if angle in {90, 270} else (width, height)
            points[:, 0] /= rotated_w
            points[:, 1] /= rotated_h
            # Paddle's orientation correction rotates the input clockwise by -angle.
            if angle == 90:
                points = np.column_stack((1 - points[:, 1], points[:, 0]))
            elif angle == 180:
                points = 1 - points
            elif angle == 270:
                points = np.column_stack((points[:, 1], 1 - points[:, 0]))
            points = np.clip(points, 0, 1)
            bbox = (float(points[:, 0].min()), float(points[:, 1].min()),
                    float(points[:, 0].max()), float(points[:, 1].max()))
            blocks.append(OCRBlock(text=str(text), confidence=float(score), bounding_box=bbox))
            text_count += len(str(text))
            block_count += 1
            if text_count > MAX_TEXT_CHARS:
                raise DocumentError("ocr_text_limit")
            if block_count > 2_000:
                raise DocumentError("ocr_block_limit")
        if not blocks:
            raise DocumentError("ocr_empty_page")
        # Preserve engine reading order; do not re-sort rotated/column text by image y.
        return OCRPageResult(page=number, blocks=blocks, engine=engine_version, model_fingerprint=fingerprint)

    if payload["suffix"] == ".pdf":
        try:
            document = pdfium.PdfDocument(data)
        except Exception:
            raise DocumentError("ocr_invalid_pdf") from None
        try:
            if len(document) > 100 or max(pages) > len(document):
                raise DocumentError("ocr_page_limit")
            for number in pages:
                page = document[number - 1]
                try:
                    scale = options.render_dpi / 72
                    width, height = page.get_size()
                    pixels = math.ceil(width * scale) * math.ceil(height * scale)
                    if (not math.isfinite(pixels) or pixels > options.max_page_pixels or
                            used_pixels + pixels > options.max_total_pixels):
                        raise DocumentError("ocr_pixel_limit")
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil()
                        try:
                            results.append(recognize(image, number))
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
        finally:
            document.close()
    else:
        try:
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {"PNG", "JPEG"} or source.width * source.height > options.max_page_pixels or getattr(source, "n_frames", 1) != 1:
                    raise DocumentError("ocr_invalid_image")
                # Do not carry EXIF or other metadata into inference/output.
                image = ImageOps.exif_transpose(source).convert("RGB")
                try:
                    results.append(recognize(image, 1))
                finally:
                    image.close()
        except (OSError, Image.DecompressionBombError):
            raise DocumentError("ocr_invalid_image") from None
    return results


def main():
    output = Path(sys.argv[1])
    try:
        raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise DocumentError("ocr_input_limit")
        with contextlib.redirect_stdout(sys.stderr):
            rows = run(json.loads(raw))
        result = {"pages": [row.model_dump() for row in rows]}
    except DocumentError as exc:
        result = {"error": str(exc)}
    except Exception:
        result = {"error": "ocr_worker_failed"}
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
