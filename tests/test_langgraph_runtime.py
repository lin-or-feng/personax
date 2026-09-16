"""PersonaX 2.2 LangGraph 持久化编排与可观测性。"""
from __future__ import annotations

import skills  # noqa: F401 - 触发 Skill 注册
from core.graph import GraphRunStore, LangGraphOrchestrator
from core.harness import Harness, RuleConfig


def _persona() -> dict:
    return {
        "name": "测试助手",
        "tone": "clear",
        "habits": [],
        "forbidden": [],
        "sentence_length": "long",
        "generation": {"model": "offline", "temperature": 0.0, "max_tokens": 128},
        "rag": {
            "knowledge_dir": "pxtest_missing_knowledge",
            "embedding_backend": "hashing",
            "top_k": 1,
            "min_score": 0.1,
        },
        "harness": {
            "rate_limit": 20,
            "max_retries": 1,
            "require_approval": True,
            "sensitive_tools": ["xhs_publish"],
        },
    }


def test_langgraph_engine_runs_real_skill_nodes_through_harness(monkeypatch, tmp_path):
    from skills import content

    def fake_complete(prompt: str, **kwargs) -> str:
        if "正文" in prompt:
            return "LangGraph 已执行正文节点。"
        if "标签" in prompt:
            return "#LangGraph\n#Agent"
        return "PersonaX 2.2 LangGraph"

    monkeypatch.setattr(content, "complete", fake_complete)
    persona = _persona()
    harness = Harness(RuleConfig(**persona["harness"]))
    checkpoint_path = tmp_path / "langgraph.sqlite3"
    service = LangGraphOrchestrator(persona, harness, checkpoint_path=checkpoint_path)
    draft = service.run(
        "LangGraph 冒烟",
        user_id="graph-user",
        skill_chain=["title_generator", "body_writer", "tag_selector", "xhs_publish"],
        thread_id="graph-runtime-test",
    )

    assert draft.title and draft.body and draft.tags
    assert draft.metadata["orchestration_engine"] == "langgraph"
    assert draft.metadata["graph_checkpoint_backend"] == "sqlite"
    assert draft.metadata["graph_checkpoint_count"] >= 1
    assert draft.metadata["graph_checkpoint_id"]
    assert draft.metadata["graph_status"] == "completed"
    assert draft.metadata["graph_steps"] == 4
    assert draft.metadata["publish_ready"] is True
    checkpoint = draft.metadata["graph_checkpoint"]
    assert checkpoint["graph_complete"] is True
    assert "published" not in checkpoint
    trace = draft.metadata["graph_trace"]
    assert [item["node"] for item in trace][:2] == ["router", "title_generator"]
    assert trace[-1]["node"] == "ready"
    assert all(item["duration_ms"] >= 0 for item in trace)
    persisted = GraphRunStore(checkpoint_path).list_recent()
    assert len(persisted) == 1
    assert persisted[0].thread_id == "graph-runtime-test"
    assert persisted[0].status == "completed"
    events = [item["event"] for item in harness.audit.entries]
    assert "langgraph_run_started" in events
    assert "approval_required" in events
    assert "langgraph_run_done" in events


def test_graph_run_store_persists_and_prunes_without_content(tmp_path):
    from core.graph import GraphRunSummary, WorkflowTraceStep

    store = GraphRunStore(tmp_path / "runs.sqlite3")
    for index in range(3):
        store.save(GraphRunSummary(
            thread_id=f"run-{index}",
            topic=f"主题 {index}",
            status="completed",
            steps=1,
            trace=[WorkflowTraceStep(index=1, node="ready")],
            updated_at=float(index),
        ))

    assert [item.thread_id for item in store.list_recent()] == ["run-2", "run-1", "run-0"]
    assert store.prune(keep=2) == ["run-0"]
    assert [item.thread_id for item in store.list_recent()] == ["run-2", "run-1"]


def test_final_style_gate_overrides_earlier_publish_ready(monkeypatch, tmp_path):
    from skills import content

    monkeypatch.setattr(content, "complete", lambda *_args, **_kwargs: "禁用内容")
    persona = _persona()
    persona["forbidden"] = ["禁用内容"]
    persona["harness"]["max_retries"] = 0
    service = LangGraphOrchestrator(
        persona,
        Harness(RuleConfig(**persona["harness"])),
        checkpoint_path=tmp_path / "blocked.sqlite3",
    )
    draft = service.run(
        "风格门禁",
        user_id="graph-user",
        skill_chain=["title_generator", "body_writer", "tag_selector", "xhs_publish"],
        thread_id="blocked-style",
    )

    assert draft.metadata["graph_status"] == "blocked"
    assert draft.metadata["publish_ready"] is False
    assert any("禁用词" in issue for issue in draft.metadata["publish_issues"])
    assert draft.metadata["graph_trace"][-1]["status"] == "blocked"


def test_failed_graph_persists_last_checkpoint_trace(monkeypatch, tmp_path):
    import pytest
    from skills import content

    def fail_complete(*_args, **_kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(content, "complete", fail_complete)
    checkpoint_path = tmp_path / "failed.sqlite3"
    persona = _persona()
    service = LangGraphOrchestrator(
        persona,
        Harness(RuleConfig(**persona["harness"])),
        checkpoint_path=checkpoint_path,
    )

    with pytest.raises(RuntimeError, match="synthetic failure"):
        service.run(
            "失败诊断",
            skill_chain=["title_generator"],
            thread_id="failed-run",
        )

    summary = GraphRunStore(checkpoint_path).list_recent()[0]
    assert summary.status == "error"
    assert summary.error.startswith("RuntimeError")
    assert [item.node for item in summary.trace] == ["router"]
