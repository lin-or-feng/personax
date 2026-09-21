"""对话上下文滑动窗口：优先保留最近消息并限制估算 token。"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .types import ChatMessage


def estimate_tokens(text: str) -> int:
    """无需模型 tokenizer 的保守估算：中文约一字一 token，ASCII 约四字符一 token。"""

    value = text or ""
    cjk = len(re.findall(r"[\u3400-\u9fff]", value))
    ascii_like = len(re.sub(r"[\u3400-\u9fff\s]", "", value))
    return cjk + math.ceil(ascii_like / 4)


def _clip_to_tokens(text: str, max_tokens: int) -> tuple[str, bool]:
    if estimate_tokens(text) <= max_tokens:
        return text, False
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle] + "…") <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…", True


@dataclass(frozen=True)
class ContextWindow:
    messages: list[ChatMessage]
    text: str
    used_tokens: int
    dropped_messages: int
    truncated_messages: int


def build_sliding_context_window(
    history: list[ChatMessage],
    *,
    max_messages: int = 8,
    max_tokens: int = 3_000,
    max_message_tokens: int = 750,
) -> ContextWindow:
    """从最新消息向前装入预算，最终恢复为正常时间顺序。"""

    message_limit = max(1, min(int(max_messages), 50))
    token_limit = max(64, min(int(max_tokens), 32_000))
    per_message_limit = max(32, min(int(max_message_tokens), token_limit))
    selected: list[ChatMessage] = []
    used_tokens = 0
    truncated = 0

    for message in reversed(history[-message_limit:]):
        normalized = message.content.strip()
        clipped, was_truncated = _clip_to_tokens(normalized, per_message_limit)
        prefix = "用户：" if message.role == "user" else "助手："
        row_tokens = estimate_tokens(prefix + clipped)
        remaining = token_limit - used_tokens
        if row_tokens > remaining:
            if selected or remaining < 32:
                break
            clipped, budget_truncated = _clip_to_tokens(
                normalized, max(1, remaining - estimate_tokens(prefix)),
            )
            was_truncated = was_truncated or budget_truncated
            row_tokens = estimate_tokens(prefix + clipped)
        selected.append(message.model_copy(update={"content": clipped}))
        used_tokens += row_tokens
        truncated += int(was_truncated)

    selected.reverse()
    text = "\n".join(
        f"{'用户' if message.role == 'user' else '助手'}：{message.content}"
        for message in selected
    ) or "（首次对话）"
    return ContextWindow(
        messages=selected,
        text=text,
        used_tokens=used_tokens,
        dropped_messages=max(0, len(history) - len(selected)),
        truncated_messages=truncated,
    )
