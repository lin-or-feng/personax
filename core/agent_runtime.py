"""Agent 运行时防护：预算、循环检测、工具异常分类与有界重试。"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .context_window import estimate_tokens


class ToolErrorKind(str, Enum):
    NONE = "none"
    BUSINESS_EMPTY = "business_empty"
    VALIDATION = "validation"
    PERMISSION = "permission"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    BUDGET = "budget"
    INTERNAL = "internal"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.05
    max_delay_seconds: float = 0.5

    def delay(self, attempt: int) -> float:
        return min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** max(0, attempt - 1)),
        )


def classify_tool_exception(exc: Exception) -> tuple[ToolErrorKind, bool]:
    """只重试瞬时 I/O 故障；参数、权限和业务空结果不重试。"""

    if isinstance(exc, (TimeoutError, ConnectionError)):
        return ToolErrorKind.TIMEOUT, True
    if isinstance(exc, PermissionError):
        return ToolErrorKind.PERMISSION, False
    if isinstance(exc, OSError):
        return ToolErrorKind.UNAVAILABLE, True
    if isinstance(exc, (ValueError, TypeError)):
        return ToolErrorKind.VALIDATION, False
    return ToolErrorKind.INTERNAL, False


@dataclass
class AgentRunBudget:
    """单次运行的硬上界；适合本地 Agent，不冒充分布式配额系统。"""

    max_steps: int = 8
    max_estimated_tokens: int = 6_000
    deadline_seconds: float = 60.0
    repeated_state_limit: int = 2
    started_at: float = field(default_factory=time.monotonic)
    steps: int = 0
    estimated_tokens: int = 0
    _fingerprints: dict[str, int] = field(default_factory=dict)

    def consume_step(self, action: str, state: Any) -> None:
        if time.monotonic() - self.started_at > self.deadline_seconds:
            raise TimeoutError("Agent 已达到本轮总超时")
        if self.steps >= self.max_steps:
            raise RuntimeError("Agent 已达到最大执行步数")
        payload = json.dumps(
            {"action": action, "state": state},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        repeated = self._fingerprints.get(fingerprint, 0) + 1
        self._fingerprints[fingerprint] = repeated
        if repeated > self.repeated_state_limit:
            raise RuntimeError("Agent 连续产生相同状态，已停止潜在死循环")
        self.steps += 1

    def reserve_text(self, text: str) -> int:
        estimated = estimate_tokens(text)
        if self.estimated_tokens + estimated > self.max_estimated_tokens:
            raise RuntimeError("Agent 已达到本轮估算 Token 预算")
        self.estimated_tokens += estimated
        return estimated


def call_with_retry(
    operation: Callable[[], Any],
    *,
    policy: RetryPolicy,
    on_retry: Callable[[int, ToolErrorKind, Exception], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Any, int]:
    """执行工具并返回结果与尝试次数；只对明确可重试异常退避。"""

    attempts = max(1, min(int(policy.max_attempts), 10))
    for attempt in range(1, attempts + 1):
        try:
            return operation(), attempt
        except Exception as exc:  # noqa: BLE001 - 统一分类后再决定是否抛出
            kind, retryable = classify_tool_exception(exc)
            if not retryable or attempt >= attempts:
                setattr(exc, "personax_error_kind", kind.value)
                setattr(exc, "personax_attempts", attempt)
                raise
            if on_retry is not None:
                on_retry(attempt, kind, exc)
            sleep(policy.delay(attempt))
    raise RuntimeError("工具重试状态异常")  # pragma: no cover
