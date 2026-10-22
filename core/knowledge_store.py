"""Transactional, local single-operator document catalog and import journal."""
from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from .document_parser import DocumentError
from .types import KnowledgeDocument, KnowledgeJob


class KnowledgeStore:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS kb_documents (
                    kb_id TEXT NOT NULL, document_id TEXT NOT NULL,
                    document_version TEXT NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(kb_id,document_id));
                CREATE TABLE IF NOT EXISTS kb_jobs (
                    job_id TEXT PRIMARY KEY, kb_id TEXT NOT NULL,
                    document_id TEXT NOT NULL, operation TEXT NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS kb_events (
                    event_id INTEGER PRIMARY KEY, job_id TEXT NOT NULL,
                    event TEXT NOT NULL, created_at REAL NOT NULL);
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def get(self, kb_id: str, document_id: str) -> KnowledgeDocument | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT payload FROM kb_documents WHERE kb_id=? AND document_id=?",
                (kb_id, document_id)).fetchone()
        return KnowledgeDocument.model_validate_json(row[0]) if row else None

    def documents(self, kb_id: str, *, include_deleted: bool = False) -> list[KnowledgeDocument]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT payload FROM kb_documents WHERE kb_id=? "
                + ("" if include_deleted else "AND status='active' ") + "ORDER BY document_id", (kb_id,)).fetchall()
        return [KnowledgeDocument.model_validate_json(row[0]) for row in rows]

    def start(self, kb_id: str, document_id: str, operation: str) -> str:
        if operation not in {"ingest", "delete"}:
            raise ValueError("invalid job operation")
        job_id = "kbjob-" + uuid.uuid4().hex
        now = time.time()
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO kb_jobs VALUES (?,?,?,?,?,?,?,?)",
                (job_id, kb_id, document_id, operation, "running", "", now, now))
        return job_id

    def _event(self, db, job_id: str, event: str):
        db.execute("INSERT INTO kb_events(job_id,event,created_at) VALUES (?,?,?)",
            (job_id, event, time.time()))

    def publish(self, job_id: str, document: KnowledgeDocument, expected: KnowledgeDocument | None) -> None:
        """All persisted chunk data and completion audit commit or roll back together."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT payload FROM kb_documents "
                "WHERE kb_id=? AND document_id=?", (document.kb_id, document.document_id)).fetchone()
            current_document = KnowledgeDocument.model_validate_json(current[0]) if current else None
            def revision(item):
                return (item.document_version, item.status, item.revision) if item else None
            if revision(current_document) != revision(expected):
                raise DocumentError("concurrent_update")
            job = db.execute("SELECT kb_id,document_id,operation,status FROM kb_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job or job[3] != "running":
                raise DocumentError("job_not_running")
            expected_operation = "delete" if document.status == "deleted" else "ingest"
            if tuple(job[:3]) != (document.kb_id, document.document_id, expected_operation):
                raise DocumentError("job_identity_mismatch")
            document.updated_at = time.time()
            document.revision = (current_document.revision if current_document else 0) + 1
            db.execute("INSERT INTO kb_documents VALUES (?,?,?,?,?) "
                "ON CONFLICT(kb_id,document_id) DO UPDATE SET "
                "document_version=excluded.document_version,status=excluded.status,payload=excluded.payload",
                (document.kb_id, document.document_id, document.document_version,
                 document.status, document.model_dump_json()))
            db.execute("UPDATE kb_jobs SET status='completed',reason='',updated_at=? WHERE job_id=?",
                (time.time(), job_id))
            self._event(db, job_id, "document_" + document.status)

    def finish(self, job_id: str, *, failed: bool = False, reason: str = "unchanged") -> None:
        status = "failed" if failed else "completed"
        with closing(self._connect()) as db, db:
            updated = db.execute("UPDATE kb_jobs SET status=?,reason=?,updated_at=? "
                "WHERE job_id=? AND status='running'", (status, reason, time.time(), job_id)).rowcount
            if updated:
                self._event(db, job_id, reason)

    def jobs(self, kb_id: str, limit: int = 50) -> list[KnowledgeJob]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM kb_jobs WHERE kb_id=? ORDER BY created_at DESC LIMIT ?",
                (kb_id, min(max(limit, 1), 100))).fetchall()
        return [KnowledgeJob.model_validate(dict(row)) for row in rows]

    def confirm_unchanged(self, job_id: str, expected: KnowledgeDocument) -> None:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM kb_documents WHERE kb_id=? AND document_id=?",
                (expected.kb_id, expected.document_id)).fetchone()
            current = KnowledgeDocument.model_validate_json(row[0]) if row else None
            if not current or current.status != "active" or current.document_version != expected.document_version or current.revision != expected.revision:
                raise DocumentError("concurrent_update")
            changed = db.execute("UPDATE kb_jobs SET status='completed',reason='unchanged',updated_at=? "
                "WHERE job_id=? AND status='running' AND kb_id=? AND document_id=? AND operation='ingest'",
                (time.time(), job_id, expected.kb_id, expected.document_id)).rowcount
            if not changed:
                raise DocumentError("job_not_running")
            self._event(db, job_id, "unchanged")

    def recover(self, kb_id: str) -> int:
        """Caller must verify no import process is running. Never resumes side effects."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT job_id FROM kb_jobs WHERE kb_id=? AND status='running'", (kb_id,)).fetchall()
            for row in rows:
                db.execute("UPDATE kb_jobs SET status='abandoned',reason='operator_recovery',updated_at=? WHERE job_id=?",
                    (time.time(), row[0]))
                self._event(db, row[0], "operator_recovery")
        return len(rows)
