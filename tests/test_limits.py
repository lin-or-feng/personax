"""滑动窗口与上下文窗口回归。"""
from __future__ import annotations

import asyncio

from core.context_window import build_sliding_context_window
from core.limits import SlidingWindowRateLimiter
from core.types import ChatMessage


def test_sliding_window_expires_by_event_time_not_minute_boundary():
    now = [100.0]
    limiter = SlidingWindowRateLimiter(
        2, 10, clock=lambda: now[0], sleeper=lambda seconds: None,
    )

    assert limiter.check("user").allowed
    assert limiter.check("user").allowed
    blocked = limiter.check("user")
    assert not blocked.allowed and blocked.retry_after == 10.0

    now[0] = 109.5
    assert not limiter.check("user").allowed
    now[0] = 110.0
    assert limiter.check("user").allowed


def test_async_acquire_waits_without_blocking_event_loop():
    now = [0.0]

    async def advance(seconds: float) -> None:
        now[0] += seconds
        await asyncio.sleep(0)

    limiter = SlidingWindowRateLimiter(
        1, 5, clock=lambda: now[0], async_sleeper=advance,
    )
    assert limiter.check("llm").allowed
    decision = asyncio.run(limiter.acquire_async("llm"))

    assert decision.allowed
    assert now[0] == 5.0


def test_context_window_keeps_newest_messages_and_token_budget():
    history = [
        ChatMessage(role="user", content=f"第{index}条" + "知识" * 120)
        for index in range(6)
    ]
    window = build_sliding_context_window(
        history, max_messages=4, max_tokens=260, max_message_tokens=180,
    )

    assert window.used_tokens <= 260
    assert window.messages[-1].content.startswith("第5条")
    assert "第0条" not in window.text
    assert window.dropped_messages >= 4
    assert window.truncated_messages >= 1
