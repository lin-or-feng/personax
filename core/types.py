"""PersonaX 核心数据契约（跨层通信统一用这些类型）"""
from __future__ import annotations
import uuid
from typing import Annotated, Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    document_id: str = ""
    document_version: str = ""
    chunk_id: str = ""
    page: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    section: str = ""
    extraction_method: Literal["native", "ocr"] = "native"
    bounding_box: tuple[float, float, float, float] | None = None
    ocr_confidence: float | None = None
    quality_flags: list[str] = Field(default_factory=list)
    ocr_engine: str = ""
    ocr_model_fingerprint: str = ""
    ocr_reviewed: bool = False
    human_edited: bool = False


class DocumentParseOptions(BaseModel):
    """Local operator options. Never exposed as model-selected tool arguments."""

    model_config = ConfigDict(extra="forbid")
    ocr_enabled: bool = False
    force_ocr: bool = False
    model_root: str = ""
    worker_python: str = ""
    timeout_seconds: float = Field(default=120, ge=1, le=600, allow_inf_nan=False)
    min_confidence: float = Field(default=0.85, ge=0, le=1, allow_inf_nan=False)
    render_dpi: int = Field(default=150, ge=72, le=200)
    max_page_pixels: int = Field(default=12_000_000, ge=100_000, le=20_000_000)
    max_total_pixels: int = Field(default=60_000_000, ge=100_000, le=100_000_000)

    @model_validator(mode="after")
    def validate_ocr(self):
        if self.force_ocr and not self.ocr_enabled:
            raise ValueError("force_ocr_requires_ocr")
        return self


class OCRBlock(BaseModel):
    """Box in [0,1], origin top-left of the displayed page; not PDF user-space."""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=12_000)
    bounding_box: tuple[float, float, float, float]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_box(self):
        import math
        x0, y0, x1, y1 = self.bounding_box
        if not all(math.isfinite(v) and 0 <= v <= 1 for v in self.bounding_box) or x0 >= x1 or y0 >= y1:
            raise ValueError("invalid_ocr_box")
        if not self.text.strip():
            raise ValueError("empty_ocr_block")
        return self


class OCRPageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(ge=1, le=100)
    blocks: list[OCRBlock] = Field(default_factory=list, max_length=2_000)
    blank: bool = False
    engine: str = Field(min_length=1, max_length=100)
    model_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_blank(self):
        if self.blank and self.blocks:
            raise ValueError("blank_with_blocks")
        return self


class DocumentPageReport(BaseModel):
    page: int = Field(ge=1, le=100)
    method: Literal["native", "ocr", "blank"]
    block_count: int = Field(default=0, ge=0)
    quality_flags: list[str] = Field(default_factory=list)


class KnowledgeImportRequest(BaseModel):
    """本地操作者导入契约；kb_id 是分区，不是身份认证。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kb_id: str = Field(default="ai-engineering", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    source_path: str = Field(min_length=1, max_length=500)
    title: str = Field(default="", max_length=300)
    source_url: str = Field(default="", max_length=2_048)
    license: str = Field(default="", max_length=300)
    accept_ocr_review: bool = False


class KnowledgeUnit(BaseModel):
    """规范化正文段；位置指向原始页或源文本行。"""

    text: str
    section: str = ""
    page: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    extraction_method: Literal["native", "ocr"] = "native"
    bounding_box: tuple[float, float, float, float] | None = None
    ocr_confidence: float | None = None
    quality_flags: list[str] = Field(default_factory=list)
    ocr_engine: str = ""
    ocr_model_fingerprint: str = ""
    ocr_reviewed: bool = False
    human_edited: bool = False


class DocumentParseResult(BaseModel):
    units: list[KnowledgeUnit] = Field(default_factory=list)
    pages: list[DocumentPageReport] = Field(default_factory=list)

    @property
    def requires_review(self) -> bool:
        return any(unit.extraction_method == "ocr" for unit in self.units)


class KnowledgeReviewedParse(BaseModel):
    """Server-owned prepared parse. Never accepted directly from an HTTP client."""
    model_config = ConfigDict(extra="forbid")
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    parser_version: str
    expected_revision: int = Field(ge=0)
    parsed: DocumentParseResult


class WorkspaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str = "logs/workspace"
    kb_id: str = "workspace"
    parse_options: DocumentParseOptions = Field(default_factory=DocumentParseOptions)
    max_jobs: int = Field(default=100, ge=1, le=500)
    storage_budget_bytes: int = Field(default=256 * 1024 * 1024, ge=10 * 1024 * 1024)
    lease_seconds: int = Field(default=900, ge=120, le=3600)


class WorkspaceJob(BaseModel):
    job_id: str
    filename: str
    content_hash: str
    status: Literal["queued", "parsing", "awaiting_review", "queued_commit", "committing", "completed", "failed", "cancelled"]
    revision: int = Field(ge=1)
    created_at: float
    updated_at: float
    preview_hash: str = ""
    page_count: int = 0
    unit_count: int = 0
    reason: str = ""
    document_id: str = ""
    document_version: str = ""


class WorkspacePreview(BaseModel):
    job: WorkspaceJob
    parsed: DocumentParseResult


class WorkspaceReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    preview_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorkspaceTextEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit_index: int = Field(ge=0, le=1999)
    text: str = Field(min_length=1, max_length=500_000)


class WorkspaceEditRequest(WorkspaceReviewRequest):
    edits: list[WorkspaceTextEdit] = Field(min_length=1, max_length=2000)


class KnowledgeChunk(KnowledgeUnit):
    chunk_id: str
    parent_id: str
    parent_text: str


class KnowledgeDocument(BaseModel):
    kb_id: str
    document_id: str
    document_version: str
    source_path: str
    title: str
    source_url: str = ""
    license: str = ""
    content_hash: str
    parser_version: str
    status: Literal["active", "deleted"] = "active"
    chunks: list[KnowledgeChunk] = Field(default_factory=list)
    updated_at: float = 0.0
    revision: int = Field(default=0, ge=0)
    page_reports: list[DocumentPageReport] = Field(default_factory=list)


class KnowledgeImportResult(BaseModel):
    job_id: str
    document_id: str
    status: Literal["created", "updated", "unchanged", "failed"]
    document_version: str = ""
    chunk_count: int = 0
    reason: str = ""


class KnowledgeJob(BaseModel):
    job_id: str
    kb_id: str
    document_id: str
    operation: Literal["ingest", "delete"]
    status: Literal["running", "completed", "failed", "abandoned"]
    reason: str = ""
    created_at: float
    updated_at: float


class KnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kb_id: str = Field(default="ai-engineering", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    query: str = Field(min_length=1, max_length=2_000)
    top_k: int = Field(default=3, ge=1, le=10)
    min_score: float = Field(default=0.15, ge=0.0, le=1.0)


class KnowledgeSearchResponse(BaseModel):
    kb_id: str
    answer: str
    sources: list[AssistantSource] = Field(default_factory=list)
    index_version: str
    mode: Literal["extractive_baseline"] = "extractive_baseline"
    document_count: int = 0
    embedding_backend: str = "hashing"
    retrieval_mode: str = "hybrid"


class KnowledgeRelationInput(BaseModel):
    """Evidence-backed association; co-occurrence never implies causation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kb_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    subject: str = Field(min_length=2, max_length=100)
    predicate: Literal["co_mentions"] = "co_mentions"
    object: str = Field(min_length=2, max_length=100)
    document_id: str = Field(min_length=1, max_length=100)
    document_version: str = Field(min_length=1, max_length=100)
    chunk_id: str = Field(min_length=1, max_length=100)
    evidence: str = Field(min_length=5, max_length=600)


class KnowledgeRelation(KnowledgeRelationInput):
    relation_id: str


class KnowledgeGraphQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kb_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    entity: str = Field(min_length=2, max_length=100)
    max_hops: int = Field(default=2, ge=1, le=2)
    limit: int = Field(default=10, ge=1, le=20)


class KnowledgeGraphPath(BaseModel):
    entities: list[str]
    relations: list[KnowledgeRelation]


class KnowledgeGraphResult(BaseModel):
    kb_id: str
    paths: list[KnowledgeGraphPath] = Field(default_factory=list)
    excluded_stale_relations: int = 0
    mode: Literal["evidence_backed_cooccurrence_graph"] = "evidence_backed_cooccurrence_graph"


class KnowledgeCatalogConfig(BaseModel):
    """Operator-owned catalog settings; paths/providers are never client arguments."""

    model_config = ConfigDict(extra="forbid")
    db_path: str = "logs/knowledge.sqlite3"
    source_root: str = "examples/ai_engineering"
    allowed_kb_ids: list[str] = Field(default_factory=lambda: ["ai-engineering", "python-official"], min_length=1, max_length=20)
    embedding_backend: Literal["hashing", "ollama", "sentence-transformers"] = "hashing"
    embedding_model: str = "bge-m3"
    retrieval_mode: Literal["bm25", "dense", "hybrid"] = "hybrid"
    min_score: float = Field(default=0.15, ge=0, le=1)
    top_k: int = Field(default=4, ge=1, le=10)

    vector_store: Literal["memory", "qdrant"] = "memory"
    qdrant_path: str = ".rag_cache/qdrant_catalog"


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
    execution_mode: Literal["workflow", "agent"] = "workflow"
    knowledge_source: Literal["legacy", "catalog"] = "legacy"
    kb_id: str = Field(default="ai-engineering", pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    task: Literal["qa", "compare", "summary"] = "qa"
    answer_mode: Literal["extractive", "llm"] = "extractive"
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
    execution_mode: Literal["workflow", "agent"] = "workflow"
    knowledge_source: Literal["legacy", "catalog"] = "legacy"
    kb_id: str = Field(default="ai-engineering", pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    task: Literal["qa", "compare", "summary"] = "qa"
    answer_mode: Literal["extractive", "llm"] = "extractive"
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
            execution_mode=self.execution_mode,
            knowledge_source=self.knowledge_source,
            kb_id=self.kb_id,
            task=self.task,
            answer_mode=self.answer_mode,
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
    execution_mode: Literal["workflow", "agent"] = "workflow"
    stop_reason: str = ""
    budget_steps: int = Field(default=0, ge=0)
    estimated_tokens: int = Field(default=0, ge=0)
    knowledge_source: Literal["legacy", "catalog"] = "legacy"
    kb_id: str = ""
    index_version: str = ""
    answer_mode: Literal["legacy", "extractive_baseline", "extractive_fallback", "llm_grounded", "refused"] = "legacy"


class RetrievalAgentDecision(BaseModel):
    """模型只能选择只读行动，不能覆盖服务端工具权限和检索参数。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["search", "finish", "clarify", "insufficient_evidence"]
    query: str = Field(default="", max_length=2_000)
    clarification: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def validate_action(self) -> "RetrievalAgentDecision":
        if self.action == "search":
            if not self.query or self.clarification:
                raise ValueError("search 只接受非空 query")
        elif self.action == "clarify":
            if not self.clarification or self.query:
                raise ValueError("clarify 只接受非空 clarification")
        elif self.query or self.clarification:
            raise ValueError("结束动作不接受工具参数或回答正文")
        return self


class RetrievalAgentConfig(BaseModel):
    """仅运维配置可修改，不作为 HTTP 客户端参数暴露。"""

    model_config = ConfigDict(extra="forbid")
    max_searches: int = Field(default=3, ge=1, le=3)
    decision_timeout_seconds: float = Field(default=20.0, gt=0, le=60)
    decision_max_tokens: int = Field(default=240, ge=64, le=400)


class RetrievalAgentObservation(BaseModel):
    """本轮内存中的工具观察，不持久化检索词或来源正文。"""

    query: str
    status: str
    error_kind: str = "none"
    sources: list[AssistantSource] = Field(default_factory=list)


class RetrievalAgentResult(BaseModel):
    disposition: Literal["answer", "clarify", "refuse", "fallback"]
    stop_reason: str
    clarification: str = ""
    sources: list[AssistantSource] = Field(default_factory=list)
    trace: list[AgentTraceStep] = Field(default_factory=list)
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
