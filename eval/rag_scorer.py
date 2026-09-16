"""RAG 可复现质量门禁：Recall@K、MRR、延迟与降级状态。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from core.envfile import load_env_file
from core.rag import build_rag_from_dir


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return round(ordered[index], 1)


def evaluate(
    eval_path: str = "eval/rag_eval_set.json",
    knowledge_dir: str = "knowledge",
    top_k: int = 3,
    *,
    embedding_backend: str | None = "hashing",
    min_recall: float = 0.80,
    min_mrr: float = 0.80,
) -> dict:
    load_env_file(PROJECT_ROOT / ".env")
    cases = json.loads(Path(eval_path).read_text(encoding="utf-8"))
    pipeline = build_rag_from_dir(
        knowledge_dir,
        embedding_backend=embedding_backend,
        environment_overrides=False,
    )
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    latencies_ms: list[float] = []
    details = []
    for case in cases:
        started = time.perf_counter()
        hits = pipeline.retrieve_hits(case["query"], top_k=top_k)
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        latencies_ms.append(latency_ms)
        sources = [str(hit.chunk.metadata.get("source", "")) for hit in hits]
        expected = set(case["expected_sources"])
        rank = next((idx for idx, source in enumerate(sources, 1) if source in expected), None)
        recalls.append(1.0 if rank else 0.0)
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)
        details.append({
            "query": case["query"], "expected": sorted(expected),
            "retrieved": sources, "rank": rank, "latency_ms": latency_ms,
        })
    count = len(cases) or 1
    recall_at_k = round(sum(recalls) / count, 4)
    mrr = round(sum(reciprocal_ranks) / count, 4)
    degraded = list(pipeline.last_trace.get("degraded_components", []))
    passed = recall_at_k >= min_recall and mrr >= min_mrr and not degraded
    return {
        "cases": len(cases), "top_k": top_k,
        "embedding_backend": pipeline.store.embedder.name,
        "recall_at_k": recall_at_k,
        "mrr": mrr,
        "latency_ms": {
            "p50": _percentile(latencies_ms, 0.50),
            "p95": _percentile(latencies_ms, 0.95),
            "max": round(max(latencies_ms), 1) if latencies_ms else 0.0,
        },
        "thresholds": {"min_recall": min_recall, "min_mrr": min_mrr},
        "passed": passed,
        "trace": pipeline.last_trace,
        "details": details,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="PersonaX RAG retrieval evaluation")
    parser.add_argument("--eval", default="eval/rag_eval_set.json")
    parser.add_argument("--knowledge", default="knowledge")
    parser.add_argument("--top-k", type=int, default=1)
    parser.add_argument(
        "--backend",
        choices=["hashing", "ollama", "sentence-transformers"],
        default="hashing",
        help="hashing 可复现且无需模型；ollama 可实测本地 bge-m3",
    )
    parser.add_argument("--min-recall", type=float, default=0.80)
    parser.add_argument("--min-mrr", type=float, default=0.80)
    args = parser.parse_args()
    report = evaluate(
        args.eval,
        args.knowledge,
        args.top_k,
        embedding_backend=args.backend,
        min_recall=args.min_recall,
        min_mrr=args.min_mrr,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
