"""Synthetic control-flow acceptance, NOT a real-model quality/ROI benchmark.

Compares one fixed-query retrieval baseline with a scripted decision adapter.
No model APIs, vector downloads, accounts or publishing are used.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from core.agent_runtime import AgentRunBudget
from core.retrieval_agent import ReadOnlyRetrievalAgent
from core.types import (AssistantRequest, AssistantSource, KnowledgeSearchOutput,
    RetrievalAgentConfig, ToolCallResult)


CASES = Path(__file__).with_name("retrieval_agent_cases.json")


async def _case(case: dict) -> dict:
    calls, model_calls = [], []
    def sources(query):
        return [AssistantSource(ref_id=str(i + 1), title=doc_id, source=doc_id + ".md",
            excerpt=f"Synthetic evidence {doc_id}.", document_id=doc_id,
            document_version="fixture-v1", chunk_id=doc_id + "-1")
            for i, doc_id in enumerate(case["results"].get(query, []))]
    async def search(query):
        calls.append(query)
        return ToolCallResult(status="ok", output=KnowledgeSearchOutput(sources=sources(query)))
    async def decide(prompt, **kwargs):
        model_calls.append(True)
        if "raw_decision" in case:
            return case["raw_decision"]
        # A deterministic stand-in, not an assertion about model intelligence.
        context = json.loads(prompt.split("\n", 1)[1].rsplit("\n", 1)[0])
        count = len(context["observations"])
        if count < len(case["queries"]):
            return json.dumps({"action": "search", "query": case["queries"][count]})
        action = {"action": case["terminal"]}
        if action["action"] == "clarify":
            action["clarification"] = "请确认需要比较的方案？"
        return json.dumps(action)

    request = AssistantRequest(question=case.get("question", case["id"]), execution_mode="agent", max_steps=8,
        max_estimated_tokens=16_000, use_knowledge=case.get("use_knowledge", True))
    budget = AgentRunBudget(max_steps=8, max_estimated_tokens=16_000)
    result = await ReadOnlyRetrievalAgent(request, RetrievalAgentConfig(), budget,
        decide=decide, search=search).run()
    passed = (result.stop_reason == case["expected_stop"]
        and len(calls) == case["expected_calls"]
        and len(result.sources) == case["expected_sources"] and budget.steps <= 8)
    return {"id": case["id"], "passed": passed,
        "fixed_query_baseline": {"retrieval_calls": int(request.use_knowledge),
            "sources": len(sources(request.question)) if request.use_knowledge else 0,
            "decision_model_calls": 0},
        "agent": {"retrieval_calls": len(calls), "decision_model_calls": len(model_calls),
            "sources": len(result.sources), "budget_steps": budget.steps,
            "estimated_tokens": budget.estimated_tokens, "stop_reason": result.stop_reason}}


def run() -> dict:
    async def all_cases():
        return [await _case(case) for case in json.loads(CASES.read_text(encoding="utf-8"))]
    rows = asyncio.run(all_cases())
    return {"scope": "synthetic_control_flow_acceptance_not_model_quality",
        "baseline": "fixed_original_query_once_no_generation",
        "limitations": ["scripted model and synthetic retrieval", "not a blind holdout",
            "no real-model accuracy, latency, cost or ROI claim"],
        "cases": len(rows), "passed_cases": sum(row["passed"] for row in rows),
        "passed": all(row["passed"] for row in rows), "details": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run()
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    raise SystemExit(0 if report["passed"] else 1)
