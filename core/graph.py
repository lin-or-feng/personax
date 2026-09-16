"""PersonaX LangGraph 编排层：可恢复状态机、持久化 checkpoint 与执行轨迹。

状态图：router → skill 链 → style_check → (retry | ready | end)
图中的 ``ready`` 只表示生成流程完成，不会调用真实 Publisher。

2.2 默认使用官方 ``SqliteSaver`` 保存本地 checkpoint。写入图状态前会把
Pydantic 契约转为 JSON 兼容字典，并显式启用严格反序列化白名单，避免 checkpoint
数据库被篡改时加载任意 Python 类型。纯 Python 编排器仍位于 ``core/orchestrator.py``。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Literal, TypedDict

try:
    from langchain_core.runnables import RunnableLambda
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, StateGraph
except ImportError as _e:  # pragma: no cover - 依赖缺失时给出明确修复方式
    raise ImportError(
        "未安装 LangGraph SQLite 运行时。请执行: "
        "pip install langgraph langchain-core langgraph-checkpoint-sqlite\n"
        "（纯 Python 编排请用 core.orchestrator.Orchestrator）"
    ) from _e

from pydantic import BaseModel, Field

from .harness import Harness
from .prompts import load_prompts
from .rag import build_rag_from_dir
from .registry import route as route_skills
from .style import StyleEnforcer
from .types import Draft, ExecutionContext, SkillInput, SkillOutput


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHECKPOINT_PATH = PROJECT_ROOT / "logs" / "langgraph_checkpoints.sqlite3"


class WorkflowTraceStep(BaseModel):
    """内容工作流单个节点的可序列化执行记录。"""

    index: int = Field(ge=1)
    node: str
    status: Literal["ok", "retry", "blocked", "skipped", "error"] = "ok"
    duration_ms: float = Field(default=0.0, ge=0.0)
    detail: str = ""


class GraphRunSummary(BaseModel):
    """只保存诊断元数据，不保存完整提示词、正文或密钥。"""

    thread_id: str
    topic: str
    status: Literal["completed", "blocked", "error"]
    steps: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    duration_ms: float = Field(default=0.0, ge=0.0)
    checkpoint_count: int = Field(default=0, ge=0)
    checkpoint_id: str = ""
    trace: list[WorkflowTraceStep] = Field(default_factory=list)
    error: str = ""
    updated_at: float = Field(default_factory=time.time)


class GraphRunStore:
    """PersonaX 自有的轻量运行索引；LangGraph checkpoint 仍由 SqliteSaver 管理。"""

    def __init__(self, path: str | Path = DEFAULT_CHECKPOINT_PATH):
        self.path = _resolve_checkpoint_path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS personax_graph_runs ("
            "thread_id TEXT PRIMARY KEY, topic TEXT NOT NULL, status TEXT NOT NULL, "
            "steps INTEGER NOT NULL, retries INTEGER NOT NULL, duration_ms REAL NOT NULL, "
            "checkpoint_count INTEGER NOT NULL, checkpoint_id TEXT NOT NULL, "
            "trace_json TEXT NOT NULL, error TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        return db

    def save(self, summary: GraphRunSummary) -> None:
        trace_json = json.dumps(
            [item.model_dump(mode="json") for item in summary.trace],
            ensure_ascii=False,
        )
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO personax_graph_runs "
                "(thread_id, topic, status, steps, retries, duration_ms, checkpoint_count, "
                "checkpoint_id, trace_json, error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    summary.thread_id,
                    summary.topic,
                    summary.status,
                    summary.steps,
                    summary.retries,
                    summary.duration_ms,
                    summary.checkpoint_count,
                    summary.checkpoint_id,
                    trace_json,
                    summary.error,
                    summary.updated_at,
                ),
            )
            db.commit()

    def list_recent(self, limit: int = 20) -> list[GraphRunSummary]:
        safe_limit = max(1, min(int(limit), 200))
        if not self.path.exists():
            return []
        with self._connect() as db:
            rows = db.execute(
                "SELECT thread_id, topic, status, steps, retries, duration_ms, "
                "checkpoint_count, checkpoint_id, trace_json, error, updated_at "
                "FROM personax_graph_runs ORDER BY updated_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        summaries: list[GraphRunSummary] = []
        for row in rows:
            try:
                trace = [WorkflowTraceStep.model_validate(item) for item in json.loads(row[8])]
                summaries.append(GraphRunSummary(
                    thread_id=row[0], topic=row[1], status=row[2], steps=row[3],
                    retries=row[4], duration_ms=row[5], checkpoint_count=row[6],
                    checkpoint_id=row[7], trace=trace, error=row[9], updated_at=row[10],
                ))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return summaries

    def prune(self, keep: int = 100) -> list[str]:
        """保留最近 N 次运行，返回需要同步删除 LangGraph checkpoint 的 thread id。"""

        safe_keep = max(1, min(int(keep), 10_000))
        if not self.path.exists():
            return []
        with self._connect() as db:
            rows = db.execute(
                "SELECT thread_id FROM personax_graph_runs "
                "ORDER BY updated_at DESC LIMIT -1 OFFSET ?",
                (safe_keep,),
            ).fetchall()
            stale = [str(row[0]) for row in rows]
            if stale:
                db.executemany(
                    "DELETE FROM personax_graph_runs WHERE thread_id = ?",
                    [(thread_id,) for thread_id in stale],
                )
                db.commit()
        return stale


class AgentState(TypedDict):
    # checkpoint 场景按 SPEC 允许使用裸 dict；节点边界会重新验证为正式契约。
    draft: dict[str, Any]
    ctx: dict[str, Any]
    skill_chain: list[str]
    step: int
    approved: bool
    next_action: str
    trace: list[dict[str, Any]]


def _resolve_checkpoint_path(path: str | Path) -> Path:
    resolved = Path(path)
    return resolved if resolved.is_absolute() else PROJECT_ROOT / resolved


def _append_trace(
    state: AgentState,
    node: str,
    *,
    status: Literal["ok", "retry", "blocked", "skipped", "error"] = "ok",
    duration_ms: float = 0.0,
    detail: str = "",
) -> None:
    trace = list(state.get("trace", []))
    item = WorkflowTraceStep(
        index=len(trace) + 1,
        node=node,
        status=status,
        duration_ms=round(max(0.0, duration_ms), 1),
        detail=detail[:500],
    )
    trace.append(item.model_dump(mode="json"))
    state["trace"] = trace


def _skill_node(skill_name: str, harness: Harness):
    """为每个 Skill 生成节点；每次执行都经过 Harness 并记录结构化 Trace。"""

    def node(state: AgentState) -> AgentState:
        from .registry import get

        started = time.perf_counter()
        skill = get(skill_name)
        if skill is None:
            harness.audit.write(event="skill_missing", skill=skill_name)
            _append_trace(state, skill_name, status="skipped", detail="skill 未注册")
            return state

        context = ExecutionContext.model_validate(state["ctx"])
        allowed, reason = harness.guard(skill_name, context.user_id)
        if not allowed:
            harness.audit.write(event="skill_skipped", skill=skill_name, reason=reason)
            _append_trace(
                state,
                skill_name,
                status="blocked",
                duration_ms=(time.perf_counter() - started) * 1000,
                detail=reason,
            )
            return state

        if harness.needs_approval(skill_name):
            # Skill 层只做就绪检查，真实发布仍由 Publisher 的人工审批与安全锁负责。
            harness.audit.write(event="approval_required", skill=skill_name)

        draft = Draft.model_validate(state["draft"])
        result: SkillOutput = skill.run(SkillInput(
            draft=draft,
            context={
                "persona": context.persona,
                "prompts": context.prompts,
                "rag_examples": context.rag_examples,
                "rag_references": context.rag_references,
                "web_context": context.web_context,
            },
        ))
        state["draft"] = result.draft.model_dump(mode="json")
        state["step"] = state.get("step", 0) + 1
        duration_ms = (time.perf_counter() - started) * 1000
        notes = "; ".join(result.notes) if result.notes else "完成"
        _append_trace(state, skill_name, duration_ms=duration_ms, detail=notes)
        harness.audit.write(
            event="skill_done",
            skill=skill_name,
            notes=result.notes,
            duration_ms=round(duration_ms, 1),
        )
        return state

    return node


def _router_node(state: AgentState) -> AgentState:
    _append_trace(state, "router", detail=" -> ".join(state.get("skill_chain", [])))
    return state


def _style_check_node(max_retries: int, has_chain: bool):
    def node(state: AgentState) -> AgentState:
        started = time.perf_counter()
        draft = Draft.model_validate(state["draft"])
        context = ExecutionContext.model_validate(state["ctx"])
        report = StyleEnforcer(context.persona).enforce(draft)
        context.checkpoint["style_report"] = report.model_dump(mode="json")
        retries = int(context.checkpoint.get("retries", 0))
        if report.ok:
            action = "ready"
            status = "ok"
            detail = "风格硬约束通过"
        elif has_chain and retries < max_retries:
            retries += 1
            context.checkpoint["retries"] = retries
            action = "retry"
            status = "retry"
            detail = f"第 {retries}/{max_retries} 次重写：{'；'.join(report.issues[:3])}"
        else:
            action = "end"
            status = "blocked"
            detail = f"重试耗尽：{'；'.join(report.issues[:3])}"
        if report.ok:
            # 发布 Skill 已完成字段完整性检查；风格通过后保留其结论。
            draft.metadata.setdefault("publish_issues", [])
        else:
            # 最终风格门禁拥有最后决定权，不能残留前序 publish_ready=True。
            draft.metadata["publish_ready"] = False
            draft.metadata["publish_issues"] = list(dict.fromkeys(
                list(draft.metadata.get("publish_issues", [])) + list(report.issues)
            ))
        state["draft"] = draft.model_dump(mode="json")
        state["ctx"] = context.model_dump(mode="json")
        state["next_action"] = action
        _append_trace(
            state,
            "style_check",
            status=status,
            duration_ms=(time.perf_counter() - started) * 1000,
            detail=detail,
        )
        return state

    return node


def _after_style(state: AgentState) -> str:
    return str(state.get("next_action") or "end")


def _ready_node(state: AgentState) -> AgentState:
    context = ExecutionContext.model_validate(state["ctx"])
    # 不得标记 published：图只完成生成/检查，没有发生平台副作用。
    context.checkpoint["graph_complete"] = True
    state["ctx"] = context.model_dump(mode="json")
    _append_trace(state, "ready", detail="生成链完成；未执行真实发布")
    return state


def build_graph(
    harness: Harness,
    persona: dict,
    skill_chain: list[str] | None = None,
    *,
    checkpointer: Any | None = None,
):
    """构建状态图；直接调用时默认内存 saver，正式执行器会注入 SQLite saver。"""

    graph = StateGraph(AgentState)
    chain = skill_chain or [skill for skill, _ in route_skills("生成内容", top_k=3)]

    graph.add_node("router", RunnableLambda(_router_node, name="personax_router"))
    for name in chain:
        graph.add_node(
            name,
            RunnableLambda(_skill_node(name, harness), name=f"personax_skill_{name}"),
        )

    max_retries = int((persona.get("harness", {}) or {}).get("max_retries", 2))
    graph.add_node(
        "style_check",
        RunnableLambda(
            _style_check_node(max_retries=max_retries, has_chain=bool(chain)),
            name="personax_style_check",
        ),
    )
    graph.add_node("ready", RunnableLambda(_ready_node, name="personax_ready"))
    graph.set_entry_point("router")
    graph.add_edge("router", chain[0] if chain else "style_check")

    for index in range(len(chain) - 1):
        graph.add_edge(chain[index], chain[index + 1])
    if chain:
        graph.add_edge(chain[-1], "style_check")

    retry_target = "body_writer" if "body_writer" in chain else (chain[0] if chain else END)
    graph.add_conditional_edges(
        "style_check",
        _after_style,
        {"retry": retry_target, "ready": "ready", "end": END},
    )
    graph.add_edge("ready", END)
    return graph.compile(checkpointer=checkpointer or MemorySaver())


def _strict_sqlite_saver(connection: sqlite3.Connection) -> SqliteSaver:
    """只允许安全内置类型；图状态已在节点边界转换为 JSON 兼容字典。"""

    serializer = JsonPlusSerializer(
        pickle_fallback=False,
        allowed_json_modules=None,
        allowed_msgpack_modules=None,
    )
    return SqliteSaver(connection, serde=serializer)


class LangGraphOrchestrator:
    """LangGraph 执行器；共用 Skill/Harness，并持久化本地 checkpoint 与运行摘要。"""

    def __init__(
        self,
        persona: dict,
        harness: Harness,
        checkpoint_path: str | Path | None = None,
    ):
        self.persona = persona
        self.harness = harness
        self.audit = harness.audit
        graph_cfg = persona.get("langgraph", {}) or {}
        configured_path = checkpoint_path or graph_cfg.get(
            "checkpoint_path", DEFAULT_CHECKPOINT_PATH)
        self.checkpoint_path = _resolve_checkpoint_path(configured_path)
        self.retention_runs = max(1, int(graph_cfg.get("retention_runs", 100)))
        self.run_store = GraphRunStore(self.checkpoint_path)

    def _rag_context(self, topic: str, draft: Draft) -> tuple[list[str], list[str]]:
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
        examples = [
            chunk.text for chunk in rag_chunks
            if (chunk.metadata or {}).get("retrieval_role", "example") != "reference"
        ]
        references = [
            chunk.text for chunk in rag_chunks
            if (chunk.metadata or {}).get("retrieval_role") == "reference"
        ]
        rag_pipe.last_trace["roles"] = {
            "examples": len(examples),
            "references": len(references),
        }
        draft.metadata["rag_trace"] = rag_pipe.last_trace
        return examples, references

    def _prune_checkpoints(self) -> None:
        stale_threads = self.run_store.prune(self.retention_runs)
        if not stale_threads:
            return
        connection = sqlite3.connect(self.checkpoint_path, check_same_thread=False)
        try:
            saver = _strict_sqlite_saver(connection)
            for thread_id in stale_threads:
                saver.delete_thread(thread_id)
        finally:
            connection.close()

    def run(
        self,
        topic: str,
        user_id: str | None = None,
        skill_chain: list[str] | None = None,
        thread_id: str | None = None,
    ) -> Draft:
        started = time.perf_counter()
        run_id = (thread_id or f"graph-{uuid.uuid4().hex[:16]}")[:64]
        draft = Draft(topic=topic)
        trace: list[WorkflowTraceStep] = []
        self.audit.write(event="langgraph_run_started", thread_id=run_id, topic=topic[:120])

        try:
            prompts = load_prompts(self.persona.get("prompts_path", "config/prompts.yaml"))
            rag_examples, rag_references = self._rag_context(topic, draft)
            from .websearch import build_web_context

            context = ExecutionContext(
                run_id=run_id,
                persona=self.persona,
                user_id=user_id,
                prompts=prompts,
                rag_examples=rag_examples,
                rag_references=rag_references,
                web_context=build_web_context(topic),
            )
            chain = skill_chain or [name for name, _ in route_skills(topic, top_k=3)]
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.checkpoint_path, check_same_thread=False)
            try:
                saver = _strict_sqlite_saver(connection)
                graph = build_graph(
                    self.harness,
                    self.persona,
                    chain,
                    checkpointer=saver,
                )
                config = {"configurable": {"thread_id": run_id}}
                initial_state: AgentState = {
                    "draft": draft.model_dump(mode="json"),
                    "ctx": context.model_dump(mode="json"),
                    "skill_chain": chain,
                    "step": 0,
                    "approved": False,
                    "next_action": "",
                    "trace": [],
                }
                try:
                    result = graph.invoke(initial_state, config=config)
                except Exception:
                    # LangGraph 已提交的最近 checkpoint 仍可用于失败诊断。
                    snapshot = graph.get_state(config)
                    trace = [
                        WorkflowTraceStep.model_validate(item)
                        for item in (snapshot.values or {}).get("trace", [])
                    ]
                    raise
                history = list(graph.get_state_history(config))
                checkpoint_id = ""
                if history:
                    checkpoint_id = str(
                        history[0].config.get("configurable", {}).get("checkpoint_id", "")
                    )
            finally:
                connection.close()

            final_draft = Draft.model_validate(result["draft"])
            final_context = ExecutionContext.model_validate(result["ctx"])
            trace = [WorkflowTraceStep.model_validate(item) for item in result.get("trace", [])]
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            checkpoint = dict(final_context.checkpoint)
            status: Literal["completed", "blocked", "error"] = (
                "completed" if checkpoint.get("graph_complete") else "blocked"
            )
            final_draft.metadata.update({
                "orchestration_engine": "langgraph",
                "graph_thread_id": run_id,
                "graph_steps": int(result.get("step", 0)),
                "graph_checkpoint": checkpoint,
                "graph_checkpoint_backend": "sqlite",
                "graph_checkpoint_count": len(history),
                "graph_checkpoint_id": checkpoint_id,
                "graph_trace": [item.model_dump(mode="json") for item in trace],
                "graph_duration_ms": duration_ms,
                "graph_status": status,
            })
            self.run_store.save(GraphRunSummary(
                thread_id=run_id,
                topic=topic[:300],
                status=status,
                steps=final_draft.metadata["graph_steps"],
                retries=int(checkpoint.get("retries", 0)),
                duration_ms=duration_ms,
                checkpoint_count=len(history),
                checkpoint_id=checkpoint_id,
                trace=trace,
            ))
            self._prune_checkpoints()
            self.audit.write(
                event="langgraph_run_done",
                thread_id=run_id,
                status=status,
                steps=final_draft.metadata["graph_steps"],
                duration_ms=duration_ms,
                checkpoint_count=len(history),
            )
            return final_draft
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            error = f"{type(exc).__name__}: {exc}"[:500]
            self.run_store.save(GraphRunSummary(
                thread_id=run_id,
                topic=topic[:300],
                status="error",
                steps=sum(1 for item in trace if item.node not in {"router", "ready"}),
                duration_ms=duration_ms,
                trace=trace,
                error=error,
            ))
            self.audit.write(
                event="langgraph_run_failed",
                thread_id=run_id,
                duration_ms=duration_ms,
                error=error,
            )
            raise
