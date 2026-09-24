"""PersonaX 2.7 用户反馈闭环与排序指标回归。"""
from __future__ import annotations

import pytest

from core.feedback import AssistantFeedbackStore
from core.types import FeedbackSourceRef
from eval.rag_scorer import _ndcg


def test_feedback_is_idempotent_per_trace_and_exports_only_opted_in_failures(tmp_path):
    store = AssistantFeedbackStore(tmp_path / "feedback.sqlite3")
    fake_api_key = "sk-" + "abcdefghijklmnop"
    helpful = store.upsert(
        trace_id="trace-feedback-001",
        thread_id="thread-1",
        rating="helpful",
        answer="初始回答",
        route="direct",
    )
    assert helpful.rating == "helpful" and helpful.content_included is False
    assert store.export_eval_candidates() == []

    updated = store.upsert(
        trace_id="trace-feedback-001",
        thread_id="thread-1",
        rating="not_helpful",
        reason="missing_evidence",
        note=f"请核对 13800138000，临时 key 是 {fake_api_key}",
        question="我的邮箱 test.user@example.com，证据在哪里？",
        answer="回答缺少可以核对的来源。",
        source_refs=[FeedbackSourceRef(
            ref_id="1", title="项目说明", source="README.md",
        )],
        route="knowledge",
        source_count=1,
        degraded=True,
        include_content=True,
    )

    rows = store.list_recent()
    candidates = store.export_eval_candidates()
    assert len(rows) == 1 and rows[0].feedback_id == helpful.feedback_id
    assert updated.redacted is True
    assert "13800138000" not in updated.note
    assert fake_api_key not in updated.note
    assert "test.user" not in updated.question
    assert len(candidates) == 1
    assert candidates[0]["review_status"] == "needs_human_label"
    assert candidates[0]["source_refs"][0]["source"] == "README.md"


def test_feedback_summary_does_not_require_stored_conversation_content(tmp_path):
    store = AssistantFeedbackStore(tmp_path / "feedback.sqlite3")
    store.upsert(
        trace_id="trace-feedback-101", thread_id="t", rating="helpful", answer="a",
    )
    store.upsert(
        trace_id="trace-feedback-102", thread_id="t", rating="not_helpful",
        reason="irrelevant", answer="b", include_content=False,
    )

    summary = store.summary()
    assert summary.total == 2
    assert summary.helpful_rate == 0.5
    assert summary.reasons == {"irrelevant": 1}
    assert summary.eval_candidates == 0
    assert store.delete_all() == 2
    assert store.summary().total == 0


def test_feedback_rejects_invalid_contract_values(tmp_path):
    store = AssistantFeedbackStore(tmp_path / "feedback.sqlite3")
    with pytest.raises(ValueError, match="trace_id"):
        store.upsert(trace_id="short", thread_id="t", rating="helpful", answer="a")
    with pytest.raises(ValueError, match="rating"):
        store.upsert(
            trace_id="trace-feedback-201", thread_id="t", rating="maybe", answer="a",
        )
    with pytest.raises(ValueError, match="原因"):
        store.upsert(
            trace_id="trace-feedback-202", thread_id="t", rating="not_helpful",
            reason="free-form-invalid", answer="a",
        )


def test_ndcg_rewards_all_relevant_sources_and_ranking_order():
    assert _ndcg(["a.md", "b.md"], {"a.md", "b.md"}, 2) == 1.0
    delayed = _ndcg(["x.md", "a.md", "b.md"], {"a.md", "b.md"}, 3)
    assert delayed == pytest.approx(0.6934)
    assert _ndcg(["x.md"], set(), 5) == 0.0
