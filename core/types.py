"""PersonaX 核心数据契约（跨层通信统一用这些类型）"""
from __future__ import annotations
import uuid
from typing import Annotated, Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


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
    memories: list[str] = Field(default_factory=list, max_length=20)
    thread_id: str = Field(default="local")
    use_knowledge: bool = True
    max_steps: int = Field(default=4, ge=2, le=8)
    max_estimated_tokens: int = Field(default=6_000, ge=512, le=32_000)
    deadline_seconds: float = Field(default=60.0, ge=1.0, le=600.0)
    trace_id: str = Field(
        default_factory=lambda: f"trace-{uuid.uuid4().hex[:16]}",
        min_length=8,
        max_length=80,
    )


AssistantAPIMemory = Annotated[str, Field(min_length=1, max_length=500)]


class AssistantAPIRequest(BaseModel):
    """HTTP 边界输入；不允许客户端伪造服务端 ``trace_id``。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [{
                "question": "请介绍 PersonaX 的 RAG 流程",
                "thread_id": "demo-1",
                "use_knowledge": True,
            }],
        },
    )

    question: str = Field(min_length=1, max_length=2_000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)
    memories: list[AssistantAPIMemory] = Field(default_factory=list, max_length=20)
    thread_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    use_knowledge: bool = True
    max_steps: int = Field(default=4, ge=2, le=8)
    max_estimated_tokens: int = Field(default=6_000, ge=512, le=32_000)
    deadline_seconds: float = Field(default=60.0, ge=1.0, le=120.0)

    def to_core_request(self) -> AssistantRequest:
        """生成服务端 Trace，并转换为现有编排层契约。"""

        return AssistantRequest(
            question=self.question,
            history=self.history,
            memories=self.memories,
            thread_id=self.thread_id or f"api-{uuid.uuid4().hex[:16]}",
            use_knowledge=self.use_knowledge,
            max_steps=self.max_steps,
            max_estimated_tokens=self.max_estimated_tokens,
            deadline_seconds=self.deadline_seconds,
            trace_id=f"trace-{uuid.uuid4().hex[:16]}",
        )


class AssistantResponse(BaseModel):
    """对话助手统一输出契约。"""

    answer: str
    route: Literal["direct", "knowledge"]
    sources: list[AssistantSource] = Field(default_factory=list)
    trace: list[AgentTraceStep] = Field(default_factory=list)
    trace_id: str = ""
    checkpoint_id: str = ""
    degraded: bool = False


class AssistantThreadSummary(BaseModel):
    """可恢复对话的轻量索引；不重复保存完整消息。"""

    thread_id: str
    route: Literal["direct", "knowledge"]
    updated_at: float
    message_count: int = Field(default=0, ge=0)
    preview: str = ""


class AssistantMemory(BaseModel):
    """用户显式保存的长期记忆。"""

    memory_id: int = Field(ge=1)
    user_id: str
    content: str = Field(min_length=1, max_length=500)
    kind: Literal["preference", "summary"] = "preference"
    source_thread_id: str = ""
    expires_at: float | None = None
    redacted: bool = False
    created_at: float
    updated_at: float


class FeedbackSourceRef(BaseModel):
    """反馈样例只保存来源定位信息，不复制知识正文。"""

    ref_id: str = Field(default="", max_length=20)
    title: str = Field(default="", max_length=300)
    source: str = Field(default="", max_length=500)


class AssistantFeedback(BaseModel):
    """用户显式提交的本地回答反馈。"""

    feedback_id: int = Field(ge=1)
    trace_id: str = Field(min_length=8, max_length=80)
    thread_id: str = Field(default="", max_length=200)
    rating: Literal["helpful", "not_helpful"]
    reason: Literal[
        "", "inaccurate", "missing_evidence", "irrelevant",
        "incomplete", "slow", "other",
    ] = ""
    note: str = Field(default="", max_length=500)
    question: str = Field(default="", max_length=500)
    answer_excerpt: str = Field(default="", max_length=500)
    answer_hash: str = Field(min_length=64, max_length=64)
    source_refs: list[FeedbackSourceRef] = Field(default_factory=list, max_length=10)
    route: Literal["", "direct", "knowledge"] = ""
    source_count: int = Field(default=0, ge=0, le=100)
    degraded: bool = False
    content_included: bool = False
    redacted: bool = False
    created_at: float
    updated_at: float


class FeedbackSummary(BaseModel):
    """状态页使用的反馈聚合，不包含问题或回答内容。"""

    total: int = 0
    helpful: int = 0
    not_helpful: int = 0
    helpful_rate: float | None = None
    eval_candidates: int = 0
    reasons: dict[str, int] = Field(default_factory=dict)


class PublishApproval(BaseModel):
    """真实发布前可跨重启恢复的人工审批记录。"""

    request_id: str
    thread_id: str
    idempotency_key: str
    user_id: str
    draft_hash: str
    title: str = ""
    status: Literal["pending", "approved", "rejected", "consumed"] = "pending"
    reason: str = ""
    interrupt_id: str = ""
    published_url: str = ""
    created_at: float
    resolved_at: float | None = None


class RouteDecision(BaseModel):
    """Supervisor 的结构化路由结果。"""

    route: Literal["direct", "knowledge"]
    reason: str = Field(default="", max_length=300)
    router: Literal["manual", "rule", "llm", "fallback"] = "rule"


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


class ToolCallRequest(BaseModel):
    """MCP-ready 工具调用边界；arguments 保留 JSON 形态再由工具 Schema 校验。"""

    name: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_\-]+$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    user_id: str = Field(default="anonymous", max_length=200)
    trace_id: str = Field(default="", max_length=80)


class ToolCallResult(BaseModel):
    """工具网关统一返回，阻断/校验错误不向上层泄漏异常。"""

    status: Literal["ok", "blocked", "error"]
    output: KnowledgeSearchOutput | None = None
    error: str = ""
    error_kind: Literal[
        "none", "business_empty", "validation", "permission",
        "timeout", "unavailable", "budget", "internal",
    ] = "none"
    retryable: bool = False
    attempts: int = Field(default=1, ge=1, le=10)
    trace_id: str = ""


class UsageEvent(BaseModel):
    """不含提示词/正文的结构化遥测事件。"""

    ts: str = ""
    event: str
    trace_id: str = ""
    backend: str = ""
    model: str = ""
    tool: str = ""
    status: str = ""
    error_kind: str = ""
    latency_ms: float | None = None
    wall_ms: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    attempts: int = 1
    sources: int = 0
    success: bool | None = None
    degraded: bool | None = None


class ObservabilitySummary(BaseModel):
    """状态页和 CLI 共用的安全聚合结果。"""

    events: int = 0
    malformed_lines: int = 0
    trace_count: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    assistant_replies: int = 0
    publish_calls: int = 0
    tool_success_rate: float | None = None
    assistant_degraded_rate: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_cny: float = 0.0
    pricing_configured: bool = False
    llm_p95_ms: float = 0.0
    assistant_p95_ms: float = 0.0
    publish_p95_ms: float = 0.0
