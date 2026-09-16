"""RAG 离线评测：Recall@K 与 MRR。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.envfile import load_env_file
from core.rag import build_rag_from_dir


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def evaluate(eval_path: str = "eval/rag_eval_set.json", knowledge_dir: str = "knowledge",
             top_k: int = 3) -> dict:
    load_env_file(PROJECT_ROOT / ".env")
    cases = json.loads(Path(eval_path).read_text(encoding="utf-8"))
    pipeline = build_rag_from_dir(knowledge_dir)
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    details = []
    for case in cases:
        hits = pipeline.retrieve_hits(case["query"], top_k=top_k)
        sources = [str(hit.chunk.metadata.get("source", "")) for hit in hits]
        expected = set(case["expected_sources"])
        rank = next((idx for idx, source in enumerate(sources, 1) if source in expected), None)
        recalls.append(1.0 if rank else 0.0)
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)
        details.append({
            "query": case["query"], "expected": sorted(expected),
            "retrieved": sources, "rank": rank,
        })
    count = len(cases) or 1
    return {
        "cases": len(cases), "top_k": top_k,
        "recall_at_k": round(sum(recalls) / count, 4),
        "mrr": round(sum(reciprocal_ranks) / count, 4),
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
    args = parser.parse_args()
    print(json.dumps(evaluate(args.eval, args.knowledge, args.top_k), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
