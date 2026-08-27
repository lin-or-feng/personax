"""Harness：独立规则引擎（对齐商用框架管控层）

管控维度：allow/deny、quota 限流、sensitive_tool 二次确认、审计日志。
"""
from __future__ import annotations
import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class RuleConfig:
    allow_all: bool = True
    denied_skills: list[str] = field(default_factory=list)
    sensitive_tools: list[str] = field(default_factory=list)
    require_approval: bool = True
    rate_limit: int = 60          # Skill 调用预算（每分钟）
    publish_rate_limit: int = 3   # 真实发布上限（每分钟）——商用核心限速
    timeout_seconds: int = 30
    max_retries: int = 2          # Skill/风格校验最大重试次数


@dataclass
class AuditLog:
    entries: list[dict] = field(default_factory=list)

    def write(self, **kwargs):
        self.entries.append({"ts": time.time(), **kwargs})


class Harness:
    """规则引擎：在 Skill 执行前后做拦截与审批"""

    def __init__(self, config: RuleConfig | None = None, audit: AuditLog | None = None):
        self.config = config or RuleConfig()
        self.audit = audit or AuditLog()
        self._call_times: dict[str, list[float]] = defaultdict(list)
        self._publish_times: dict[str, list[float]] = defaultdict(list)

    def check_allow(self, skill_name: str) -> tuple[bool, str]:
        if skill_name in self.config.denied_skills:
            return False, f"skill {skill_name} 在黑名单"
        if not self.config.allow_all:
            return False, "默认拒绝（白名单未配置）"
        return True, "ok"

    def check_quota(self, user_id: str | None) -> tuple[bool, str]:
        key = user_id or "anonymous"
        now = time.time()
        window = 60
        self._call_times[key] = [t for t in self._call_times[key] if now - t < window]
        if len(self._call_times[key]) >= self.config.rate_limit:
            return False, f"限流：每分钟最多{self.config.rate_limit}次"
        self._call_times[key].append(now)
        return True, "ok"

    def check_publish_quota(self, user_id: str | None = None) -> tuple[bool, str]:
        """真实发布专用限速（与 Skill 调用预算分离，商用核心）"""
        key = user_id or "anonymous"
        now = time.time()
        window = 60
        self._publish_times[key] = [t for t in self._publish_times[key] if now - t < window]
        if len(self._publish_times[key]) >= self.config.publish_rate_limit:
            return False, f"发布限流：每分钟最多{self.config.publish_rate_limit}次"
        self._publish_times[key].append(now)
        self.audit.write(event="publish_quota_ok", user_id=key)
        return True, "ok"

    def needs_approval(self, skill_name: str) -> bool:
        return self.config.require_approval and skill_name in self.config.sensitive_tools

    def guard(self, skill_name: str, user_id: str | None = None) -> tuple[bool, str]:
        """执行前总闸：allow + quota"""
        ok, msg = self.check_allow(skill_name)
        if not ok:
            self.audit.write(event="deny", skill=skill_name, reason=msg)
            return False, msg
        ok, msg = self.check_quota(user_id)
        if not ok:
            self.audit.write(event="rate_limit", skill=skill_name, reason=msg)
            return False, msg
        self.audit.write(event="allow", skill=skill_name)
        return True, msg
