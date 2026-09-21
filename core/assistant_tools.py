"""AI 助手的类型化工具边界。

工具本身只提供能力；是否允许调用、限流和审计由上层 Harness 统一处理。
Pydantic 输入/输出 Schema 可被后续 MCP adapter 直接复用。
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from .rag import RAGPipeline
from .types import (
    AssistantSource,
    KnowledgeSearchInput,
    KnowledgeSearchOutput,
    ToolManifest,
)


class KnowledgeSearchTool:
    """在本地 PersonaX 知识库中执行带门控的混合检索。"""

    name = "assistant_knowledge_search"

    def __init__(self, pipeline: RAGPipeline):
        self.pipeline = pipeline

    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name=self.name,
            description="检索 PersonaX 本地知识库，返回可追溯的参考片段；不执行发布。",
            sensitive=False,
            input_schema=KnowledgeSearchInput.model_json_schema(),
            output_schema=KnowledgeSearchOutput.model_json_schema(),
        )

    def run(self, inp: KnowledgeSearchInput) -> KnowledgeSearchOutput:
        hits = self.pipeline.retrieve_hits(inp.query, top_k=max(inp.top_k * 3, inp.top_k))
        kept = []
        seen_sources: set[str] = set()
        for hit in hits:
            if max(hit.lexical_score, hit.dense_score) < inp.min_score:
                continue
            source_key = str(
                (hit.chunk.metadata or {}).get("source") or hit.chunk.id
            )
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)
            kept.append(hit)
            if len(kept) >= inp.top_k:
                break
        sources: list[AssistantSource] = []
        for index, hit in enumerate(kept, 1):
            metadata: dict[str, Any] = hit.chunk.metadata or {}
            source_path = str(metadata.get("source") or "")
            title = str(
                metadata.get("topic")
                or metadata.get("source_name")
                or source_path
                or hit.chunk.summary
                or f"知识片段 {index}"
            )
            excerpt = " ".join(hit.chunk.text.split())[:360]
            sources.append(AssistantSource(
                ref_id=str(index),
                title=sanitize_source_text(title, fallback=f"知识片段 {index}"),
                source=sanitize_source_text(source_path),
                source_url=sanitize_source_url(metadata.get("source_url")),
                excerpt=excerpt,
                score=round(max(hit.lexical_score, hit.dense_score), 4),
            ))
        trace = dict(self.pipeline.last_trace)
        trace.update(min_score=inp.min_score, returned=len(sources))
        return KnowledgeSearchOutput(sources=sources, trace=trace)


def tool_manifests(tool: KnowledgeSearchTool) -> list[ToolManifest]:
    """集中暴露工具清单，供 UI、测试和未来 MCP server 发现。"""

    return [tool.manifest]


def sanitize_source_url(value: Any) -> str:
    """只允许可明确展示的 HTTP(S) 来源链接。

    知识库 front-matter 是本地可编辑数据，不应被默认信任为
    安全 URL。拒绝 javascript/file/data 等协议与内嵌账号信息。
    """

    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 2_048:
        return ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.username or parsed.password:
        return ""
    return candidate


def sanitize_source_text(value: Any, *, fallback: str = "") -> str:
    """压平来源标题/路径，避免未受信内容破坏 Markdown 链接标签。"""

    text = re.sub(r"\s+", " ", str(value or "")).strip()[:240]
    return text.translate(str.maketrans({"[": "［", "]": "］", "(": "（", ")": "）"})) or fallback
