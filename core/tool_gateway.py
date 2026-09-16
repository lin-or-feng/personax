"""PersonaX 只读工具网关。

这是 transport-neutral 的 MCP-ready 适配层：对外暴露 Manifest/JSON Schema，
对内统一执行 Harness 权限、限流与审计。它不启动网络服务。
"""
from __future__ import annotations

from pydantic import ValidationError

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

    def __init__(self, knowledge_tool: KnowledgeSearchTool, harness: Harness):
        self.knowledge_tool = knowledge_tool
        self.harness = harness
        self._tools = {knowledge_tool.name: knowledge_tool}

    def list_tools(self) -> list[ToolManifest]:
        return tool_manifests(self.knowledge_tool)

    def as_langchain_tool(self, user_id: str):
        """导出 LangChain Core StructuredTool，但实际执行仍必须经过本网关。"""

        from langchain_core.tools import StructuredTool

        manifest = self.knowledge_tool.manifest

        def invoke(query: str, top_k: int = 4, min_score: float = 0.10) -> dict:
            result = self.call(ToolCallRequest(
                name=manifest.name,
                arguments={"query": query, "top_k": top_k, "min_score": min_score},
                user_id=user_id,
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
        tool = self._tools.get(request.name)
        if tool is None:
            self.harness.audit.write(
                event="tool_denied",
                tool=request.name,
                user_id=request.user_id,
                reason="unknown_tool",
            )
            return ToolCallResult(status="blocked", error=f"未允许的工具: {request.name}")

        allowed, reason = self.harness.guard(request.name, request.user_id)
        if not allowed:
            return ToolCallResult(status="blocked", error=reason)

        try:
            typed_input = KnowledgeSearchInput.model_validate(request.arguments)
            output = tool.run(typed_input)
        except ValidationError as exc:
            self.harness.audit.write(
                event="tool_validation_error",
                tool=request.name,
                user_id=request.user_id,
            )
            return ToolCallResult(status="error", error=f"工具参数校验失败: {exc.error_count()} 处")
        except Exception as exc:  # noqa: BLE001 - 网关必须转为可审计的受控结果
            self.harness.audit.write(
                event="tool_error",
                tool=request.name,
                user_id=request.user_id,
                error_type=type(exc).__name__,
            )
            return ToolCallResult(status="error", error=f"工具执行失败: {type(exc).__name__}")

        self.harness.audit.write(
            event="tool_completed",
            tool=request.name,
            user_id=request.user_id,
            sources=len(output.sources),
        )
        return ToolCallResult(status="ok", output=output)
