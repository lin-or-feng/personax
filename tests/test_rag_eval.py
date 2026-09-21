"""PersonaX 2.3 RAG 质量门禁与消融报告。"""
from __future__ import annotations

import json

from eval.rag_scorer import (
    compact_report,
    compare_modes,
    evaluate,
    render_markdown,
    write_report,
)


def test_rag_eval_is_reproducible_and_passes_gate(monkeypatch):
    monkeypatch.setenv("RAG_EMBEDDING_BACKEND", "ollama")
    report = evaluate(embedding_backend="hashing")

    assert report["embedding_backend"] == "hashing"
    assert report["cases"] == 120
    assert report["positive_cases"] == 108
    assert report["negative_cases"] == 12
    assert report["passed"] is True
    assert report["recall_at_k"] >= report["thresholds"]["min_recall"]
    assert report["gated_recall_at_k"] >= report["thresholds"]["min_recall"]
    assert report["mrr"] >= report["thresholds"]["min_mrr"]
    assert report["no_hit_accuracy"] >= report["thresholds"]["min_no_hit"]
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


def test_ablation_and_markdown_report_use_same_small_dataset(tmp_path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "rag.md").write_text(
        "---\ntopic: 混合检索\nkeywords: [BM25, RRF]\n---\n"
        "BM25 负责关键词召回，RRF 融合多路排名。",
        encoding="utf-8",
    )
    (knowledge / "campus.md").write_text(
        "---\ntopic: 校园生活\nkeywords: [社团, 新生]\n---\n"
        "新生可以通过社团活动认识同学。",
        encoding="utf-8",
    )
    eval_set = tmp_path / "cases.json"
    eval_set.write_text(json.dumps([
        {
            "id": "rag",
            "query": "BM25 和 RRF 如何配合",
            "expected_sources": ["rag.md"],
            "category": "agent_rag",
        },
        {
            "id": "campus",
            "query": "新生参加社团",
            "expected_sources": ["campus.md"],
            "category": "campus",
        },
        {
            "id": "negative",
            "query": "红烧牛肉放什么香料",
            "expected_sources": [],
            "answerable": False,
            "category": "out_of_scope",
        },
    ], ensure_ascii=False), encoding="utf-8")

    report = compare_modes(
        eval_path=str(eval_set),
        knowledge_dir=str(knowledge),
        top_k=2,
        embedding_backend="hashing",
        min_score=0.2,
        min_recall=0.0,
        min_mrr=0.0,
        min_no_hit=0.0,
    )
    markdown = render_markdown(report)
    report_path = tmp_path / "reports" / "ablation.md"
    write_report(report, markdown_path=str(report_path))

    assert set(report["reports"]) == {"bm25", "dense", "hybrid"}
    assert report["reports"]["hybrid"]["cases"] == 3
    assert "details" not in compact_report(report)["reports"]["hybrid"]
    assert "RAG 消融报告" in markdown
    assert report_path.read_text(encoding="utf-8") == markdown + "\n"
