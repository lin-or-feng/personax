"""Opt-in smoke using installed local models, never downloads or cloud providers.

This is a small engineering integration probe, NOT independent answer accuracy.
Start your own loopback Ollama server before running this script.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--embedding-model", default="bge-m3:latest")
    parser.add_argument("--kb-id", default="python-official")
    parser.add_argument("--vector-store", choices=["memory", "qdrant"], default="qdrant")
    parser.add_argument("--report-json", default="")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    endpoint = urlsplit(base_url)
    if (endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
            or endpoint.username or endpoint.password or endpoint.path or endpoint.query or endpoint.fragment):
        parser.error("only a bare loopback HTTP Ollama endpoint is allowed")
    with urllib.request.urlopen(base_url + "/api/tags", timeout=10) as response:
        available = {row["name"] for row in json.load(response)["models"]}
    if args.model not in available or args.embedding_model not in available:
        parser.error("both models must already exist; this script never pulls models")

    # .env uses setdefault; force the local backend before importing project code.
    os.environ.update(LLM_BACKEND="ollama", LLM_MODEL=args.model,
        OLLAMA_BASE_URL=base_url + "/v1", DEEPSEEK_API_KEY="", OPENAI_API_KEY="",
        DEEPSEEK_TIMEOUT="60")
    from core.assistant import AssistantOrchestrator
    from core.knowledge_store import KnowledgeStore
    from core.llm import complete, configure
    from core.types import AssistantRequest

    configure(backend="ollama", model=args.model, max_tokens=160)
    calls: list[dict] = []

    def observed_complete(*positional, **kwargs):
        kwargs.update(max_tokens=160, max_retries=1)
        started = time.perf_counter()
        text = complete(*positional, **kwargs)
        calls.append({"duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "raw_answer": text})
        return text

    config = {"knowledge_catalog": {"allowed_kb_ids": [args.kb_id],
        "embedding_backend": "ollama", "embedding_model": args.embedding_model,
        "vector_store": args.vector_store, "min_score": 0.5, "top_k": 2},
        "assistant": {"tool_runtime": "native"}}
    assistant = AssistantOrchestrator(config, complete_fn=observed_complete)
    results = []
    for query, answer_mode in [("Semaphore acquire release 信号量", "extractive"),
                               ("Semaphore acquire release 信号量，请用一句话说明并引用来源。", "llm"),
                               ("UnobtainiumHyperdriveNebula", "llm")]:
        started = time.perf_counter()
        before = len(calls)
        response = assistant.reply(AssistantRequest(question=query, knowledge_source="catalog",
            kb_id=args.kb_id, answer_mode=answer_mode))
        results.append({"question": query, "requested_answer_mode": answer_mode,
            "answer_mode": response.answer_mode, "model_calls": len(calls) - before,
            "index_version": response.index_version, "answer": response.answer,
            "sources": [source.model_dump() for source in response.sources],
            "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
    documents = KnowledgeStore(assistant.catalog.db_path).documents(args.kb_id)
    report = {"measured_at": datetime.now(timezone.utc).isoformat(),
        "scope": "local_real_model_engineering_smoke_not_quality_benchmark",
        "generation_model": args.model, "embedding_model": args.embedding_model,
        "vector_store": args.vector_store, "min_score": 0.5,
        "documents": len(documents), "chunks": sum(len(doc.chunks) for doc in documents),
        "cloud_provider_called": False, "models_downloaded": False, "publishing_performed": False,
        "independent_human_evaluation": False, "calls": calls, "results": results,
        "checks": {
            "positive_has_evidence": bool(results[0]["sources"]),
            "generation_model_called": results[1]["model_calls"] == 1,
            "outside_refused_without_model": results[2]["answer_mode"] == "refused" and results[2]["model_calls"] == 0,
            "same_index_version": len({result["index_version"] for result in results}) == 1,
        }}
    if args.report_json:
        path = Path(args.report_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"calls", "results"}},
        ensure_ascii=False, indent=2))
    if not all(report["checks"].values()):
        raise SystemExit("local model smoke has a failed check; inspect the saved report")


if __name__ == "__main__":
    main()
