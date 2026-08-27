from __future__ import annotations
from core.types import Draft, PublishResult


class Publisher:
    """发布渠道抽象接口"""
    name: str = ""

    def publish(self, draft: Draft) -> PublishResult:
        raise NotImplementedError
