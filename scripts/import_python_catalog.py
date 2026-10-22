"""Small official Python documentation pack, with provenance and retained license.

Network access is an explicit operator CLI action, never a chat tool or upload API.
Default imports into the same catalog used by CLI, FastAPI and Streamlit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.knowledge import KnowledgeBaseService
from core.types import KnowledgeImportRequest

PAGES = {
    "asyncio-task": "asyncio 协程与任务 Task 超时和取消",
    "asyncio-sync": "asyncio 同步 Semaphore Lock Event",
    "concurrent.futures": "并发执行 Executor Future ThreadPoolExecutor",
    "sqlite3": "SQLite 数据库事务 autocommit 与参数绑定",
    "json": "JSON 编解码 dumps loads ensure_ascii",
    "pathlib": "文件系统路径 Path resolve 符号链接",
    "logging": "日志 Logger Handler Formatter",
    "hashlib": "安全散列 sha256 digest hexdigest",
    "contextlib": "上下文管理 contextmanager ExitStack",
    "unittest.mock": "测试替身 Mock patch MagicMock",
}
LICENSE_URL = "https://docs.python.org/3.12/license.html"
LICENSE_NOTE = "Python documentation PSF License Version 2; code examples additionally 0BSD; see retained PYTHON_LICENSE.html"


def download(url: str) -> bytes:
    if not url.startswith("https://docs.python.org/"):
        raise ValueError("source_not_allowlisted")
    request = urllib.request.Request(url, headers={"User-Agent": "PersonaX-local-document-demo/1.0"})
    with urllib.request.urlopen(request, timeout=25) as response:
        if not response.geturl().startswith("https://docs.python.org/"):
            raise ValueError("source_redirect_not_allowed")
        data = response.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise ValueError("source_too_large")
    data.decode("utf-8-sig")
    return data


def run(source_root: Path, db_path: Path, *, refresh: bool = False) -> dict:
    source_root.mkdir(parents=True, exist_ok=True)
    manifest_path = source_root / "manifest.json"
    existing = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"documents": []}
    owned = {row["file"] for row in existing["documents"]} | {"PYTHON_LICENSE.html"}
    previous_by_file = {row["file"]: row for row in existing["documents"]}
    service = KnowledgeBaseService(db_path, source_root)
    rows = []
    license_path = source_root / "PYTHON_LICENSE.html"
    if not license_path.exists() or refresh:
        license_path.write_bytes(download(LICENSE_URL))
    for slug, title in PAGES.items():
        filename = slug + ".html"
        path = source_root / filename
        url = f"https://docs.python.org/zh-cn/3.12/library/{slug}.html"
        if path.exists() and filename not in owned:
            raise ValueError("unmanaged_existing_document")
        fetched_at = previous_by_file.get(filename, {}).get("fetched_at", existing.get("fetched_at", ""))
        if not path.exists() or refresh:
            data = download(url)
            path.write_bytes(data)
            fetched_at = datetime.now(timezone.utc).isoformat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        previous_digest = previous_by_file.get(filename, {}).get("sha256")
        if not refresh and previous_digest and digest != previous_digest:
            raise ValueError("cached_source_hash_mismatch_use_explicit_refresh")
        result = service.ingest(KnowledgeImportRequest(kb_id="python-official", source_path=filename,
            title="Python 3.12 " + title, source_url=url, license=LICENSE_NOTE))
        if result.status == "failed":
            raise RuntimeError(f"import_failed:{slug}:{result.reason}")
        rows.append({"file": filename, "source_url": url, "title": title, "sha256": digest, "fetched_at": fetched_at,
            "document_id": result.document_id, "document_version": result.document_version,
            "chunk_count": result.chunk_count, "status": result.status})
        # Retain completed ownership; an untracked partial file requires operator verification.
        manifest_path.write_text(json.dumps({"provider": "Python official documentation", "series": "3.12",
            "fetched_at": existing.get("fetched_at") or fetched_at,
            "verified_at": datetime.now(timezone.utc).isoformat(), "license_url": LICENSE_URL,
            "license_notice": LICENSE_NOTE, "changes": "Original UTF-8 HTML retained; extraction removes navigation/script/style for local retrieval.",
            "independent_domain_evaluation": False,
            "documents": rows + [r for r in existing["documents"] if r["file"] not in {x["file"] for x in rows}]},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"file": filename, "status": result.status, "chunks": result.chunk_count}), flush=True)
    return {"documents": len(rows), "chunks": sum(row["chunk_count"] for row in rows),
        "source_root": str(source_root), "db_path": str(db_path), "kb_id": "python-official"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT / "examples/python_official")
    parser.add_argument("--db", type=Path, default=ROOT / "logs/knowledge.sqlite3")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.source_root, args.db, refresh=args.refresh), indent=2))
