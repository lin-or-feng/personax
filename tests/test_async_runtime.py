"""异步 LLM 并发门与批处理回归。"""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import core.llm as llm
from core.async_runtime import gather_bounded
from core.llm import acomplete_batch, acomplete_stream, complete, configure


def test_gather_bounded_preserves_order_and_limit():
    active = 0
    observed_peak = 0

    async def worker(value: int) -> int:
        nonlocal active, observed_peak
        active += 1
        observed_peak = max(observed_peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return value * 2

    result = asyncio.run(gather_bounded(
        list(range(8)), worker, max_concurrency=3,
    ))

    assert result.results == [value * 2 for value in range(8)]
    assert result.peak_concurrency == observed_peak == 3
    assert result.max_concurrency == 3
    assert result.completed == 8 and result.failed == 0


def test_gather_bounded_can_report_partial_failures():
    async def worker(value: int) -> int:
        await asyncio.sleep(0)
        if value == 2:
            raise RuntimeError("boom")
        return value

    result = asyncio.run(gather_bounded(
        [1, 2, 3], worker, max_concurrency=2, return_exceptions=True,
    ))

    assert result.results[0] == 1 and result.results[2] == 3
    assert isinstance(result.results[1], RuntimeError)
    assert result.completed == 2 and result.failed == 1


def test_offline_async_llm_batch_and_stream_share_public_contract():
    configure(backend="offline", model="离线模板", temperature=0.0, max_tokens=64)
    try:
        batch = asyncio.run(acomplete_batch(
            ["问题一", "问题二", "问题三"],
            max_concurrency=2,
        ))

        async def collect_stream() -> str:
            chunks = []
            async for chunk in acomplete_stream("流式问题", max_concurrency=2):
                chunks.append(chunk)
            return "".join(chunks)

        streamed = asyncio.run(collect_stream())
    finally:
        configure(backend="deepseek", model="deepseek-chat")

    assert len(batch.results) == 3
    assert batch.failed == 0
    assert batch.peak_concurrency <= 2
    assert all(isinstance(value, str) and value for value in batch.results)
    assert streamed


def test_concurrent_batches_share_one_api_semaphore(monkeypatch):
    active = 0
    observed_peak = 0

    class FakeCompletions:
        async def create(self, **kwargs):
            nonlocal active, observed_peak
            active += 1
            observed_peak = max(observed_peak, active)
            try:
                await asyncio.sleep(0.01)
                message = SimpleNamespace(content="ok")
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=message)], usage=None,
                )
            finally:
                active -= 1

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions()),
    )
    monkeypatch.setattr(llm, "_get_async_client", lambda: fake_client)
    configure(backend="offline", model="离线模板", temperature=0.0, max_tokens=64)

    async def run_two_batches():
        return await asyncio.gather(
            acomplete_batch(["a", "b", "c"], max_concurrency=2),
            acomplete_batch(["d", "e", "f"], max_concurrency=2),
        )

    try:
        batches = asyncio.run(run_two_batches())
    finally:
        configure(backend="deepseek", model="deepseek-chat")

    assert observed_peak == 2
    assert sum(batch.completed for batch in batches) == 6


def test_sync_llm_calls_share_semaphore(monkeypatch):
    active = 0
    observed_peak = 0
    state_lock = threading.Lock()

    class FakeCompletions:
        def create(self, **kwargs):
            nonlocal active, observed_peak
            with state_lock:
                active += 1
                observed_peak = max(observed_peak, active)
            try:
                time.sleep(0.01)
                message = SimpleNamespace(content="ok")
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=message)], usage=None,
                )
            finally:
                with state_lock:
                    active -= 1

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions()),
    )
    monkeypatch.setattr(llm, "_get_client", lambda: fake_client)
    monkeypatch.setattr(llm, "_backend", lambda: "ollama")
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("LLM_REQUESTS_PER_MINUTE", "600")
    llm._SYNC_SEMAPHORES.clear()
    llm._REQUEST_LIMITERS.clear()

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda value: complete(str(value)), range(3)))

    assert results == ["ok", "ok", "ok"]
    assert observed_peak == 1
