"""本地遥测读取、P95/成功率/Token 成本聚合与 trace 查询。"""
from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Iterable

from .types import ObservabilitySummary, UsageEvent


DEFAULT_USAGE_PATH = Path(__file__).resolve().parent.parent / "logs" / "usage.jsonl"


def _non_negative_rate(value: object) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _percentile(values: Iterable[float], quantile: float) -> float:
    ordered = sorted(float(value) for value in values if value is not None and value >= 0)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 1)


def load_usage_events(
    path: str | Path = DEFAULT_USAGE_PATH,
    *,
    limit: int = 5_000,
    max_line_bytes: int = 64 * 1024,
) -> tuple[list[UsageEvent], int]:
    """只读最后 N 条遥测；损坏或异常超长行计数后跳过。"""

    source = Path(path)
    if not source.exists():
        return [], 0
    safe_limit = max(1, min(int(limit), 50_000))
    lines: deque[str] = deque(maxlen=safe_limit)
    malformed = 0
    try:
        with source.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if len(line.encode("utf-8", errors="ignore")) > max_line_bytes:
                    malformed += 1
                    continue
                lines.append(line)
    except OSError:
        return [], 1
    events: list[UsageEvent] = []
    for line in lines:
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("event is not an object")
            events.append(UsageEvent.model_validate(payload))
        except (ValueError, TypeError, json.JSONDecodeError):
            malformed += 1
    return events, malformed


def aggregate_usage(
    events: Iterable[UsageEvent],
    *,
    malformed_lines: int = 0,
    input_cny_per_million: float | None = None,
    output_cny_per_million: float | None = None,
) -> ObservabilitySummary:
    rows = list(events)
    llm = [item for item in rows if item.event == "llm_call"]
    tools = [item for item in rows if item.event == "tool_call"]
    replies = [item for item in rows if item.event == "assistant_reply"]
    publishes = [item for item in rows if item.event == "publish"]
    input_rate = (
        _non_negative_rate(os.getenv("LLM_INPUT_COST_CNY_PER_MILLION", "0"))
        if input_cny_per_million is None else _non_negative_rate(input_cny_per_million)
    )
    output_rate = (
        _non_negative_rate(os.getenv("LLM_OUTPUT_COST_CNY_PER_MILLION", "0"))
        if output_cny_per_million is None else _non_negative_rate(output_cny_per_million)
    )
    prompt_tokens = sum(max(0, item.prompt_tokens) for item in llm)
    completion_tokens = sum(max(0, item.completion_tokens) for item in llm)
    cost = (prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000
    return ObservabilitySummary(
        events=len(rows),
        malformed_lines=max(0, int(malformed_lines)),
        trace_count=len({item.trace_id for item in rows if item.trace_id}),
        llm_calls=len(llm),
        tool_calls=len(tools),
        assistant_replies=len(replies),
        publish_calls=len(publishes),
        tool_success_rate=(
            round(sum(item.status == "ok" for item in tools) / len(tools), 4)
            if tools else None
        ),
        assistant_degraded_rate=(
            round(sum(item.degraded is True for item in replies) / len(replies), 4)
            if replies else None
        ),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_cost_cny=round(cost, 6),
        pricing_configured=input_rate > 0 or output_rate > 0,
        llm_p95_ms=_percentile(
            (item.latency_ms for item in llm if item.latency_ms is not None), 0.95,
        ),
        assistant_p95_ms=_percentile(
            (item.wall_ms for item in replies if item.wall_ms is not None), 0.95,
        ),
        publish_p95_ms=_percentile(
            (
                item.wall_ms if item.wall_ms is not None else item.latency_ms
                for item in publishes
                if item.wall_ms is not None or item.latency_ms is not None
            ),
            0.95,
        ),
    )


def trace_events(events: Iterable[UsageEvent], trace_id: str) -> list[UsageEvent]:
    normalized = (trace_id or "").strip()
    return [item for item in events if item.trace_id == normalized] if normalized else []


def safe_event_rows(events: Iterable[UsageEvent]) -> list[dict]:
    """只返回可公开到前端的元数据，不包含提示词、正文或工具参数。"""

    return [
        {
            "时间": item.ts,
            "事件": item.event,
            "Trace ID": item.trace_id,
            "组件": item.tool or item.model or item.backend,
            "状态": item.status or ("degraded" if item.degraded else "ok"),
            "耗时(ms)": item.latency_ms if item.latency_ms is not None else item.wall_ms,
            "错误类型": item.error_kind,
            "尝试": item.attempts,
        }
        for item in events
    ]
