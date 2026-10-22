"""Real loopback HTTP acceptance with an owned Uvicorn thread and offline models.

No paid model, browser login, publishing, remote deployment or background daemon.
"""
from __future__ import annotations

import json
import argparse
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run() -> dict:
    import uvicorn

    token = uuid.uuid4().hex
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="personax-native-catalog-") as directory:
        os.environ.update(LLM_BACKEND="offline", DEEPSEEK_API_KEY="", OPENAI_API_KEY="",
            PERSONAX_API_TOKEN=token, PERSONAX_KNOWLEDGE_DB=str(ROOT / "logs/knowledge.sqlite3"),
            PERSONAX_ASSISTANT_DB=str(Path(directory) / "checkpoints.db"), PYTHONIOENCODING="utf-8")
        server = uvicorn.Server(uvicorn.Config("api:app", host="127.0.0.1", port=port,
            workers=1, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, name="catalog-http-acceptance", daemon=True)
        thread.start()

        def call(path, payload=None, *, authenticated=True):
            request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if authenticated else {})})
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read().decode("utf-8")
        try:
            for attempt in range(50):
                if not thread.is_alive():
                    raise RuntimeError("api_startup_failed")
                try:
                    if call("/healthz")[0] == 200:
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                raise RuntimeError("api_startup_timed_out")
            payload = {"question": "Semaphore acquire release 信号量", "knowledge_source": "catalog",
                "kb_id": "python-official", "answer_mode": "extractive"}
            try:
                call("/v1/assistant/reply", payload, authenticated=False)
                raise AssertionError("missing auth accepted")
            except urllib.error.HTTPError as error:
                assert error.code == 401
            status, body = call("/v1/assistant/reply", payload)
            answer = json.loads(body)
            assert status == 200 and answer["sources"] and answer["answer_mode"] == "extractive_baseline"
            status, streamed = call("/v1/assistant/stream", payload)
            assert status == 200 and "event: token" in streamed and "event: done" in streamed
            status, body = call("/v1/knowledge/graph", {"kb_id": "python-official", "entity": "Task"})
            assert status == 200 and json.loads(body)["paths"]
            cli = subprocess.run([sys.executable, "main.py", "knowledge", "--kb-id", "python-official",
                "answer", "--query", payload["question"]], cwd=ROOT, env=os.environ.copy(),
                capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
            cli_result = json.loads(cli.stdout)
            assert cli_result["index_version"] == answer["index_version"]
            refused = json.loads(call("/v1/assistant/reply", payload | {"question": "UnobtainiumHyperdriveNebula"})[1])
            assert refused["answer_mode"] == "refused" and not refused["sources"]
            result = {"native_http": True, "auth_rejection": True, "json_sources": True, "sse_token_done": True,
                "graph_paths": True, "cli_http_same_index_version": True, "outside_refusal": True,
                "answer_mode": answer["answer_mode"], "index_version": answer["index_version"],
                "sources": [s["source"] for s in answer["sources"]], "model_called": False,
                "publishing_performed": False}
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("task_owned_http_thread_did_not_stop")
        result["test_server_stopped_on_exit"] = True
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-json", default="")
    args = parser.parse_args()
    report = run()
    if args.report_json:
        target = Path(args.report_json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
