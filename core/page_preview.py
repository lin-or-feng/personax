"""Authenticated callers render immutable uploaded pages in a bounded child."""
from __future__ import annotations

import base64
import io
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .document_parser import MAX_FILE_BYTES
from .local_ocr import _run_bounded_process
from .workspace import WorkspaceError


def render_page(data: bytes, suffix: str, page: int, *, worker_python: str = "") -> bytes:
    root = Path(__file__).resolve().parents[1]
    temporary = root / ".tmp" / "page-preview"
    temporary.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}}
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    try:
        with tempfile.TemporaryDirectory(dir=temporary) as directory:
            env.update(TEMP=directory, TMP=directory)
            output = Path(directory) / "page.png"
            payload = {"data": base64.b64encode(data).decode(), "suffix": suffix, "page": page}
            _run_bounded_process([worker_python or sys.executable, "-m", "core.page_preview", str(output)],
                input=json.dumps(payload).encode(), timeout=15, cwd=root, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if not output.is_file() or output.is_symlink() or output.stat().st_size > 8 * 1024 * 1024:
                raise WorkspaceError("page_preview_unavailable", 422)
            return output.read_bytes()
    except (OSError, subprocess.TimeoutExpired):
        raise WorkspaceError("page_preview_unavailable", 503) from None


def main():
    from PIL import Image, ImageOps
    import pypdfium2 as pdfium
    raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("limit")
    payload = json.loads(raw)
    data = base64.b64decode(payload["data"], validate=True)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("limit")
    number = payload["page"]
    if type(number) is not int or not 1 <= number <= 100:
        raise ValueError("page")
    if payload["suffix"] == ".pdf":
        document = pdfium.PdfDocument(data)
        try:
            page = document[number - 1]
            try:
                width, height = page.get_size()
                if not math.isfinite(width * height) or width * height * 4 > 4_000_000:
                    raise ValueError("pixels")
                bitmap = page.render(scale=2)
                try:
                    image = bitmap.to_pil()
                    try:
                        image.save(sys.argv[1], "PNG")
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            document.close()
    elif payload["suffix"] in {".png", ".jpg", ".jpeg"}:
        if number != 1:
            raise ValueError("page")
        with Image.open(io.BytesIO(data)) as original:
            if original.width * original.height > 12_000_000:
                raise ValueError("pixels")
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.thumbnail((1600, 1600))
            try:
                image.save(sys.argv[1], "PNG")
            finally:
                image.close()
    else:
        raise ValueError("format")


if __name__ == "__main__":
    main()
