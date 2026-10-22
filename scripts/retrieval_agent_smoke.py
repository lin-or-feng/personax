"""Opt-in local Ollama agent smoke with disposable, non-private teaching notes.

Never pulls models, calls a cloud endpoint, reads the user's catalog, or publishes.
This is engineering integration evidence, not independent answer-quality evaluation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--answer-mode", choices=["extractive", "llm"], default="extractive")
    parser.add_argument("--report-json", type=Path)
    parser.add_argument("--probe-transport", action="store_true", help="time client initialization and one minimal local model call")
    args = parser.parse_args()
    endpoint = urlsplit(args.base_url)
    if (endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
            or endpoint.port != 11434 or endpoint.username or endpoint.password
            or endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment):
        parser.error("only the local loopback HTTP Ollama endpoint on port 11434 is allowed")
    base_url = args.base_url.rstrip("/")
    with urllib.request.urlopen(base_url + "/api/tags", timeout=5) as response:
        available = {row["name"] for row in json.load(response)["models"]}
    if args.model not in available:
        parser.error("selected model is not installed; this script never downloads models")
    os.environ.update(LLM_BACKEND="ollama", LLM_MODEL=args.model,
        OLLAMA_BASE_URL=base_url + "/v1", DEEPSEEK_API_KEY="", OPENAI_API_KEY="",
        DEEPSEEK_TIMEOUT="40", RAG_EMBEDDING_BACKEND="hashing",
        RAG_QUERY_ENHANCER="rule", RAG_ENABLE_HYDE="0", RAG_RERANKER="none")

    # Own only the disposable directory; cleanup never touches an existing user DB.
    temp_parent = ROOT.parent / "tmp"
    temp_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="personax-agent-smoke-", dir=temp_parent) as folder:
        root = Path(folder)
        os.environ["PERSONAX_KNOWLEDGE_DB"] = str(root / "catalog.db")
        from core.assistant import AssistantOrchestrator
        from core.knowledge import KnowledgeBaseService
        from core.llm import acomplete, configure
        from core.retrieval_agent import DECISION_SYSTEM, parse_decision
        from core.types import AssistantRequest, KnowledgeImportRequest

        notes = {
            "concurrency.md": "# Semaphore 并发限制\nasyncio.Semaphore 控制同时执行的任务数，也就是在途请求数量；不是每分钟请求次数限制。",
            "rate.md": "# RPM 滑动窗口限流\nRPM 是每分钟请求次数。滑动窗口记录最近 60 秒请求时间，达到额度后等待；用于控制请求频率而非在途任务数。",
        }
        kb = KnowledgeBaseService(root / "catalog.db", root)
        for name, content in notes.items():
            # Only writes this script's own temporary teaching fixtures.
            (root / name).write_text(content, encoding="utf-8")
            kb.ingest(KnowledgeImportRequest(kb_id="smoke", source_path=name))
        configure(backend="ollama", model=args.model, temperature=0.0)
        if args.probe_transport:
            from core.llm import _get_async_client, close_loop_client
            async def probe():
                started = time.perf_counter()
                _get_async_client()
                print(json.dumps({"client_init_seconds": round(time.perf_counter() - started, 3)}, ensure_ascii=False), flush=True)
                started = time.perf_counter()
                try:
                    raw = await asyncio.wait_for(acomplete(
                        '只输出 JSON：{"action":"finish"}', temperature=0.0,
                        max_tokens=30, max_retries=1), timeout=15)
                    print(json.dumps({"request_seconds": round(time.perf_counter() - started, 3),
                        "output_chars": len(raw)}, ensure_ascii=False), flush=True)
                except TimeoutError:
                    print(json.dumps({"probe_timeout_seconds": 15}), flush=True)
                await close_loop_client()
            asyncio.run(probe())
            return 0
        calls = []
        async def observed(prompt, **kwargs):
            started = time.perf_counter()
            call = {"action": "unfinished", "status": "started"}
            calls.append(call)
            try:
                raw = await acomplete(prompt, **kwargs)
                if kwargs.get("system") == DECISION_SYSTEM:
                    try:
                        call["action"] = parse_decision(raw).action
                    except ValueError:
                        call["action"] = "invalid_decision"
                else:
                    call["action"] = "answer_generation"
                call["status"] = "completed"
                return raw
            except BaseException as exc:
                call["status"] = type(exc).__name__
                raise
            finally:
                call["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        config = {"knowledge_catalog": {"db_path": str(root / "catalog.db"),
            "source_root": str(root), "allowed_kb_ids": ["smoke"],
            "embedding_backend": "hashing", "min_score": 0.1},
            "assistant": {"tool_runtime": "langchain", "agent": {"decision_timeout_seconds": 30}}}
        service = AssistantOrchestrator(config, acomplete_fn=observed)
        results = []
        for name, question, task in [
            ("compare", "请比较 Semaphore 并发限制与 RPM 滑动窗口限流，分别检索资料并给出来源。", "compare"),
            ("clarify", "哪个更好？", "qa"),
        ]:
            started, before = time.perf_counter(), len(calls)
            response = service.reply(AssistantRequest(question=question, task=task,
                execution_mode="agent", knowledge_source="catalog", kb_id="smoke",
                answer_mode=args.answer_mode,
                max_steps=8, max_estimated_tokens=16_000, deadline_seconds=90))
            results.append({"case": name, "stop_reason": response.stop_reason,
                "answer_mode": response.answer_mode, "degraded": response.degraded,
                "sources": len(response.sources), "model_calls": len(calls) - before,
                "budget_steps": response.budget_steps, "estimated_tokens": response.estimated_tokens,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "actions": [step.action for step in response.trace]})
        report = {"measured_at": datetime.now(timezone.utc).isoformat(),
            "scope": "local_real_model_smoke_with_temporary_teaching_notes_not_quality_benchmark",
            "generation_model": args.model, "embedding_backend": "hashing",
            "requested_answer_mode": args.answer_mode,
            "cloud_called": False, "models_downloaded": False, "publishing_performed": False,
            "user_catalog_read": False, "calls": calls, "results": results,
            "checks": {"comparison_two_sources": results[0]["sources"] >= 2,
                "comparison_completed": results[0]["stop_reason"] == "evidence_ready",
                "ambiguity_clarified_without_search": results[1]["stop_reason"] == "needs_clarification"
                    and results[1]["sources"] == 0,
                "real_model_called": len(calls) >= 3}}
        report["passed"] = all(report["checks"].values())
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        if args.report_json:
            args.report_json.write_text(encoded + "\n", encoding="utf-8")
        print(encoded)
        return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
