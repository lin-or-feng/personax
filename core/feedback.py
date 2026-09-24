"""本地回答反馈闭环：显式同意、PII 掩码、可导出评测候选。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from .memory import sanitize_memory_text
from .types import (
    AssistantFeedback,
    FeedbackSourceRef,
    FeedbackSummary,
)


DEFAULT_FEEDBACK_PATH = (
    Path(__file__).resolve().parent.parent / "logs" / "assistant_feedback.sqlite3"
)
ALLOWED_REASONS = {
    "", "inaccurate", "missing_evidence", "irrelevant", "incomplete", "slow", "other",
}
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.I),
)


def _safe_text(value: str, *, limit: int = 500) -> tuple[str, bool]:
    cleaned, redacted = sanitize_memory_text(value)
    for pattern in _SECRET_PATTERNS:
        cleaned, count = pattern.subn("[REDACTED_SECRET]", cleaned)
        redacted = redacted or bool(count)
    return cleaned[:limit], redacted


class AssistantFeedbackStore:
    """每个 trace 只保留一条最新反馈；不维持共享数据库连接。"""

    def __init__(self, path: str | Path = DEFAULT_FEEDBACK_PATH):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute(
            "CREATE TABLE IF NOT EXISTS assistant_feedback ("
            "feedback_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "trace_id TEXT NOT NULL UNIQUE, thread_id TEXT NOT NULL DEFAULT '', "
            "rating TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', "
            "note TEXT NOT NULL DEFAULT '', question TEXT NOT NULL DEFAULT '', "
            "answer_excerpt TEXT NOT NULL DEFAULT '', answer_hash TEXT NOT NULL, "
            "source_refs_json TEXT NOT NULL DEFAULT '[]', "
            "route TEXT NOT NULL DEFAULT '', source_count INTEGER NOT NULL DEFAULT 0, "
            "degraded INTEGER NOT NULL DEFAULT 0, "
            "content_included INTEGER NOT NULL DEFAULT 0, "
            "redacted INTEGER NOT NULL DEFAULT 0, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_assistant_feedback_updated "
            "ON assistant_feedback(updated_at DESC)"
        )
        db.commit()
        return db

    @staticmethod
    def _from_row(row: sqlite3.Row) -> AssistantFeedback:
        try:
            raw_refs = json.loads(row["source_refs_json"] or "[]")
        except json.JSONDecodeError:
            raw_refs = []
        refs = [
            FeedbackSourceRef.model_validate(item)
            for item in raw_refs
            if isinstance(item, dict)
        ][:10]
        return AssistantFeedback(
            feedback_id=row["feedback_id"],
            trace_id=row["trace_id"],
            thread_id=row["thread_id"],
            rating=row["rating"],
            reason=row["reason"],
            note=row["note"],
            question=row["question"],
            answer_excerpt=row["answer_excerpt"],
            answer_hash=row["answer_hash"],
            source_refs=refs,
            route=row["route"],
            source_count=row["source_count"],
            degraded=bool(row["degraded"]),
            content_included=bool(row["content_included"]),
            redacted=bool(row["redacted"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def upsert(
        self,
        *,
        trace_id: str,
        thread_id: str,
        rating: str,
        answer: str,
        reason: str = "",
        note: str = "",
        question: str = "",
        source_refs: Iterable[FeedbackSourceRef | dict] = (),
        route: str = "",
        source_count: int = 0,
        degraded: bool = False,
        include_content: bool = False,
    ) -> AssistantFeedback:
        normalized_trace = (trace_id or "").strip()[:80]
        if len(normalized_trace) < 8:
            raise ValueError("反馈缺少有效 trace_id")
        if rating not in {"helpful", "not_helpful"}:
            raise ValueError("rating 必须是 helpful 或 not_helpful")
        if reason not in ALLOWED_REASONS:
            raise ValueError("反馈原因不在允许列表")
        if route not in {"", "direct", "knowledge"}:
            raise ValueError("反馈 route 非法")

        safe_note, note_redacted = _safe_text(note)
        safe_question = ""
        safe_answer = ""
        content_redacted = False
        refs: list[FeedbackSourceRef] = []
        if include_content:
            safe_question, question_redacted = _safe_text(question)
            safe_answer, answer_redacted = _safe_text(answer)
            content_redacted = question_redacted or answer_redacted
            for item in source_refs:
                ref = item if isinstance(item, FeedbackSourceRef) else FeedbackSourceRef.model_validate(item)
                refs.append(ref)
                if len(refs) >= 10:
                    break
        now = time.time()
        answer_hash = hashlib.sha256((answer or "").encode("utf-8")).hexdigest()
        refs_json = json.dumps(
            [item.model_dump(mode="json") for item in refs], ensure_ascii=False,
        )
        with self._connect() as db:
            db.execute(
                "INSERT INTO assistant_feedback ("
                "trace_id, thread_id, rating, reason, note, question, answer_excerpt, "
                "answer_hash, source_refs_json, route, source_count, degraded, "
                "content_included, redacted, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(trace_id) DO UPDATE SET "
                "thread_id=excluded.thread_id, rating=excluded.rating, "
                "reason=excluded.reason, note=excluded.note, "
                "question=excluded.question, answer_excerpt=excluded.answer_excerpt, "
                "answer_hash=excluded.answer_hash, source_refs_json=excluded.source_refs_json, "
                "route=excluded.route, source_count=excluded.source_count, "
                "degraded=excluded.degraded, content_included=excluded.content_included, "
                "redacted=excluded.redacted, updated_at=excluded.updated_at",
                (
                    normalized_trace, (thread_id or "").strip()[:200], rating, reason,
                    safe_note, safe_question, safe_answer, answer_hash, refs_json,
                    route, max(0, min(int(source_count), 100)), int(degraded),
                    int(include_content), int(note_redacted or content_redacted), now, now,
                ),
            )
            row = db.execute(
                "SELECT * FROM assistant_feedback WHERE trace_id = ?",
                (normalized_trace,),
            ).fetchone()
            db.commit()
        if row is None:  # pragma: no cover - SQLite 写入后理论上不可达
            raise RuntimeError("回答反馈保存失败")
        return self._from_row(row)

    def get(self, trace_id: str) -> AssistantFeedback | None:
        if not self.path.exists():
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM assistant_feedback WHERE trace_id = ?",
                ((trace_id or "").strip()[:80],),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_recent(self, limit: int = 100) -> list[AssistantFeedback]:
        if not self.path.exists():
            return []
        safe_limit = max(1, min(int(limit), 1_000))
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM assistant_feedback ORDER BY updated_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def summary(self) -> FeedbackSummary:
        rows = self.list_recent(limit=1_000)
        helpful = sum(item.rating == "helpful" for item in rows)
        not_helpful = len(rows) - helpful
        reasons: dict[str, int] = {}
        for item in rows:
            if item.reason:
                reasons[item.reason] = reasons.get(item.reason, 0) + 1
        return FeedbackSummary(
            total=len(rows),
            helpful=helpful,
            not_helpful=not_helpful,
            helpful_rate=round(helpful / len(rows), 4) if rows else None,
            eval_candidates=sum(
                item.rating == "not_helpful" and item.content_included
                for item in rows
            ),
            reasons=reasons,
        )

    def export_eval_candidates(self) -> list[dict]:
        """仅导出用户显式同意保存内容的负反馈，保持待人工标注状态。"""

        return [
            {
                "id": f"feedback_{item.feedback_id:04d}",
                "trace_id": item.trace_id,
                "question": item.question,
                "answer": item.answer_excerpt,
                "failure_reason": item.reason or "unspecified",
                "source_refs": [ref.model_dump(mode="json") for ref in item.source_refs],
                "review_status": "needs_human_label",
                "redacted": item.redacted,
            }
            for item in self.list_recent(limit=1_000)
            if item.rating == "not_helpful" and item.content_included
        ]

    def delete_all(self) -> int:
        if not self.path.exists():
            return 0
        with self._connect() as db:
            cursor = db.execute("DELETE FROM assistant_feedback")
            db.commit()
        return int(cursor.rowcount)
