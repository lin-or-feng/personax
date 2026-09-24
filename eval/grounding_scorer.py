"""PersonaX 2.6 答案级事实支持度离线评测。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from core.grounding import claim_support
from core.types import AssistantSource


ROOT = Path(__file__).resolve().parents[1]


def evaluate(
    path: str = "eval/grounding_eval_set.json",
    *,
    min_score: float = 0.42,
    min_accuracy: float = 0.90,
    min_unsupported_recall: float = 0.85,
) -> dict[str, Any]:
    eval_path = Path(path)
    if not eval_path.is_absolute():
        eval_path = ROOT / eval_path
    cases = json.loads(eval_path.read_text(encoding="utf-8"))
    rows = []
    for case in cases:
        result = claim_support(
            case["answer"],
            [AssistantSource.model_validate(item) for item in case["sources"]],
            min_score=min_score,
        )
        expected = bool(case["expected_supported"])
        rows.append({
            "id": case["id"],
            "expected_supported": expected,
            "predicted_supported": result.supported,
            "score": result.score,
            "reason": result.reason,
            "correct": result.supported == expected,
        })
    total = len(rows) or 1
    unsupported = [item for item in rows if not item["expected_supported"]]
    accuracy = sum(item["correct"] for item in rows) / total
    unsupported_recall = (
        sum(not item["predicted_supported"] for item in unsupported) / len(unsupported)
        if unsupported else 1.0
    )
    return {
        "schema_version": "2.6",
        "dataset": str(path),
        "cases": len(rows),
        "accuracy": round(accuracy, 4),
        "unsupported_recall": round(unsupported_recall, 4),
        "min_score": min_score,
        "thresholds": {
            "min_accuracy": min_accuracy,
            "min_unsupported_recall": min_unsupported_recall,
        },
        "passed": accuracy >= min_accuracy and unsupported_recall >= min_unsupported_recall,
        "details": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="PersonaX grounding evaluation")
    parser.add_argument("--eval", default="eval/grounding_eval_set.json")
    parser.add_argument("--min-score", type=float, default=0.42)
    parser.add_argument("--min-accuracy", type=float, default=0.90)
    parser.add_argument("--min-unsupported-recall", type=float, default=0.85)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    report = evaluate(
        args.eval,
        min_score=args.min_score,
        min_accuracy=args.min_accuracy,
        min_unsupported_recall=args.min_unsupported_recall,
    )
    if args.summary_only:
        report = {key: value for key, value in report.items() if key != "details"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
