"""PersonaX 对话助手离线回归评测。

不调用真实 LLM，不依赖 Ollama；用于验证路由、引用编号、无证据声明、
步数上限和回答完整性。语义事实支持度需要另行使用人工集或 Judge 评测。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from core.assistant import AssistantOrchestrator
from core.rag import Chunk, RAGPipeline, VectorStore
from core.types import AssistantRequest


ROOT = Path(__file__).resolve().parent.parent
EVAL_SET = ROOT / "eval" / "assistant_eval_set.json"


def _offline_pipeline() -> RAGPipeline:
    store = VectorStore()
    documents = [
        ("rag", "BM25 负责关键词召回，dense kNN 负责语义召回，RRF 融合两路排名。"),
        ("gate", "相关性门控使用 min_score 过滤弱相关片段，减少无关资料进入提示词。"),
        ("trace", "Agent Trace 记录 worker、动作、状态、耗时与降级原因，便于定位故障。"),
        ("checkpoint", "Checkpoint 保存会话状态，使流程能够恢复并保留多轮上下文。"),
        ("tool", "MCP-ready 工具契约声明名称、描述、版本、敏感级别和输入输出 JSON Schema。"),
    ]
    for doc_id, text in documents:
        store.add(Chunk(
            id=doc_id,
            text=text,
            summary=text[:24],
            metadata={"topic": text[:24], "source": f"eval/{doc_id}.md", "keywords": text.split("，")},
        ))
    return RAGPipeline(store)


def _fake_complete(prompt: str, **kwargs) -> str:
    if "<untrusted_knowledge_json>" in prompt:
        return "根据本地资料，可以从职责、状态和可观测性三个方面理解 [1]。"
    return "这是一个不需要本地知识库的直接回答。"


def run() -> dict:
    cases = json.loads(EVAL_SET.read_text(encoding="utf-8"))
    pipeline = _offline_pipeline()
    rows = []
    for case in cases:
        service = AssistantOrchestrator(
            {
                "rag": {
                    "min_score": float(case.get("min_score", 0.10)),
                    "assistant_top_k": 3,
                }
            },
            rag_pipeline=pipeline,
            complete_fn=_fake_complete,
        )
        response = service.reply(AssistantRequest(
            question=case["question"],
            use_knowledge=case["use_knowledge"],
            thread_id=f"eval-{case['id']}",
            max_steps=4,
        ))
        route_ok = response.route == case["expected_route"]
        citation_numbers = [
            int(value) for value in re.findall(r"\[(\d+)\]", response.answer)
        ]
        citation_ok = (not case["citation_required"]) or (
            bool(response.sources) and bool(citation_numbers)
        )
        citation_valid = all(
            1 <= value <= len(response.sources) for value in citation_numbers
        )
        expects_no_evidence = bool(case.get("expect_no_evidence", False))
        no_evidence_ok = (not expects_no_evidence) or (
            not response.sources
            and "本地知识库未检索到可核验资料" in response.answer
        )
        bounded = len(response.trace) <= 4
        answered = bool(response.answer.strip())
        rows.append({
            "id": case["id"],
            "route_ok": route_ok,
            "citation_ok": citation_ok,
            "citation_valid": citation_valid,
            "no_evidence_ok": no_evidence_ok,
            "bounded": bounded,
            "answered": answered,
            "route": response.route,
            "sources": len(response.sources),
            "citations": citation_numbers,
            "steps": len(response.trace),
            "degraded": response.degraded,
        })
    total = len(rows) or 1
    no_evidence_rows = [
        row for case, row in zip(cases, rows)
        if case.get("expect_no_evidence", False)
    ]
    summary = {
        "schema_version": "2.3",
        "cases": len(rows),
        "route_accuracy": sum(row["route_ok"] for row in rows) / total,
        "citation_coverage": sum(row["citation_ok"] for row in rows) / total,
        "citation_validity": sum(row["citation_valid"] for row in rows) / total,
        "no_evidence_disclosure_rate": (
            sum(row["no_evidence_ok"] for row in no_evidence_rows)
            / len(no_evidence_rows)
        ) if no_evidence_rows else 1.0,
        "bounded_step_rate": sum(row["bounded"] for row in rows) / total,
        "answer_rate": sum(row["answered"] for row in rows) / total,
        "passed": all(
            row[metric] for row in rows
            for metric in (
                "route_ok",
                "citation_ok",
                "citation_valid",
                "no_evidence_ok",
                "bounded",
                "answered",
            )
        ),
        "details": rows,
    }
    return summary


if __name__ == "__main__":
    report = run()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)
