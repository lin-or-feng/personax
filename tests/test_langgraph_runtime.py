"""PersonaX 2.1 LangChain Core + LangGraph 可选编排引擎。"""
from __future__ import annotations

import skills  # noqa: F401 - 触发 Skill 注册
from core.graph import LangGraphOrchestrator
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


def test_langgraph_engine_runs_real_skill_nodes_through_harness(monkeypatch):
    from skills import content

    def fake_complete(prompt: str, **kwargs) -> str:
        if "正文" in prompt:
            return "LangGraph 已执行正文节点。"
        if "标签" in prompt:
            return "#LangGraph\n#Agent"
        return "PersonaX 2.1 LangGraph"

    monkeypatch.setattr(content, "complete", fake_complete)
    persona = _persona()
    harness = Harness(RuleConfig(**persona["harness"]))
    service = LangGraphOrchestrator(persona, harness)
    draft = service.run(
        "LangGraph 冒烟",
        user_id="graph-user",
        skill_chain=["title_generator", "body_writer", "tag_selector", "xhs_publish"],
        thread_id="graph-runtime-test",
    )

    assert draft.title and draft.body and draft.tags
    assert draft.metadata["orchestration_engine"] == "langgraph"
    assert draft.metadata["graph_steps"] == 4
    assert draft.metadata["publish_ready"] is True
    checkpoint = draft.metadata["graph_checkpoint"]
    assert checkpoint["graph_complete"] is True
    assert "published" not in checkpoint
    events = [item["event"] for item in harness.audit.entries]
    assert "approval_required" in events
    assert "langgraph_run_done" in events
