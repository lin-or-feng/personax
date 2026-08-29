"""用量埋点（商用 · 量化数据自动采集）

每次生成 / 每次 LLM 调用 / 每次发布，自动追加一行 JSON 到 logs/usage.jsonl。
- 不依赖任何第三方库
- 失败静默：埋点出错绝不影响主流程
- 线程本地累加器：一次 run 内多个 Skill 的 token 自动合计

采集到的字段（供简历/技术说明引用）：
- gen      单篇生成端到端耗时（topic → 完整草稿）+ 本次 token 合计
- llm_call 单次模型调用的 prompt_tokens / completion_tokens / 耗时
- publish  单次发布结果（success / cost_ms / mode）
"""
from __future__ import annotations
import json
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_PATH = Path(__file__).resolve().parent.parent / "logs" / "usage.jsonl"

# 线程本地 token 累加器（一次 run 内多 skill 的 LLM 调用合计）
_tls = threading.local()


def _acc() -> dict:
    a = getattr(_tls, "acc", None)
    if a is None:
        a = _tls.acc = {"prompt": 0, "completion": 0, "calls": 0}
    return a


def add_tokens(prompt: int, completion: int) -> None:
    """一次 LLM 调用后累加 token（complete() 里调用）"""
    a = _acc()
    a["prompt"] += int(prompt or 0)
    a["completion"] += int(completion or 0)
    a["calls"] += 1


def take_tokens() -> dict:
    """取走并清零当前线程累计的 token（orchestrator 在 run 结束时调用）"""
    a = _acc()
    out = dict(a)
    a["prompt"] = a["completion"] = a["calls"] = 0
    return out


def record(event: str, **fields) -> None:
    """追加一行记录。任何异常都被吞掉，不影响业务。"""
    try:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event}
        rec.update({k: v for k, v in fields.items() if v is not None})
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with open(_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 —— 埋点失败静默
        pass
