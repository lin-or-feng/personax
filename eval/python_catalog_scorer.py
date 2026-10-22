"""Agent-authored source-location regression on real official documents, not human QA accuracy."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

from core.assistant_tools import KnowledgeSearchTool
from core.knowledge_store import KnowledgeStore
from core.rag import HashingEmbedder, build_rag_from_documents
from core.types import KnowledgeSearchInput

ROOT = Path(__file__).resolve().parents[1]


def evaluate(db_path: str | Path, *, min_score: float = 0.15) -> dict:
    cases = json.loads((ROOT / "eval/python_catalog_cases.json").read_text(encoding="utf-8"))
    documents = KnowledgeStore(db_path).documents("python-official")
    if not documents:
        raise ValueError("import_official_pack_first")
    modes = []
    for mode in ("bm25", "dense", "hybrid"):
        started = time.perf_counter()
        pipeline = build_rag_from_documents(documents, embedder=HashingEmbedder(), retrieval_mode=mode)
        build_ms = round((time.perf_counter() - started) * 1000, 2)
        tool = KnowledgeSearchTool(pipeline)
        results = []
        for case in cases:
            started = time.perf_counter()
            output = tool.run(KnowledgeSearchInput(query=case["query"], top_k=3, min_score=min_score))
            elapsed = (time.perf_counter() - started) * 1000
            actual = [source.source for source in output.sources]
            expected = set(case["sources"])
            ranks = [i + 1 for i, source in enumerate(actual) if source in expected]
            raw_sources = list(dict.fromkeys(str((hit.chunk.metadata or {}).get("source", ""))
                for hit in pipeline.retrieve_hits(case["query"], top_k=9)))[:3]
            raw_ranks = [i + 1 for i, source in enumerate(raw_sources) if source in expected]
            results.append({"id": case["id"], "split": case["split"], "expected_sources": sorted(expected),
                "actual_sources": actual, "source_recall": len(expected & set(actual)) / len(expected) if expected else None,
                "raw_candidate_sources": raw_sources,
                "raw_source_recall": len(expected & set(raw_sources)) / len(expected) if expected else None,
                "raw_reciprocal_rank": 1 / min(raw_ranks) if raw_ranks else 0,
                "reciprocal_rank": 1 / min(ranks) if ranks else 0,
                "rejected": not actual, "latency_ms": round(elapsed, 2),
                "provenance_complete": all(s.document_id and s.document_version and s.chunk_id and s.line_start for s in output.sources)})
        slices = {}
        for split in ("development", "holdout"):
            subset = [r for r in results if r["split"] == split]
            positives = [r for r in subset if r["source_recall"] is not None]
            negatives = [r for r in subset if r["source_recall"] is None]
            slices[split] = {"answerable_cases": len(positives), "outside_cases": len(negatives),
                "gated_source_recall_at_3": round(statistics.mean(r["source_recall"] for r in positives), 4),
                "raw_source_recall_at_3": round(statistics.mean(r["raw_source_recall"] for r in positives), 4),
                "gated_source_mrr": round(statistics.mean(r["reciprocal_rank"] for r in positives), 4),
                "outside_rejection": round(statistics.mean(r["rejected"] for r in negatives), 4) if negatives else None}
        modes.append({"mode": mode, "build_ms": build_ms, "retrieval_ms_median": round(statistics.median(r["latency_ms"] for r in results), 2),
            "slices": slices, "results": results})
    return {"scope": "real_official_documents_agent_authored_source_location_regression",
        "independent_human_domain_evaluation": False, "answer_factual_accuracy_measured": False,
        "embedding_backend": "hashing_not_semantic_model", "min_score": min_score,
        "holdout_seen_during_failure_diagnosis": True,
        "split_note": "Provisional slices only; outside cases were inspected during diagnosis. Not an untouched final holdout.",
        "timing_scope": "one_snapshot_build_per_mode_then_warm_queries_not_http_end_to_end",
        "documents": len(documents), "chunks": sum(len(d.chunks) for d in documents), "cases": len(cases),
        "corpus_version": hashlib.sha256("".join(sorted(d.document_version for d in documents)).encode()).hexdigest(),
        "queryset_sha256": hashlib.sha256((ROOT / "eval/python_catalog_cases.json").read_bytes()).hexdigest(),
        "modes": modes}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "logs/knowledge.sqlite3"))
    parser.add_argument("--report-json", default="")
    args = parser.parse_args()
    report = evaluate(args.db)
    if args.report_json:
        path = Path(args.report_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value if key != "modes" else [{k: v for k, v in mode.items() if k != "results"} for mode in value]
        for key, value in report.items()}, indent=2))
