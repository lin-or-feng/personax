"""PersonaX 核心数据契约（跨层通信统一用这些类型）"""
from __future__ import annotations
from typing import Any, Literal, Optional
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
    rag_references: list[str] = Field(default_factory=list)        # RAG 事实资料（不模仿文风）
    web_context: str = ""                                          # 联网热点/参考（正文时效性）


class PublishResult(BaseModel):
    """发布结果"""
    success: bool
    url: Optional[str] = None
    message: str = ""
    cost_ms: int = 0


class ChatMessage(BaseModel):
    """对话消息；Assistant 跨层传递时不使用裸 dict。"""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12_000)


class AssistantSource(BaseModel):
    """可追溯的知识库引用。"""

    ref_id: str
    title: str
    source: str = ""
    source_url: str = ""
    excerpt: str = ""
    score: float = 0.0


class AgentTraceStep(BaseModel):
    """一次 Agent/Worker 动作的审计记录。"""

    step: int = Field(ge=1)
    worker: str
    action: str
    status: Literal["ok", "skipped", "degraded", "blocked", "error"] = "ok"
    duration_ms: float = Field(default=0.0, ge=0.0)
    detail: str = ""


class AssistantRequest(BaseModel):
    """对话助手统一输入契约。"""

    question: str = Field(min_length=1, max_length=2_000)
    history: list[ChatMessage] = Field(default_factory=list)
    thread_id: str = Field(default="local")
    use_knowledge: bool = True
    max_steps: int = Field(default=4, ge=2, le=8)


class AssistantResponse(BaseModel):
    """对话助手统一输出契约。"""

    answer: str
    route: Literal["direct", "knowledge"]
    sources: list[AssistantSource] = Field(default_factory=list)
    trace: list[AgentTraceStep] = Field(default_factory=list)
    checkpoint_id: str = ""
    degraded: bool = False


class ToolManifest(BaseModel):
    """MCP-ready 工具描述；可直接导出 JSON Schema。"""

    name: str
    description: str
    version: str = "1.0.0"
    sensitive: bool = False
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    top_k: int = Field(default=4, ge=1, le=10)
    min_score: float = Field(default=0.10, ge=0.0, le=1.0)


class KnowledgeSearchOutput(BaseModel):
    sources: list[AssistantSource] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)
