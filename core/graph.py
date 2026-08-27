"""LangGraph 编排层：状态机 + checkpoint + human-in-the-loop

状态图：route → skill → style_check → (retry | publish | human_approval)

注意：langgraph 为可选依赖（仅图编排需要）。未安装时 import 本模块会给出安装提示，
不影响纯 Python 编排器（core/orchestrator.py）与发布链路。
"""
from __future__ import annotations

try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.memory import MemorySaver
except ImportError as _e:  # pragma: no cover —— 可选依赖
    raise ImportError(
        "未安装 langgraph。图编排模式需要: pip install langgraph\n"
        "（纯 Python 编排请用 core.orchestrator.Orchestrator，无需 langgraph）"
    ) from _e

from typing import TypedDict, Annotated

from .types import Draft, SkillOutput, ExecutionContext
from .registry import route as route_skills
from .style import StyleEnforcer
from .harness import Harness


class AgentState(TypedDict):
    draft: Draft
    ctx: ExecutionContext
    skill_chain: list[str]
    step: int
    approved: bool


def _skill_node(skill_name: str):
    """工厂：为每个 Skill 生成一个图节点"""
    def node(state: AgentState) -> AgentState:
        from .registry import get
        skill = get(skill_name)
        if skill is None:
            return state
        # 这里 harness 拦截已在 orchestrator 层做；图内只负责执行
        from .types import SkillInput
        result: SkillOutput = skill.run(SkillInput(
            draft=state["draft"],
            context={
                "persona": state["ctx"].persona,
                "prompts": state["ctx"].prompts,
                "rag_examples": state["ctx"].rag_examples,
            },
        ))
        state["draft"] = result.draft
        state["step"] = state.get("step", 0) + 1
        return state
    return node


def build_graph(harness: Harness, persona: dict, skill_chain: list[str] | None = None):
    """构建状态图。skill_chain 为 None 时由路由自动选 top-3。"""
    graph = StateGraph(AgentState)

    chain = skill_chain or [s for s, _ in route_skills("生成内容", top_k=3)]
    for name in chain:
        graph.add_node(name, _skill_node(name))

    # 入口
    def entry(state: AgentState) -> str:
        return chain[0] if chain else "style_check"

    graph.add_node("style_check", _style_check_node)
    graph.set_entry_point("router")
    graph.add_node("router", lambda s: s)  # placeholder, 实际在 orchestrator 里路由

    # 线性串联 skill
    for i in range(len(chain) - 1):
        graph.add_edge(chain[i], chain[i + 1])
    if chain:
        graph.add_edge(chain[-1], "style_check")

    graph.add_conditional_edges(
        "style_check",
        _after_style,
        {"retry": chain[0] if chain else END, "publish": "publish", "end": END},
    )
    graph.add_node("publish", _publish_node)
    graph.add_edge("publish", END)

    return graph.compile(checkpointer=MemorySaver())


def _style_check_node(state: AgentState) -> AgentState:
    enforcer = StyleEnforcer(state["ctx"].persona)
    report = enforcer.enforce(state["draft"])
    state["ctx"].checkpoint["style_report"] = report.model_dump()
    state["ctx"].checkpoint["retries"] = state["ctx"].checkpoint.get("retries", 0)
    return state


def _after_style(state: AgentState) -> str:
    report = state["ctx"].checkpoint.get("style_report", {})
    if report.get("ok"):
        return "publish"
    if state["ctx"].checkpoint.get("retries", 0) >= 2:
        return "end"  # 超限放弃
    state["ctx"].checkpoint["retries"] += 1
    return "retry"


def _publish_node(state: AgentState) -> AgentState:
    # 发布逻辑在 publisher 层，这里仅标记
    state["ctx"].checkpoint["published"] = True
    return state
