"""Build a small literal co-mention graph from already imported official docs.

Curated symbol pairs define the extraction scope, not asserted semantic relations.
No model, external URL, biomedical assertion or arbitrary query language is used.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.knowledge_graph import KnowledgeGraphService, contains_entity
from core.knowledge_store import KnowledgeStore
from core.types import KnowledgeRelationInput

PAIRS = [("Task", "create_task"), ("Task", "TaskGroup"), ("TaskGroup", "create_task"),
    ("Semaphore", "Lock"), ("Semaphore", "BoundedSemaphore"), ("Lock", "Event"),
    ("ThreadPoolExecutor", "Future"), ("ThreadPoolExecutor", "Executor"),
    ("Connection", "commit"), ("Connection", "rollback"), ("commit", "rollback"),
    ("dumps", "ensure_ascii"), ("dumps", "loads"), ("Logger", "Handler"),
    ("Handler", "Formatter"), ("sha256", "hexdigest"), ("sha256", "digest"),
    ("contextmanager", "ExitStack"), ("Mock", "MagicMock"), ("Mock", "patch")]


def build(db_path: Path) -> dict:
    service = KnowledgeGraphService(db_path)
    documents = KnowledgeStore(db_path).documents("python-official")
    count = 0
    for subject, obj in PAIRS:
        added = False
        for document in documents:
            for chunk in document.chunks:
                if not all(contains_entity(chunk.text, term) for term in (subject, obj)):
                    continue
                if len(chunk.text) <= 600:
                    quote = chunk.text
                else:
                    positions = [chunk.text.casefold().find(x.casefold()) for x in (subject, obj)]
                    start = max(0, min(positions) - 50)
                    quote = chunk.text[start:start + 600]
                if not all(contains_entity(quote, term) for term in (subject, obj)):
                    continue
                service.add(KnowledgeRelationInput(kb_id="python-official", subject=subject, object=obj,
                    document_id=document.document_id, document_version=document.document_version,
                    chunk_id=chunk.chunk_id, evidence=quote))
                count += 1
                added = True
                break
            if added:
                break
    return {"kb_id": "python-official", "relations": count,
        "mode": "evidence_backed_cooccurrence_graph", "semantic_or_causal_extraction": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "logs/knowledge.sqlite3")
    args = parser.parse_args()
    print(json.dumps(build(args.db), indent=2))
