"""PersonaX 2.0 对话助手离线回归评测。

不调用真实 LLM，不依赖 Ollama；用于验证路由、引用、步数上限和回答完整性。
"""
from __future__ import annotations

import json
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
    if "[1]" in prompt:
        return "根据本地资料，可以从职责、状态和可观测性三个方面理解 [1]。"
    return "这是一个不需要本地知识库的直接回答。"


def run() -> dict:
    cases = json.loads(EVAL_SET.read_text(encoding="utf-8"))
    service = AssistantOrchestrator(
        {"rag": {"min_score": 0.0, "assistant_top_k": 3}},
        rag_pipeline=_offline_pipeline(),
        complete_fn=_fake_complete,
    )
    rows = []
    for case in cases:
        response = service.reply(AssistantRequest(
            question=case["question"],
            use_knowledge=case["use_knowledge"],
            thread_id=f"eval-{case['id']}",
            max_steps=4,
        ))
        route_ok = response.route == case["expected_route"]
        citation_ok = (not case["citation_required"]) or (
            bool(response.sources) and "[1]" in response.answer)
        bounded = len(response.trace) <= 4
        answered = bool(response.answer.strip())
        rows.append({
            "id": case["id"],
            "route_ok": route_ok,
            "citation_ok": citation_ok,
            "bounded": bounded,
            "answered": answered,
            "route": response.route,
            "sources": len(response.sources),
            "steps": len(response.trace),
        })
    total = len(rows) or 1
    summary = {
        "cases": len(rows),
        "route_accuracy": sum(row["route_ok"] for row in rows) / total,
        "citation_coverage": sum(row["citation_ok"] for row in rows) / total,
        "bounded_step_rate": sum(row["bounded"] for row in rows) / total,
        "answer_rate": sum(row["answered"] for row in rows) / total,
        "passed": all(
            row[metric] for row in rows
            for metric in ("route_ok", "citation_ok", "bounded", "answered")
        ),
        "details": rows,
    }
    return summary


if __name__ == "__main__":
    report = run()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)
