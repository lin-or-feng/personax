"""比较 PersonaX 串行 LLM 调用与 Semaphore 限流异步调用。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.async_runtime import gather_bounded
from core.llm import acomplete_batch, complete, configure


def _summary(
    *,
    mode: str,
    tasks: int,
    concurrency: int,
    serial_ms: float,
    async_ms: float,
    peak: int,
    serial_failed: int,
    async_failed: int,
) -> dict[str, Any]:
    speedup = (serial_ms / async_ms) if async_ms > 0 else 0.0
    return {
        "mode": mode,
        "tasks": tasks,
        "max_concurrency": concurrency,
        "peak_concurrency": peak,
        "serial": {
            "elapsed_ms": round(serial_ms, 1),
            "throughput_per_second": round(
                (tasks - serial_failed) / (serial_ms / 1000.0), 3
            ) if serial_ms > 0 else 0.0,
            "failed": serial_failed,
        },
        "async": {
            "elapsed_ms": round(async_ms, 1),
            "throughput_per_second": round(
                (tasks - async_failed) / (async_ms / 1000.0), 3
            ) if async_ms > 0 else 0.0,
            "failed": async_failed,
        },
        "speedup": round(speedup, 3),
        "saved_ms": round(serial_ms - async_ms, 1),
    }


async def _simulated(tasks: int, concurrency: int, delay_ms: int) -> dict[str, Any]:
    def blocking_call(_: int) -> str:
        time.sleep(delay_ms / 1000.0)
        return "ok"

    started = time.perf_counter()
    serial_results = [blocking_call(index) for index in range(tasks)]
    serial_ms = (time.perf_counter() - started) * 1000.0

    async def async_call(_: int) -> str:
        await asyncio.sleep(delay_ms / 1000.0)
        return "ok"

    async_result = await gather_bounded(
        list(range(tasks)), async_call, max_concurrency=concurrency,
        return_exceptions=True,
    )
    return _summary(
        mode="simulated_io",
        tasks=tasks,
        concurrency=concurrency,
        serial_ms=serial_ms,
        async_ms=async_result.elapsed_ms,
        peak=async_result.peak_concurrency,
        serial_failed=sum(isinstance(item, Exception) for item in serial_results),
        async_failed=async_result.failed,
    )


async def _ollama(
    tasks: int,
    concurrency: int,
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    configure(
        backend="ollama", model=model, temperature=0.0, max_tokens=max_tokens,
    )
    prompts = [
        "用约80个中文字说明 BM25 与向量检索的区别，"
        f"最后标注测试编号 {index + 1}。"
        for index in range(tasks)
    ]

    # 单次预热不进入对比，避免把首次模型加载只算进串行基线。
    complete("只回复：预热", temperature=0.0, max_tokens=8, max_retries=1)

    serial_results: list[str | Exception] = []
    started = time.perf_counter()
    for prompt in prompts:
        try:
            serial_results.append(complete(
                prompt, temperature=0.0, max_tokens=max_tokens, max_retries=1,
            ))
        except Exception as exc:  # noqa: BLE001 - benchmark 必须记录失败而非中断
            serial_results.append(exc)
    serial_ms = (time.perf_counter() - started) * 1000.0

    async_result = await acomplete_batch(
        prompts,
        temperature=0.0,
        max_tokens=max_tokens,
        max_retries=1,
        max_concurrency=concurrency,
        return_exceptions=True,
    )
    return _summary(
        mode=f"ollama:{model}",
        tasks=tasks,
        concurrency=concurrency,
        serial_ms=serial_ms,
        async_ms=async_result.elapsed_ms,
        peak=async_result.peak_concurrency,
        serial_failed=sum(isinstance(item, Exception) for item in serial_results),
        async_failed=async_result.failed,
    )


def _markdown(report: dict[str, Any]) -> str:
    serial = report["serial"]
    async_row = report["async"]
    return "\n".join([
        "# PersonaX async LLM benchmark",
        "",
        f"- Mode: `{report['mode']}`",
        f"- Tasks: {report['tasks']}",
        f"- Semaphore limit: {report['max_concurrency']}",
        f"- Observed peak concurrency: {report['peak_concurrency']}",
        "",
        "| Execution | Elapsed (ms) | Throughput (req/s) | Failed |",
        "|---|---:|---:|---:|",
        f"| Serial baseline | {serial['elapsed_ms']} | {serial['throughput_per_second']} | {serial['failed']} |",
        f"| Async bounded | {async_row['elapsed_ms']} | {async_row['throughput_per_second']} | {async_row['failed']} |",
        "",
        f"**Speedup: {report['speedup']}x; saved {report['saved_ms']} ms.**",
        "",
        "> A local Ollama server may serialize GPU work. Async improves waiting overlap and throughput only when the backend accepts concurrent work; it does not reduce one request's model inference time.",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("simulated", "ollama"), default="simulated")
    parser.add_argument("--tasks", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--delay-ms", type=int, default=250)
    parser.add_argument("--model", default="qwen2.5:3b")
    parser.add_argument("--max-tokens", type=int, default=24)
    parser.add_argument("--json-out")
    parser.add_argument("--markdown-out")
    args = parser.parse_args()
    tasks = max(1, min(args.tasks, 100))
    concurrency = max(1, min(args.concurrency, 16))
    if args.mode == "ollama":
        report = asyncio.run(_ollama(tasks, concurrency, args.model, args.max_tokens))
    else:
        report = asyncio.run(_simulated(tasks, concurrency, max(1, args.delay_ms)))

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    if args.json_out:
        Path(args.json_out).write_text(payload + "\n", encoding="utf-8")
    if args.markdown_out:
        Path(args.markdown_out).write_text(_markdown(report) + "\n", encoding="utf-8")
    return 0 if report["serial"]["failed"] == 0 and report["async"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
