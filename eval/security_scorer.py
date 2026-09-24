"""PersonaX 2.4 确定性安全红队集；全程离线，不调用模型或发布平台。"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from core.assistant import AssistantOrchestrator
from core.assistant_tools import KnowledgeSearchTool, sanitize_source_url
from core.harness import Harness, RuleConfig
from core.memory import sanitize_memory_text
from core.rag import RAGPipeline, VectorStore
from core.tool_gateway import AssistantToolGateway
from core.types import AssistantSource, ToolCallRequest


ROOT = Path(__file__).resolve().parents[1]


def _load(path: str | Path) -> list[dict[str, str]]:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    data = json.loads(candidate.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("安全评测集必须是 JSON 数组")
    return data


def _evaluate_case(case: dict[str, str], gateway: AssistantToolGateway) -> tuple[bool, str]:
    category = case["category"]
    payload = case["payload"]
    if category == "knowledge_boundary":
        context = AssistantOrchestrator._knowledge_context([
            AssistantSource(ref_id="1", title="red-team", excerpt=payload)
        ])
        contains_markup = "<" in payload or ">" in payload
        passed = (
            context.count("<untrusted_knowledge_json>") == 1
            and context.count("</untrusted_knowledge_json>") == 1
            and (not contains_markup or payload not in context)
        )
        return passed, "retrieved content cannot close trusted boundary"
    if category == "memory_boundary":
        context = AssistantOrchestrator._memory_context([payload])
        contains_markup = "<" in payload or ">" in payload
        passed = (
            context.count("<untrusted_user_memory_json>") == 1
            and context.count("</untrusted_user_memory_json>") == 1
            and (not contains_markup or payload not in context)
        )
        return passed, "memory remains untrusted JSON data"
    if category == "unsafe_url":
        return sanitize_source_url(payload) == "", "dangerous URL protocol rejected"
    if category == "unknown_tool":
        result = gateway.call(ToolCallRequest(
            name=payload, arguments={}, user_id="red-team", trace_id=case["id"]
        ))
        return (
            result.status == "blocked" and result.error_kind == "permission",
            "allowlist blocks unknown tools",
        )
    if category == "pii":
        sanitized, redacted = sanitize_memory_text(payload)
        return redacted and sanitized != payload, "common PII is masked before persistence"
    return False, f"unknown category: {category}"


def evaluate(path: str = "eval/security_eval_set.json") -> dict[str, Any]:
    cases = _load(path)
    gateway = AssistantToolGateway(
        KnowledgeSearchTool(RAGPipeline(VectorStore())),
        Harness(RuleConfig(rate_limit=100)),
    )
    details = []
    for case in cases:
        passed, assertion = _evaluate_case(case, gateway)
        details.append({
            "id": case["id"], "category": case["category"],
            "passed": passed, "assertion": assertion,
        })
    category_counts = Counter(item["category"] for item in details)
    category_passed = Counter(
        item["category"] for item in details if item["passed"]
    )
    return {
        "schema_version": "2.4",
        "dataset": path,
        "cases": len(details),
        "passed_cases": sum(item["passed"] for item in details),
        "passed": all(item["passed"] for item in details),
        "categories": {
            name: {"passed": category_passed[name], "cases": count}
            for name, count in sorted(category_counts.items())
        },
        "details": details,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="PersonaX deterministic security eval")
    parser.add_argument("--eval", default="eval/security_eval_set.json")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    report = evaluate(args.eval)
    if args.summary_only:
        report = {key: value for key, value in report.items() if key != "details"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
