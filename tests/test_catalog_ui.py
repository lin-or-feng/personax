"""Native Streamlit AppTest, isolated catalog, no model/browser/platform calls."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="optional UI tests require installation with .[web]")

from core.knowledge import KnowledgeBaseService
from core.knowledge_graph import KnowledgeGraphService
from core.types import KnowledgeImportRequest, KnowledgeRelationInput

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ui_catalog(tmp_path, monkeypatch):
    from core import llm
    monkeypatch.setenv("LLM_BACKEND", "offline")
    monkeypatch.setattr(llm, "_OVERRIDES", {})
    monkeypatch.setenv("PERSONAX_KNOWLEDGE_DB", str(tmp_path / "catalog.db"))
    root = tmp_path / "sources"
    root.mkdir()
    (root / "guide.md").write_text("# QuartzToken\nQuartzToken uses SQLite transactions.", encoding="utf-8")
    service = KnowledgeBaseService(tmp_path / "catalog.db", root)
    imported = service.ingest(KnowledgeImportRequest(source_path="guide.md"))
    doc = service.store.get("ai-engineering", imported.document_id)
    KnowledgeGraphService(service.store.path).add(KnowledgeRelationInput(kb_id="ai-engineering",
        subject="QuartzToken", object="SQLite", document_id=doc.document_id,
        document_version=doc.document_version, chunk_id=doc.chunks[0].chunk_id, evidence=doc.chunks[0].text))
    return service


def test_streamlit_catalog_answer_and_source_switch(ui_catalog):
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    app.session_state["nav"] = "assistant"
    app.session_state["assistant_use_memory"] = False
    app.run()
    assert not app.exception
    assert app.selectbox(key="assistant_knowledge_source").value == "catalog"
    app.chat_input[0].set_value("QuartzToken").run()
    assert not app.exception
    message = app.session_state["assistant_messages"][-1]
    assert message["answer_mode"] == "extractive_baseline" and message["sources"]
    assert message["index_version"] and message["sources"][0]["line_start"]
    app.selectbox(key="assistant_knowledge_source").select("legacy").run()
    assert not app.exception and not app.session_state["assistant_messages"]


def test_streamlit_catalog_browser_and_evidence_graph(ui_catalog):
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    app.session_state["nav"] = "knowledge"
    app.run()
    assert not app.exception and app.dataframe
    entity_input = next(x for x in app.text_input if x.label == "实体关联查询")
    entity_input.set_value("QuartzToken")
    next(x for x in app.button if x.label == "查询证据路径").click().run()
    assert not app.exception
    assert any("关联" in x.value for x in app.text)


def test_streamlit_agent_mode_runs_real_gateway_and_renders_stop_reason(ui_catalog, monkeypatch):
    from core.assistant import AssistantOrchestrator
    from streamlit.testing.v1 import AppTest
    original_init = AssistantOrchestrator.__init__
    async def model(prompt, **kwargs):
        if "QuartzToken uses SQLite" in prompt:
            return '{"action":"finish"}'
        return '{"action":"search","query":"QuartzToken"}'
    def init(self, *args, **kwargs):
        kwargs["acomplete_fn"] = model
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(AssistantOrchestrator, "__init__", init)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    app.session_state["nav"] = "assistant"
    app.session_state["assistant_use_memory"] = False
    app.run()
    app.segmented_control(key="assistant_execution_mode").set_value("agent").run()
    app.chat_input[0].set_value("QuartzToken").run()
    assert not app.exception
    message = app.session_state["assistant_messages"][-1]
    assert message["execution_mode"] == "agent" and message["stop_reason"] == "evidence_ready"
    assert message["sources"] and any("停止原因 evidence_ready" in x.value for x in app.caption)
    app.segmented_control(key="assistant_execution_mode").set_value("workflow").run()
    assert not app.exception and not app.session_state["assistant_messages"]
