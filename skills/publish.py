"""发布就绪门禁 Skill：xhs_publish（无副作用）

对齐 AGENTS.md：Skill 禁止副作用，真实发布在 publishers/ 层。
本 Skill 只做「发布就绪校验」并写 metadata.publish_ready，供 Harness
审批（sensitive_tools: [xhs_publish]）与发布器消费。

校验项（商用标准）：
- 标题非空且 ≤ 20 字（小红书标题上限）
- 正文非空且 ≤ 1000 字（平台正文上限）
- 至少 1 个话题标签（无标签限流/降权风险）
- 配图存在（draft.metadata.images/cover 路径有效，缺图给 warning）
"""
from __future__ import annotations
from pathlib import Path

from core.registry import Skill, register
from core.types import SkillInput, SkillOutput

TITLE_MAX = 20
BODY_MAX = 1000


@register
class XhsPublishGate(Skill):
    name = "xhs_publish"
    description = "发布就绪门禁：校验标题/正文/标签/配图，标记 publish_ready"
    triggers = ["发布", "publish", "上架", "发笔记"]
    popularity = 0.5
    version = "1.0.0"

    def run(self, inp: SkillInput) -> SkillOutput:
        draft = inp.draft
        notes: list[str] = []
        errors: list[str] = []

        title_len = len(draft.title or "")
        if not draft.title:
            errors.append("标题为空")
        elif title_len > TITLE_MAX:
            errors.append(f"标题超长（{title_len}>{TITLE_MAX} 字）")

        body_len = len(draft.body or "")
        if not draft.body:
            errors.append("正文为空")
        elif body_len > BODY_MAX:
            errors.append(f"正文超长（{body_len}>{BODY_MAX} 字）")

        if not draft.tags:
            errors.append("无话题标签（建议 ≥1 个，无标签影响曝光）")

        # 配图检查（警告不阻断）
        md = draft.metadata or {}
        images = [str(p) for p in (md.get("images", []) or [])]
        if md.get("cover"):
            images.insert(0, str(md["cover"]))
        missing = [p for p in images if not Path(p).exists()]
        if missing:
            notes.append(f"有 {len(missing)} 张配图路径不存在: {missing[:2]}")
        if not images:
            notes.append("未配置配图（纯文字笔记）")

        ready = not errors
        draft.metadata["publish_ready"] = ready
        draft.metadata["publish_issues"] = errors
        notes += errors or ["发布就绪 ✓"]
        return SkillOutput(draft=draft, notes=notes)
