"""Explicit download of four official CPU OCR models, with bounded safe extraction.

Run with the intended OCR Python. This command is never called by ingestion.
Local SHA-256 detects later changes; first-download publisher authenticity still
depends on HTTPS/the selected official host, not independent signature checking.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.local_ocr import MODEL_NAMES, RUNTIME_PACKAGES, verified_manifest

MODEL_HOST = "paddle-model-ecology.bj.bcebos.com"
MODEL_PREFIX = f"https://{MODEL_HOST}/paddlex/official_inference_model/paddle3.0.0/"
REQUIRED = {"inference.json", "inference.pdiparams", "inference.yml"}
MAX_ARCHIVE_BYTES = 110 * 1024 * 1024


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https" or urlsplit(newurl).hostname != MODEL_HOST:
            raise ValueError("model_redirect_rejected")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def unpack_model(data: bytes, folder: Path) -> dict[str, str]:
    """Never extract arbitrary archive paths/links or execute a downloaded script."""
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        members = archive.getmembers()
        if len(members) > 50:
            raise ValueError("model_archive_invalid")
        for member in members:
            if member.isdir():
                continue
            if not member.isfile() or member.size > 100 * 1024 * 1024:
                raise ValueError("model_archive_invalid")
            parts = member.name.replace("\\", "/").split("/")
            if len(parts) != 2 or any(p in {"", ".", ".."} or ":" in p for p in parts):
                raise ValueError("model_archive_invalid")
            if parts[-1] not in REQUIRED:
                continue
            if parts[-1] in files:
                raise ValueError("model_archive_invalid")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError("model_archive_invalid")
            with handle:
                content = handle.read(MAX_ARCHIVE_BYTES + 1)
            if len(content) != member.size:
                raise ValueError("model_archive_invalid")
            path = folder / parts[-1]
            path.write_bytes(content)
            files[parts[-1]] = hashlib.sha256(content).hexdigest()
    if set(files) != REQUIRED:
        raise ValueError("model_archive_invalid")
    return files


def prepare(root: Path, *, allow_download: bool) -> dict:
    versions = {name: importlib.metadata.version(name) for name in RUNTIME_PACKAGES}
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        manifest, fingerprint = verified_manifest(str(root))
        if manifest["runtime_versions"] != versions:
            raise ValueError("ocr_runtime_mismatch_use_new_model_root")
        return {"ready": True, "model_fingerprint": fingerprint, "downloaded": False}
    if not allow_download:
        raise ValueError("explicit_download_flag_required")
    root.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(SameHostRedirect())
    manifest = {"schema": 1, "models": MODEL_NAMES, "runtime_versions": versions, "files": {}, "archives": {}}
    with tempfile.TemporaryDirectory(prefix="ocr-prepare-", dir=root.parent) as directory:
        stage = Path(directory)
        for key, model in MODEL_NAMES.items():
            url = MODEL_PREFIX + model + "_infer.tar"
            with opener.open(url, timeout=60) as response:
                data = response.read(MAX_ARCHIVE_BYTES + 1)
            if len(data) > MAX_ARCHIVE_BYTES:
                raise ValueError("model_download_limit")
            folder = stage / key
            folder.mkdir()
            manifest["files"].update({f"{key}/{name}": digest for name, digest in unpack_model(data, folder).items()})
            manifest["archives"][key] = {"url": url, "sha256": hashlib.sha256(data).hexdigest()}
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _, fingerprint = verified_manifest(str(stage))
        # Publish a complete model directory at once; never overwrite active files.
        if root.exists() and any(root.iterdir()):
            raise ValueError("model_root_not_empty")
        if root.exists():
            root.rmdir()  # explicitly checked empty, never recursive deletion
        stage.rename(root)
    return {"ready": True, "model_fingerprint": fingerprint, "downloaded": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-root", default=".rag_cache/ocr-models")
    parser.add_argument("--download-models", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(Path(args.model_root), allow_download=args.download_models)))
    except Exception as exc:
        # Exception text can contain local paths/URLs; emit a fixed category only.
        print(json.dumps({"ready": False, "error": "ocr_prepare_failed", "kind": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
