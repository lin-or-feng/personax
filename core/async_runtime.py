"""PersonaX 异步并发基元：有界 gather + 可观测批处理结果。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, Sequence, TypeVar


InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


@dataclass(frozen=True)
class AsyncBatchResult(Generic[OutputT]):
    """有界异步批处理结果；结果顺序与输入顺序一致。"""

    results: list[OutputT | Exception]
    elapsed_ms: float
    max_concurrency: int
    peak_concurrency: int
    completed: int
    failed: int

    @property
    def throughput_per_second(self) -> float:
        if self.elapsed_ms <= 0:
            return 0.0
        return round(self.completed / (self.elapsed_ms / 1000.0), 3)


async def gather_bounded(
    items: Sequence[InputT],
    worker: Callable[[InputT], Awaitable[OutputT]],
    *,
    max_concurrency: int = 2,
    return_exceptions: bool = False,
) -> AsyncBatchResult[OutputT]:
    """用 ``asyncio.Semaphore`` 限制同时在途任务数。

    适用于等待 LLM、搜索或远程 embedding 的 I/O 型任务。CPU 密集推理应使用
    进程池、独立模型服务或模型自身的批处理能力，而不是提高这里的并发数。
    """

    limit = max(1, min(int(max_concurrency), 32))
    semaphore = asyncio.Semaphore(limit)
    state_lock = asyncio.Lock()
    active = 0
    peak = 0
    started = time.perf_counter()

    async def run_one(item: InputT) -> OutputT:
        nonlocal active, peak
        async with semaphore:
            async with state_lock:
                active += 1
                peak = max(peak, active)
            try:
                return await worker(item)
            finally:
                async with state_lock:
                    active -= 1

    raw_results = await asyncio.gather(
        *(run_one(item) for item in items),
        return_exceptions=return_exceptions,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    results = list(raw_results)
    failed = sum(isinstance(result, Exception) for result in results)
    return AsyncBatchResult(
        results=results,
        elapsed_ms=elapsed_ms,
        max_concurrency=limit,
        peak_concurrency=peak,
        completed=len(results) - failed,
        failed=failed,
    )
