"""LangGraph 人工审批：发布前中断、跨重启恢复与持久化幂等。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from .types import Draft, PublishApproval


class ApprovalState(TypedDict):
    request_id: str
    user_id: str
    draft_hash: str
    title: str
    status: str
    reason: str


def draft_fingerprint(draft: Draft) -> str:
    """只对会影响发布内容的字段计算稳定指纹。"""

    payload = json.dumps(
        {
            "topic": draft.topic,
            "title": draft.title or "",
            "body": draft.body or "",
            "tags": list(draft.tags),
            "cover": str((draft.metadata or {}).get("cover") or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _approval_node(state: ApprovalState) -> ApprovalState:
    decision = interrupt({
        "kind": "publish_approval",
        "request_id": state["request_id"],
        "draft_hash": state["draft_hash"],
        "title": state["title"],
        "allowed_decisions": ["approved", "rejected"],
    })
    if isinstance(decision, dict):
        status = str(decision.get("decision") or "rejected").strip().lower()
        reason = str(decision.get("reason") or "").strip()[:500]
    else:
        status = str(decision).strip().lower()
        reason = ""
    if status not in {"approved", "rejected"}:
        status = "rejected"
        reason = reason or "无效审批决定"
    return {**state, "status": status, "reason": reason}


def _build_approval_graph(checkpointer: SqliteSaver):
    graph = StateGraph(ApprovalState)
    graph.add_node("approval_gate", _approval_node)
    graph.set_entry_point("approval_gate")
    graph.add_edge("approval_gate", END)
    return graph.compile(checkpointer=checkpointer)


class PublishApprovalStore:
    """审批索引与 LangGraph Checkpoint 共用一个 SQLite 文件。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, check_same_thread=False)

    @staticmethod
    def _saver(connection: sqlite3.Connection) -> SqliteSaver:
        serializer = JsonPlusSerializer(
            pickle_fallback=False,
            allowed_json_modules=None,
            allowed_msgpack_modules=None,
        )
        return SqliteSaver(connection, serde=serializer)

    def _ensure_schema(self) -> None:
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS publish_approvals ("
                "request_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE, "
                "idempotency_key TEXT NOT NULL, user_id TEXT NOT NULL, "
                "draft_hash TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, "
                "reason TEXT NOT NULL, interrupt_id TEXT NOT NULL, "
                "published_url TEXT NOT NULL, created_at REAL NOT NULL, "
                "resolved_at REAL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_publish_approvals_key "
                "ON publish_approvals(idempotency_key, created_at DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_publish_approvals_status "
                "ON publish_approvals(status, created_at DESC)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_publish_approvals_active_key "
                "ON publish_approvals(idempotency_key) "
                "WHERE status IN ('pending', 'approved', 'consumed')"
            )

    @staticmethod
    def _row(row: tuple[Any, ...] | None) -> PublishApproval | None:
        if row is None:
            return None
        return PublishApproval(
            request_id=row[0], thread_id=row[1], idempotency_key=row[2],
            user_id=row[3], draft_hash=row[4], title=row[5], status=row[6],
            reason=row[7], interrupt_id=row[8], published_url=row[9],
            created_at=row[10], resolved_at=row[11],
        )

    def get(self, request_id: str) -> PublishApproval | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT request_id, thread_id, idempotency_key, user_id, draft_hash, "
                "title, status, reason, interrupt_id, published_url, created_at, resolved_at "
                "FROM publish_approvals WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._row(row)

    def list_recent(self, limit: int = 20) -> list[PublishApproval]:
        safe_limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            rows = db.execute(
                "SELECT request_id, thread_id, idempotency_key, user_id, draft_hash, "
                "title, status, reason, interrupt_id, published_url, created_at, resolved_at "
                "FROM publish_approvals ORDER BY created_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [item for row in rows if (item := self._row(row)) is not None]

    def request(self, draft: Draft, user_id: str) -> PublishApproval:
        normalized_user = (user_id or "anonymous").strip()[:120]
        digest = draft_fingerprint(draft)
        idempotency_key = hashlib.sha256(
            f"{normalized_user}:{digest}".encode("utf-8")
        ).hexdigest()
        with self._connect() as db:
            existing = db.execute(
                "SELECT request_id, thread_id, idempotency_key, user_id, draft_hash, "
                "title, status, reason, interrupt_id, published_url, created_at, resolved_at "
                "FROM publish_approvals WHERE idempotency_key = ? "
                "AND status IN ('pending', 'approved', 'consumed') "
                "ORDER BY created_at DESC LIMIT 1",
                (idempotency_key,),
            ).fetchone()
        if existing is not None:
            record = self._row(existing)
            if record is not None:
                return record

        request_id = f"approval-{uuid.uuid4().hex[:16]}"
        thread_id = f"publish-{request_id}"
        created_at = time.time()
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO publish_approvals "
                    "(request_id, thread_id, idempotency_key, user_id, draft_hash, title, "
                    "status, reason, interrupt_id, published_url, created_at, resolved_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'pending', '', '', '', ?, NULL)",
                    (
                        request_id, thread_id, idempotency_key, normalized_user,
                        digest, (draft.title or "")[:300], created_at,
                    ),
                )
                db.commit()
        except sqlite3.IntegrityError:
            # 并发提交相同草稿时只保留第一个请求，其他调用复用其审批状态。
            with self._connect() as db:
                raced = db.execute(
                    "SELECT request_id, thread_id, idempotency_key, user_id, draft_hash, "
                    "title, status, reason, interrupt_id, published_url, created_at, resolved_at "
                    "FROM publish_approvals WHERE idempotency_key = ? "
                    "AND status IN ('pending', 'approved', 'consumed') LIMIT 1",
                    (idempotency_key,),
                ).fetchone()
            record = self._row(raced)
            if record is None:
                raise
            return record

        config = {"configurable": {"thread_id": thread_id}}
        connection = self._connect()
        try:
            graph = _build_approval_graph(self._saver(connection))
            graph.invoke(
                ApprovalState(
                    request_id=request_id,
                    user_id=normalized_user,
                    draft_hash=digest,
                    title=(draft.title or "")[:300],
                    status="pending",
                    reason="",
                ),
                config=config,
            )
            snapshot = graph.get_state(config)
            interrupt_id = snapshot.interrupts[0].id if snapshot.interrupts else ""
        finally:
            connection.close()
        with self._connect() as db:
            db.execute(
                "UPDATE publish_approvals SET interrupt_id = ? WHERE request_id = ?",
                (interrupt_id, request_id),
            )
            db.commit()
        record = self.get(request_id)
        if record is None:  # pragma: no cover - 写入后理论上不可达
            raise RuntimeError("审批请求保存失败")
        return record

    def decide(
        self,
        request_id: str,
        decision: Literal["approved", "rejected"],
        *,
        reason: str = "",
    ) -> PublishApproval:
        record = self.get(request_id)
        if record is None:
            raise KeyError("审批请求不存在")
        if record.status != "pending":
            return record
        config = {"configurable": {"thread_id": record.thread_id}}
        connection = self._connect()
        try:
            graph = _build_approval_graph(self._saver(connection))
            result = graph.invoke(
                Command(resume={"decision": decision, "reason": reason[:500]}),
                config=config,
            )
        finally:
            connection.close()
        status = str(result.get("status") or "rejected")
        with self._connect() as db:
            db.execute(
                "UPDATE publish_approvals SET status = ?, reason = ?, resolved_at = ? "
                "WHERE request_id = ? AND status = 'pending'",
                (status, str(result.get("reason") or "")[:500], time.time(), request_id),
            )
            db.commit()
        updated = self.get(request_id)
        if updated is None:  # pragma: no cover
            raise RuntimeError("审批结果保存失败")
        return updated

    def require_approved(
        self, request_id: str, draft: Draft, user_id: str,
    ) -> PublishApproval:
        record = self.get(request_id)
        if record is None:
            raise PermissionError("持久化审批不存在")
        if record.user_id != (user_id or "anonymous").strip()[:120]:
            raise PermissionError("审批用户不匹配")
        if record.draft_hash != draft_fingerprint(draft):
            raise PermissionError("草稿已在审批后修改，请重新提交审批")
        if record.status != "approved":
            raise PermissionError(f"审批状态不是 approved：{record.status}")
        return record

    def mark_consumed(self, request_id: str, published_url: str) -> PublishApproval:
        with self._connect() as db:
            db.execute(
                "UPDATE publish_approvals SET status = 'consumed', published_url = ?, "
                "resolved_at = ? WHERE request_id = ? AND status = 'approved'",
                (published_url[:1000], time.time(), request_id),
            )
            db.commit()
        record = self.get(request_id)
        if record is None:  # pragma: no cover
            raise RuntimeError("审批消费状态保存失败")
        return record
