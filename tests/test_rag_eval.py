"""PersonaX 2.2 RAG 质量门禁。"""
from __future__ import annotations

from eval.rag_scorer import evaluate


def test_rag_eval_is_reproducible_and_passes_gate(monkeypatch):
    monkeypatch.setenv("RAG_EMBEDDING_BACKEND", "ollama")
    report = evaluate(top_k=1, embedding_backend="hashing")

    assert report["embedding_backend"] == "hashing"
    assert report["passed"] is True
    assert report["recall_at_k"] >= report["thresholds"]["min_recall"]
    assert report["mrr"] >= report["thresholds"]["min_mrr"]
    assert report["latency_ms"]["p95"] >= 0
    assert report["trace"]["degraded_components"] == []


def test_rag_eval_gate_fails_when_threshold_is_unreachable():
    report = evaluate(
        top_k=1,
        embedding_backend="hashing",
        min_recall=1.01,
        min_mrr=1.01,
    )
    assert report["passed"] is False
