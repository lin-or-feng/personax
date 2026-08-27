import pytest
from core.types import Draft, SkillInput
from core.registry import route, all_skills, get, register, Skill
from core.style import StyleEnforcer
from core.harness import Harness, RuleConfig
from core.orchestrator import Orchestrator
from publishers.xhs import DryRunPublisher


@pytest.fixture()
def persona():
    return {
        "name": "小鹿学姐", "tone": "warm_girly",
        "habits": ["每3句一个emoji"], "forbidden": ["综上所述", "IaaS"],
        "sentence_length": "short",
        "harness": {"max_retries": 1, "rate_limit": 10, "require_approval": False},
    }


def test_skill_registry():
    assert "title_generator" in all_skills()
    assert "body_writer" in all_skills()
    res = route("帮我写个标题", top_k=2)
    assert len(res) > 0
    assert res[0][0] in all_skills()


def test_style_enforcer_blocks_forbidden(persona):
    e = StyleEnforcer(persona)
    rep = e.check("综上所述这个方法很好")
    assert not rep.ok
    assert any("禁用词" in i for i in rep.issues)


def test_style_enforcer_passes(persona):
    e = StyleEnforcer(persona)
    rep = e.check("家人们谁懂啊，这个真的绝了🍃")
    assert rep.ok


def test_harness_deny():
    cfg = RuleConfig(denied_skills=["xhs_publish"], allow_all=True)
    h = Harness(cfg)
    ok, _ = h.check_allow("xhs_publish")
    assert not ok


def test_harness_quota():
    cfg = RuleConfig(rate_limit=2)
    h = Harness(cfg)
    assert h.check_quota("u")[0]
    assert h.check_quota("u")[0]
    assert not h.check_quota("u")[0]   # 第3次超限


def test_orchestrator_full_pipeline(persona):
    orch = Orchestrator(persona=persona, harness=Harness(RuleConfig(**persona["harness"])))
    draft = orch.run("秋招穿搭")
    assert draft.title
    assert draft.body
    assert len(draft.tags) > 0


def test_publisher_dry_run():
    draft = Draft(topic="测试", title="测试标题", body="正文")
    res = DryRunPublisher().publish(draft)
    assert res.success
    assert "mock-123" in res.url


def test_rag_retrieve():
    from core.rag import VectorStore, Chunk, RAGPipeline
    store = VectorStore()
    store.add(Chunk(id="1", text="秋招穿搭技巧 衬衫 西装", summary="穿搭总结"))
    store.add(Chunk(id="2", text="考研英语复习方法", summary="考研"))
    rag = RAGPipeline(store)
    results = rag.retrieve("秋招穿搭怎么选衬衫", top_k=2)
    assert len(results) >= 1
    assert results[0].id == "1"
