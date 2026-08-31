"""真实发布安全开关。

默认关闭，避免本地调试、演示或账号受限期间误触发平台发布。
"""
from __future__ import annotations

import os


REAL_PUBLISH_ENV = "XHS_REAL_PUBLISH_ENABLED"


def real_publish_enabled() -> bool:
    """只有显式设置 XHS_REAL_PUBLISH_ENABLED=1 才允许真实发布。"""
    return os.getenv(REAL_PUBLISH_ENV, "0").strip() == "1"


def real_publish_disabled_message() -> str:
    return (
        "真实发布安全锁处于关闭状态。当前仅可生成、预览和干跑；"
        f"确认账号恢复且准备发布时，在 .env 中设置 {REAL_PUBLISH_ENV}=1 后重启应用。"
    )
