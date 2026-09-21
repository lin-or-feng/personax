"""PersonaX 进程内限流基元：线程安全的滑动窗口日志。"""
from __future__ import annotations

import asyncio
import math
import threading
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class LimitDecision:
    """一次限流判定；``retry_after`` 使用秒。"""

    allowed: bool
    remaining: int
    retry_after: float = 0.0


class SlidingWindowRateLimiter:
    """精确滑动窗口计数器。

    与按整分钟清零的固定窗口不同，本实现只统计“当前时刻往前
    ``window_seconds``”内的事件，避免分钟边界突发双倍请求。
    ``monotonic`` 时钟和互斥锁保证系统校时与多线程不会破坏窗口。
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        async_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.limit = max(1, int(limit))
        self.window_seconds = max(0.001, float(window_seconds))
        self._clock = clock
        self._sleeper = sleeper
        self._async_sleeper = async_sleeper
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str = "global", *, consume: bool = True) -> LimitDecision:
        normalized_key = key or "global"
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            events = self._events[normalized_key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                retry_after = max(0.0, events[0] + self.window_seconds - now)
                return LimitDecision(False, 0, retry_after)
            if consume:
                events.append(now)
            remaining = max(0, self.limit - len(events))
            return LimitDecision(True, remaining, 0.0)

    def acquire(self, key: str = "global") -> LimitDecision:
        """同步等待直到获得额度；等待期间不占用 Semaphore 槽位。"""

        while True:
            decision = self.check(key)
            if decision.allowed:
                return decision
            self._sleeper(max(0.001, decision.retry_after))

    async def acquire_async(self, key: str = "global") -> LimitDecision:
        """异步等待直到获得额度；与同步入口共享同一事件日志。"""

        while True:
            decision = self.check(key)
            if decision.allowed:
                return decision
            await self._async_sleeper(max(0.001, decision.retry_after))

    @staticmethod
    def retry_seconds(decision: LimitDecision) -> int:
        return max(1, math.ceil(decision.retry_after))
