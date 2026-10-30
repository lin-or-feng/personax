"""Single-operator durable document intake. Private payloads are not audit logs."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import uuid
from contextlib import closing
from pathlib import Path

from .document_parser import DocumentError, MAX_FILE_BYTES, MAX_TEXT_CHARS, clean_text
from .knowledge import KnowledgeBaseService
from .types import (KnowledgeImportRequest, KnowledgeReviewedParse, WorkspaceConfig,
    WorkspaceEditRequest, WorkspaceJob, WorkspacePreview, WorkspaceReviewRequest)

SUFFIXES = {".md", ".txt", ".html", ".htm", ".pdf", ".png", ".jpg", ".jpeg"}
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
JOB_ID = re.compile(r"^job-[a-f0-9]{32}$")


class WorkspaceError(ValueError):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def review_bytes(review: KnowledgeReviewedParse) -> bytes:
    return review.model_dump_json().encode("utf-8")


class WorkspaceService:
    def __init__(self, config: WorkspaceConfig, *, ocr_backend=None):
        self.config = config
        self.root = Path(config.root).resolve()
        self.sources, self.previews = self.root / "sources", self.root / "previews"
        for path in (self.root, self.sources, self.previews):
            if path.is_symlink():
                raise WorkspaceError("workspace_path_invalid", 503)
            path.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "jobs.sqlite3"
        self.knowledge = KnowledgeBaseService(self.root / "catalog.sqlite3", self.sources,
            parse_options=config.parse_options, ocr_backend=ocr_backend)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workspace_jobs (
                    job_id TEXT PRIMARY KEY, filename TEXT NOT NULL, suffix TEXT NOT NULL,
                    content_hash TEXT NOT NULL, file_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    preview_key TEXT NOT NULL DEFAULT '', preview_hash TEXT NOT NULL DEFAULT '',
                    page_count INTEGER NOT NULL DEFAULT 0, unit_count INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '', document_id TEXT NOT NULL DEFAULT '',
                    document_version TEXT NOT NULL DEFAULT '', lease_token TEXT NOT NULL DEFAULT '',
                    lease_until REAL NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS workspace_events (
                    event_id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, event TEXT NOT NULL,
                    revision INTEGER NOT NULL, created_at REAL NOT NULL);
            """)

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _event(db, row, event):
        db.execute("INSERT INTO workspace_events(job_id,event,revision,created_at) VALUES (?,?,?,?)",
            (row["job_id"], event, row["revision"], time.time()))

    @staticmethod
    def _job(row) -> WorkspaceJob:
        if row is None:
            raise WorkspaceError("job_not_found", 404)
        return WorkspaceJob.model_validate({key: row[key] for key in WorkspaceJob.model_fields})

    def _row(self, job_id: str, db=None):
        if not JOB_ID.fullmatch(job_id):
            raise WorkspaceError("job_not_found", 404)
        if db is not None:
            row = db.execute("SELECT * FROM workspace_jobs WHERE job_id=?", (job_id,)).fetchone()
        else:
            with closing(self._connect()) as connection:
                row = connection.execute("SELECT * FROM workspace_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise WorkspaceError("job_not_found", 404)
        return row

    def source_path(self, job_id: str) -> Path:
        row = self._row(job_id)
        path = self.sources / (job_id + row["suffix"])
        if path.is_symlink() or not path.resolve().is_relative_to(self.sources):
            raise WorkspaceError("source_changed", 409)
        return path

    def source_bytes(self, job_id: str) -> tuple[bytes, str]:
        row = self._row(job_id)
        if row["status"] == "cancelled":
            raise WorkspaceError("job_not_found", 404)
        path = self.source_path(job_id)
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise WorkspaceError("source_changed")
            data = path.read_bytes()
        except OSError:
            raise WorkspaceError("source_unavailable") from None
        if digest(data) != row["content_hash"]:
            raise WorkspaceError("source_changed")
        return data, row["suffix"]

    def upload(self, filename: str, data: bytes) -> WorkspaceJob:
        filename = unicodedata.normalize("NFC", filename).strip()
        if (not filename or len(filename) > 180 or any(c in filename for c in "/\\:\0")
                or any(unicodedata.category(c).startswith("C") for c in filename)):
            raise WorkspaceError("invalid_filename", 400)
        suffix = Path(filename).suffix.lower()
        if suffix not in SUFFIXES:
            raise WorkspaceError("unsupported_format", 415)
        if not data or len(data) > MAX_FILE_BYTES:
            raise WorkspaceError("file_size_invalid", 413)
        # Fail obvious extension spoofing before queuing expensive parsing.
        if (suffix == ".pdf" and not data.startswith(b"%PDF-") or
                suffix == ".png" and not data.startswith(b"\x89PNG\r\n\x1a\n") or
                suffix in {".jpg", ".jpeg"} and not data.startswith(b"\xff\xd8\xff")):
            raise WorkspaceError("file_signature_invalid", 415)
        job_id = "job-" + uuid.uuid4().hex
        now, content_hash = time.time(), digest(data)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            count, reserved = db.execute("SELECT COUNT(*),COALESCE(SUM(file_bytes + ?),0) FROM workspace_jobs",
                (MAX_PREVIEW_BYTES * 4,)).fetchone()
            if count >= self.config.max_jobs or reserved + len(data) + MAX_PREVIEW_BYTES * 4 > self.config.storage_budget_bytes:
                raise WorkspaceError("workspace_capacity_reached", 429)
            path = self.sources / (job_id + suffix)
            try:
                with path.open("xb") as handle:
                    handle.write(data)
                db.execute("INSERT INTO workspace_jobs(job_id,filename,suffix,content_hash,file_bytes,status,revision,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,'queued',1,?,?)", (job_id, filename, suffix, content_hash, len(data), now, now))
                row = self._row(job_id, db)
                self._event(db, row, "uploaded")
            except Exception:
                if path.exists():
                    path.unlink()
                raise
        return self._job(row)

    def jobs(self) -> list[WorkspaceJob]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM workspace_jobs ORDER BY created_at DESC LIMIT ?", (self.config.max_jobs,)).fetchall()
        return [self._job(row) for row in rows]

    def job(self, job_id: str) -> WorkspaceJob:
        return self._job(self._row(job_id))

    def events(self, job_id: str) -> list[dict]:
        self._row(job_id)
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute("SELECT event,revision,created_at FROM workspace_events WHERE job_id=? ORDER BY event_id", (job_id,))]

    def _artifact(self, key: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}\.json", key):
            raise WorkspaceError("preview_unavailable")
        path = self.previews / key
        if path.is_symlink() or not path.resolve().is_relative_to(self.previews):
            raise WorkspaceError("preview_unavailable")
        return path

    def _review(self, row) -> KnowledgeReviewedParse:
        try:
            path = self._artifact(row["preview_key"])
            if path.stat().st_size > MAX_PREVIEW_BYTES:
                raise WorkspaceError("preview_changed")
            raw = path.read_bytes()
            if digest(raw) != row["preview_hash"]:
                raise WorkspaceError("preview_changed")
            review = KnowledgeReviewedParse.model_validate_json(raw)
            if review.content_hash != row["content_hash"]:
                raise WorkspaceError("preview_changed")
            return review
        except (OSError, ValueError) as exc:
            if isinstance(exc, WorkspaceError):
                raise
            raise WorkspaceError("preview_unavailable") from None

    def preview(self, job_id: str) -> WorkspacePreview:
        row = self._row(job_id)
        if row["status"] not in {"awaiting_review", "queued_commit", "committing", "completed"}:
            raise WorkspaceError("preview_not_ready")
        self.source_bytes(job_id)
        return WorkspacePreview(job=self._job(row), parsed=self._review(row).parsed)

    def _write_review(self, review: KnowledgeReviewedParse) -> tuple[str, str]:
        raw = review_bytes(review)
        if len(raw) > MAX_PREVIEW_BYTES or len(review.parsed.units) > 2000 or sum(len(u.text) for u in review.parsed.units) > MAX_TEXT_CHARS:
            raise WorkspaceError("preview_limit")
        key = uuid.uuid4().hex + ".json"
        with self._artifact(key).open("xb") as handle:
            handle.write(raw)
        return key, digest(raw)

    @staticmethod
    def _check_review(row, request):
        if row["revision"] != request.revision or row["preview_hash"] != request.preview_hash:
            raise WorkspaceError("stale_review")

    def edit(self, job_id: str, request: WorkspaceEditRequest) -> WorkspacePreview:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(job_id, db)
            self._check_review(row, request)
            if row["status"] != "awaiting_review":
                raise WorkspaceError("job_not_editable")
            self.source_bytes(job_id)
            review = self._review(row)
            seen = set()
            for edit in request.edits:
                if edit.unit_index >= len(review.parsed.units) or edit.unit_index in seen:
                    raise WorkspaceError("invalid_edit", 422)
                seen.add(edit.unit_index)
                text = clean_text(edit.text)
                if not text.strip():
                    raise WorkspaceError("invalid_edit", 422)
                unit = review.parsed.units[edit.unit_index]
                review.parsed.units[edit.unit_index] = unit.model_copy(update={"text": text, "human_edited": True})
            key, preview_hash = self._write_review(review)
            try:
                db.execute("UPDATE workspace_jobs SET preview_key=?,preview_hash=?,revision=revision+1,updated_at=? WHERE job_id=?",
                    (key, preview_hash, time.time(), job_id))
                current = self._row(job_id, db)
                self._event(db, current, "preview_edited")
            except Exception:
                self._artifact(key).unlink(missing_ok=True)
                raise
        self._artifact(row["preview_key"]).unlink(missing_ok=True)
        return WorkspacePreview(job=self._job(current), parsed=review.parsed)

    def confirm(self, job_id: str, request: WorkspaceReviewRequest) -> WorkspaceJob:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(job_id, db)
            # Exact repeated requests after acceptance are idempotent, never a second publish.
            if (row["status"] in {"queued_commit", "committing", "completed"}
                    and row["preview_hash"] == request.preview_hash and row["revision"] >= request.revision + 1):
                return self._job(row)
            self._check_review(row, request)
            if row["status"] != "awaiting_review":
                raise WorkspaceError("job_not_reviewable")
            self.source_bytes(job_id)
            self._review(row)
            db.execute("UPDATE workspace_jobs SET status='queued_commit',revision=revision+1,updated_at=? WHERE job_id=?", (time.time(), job_id))
            current = self._row(job_id, db)
            self._event(db, current, "review_confirmed")
        return self._job(current)

    def cancel(self, job_id: str) -> WorkspaceJob:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(job_id, db)
            if row["status"] in {"committing", "completed"}:
                raise WorkspaceError("job_not_cancellable")
            if row["status"] != "cancelled":
                db.execute("UPDATE workspace_jobs SET status='cancelled',revision=revision+1,lease_token='',lease_until=0,updated_at=? WHERE job_id=?", (time.time(), job_id))
                row = self._row(job_id, db)
                self._event(db, row, "cancelled")
        return self._job(row)

    def retry(self, job_id: str) -> WorkspaceJob:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(job_id, db)
            if row["status"] != "failed" or row["retries"] >= 3:
                raise WorkspaceError("retry_not_allowed")
            self.source_bytes(job_id)
            db.execute("UPDATE workspace_jobs SET status='queued',revision=revision+1,retries=retries+1,preview_key='',preview_hash='',reason='',updated_at=? WHERE job_id=?", (time.time(), job_id))
            row = self._row(job_id, db)
            self._event(db, row, "retried")
        return self._job(row)

    def _claim(self):
        now = time.time()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for old in db.execute("SELECT * FROM workspace_jobs WHERE status IN ('parsing','committing') AND lease_until < ?", (now,)).fetchall():
                db.execute("UPDATE workspace_jobs SET status='failed',reason='worker_lease_expired',revision=revision+1,lease_token='',updated_at=? WHERE job_id=?", (now, old["job_id"]))
                self._event(db, self._row(old["job_id"], db), "worker_lease_expired")
            row = db.execute("SELECT * FROM workspace_jobs WHERE status IN ('queued','queued_commit') ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            state = "parsing" if row["status"] == "queued" else "committing"
            db.execute("UPDATE workspace_jobs SET status=?,revision=revision+1,lease_token=?,lease_until=?,updated_at=? WHERE job_id=?",
                (state, token, now + self.config.lease_seconds, now, row["job_id"]))
            row = self._row(row["job_id"], db)
            self._event(db, row, state)
            return row

    def _finish(self, claim, state, *, reason="", key="", preview_hash="", pages=0, units=0, document_id="", document_version=""):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = self._row(claim["job_id"], db)
            if current["lease_token"] != claim["lease_token"] or current["status"] != claim["status"]:
                return False
            db.execute("UPDATE workspace_jobs SET status=?,reason=?,revision=revision+1,lease_token='',lease_until=0,updated_at=?,"
                "preview_key=CASE WHEN ?='' THEN preview_key ELSE ? END,preview_hash=CASE WHEN ?='' THEN preview_hash ELSE ? END,"
                "page_count=CASE WHEN ?=0 THEN page_count ELSE ? END,unit_count=CASE WHEN ?=0 THEN unit_count ELSE ? END,"
                "document_id=?,document_version=? WHERE job_id=?",
                (state, reason, time.time(), key, key, preview_hash, preview_hash, pages, pages, units, units,
                 document_id, document_version, claim["job_id"]))
            self._event(db, self._row(claim["job_id"], db), state)
            return True

    def run_once(self) -> bool:
        claim = self._claim()
        if claim is None:
            return False
        job_id, key = claim["job_id"], ""
        try:
            self.source_bytes(job_id)
            request = KnowledgeImportRequest(kb_id=self.config.kb_id, source_path=job_id + claim["suffix"],
                title=claim["filename"], license="operator-provided", accept_ocr_review=True)
            if claim["status"] == "parsing":
                review = self.knowledge.prepare_review(request)
                key, preview_hash = self._write_review(review)
                accepted = self._finish(claim, "awaiting_review", key=key, preview_hash=preview_hash,
                    pages=len(review.parsed.pages), units=len(review.parsed.units))
                if not accepted:
                    self._artifact(key).unlink(missing_ok=True)
            else:
                result = self.knowledge.ingest(request, reviewed_parse=self._review(claim))
                if result.status == "failed":
                    raise DocumentError(result.reason)
                self._finish(claim, "completed", document_id=result.document_id, document_version=result.document_version)
        except (DocumentError, WorkspaceError) as exc:
            self._finish(claim, "failed", reason=str(exc))
        except Exception:
            self._finish(claim, "failed", reason="internal_error")
        return True

    def source_job(self, document_id: str, document_version: str) -> WorkspaceJob:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM workspace_jobs WHERE document_id=? AND document_version=? AND status='completed'",
                (document_id, document_version)).fetchone()
        return self._job(row)
