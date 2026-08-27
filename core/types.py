"""PersonaX 核心数据契约（跨层通信统一用这些类型）"""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field


class Draft(BaseModel):
    """内容草稿——Skill 之间传递的唯一结构化对象"""
    topic: str
    title: Optional[str] = None
    body: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    cover_text: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillInput(BaseModel):
    """Skill 统一输入"""
    draft: Draft
    context: dict[str, Any] = Field(default_factory=dict)


class SkillOutput(BaseModel):
    """Skill 统一输出（必须可序列化，便于 checkpoint）"""
    draft: Draft
    notes: list[str] = Field(default_factory=list)  # 过程记录


class ExecutionContext(BaseModel):
    """编排层 → 各 Skill 的上下文（不可变快照）"""
    run_id: str
    persona: dict[str, Any] = Field(default_factory=dict)
    user_id: Optional[str] = None
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    prompts: dict[str, Any] = Field(default_factory=dict)          # 提示词资产
    rag_examples: list[str] = Field(default_factory=list)          # RAG 召回样例（正文参考）


class PublishResult(BaseModel):
    """发布结果"""
    success: bool
    url: Optional[str] = None
    message: str = ""
    cost_ms: int = 0
