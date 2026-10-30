"""Run only this local API and its owned worker; Ctrl+C stops both.

No autorun/service installation or changes to existing servers/containers.
"""
from __future__ import annotations
import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.envfile import load_env_file
from core.local_ocr import _terminate_owned_process, verified_manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--no-ocr", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("port must be 1024..65535")
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        raise SystemExit("Build frontend first: npm --prefix frontend ci; npm --prefix frontend run build")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", args.port))
        except OSError:
            raise SystemExit("Port unavailable. Choose --port; existing processes were not stopped.") from None
    load_env_file(ROOT / ".env")
    directory = Path(os.environ.get("PERSONAX_WORKSPACE_ROOT", str(ROOT / "logs" / "workspace"))).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    access = directory / "access.json"
    if access.is_symlink():
        raise SystemExit("access file must not be a link")
    if access.exists():
        token = json.loads(access.read_text(encoding="utf-8"))["token"]
        if not isinstance(token, str) or len(token) < 24:
            raise SystemExit("invalid access file")
    else:
        token = secrets.token_urlsafe(32)
        with access.open("x", encoding="utf-8") as handle:
            json.dump({"token": token}, handle)
        access.chmod(0o600)
    os.environ.update(PERSONAX_WORKSPACE_ENABLED="1", PERSONAX_WORKSPACE_ROOT=str(directory), PERSONAX_WORKSPACE_TOKEN=token)
    if args.no_ocr:
        os.environ["PERSONAX_WORKSPACE_OCR"] = "0"
    else:
        models = os.environ.get("PERSONAX_OCR_MODEL_ROOT", str(ROOT / ".rag_cache" / "ocr-models"))
        verified_manifest(models)
        os.environ["PERSONAX_WORKSPACE_OCR"] = "1"
        os.environ["PERSONAX_OCR_MODEL_ROOT"] = models
    print(f"Workspace: http://127.0.0.1:{args.port}/workspace/", flush=True)
    print("Access key: local private file logs/workspace/access.json (or configured workspace root).", flush=True)
    print("Ctrl+C stops this API and its document worker. Other services are untouched.", flush=True)
    worker = subprocess.Popen([sys.executable, "scripts/workspace_worker.py"], cwd=ROOT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt")
    try:
        import uvicorn
        from api import create_app
        uvicorn.run(create_app(), host="127.0.0.1", port=args.port, workers=1, access_log=False)
    finally:
        _terminate_owned_process(worker)
        worker.wait(timeout=10)


if __name__ == "__main__":
    main()
