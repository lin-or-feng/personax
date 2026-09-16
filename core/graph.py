"""LangGraph 编排层：状态机 + 内存 checkpoint。

状态图：router → skill 链 → style_check → (retry | ready | end)
图中的 ``ready`` 只表示生成流程完成，不会调用真实 Publisher。

注意：langgraph 为可选依赖（仅图编排需要）。未安装时 import 本模块会给出安装提示，
不影响纯 Python 编排器（core/orchestrator.py）与发布链路。
"""
from __future__ import annotations

try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.memory import MemorySaver
    from langchain_core.runnables import RunnableLambda
except ImportError as _e:  # pragma: no cover —— 可选依赖
    raise ImportError(
        "未安装 langgraph。图编排模式需要: pip install langgraph\n"
        "（纯 Python 编排请用 core.orchestrator.Orchestrator，无需 langgraph）"
    ) from _e

import time
import uuid
from typing import TypedDict

from .types import Draft, SkillOutput, ExecutionContext
from .registry import route as route_skills
from .style import StyleEnforcer
from .harness import Harness
from .prompts import load_prompts
from .rag import build_rag_from_dir


class AgentState(TypedDict):
    draft: Draft
    ctx: ExecutionContext
    skill_chain: list[str]
    step: int
    approved: bool


def _skill_node(skill_name: str, harness: Harness):
    """工厂：为每个 Skill 生成一个图节点"""
    def node(state: AgentState) -> AgentState:
        from .registry import get
        skill = get(skill_name)
        if skill is None:
            harness.audit.write(event="skill_missing", skill=skill_name)
            return state
        allowed, reason = harness.guard(skill_name, state["ctx"].user_id)
        if not allowed:
            harness.audit.write(event="skill_skipped", skill=skill_name, reason=reason)
            return state
        if harness.needs_approval(skill_name):
            # Skill 层只做就绪检查，真实发布仍由 Publisher 的人工审批与安全锁负责。
            harness.audit.write(event="approval_required", skill=skill_name)
        from .types import SkillInput
        result: SkillOutput = skill.run(SkillInput(
            draft=state["draft"],
            context={
                "persona": state["ctx"].persona,
                "prompts": state["ctx"].prompts,
                "rag_examples": state["ctx"].rag_examples,
                "rag_references": state["ctx"].rag_references,
                "web_context": state["ctx"].web_context,
            },
        ))
        state["draft"] = result.draft
        state["step"] = state.get("step", 0) + 1
        harness.audit.write(event="skill_done", skill=skill_name, notes=result.notes)
        return state
    return node


def build_graph(harness: Harness, persona: dict, skill_chain: list[str] | None = None):
    """构建状态图。skill_chain 为 None 时由路由自动选 top-3。"""
    graph = StateGraph(AgentState)

    chain = skill_chain or [s for s, _ in route_skills("生成内容", top_k=3)]
    for name in chain:
        graph.add_node(
            name,
            RunnableLambda(_skill_node(name, harness), name=f"personax_skill_{name}"),
        )

    graph.add_node("style_check", RunnableLambda(_style_check_node, name="personax_style_check"))
    graph.set_entry_point("router")
    graph.add_node("router", RunnableLambda(lambda state: state, name="personax_router"))
    graph.add_edge("router", chain[0] if chain else "style_check")

    # 线性串联 skill
    for i in range(len(chain) - 1):
        graph.add_edge(chain[i], chain[i + 1])
    if chain:
        graph.add_edge(chain[-1], "style_check")

    retry_target = "body_writer" if "body_writer" in chain else (chain[0] if chain else END)
    graph.add_conditional_edges(
        "style_check",
        _after_style(int((persona.get("harness", {}) or {}).get("max_retries", 2)), bool(chain)),
        {"retry": retry_target, "ready": "ready", "end": END},
    )
    graph.add_node("ready", RunnableLambda(_ready_node, name="personax_ready"))
    graph.add_edge("ready", END)

    return graph.compile(checkpointer=MemorySaver())


def _style_check_node(state: AgentState) -> AgentState:
    enforcer = StyleEnforcer(state["ctx"].persona)
    report = enforcer.enforce(state["draft"])
    state["ctx"].checkpoint["style_report"] = report.model_dump()
    state["ctx"].checkpoint["retries"] = state["ctx"].checkpoint.get("retries", 0)
    return state


def _after_style(max_retries: int, has_chain: bool):
    def route(state: AgentState) -> str:
        report = state["ctx"].checkpoint.get("style_report", {})
        if report.get("ok"):
            return "ready"
        if not has_chain or state["ctx"].checkpoint.get("retries", 0) >= max_retries:
            return "end"
        state["ctx"].checkpoint["retries"] += 1
        return "retry"

    return route


def _ready_node(state: AgentState) -> AgentState:
    # 不得标记 published：图只完成生成/检查，没有发生平台副作用。
    state["ctx"].checkpoint["graph_complete"] = True
    return state


class LangGraphOrchestrator:
    """可选的真实 LangGraph 执行器；与默认纯 Python 编排共用 Skill/Harness。"""

    def __init__(self, persona: dict, harness: Harness):
        self.persona = persona
        self.harness = harness
        self.audit = harness.audit

    def run(
        self,
        topic: str,
        user_id: str | None = None,
        skill_chain: list[str] | None = None,
        thread_id: str | None = None,
    ) -> Draft:
        started = time.perf_counter()
        draft = Draft(topic=topic)
        prompts = load_prompts(self.persona.get("prompts_path", "config/prompts.yaml"))
        rag_cfg = self.persona.get("rag", {}) or {}
        rag_pipe = build_rag_from_dir(
            rag_cfg.get("knowledge_dir", "knowledge"),
            chunk_size=int(rag_cfg.get("chunk_size", 600)),
            embedding_backend=rag_cfg.get("embedding_backend"),
            embedding_model=rag_cfg.get("embedding_model"),
            query_enhancer=rag_cfg.get("query_enhancer"),
            enable_hyde=bool(rag_cfg.get("enable_hyde", False)),
            reranker=rag_cfg.get("reranker"),
            reranker_model=rag_cfg.get("reranker_model"),
        )
        rag_chunks = (
            rag_pipe.retrieve_relevant(
                topic,
                top_k=int(rag_cfg.get("top_k", 2)),
                min_score=float(rag_cfg.get("min_score", 0.10)),
            )
            if rag_pipe.store.chunks else []
        )
        rag_examples = [
            chunk.text for chunk in rag_chunks
            if (chunk.metadata or {}).get("retrieval_role", "example") != "reference"
        ]
        rag_references = [
            chunk.text for chunk in rag_chunks
            if (chunk.metadata or {}).get("retrieval_role") == "reference"
        ]
        rag_pipe.last_trace["roles"] = {
            "examples": len(rag_examples),
            "references": len(rag_references),
        }
        draft.metadata["rag_trace"] = rag_pipe.last_trace

        from .websearch import build_web_context

        context = ExecutionContext(
            run_id=(thread_id or str(uuid.uuid4()))[:64],
            persona=self.persona,
            user_id=user_id,
            prompts=prompts,
            rag_examples=rag_examples,
            rag_references=rag_references,
            web_context=build_web_context(topic),
        )
        chain = skill_chain or [name for name, _ in route_skills(topic, top_k=3)]
        graph = build_graph(self.harness, self.persona, chain)
        result = graph.invoke(
            {
                "draft": draft,
                "ctx": context,
                "skill_chain": chain,
                "step": 0,
                "approved": False,
            },
            config={"configurable": {"thread_id": context.run_id}},
        )
        final_draft: Draft = result["draft"]
        final_draft.metadata["orchestration_engine"] = "langgraph"
        final_draft.metadata["graph_steps"] = int(result.get("step", 0))
        final_draft.metadata["graph_checkpoint"] = dict(result["ctx"].checkpoint)
        self.audit.write(
            event="langgraph_run_done",
            thread_id=context.run_id,
            steps=final_draft.metadata["graph_steps"],
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return final_draft
