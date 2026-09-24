"""PersonaX 只读工具网关。

这是 transport-neutral 的 MCP-ready 适配层：对外暴露 Manifest/JSON Schema，
对内统一执行 Harness 权限、限流与审计。它不启动网络服务。
"""
from __future__ import annotations

import time

from pydantic import ValidationError

from .agent_runtime import (
    RetryPolicy,
    ToolErrorKind,
    call_with_retry,
    classify_tool_exception,
)
from .assistant_tools import KnowledgeSearchTool, tool_manifests
from .harness import Harness
from .types import (
    KnowledgeSearchInput,
    ToolCallRequest,
    ToolCallResult,
    ToolManifest,
)


class AssistantToolGateway:
    """仅登记无副作用的知识检索工具。"""

    def __init__(
        self,
        knowledge_tool: KnowledgeSearchTool,
        harness: Harness,
        *,
        retry_policy: RetryPolicy | None = None,
        max_output_chars: int = 6_000,
    ):
        self.knowledge_tool = knowledge_tool
        self.harness = harness
        self._tools = {knowledge_tool.name: knowledge_tool}
        self.retry_policy = retry_policy or RetryPolicy()
        self.max_output_chars = max(500, min(int(max_output_chars), 50_000))

    @staticmethod
    def _finish(
        request: ToolCallRequest,
        result: ToolCallResult,
        started: float,
    ) -> ToolCallResult:
        """持久化安全遥测；不记录 query、参数或来源正文。"""

        try:
            from .usage import record

            record(
                "tool_call",
                trace_id=request.trace_id,
                tool=request.name,
                status=result.status,
                error_kind=result.error_kind,
                retryable=result.retryable,
                attempts=result.attempts,
                sources=len(result.output.sources) if result.output else 0,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        except Exception:  # noqa: BLE001 - 遥测失败不得影响工具主链路
            pass
        return result

    def list_tools(self) -> list[ToolManifest]:
        return tool_manifests(self.knowledge_tool)

    def as_langchain_tool(self, user_id: str, trace_id: str = ""):
        """导出 LangChain Core StructuredTool，但实际执行仍必须经过本网关。"""

        from langchain_core.tools import StructuredTool

        manifest = self.knowledge_tool.manifest

        def invoke(query: str, top_k: int = 4, min_score: float = 0.10) -> dict:
            result = self.call(ToolCallRequest(
                name=manifest.name,
                arguments={"query": query, "top_k": top_k, "min_score": min_score},
                user_id=user_id,
                trace_id=trace_id,
            ))
            return result.model_dump(mode="json")

        return StructuredTool.from_function(
            func=invoke,
            name=manifest.name,
            description=manifest.description,
            args_schema=KnowledgeSearchInput,
            return_direct=False,
            tags=["personax", "read-only", "rag"],
            metadata={"sensitive": manifest.sensitive, "version": manifest.version},
        )

    def call(self, request: ToolCallRequest) -> ToolCallResult:
        started = time.perf_counter()
        tool = self._tools.get(request.name)
        if tool is None:
            self.harness.audit.write(
                event="tool_denied",
                tool=request.name,
                user_id=request.user_id,
                trace_id=request.trace_id,
                reason="unknown_tool",
            )
            return self._finish(request, ToolCallResult(
                status="blocked",
                error=f"未允许的工具: {request.name}",
                error_kind=ToolErrorKind.PERMISSION.value,
                trace_id=request.trace_id,
            ), started)

        allowed, reason = self.harness.guard(request.name, request.user_id)
        if not allowed:
            return self._finish(request, ToolCallResult(
                status="blocked",
                error=reason,
                error_kind=ToolErrorKind.PERMISSION.value,
                trace_id=request.trace_id,
            ), started)

        try:
            typed_input = KnowledgeSearchInput.model_validate(request.arguments)
        except ValidationError as exc:
            self.harness.audit.write(
                event="tool_validation_error",
                tool=request.name,
                user_id=request.user_id,
                trace_id=request.trace_id,
            )
            return self._finish(request, ToolCallResult(
                status="error",
                error=f"工具参数校验失败: {exc.error_count()} 处",
                error_kind=ToolErrorKind.VALIDATION.value,
                trace_id=request.trace_id,
            ), started)

        def on_retry(attempt: int, kind: ToolErrorKind, exc: Exception) -> None:
            self.harness.audit.write(
                event="tool_retry",
                tool=request.name,
                user_id=request.user_id,
                trace_id=request.trace_id,
                attempt=attempt,
                error_kind=kind.value,
                error_type=type(exc).__name__,
            )

        try:
            output, attempts = call_with_retry(
                lambda: tool.run(typed_input),
                policy=self.retry_policy,
                on_retry=on_retry,
            )
        except Exception as exc:  # noqa: BLE001 - 网关必须转为可审计的受控结果
            kind, retryable = classify_tool_exception(exc)
            attempts = int(getattr(exc, "personax_attempts", 1))
            self.harness.audit.write(
                event="tool_error",
                tool=request.name,
                user_id=request.user_id,
                trace_id=request.trace_id,
                attempts=attempts,
                error_kind=kind.value,
                error_type=type(exc).__name__,
            )
            return self._finish(request, ToolCallResult(
                status="error",
                error=f"工具执行失败: {type(exc).__name__}",
                error_kind=kind.value,
                retryable=retryable,
                attempts=attempts,
                trace_id=request.trace_id,
            ), started)

        remaining = self.max_output_chars
        bounded_sources = []
        truncated = False
        for source in output.sources:
            if remaining <= 0:
                truncated = True
                break
            excerpt = source.excerpt[:remaining]
            truncated = truncated or len(excerpt) < len(source.excerpt)
            bounded_sources.append(source.model_copy(update={"excerpt": excerpt}))
            remaining -= len(excerpt)
        if truncated:
            trace = dict(output.trace)
            trace["tool_output_truncated"] = True
            trace["tool_output_char_budget"] = self.max_output_chars
            output = output.model_copy(update={"sources": bounded_sources, "trace": trace})

        self.harness.audit.write(
            event="tool_completed",
            tool=request.name,
            user_id=request.user_id,
            trace_id=request.trace_id,
            attempts=attempts,
            sources=len(output.sources),
        )
        return self._finish(request, ToolCallResult(
            status="ok",
            output=output,
            error_kind=(
                ToolErrorKind.NONE.value if output.sources
                else ToolErrorKind.BUSINESS_EMPTY.value
            ),
            attempts=attempts,
            trace_id=request.trace_id,
        ), started)
