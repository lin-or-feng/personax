"""Operator-only CPU OCR adapter. No network/model download on the import path."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from .document_parser import DocumentError, MAX_FILE_BYTES
from .types import DocumentParseOptions, OCRPageResult

MODEL_NAMES = {
    "det": "PP-OCRv5_mobile_det",
    "rec": "PP-OCRv5_mobile_rec",
    "orientation": "PP-LCNet_x1_0_doc_ori",
    "line": "PP-LCNet_x1_0_textline_ori",
}
RUNTIME_PACKAGES = ("paddleocr", "paddlepaddle", "pypdfium2", "pillow")
MAX_RESULT_BYTES = 4 * 1024 * 1024


def _terminate_owned_process(process):
    """Terminate only the OCR process we just created and its descendants."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        # Keep the parent alive until /T has found the owned tree (venv redirectors
        # may themselves have a child). Never enumerate/kill unrelated Python jobs.
        executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run([str(executable), "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def _run_bounded_process(command, *, input: bytes, timeout: float, **kwargs):
    with subprocess.Popen(command, stdin=subprocess.PIPE, start_new_session=os.name != "nt", **kwargs) as process:
        try:
            process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_owned_process(process)
            process.communicate(timeout=5)
            raise


def verified_manifest(root: str) -> tuple[dict, str]:
    """Hashes pin local artifacts, not the identity/trustworthiness of their publisher."""
    try:
        if not root:
            raise ValueError()
        base = Path(root).resolve(strict=True)
        manifest_path = base / "manifest.json"
        if manifest_path.is_symlink() or manifest_path.stat().st_size > 64_000:
            raise ValueError()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["schema"] != 1 or manifest["models"] != MODEL_NAMES:
            raise ValueError()
        if set(manifest["runtime_versions"]) != set(RUNTIME_PACKAGES):
            raise ValueError()
        if not manifest["files"] or len(manifest["files"]) > 50:
            raise ValueError()
        expected_dirs = set(MODEL_NAMES)
        observed_dirs = set()
        for relative, expected_hash in manifest["files"].items():
            parts = Path(relative).parts
            if (len(parts) != 2 or parts[0] not in expected_dirs or
                    parts[1] not in {"inference.json", "inference.pdiparams", "inference.yml"}):
                raise ValueError()
            path = base / relative
            if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(base):
                raise ValueError()
            if not path.is_file() or path.stat().st_size > 100 * 1024 * 1024:
                raise ValueError()
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                raise ValueError()
            observed_dirs.add(parts[0])
        required = {f"{folder}/{name}" for folder in expected_dirs
                    for name in ("inference.json", "inference.pdiparams", "inference.yml")}
        if observed_dirs != expected_dirs or set(manifest["files"]) != required:
            raise ValueError()
        if {p.name for p in base.iterdir()} != expected_dirs | {"manifest.json"}:
            raise ValueError()
        for folder in expected_dirs:
            if {p.name for p in (base / folder).iterdir()} != {"inference.json", "inference.pdiparams", "inference.yml"}:
                raise ValueError()
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        return manifest, hashlib.sha256(canonical).hexdigest()
    except (OSError, ValueError, KeyError, TypeError):
        raise DocumentError("ocr_models_invalid") from None


class LocalPaddleOCR:
    """One bounded child per document; stdout/stderr never expose source text."""

    def __init__(self, options: DocumentParseOptions):
        self.options = options
        _, self.fingerprint = verified_manifest(options.model_root)

    def recognize(self, data: bytes, suffix: str, pages: list[int]) -> list[OCRPageResult]:
        if len(data) > MAX_FILE_BYTES or len(pages) > 100:
            raise DocumentError("ocr_input_limit")
        python = Path(self.options.worker_python or sys.executable).resolve()
        if not python.is_file():
            raise DocumentError("ocr_worker_missing")
        root = Path(__file__).resolve().parents[1]
        temporary = root / ".tmp" / "ocr"
        temporary.mkdir(parents=True, exist_ok=True)
        # Do not propagate API keys, cookies or cloud configuration into the child.
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "COMSPEC", "PATHEXT", "LANG"}}
        env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
                   CUDA_VISIBLE_DEVICES="-1", OMP_NUM_THREADS="2", MKL_NUM_THREADS="2",
                   PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True")
        payload = {"data": base64.b64encode(data).decode("ascii"), "suffix": suffix,
                   "pages": pages, "options": self.options.model_dump(), "fingerprint": self.fingerprint}
        try:
            with tempfile.TemporaryDirectory(prefix="job-", dir=temporary) as directory:
                env.update(TEMP=directory, TMP=directory, HOME=directory, USERPROFILE=directory,
                           PADDLE_HOME=directory, PADDLE_PDX_CACHE_HOME=directory,
                           HF_HOME=directory, XDG_CACHE_HOME=directory)
                output = Path(directory) / "result.json"
                _run_bounded_process([str(python), "-m", "core.ocr_worker", str(output)], cwd=root,
                    input=json.dumps(payload).encode("utf-8"), stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=self.options.timeout_seconds,
                    env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if not output.is_file() or output.stat().st_size > MAX_RESULT_BYTES:
                    raise DocumentError("ocr_worker_failed")
                result = json.loads(output.read_text(encoding="utf-8"))
                if result.get("error"):
                    allowed = {"ocr_dependency_missing", "ocr_runtime_mismatch", "ocr_models_invalid",
                               "ocr_pixel_limit", "ocr_empty_page", "ocr_block_limit", "ocr_text_limit",
                               "ocr_invalid_image", "ocr_invalid_pdf", "ocr_page_limit"}
                    raise DocumentError(result["error"] if result["error"] in allowed else "ocr_worker_failed")
                rows = [OCRPageResult.model_validate(row) for row in result["pages"]]
                if [row.page for row in rows] != pages or any(row.model_fingerprint != self.fingerprint for row in rows):
                    raise DocumentError("ocr_invalid_result")
                return rows
        except subprocess.TimeoutExpired:
            raise DocumentError("ocr_timeout") from None
        except DocumentError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise DocumentError("ocr_worker_failed") from None
