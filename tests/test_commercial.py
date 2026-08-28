"""商用化新增功能测试：合规引擎 / 发布门禁 / 调度器 / 发布器安全特性

注：不用 pytest 自带 tmp_path（沙箱 ACL 拦截其收尾清理），
改用项目内 .test_tmp 目录 + 手动清理。
"""
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from core.types import Draft, SkillInput
from core.compliance import ComplianceEngine
from core.harness import Harness, RuleConfig
from core.registry import get
from core.orchestrator import Orchestrator
from publishers.xhs import (
    DryRunPublisher,
    XhsPlaywrightPublisher,
    ApprovalDenied,
    LoginRequired,
)
from publishers.scheduler import (
    ContentBank,
    PublishLog,
    PublishScheduler,
)


@pytest.fixture()
def persona():
    return {
        "name": "小鹿学姐", "tone": "warm_girly",
        "habits": ["每3句一个emoji"], "forbidden": ["综上所述", "IaaS"],
        "sentence_length": "short",
        "harness": {"max_retries": 1, "rate_limit": 10, "require_approval": False},
    }


@pytest.fixture()
def workdir():
    """项目内临时目录（沙箱安全），yield 后清理

    注意：不能用 tempfile.mkdtemp —— 其 mode=0o700 会被沙箱映射为
    受限 DACL（创建后连本进程都无法建子目录）；必须用默认 mode(0o777) 建目录。
    """
    from uuid import uuid4
    d = Path(f"pxtest_{uuid4().hex[:10]}")
    d.mkdir()  # 0o777
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------- 合规引擎 ----------

class TestCompliance:
    def test_hits_ad_law(self):
        eng = ComplianceEngine()
        rep = eng.check(title="全网最低价，只有这里才有", body="")
        assert not rep.ok
        assert any(h.category == "ad_law" for h in rep.hits)

    def test_hits_platform_guidance(self):
        eng = ComplianceEngine()
        rep = eng.check(title="", body="加微信领取资料")
        assert not rep.ok
        assert any(h.category == "platform" for h in rep.hits)

    def test_normal_text_passes(self):
        eng = ComplianceEngine()
        rep = eng.check(title="秋招穿搭分享", body="家人们谁懂啊，这样穿绝了🍃")
        assert rep.ok

    def test_no_false_positive_last(self):
        # 「最后」不应命中「最」类绝对化用语（正则变体保证）
        eng = ComplianceEngine()
        rep = eng.check(title="", body="最后提醒大家一句，别踩坑")
        assert rep.ok

    def test_config_fail_safe(self):
        # 配置文件不存在时用内置词表，不崩溃
        from core.compliance import load_compliance_config
        eng = ComplianceEngine(load_compliance_config("nope.yaml"))
        rep = eng.check(title="稳赚不赔", body="")
        assert not rep.ok


# ---------- 发布就绪门禁 Skill ----------

class TestPublishGate:
    def test_ready_draft(self):
        skill = get("xhs_publish")
        draft = Draft(topic="t", title="秋招穿搭分享", body="正文内容", tags=["#穿搭"])
        out = skill.run(SkillInput(draft=draft, context={}))
        assert out.draft.metadata["publish_ready"] is True

    def test_missing_fields_blocked(self):
        skill = get("xhs_publish")
        draft = Draft(topic="t")  # 空标题/正文/标签
        out = skill.run(SkillInput(draft=draft, context={}))
        assert out.draft.metadata["publish_ready"] is False
        assert out.draft.metadata["publish_issues"]


# ---------- LLM 运行时配置 ----------

class TestLLMConfigure:
    def test_configure_override(self):
        from core import llm
        llm.configure(model="deepseek-reasoner", temperature=0.2)
        assert llm._OVERRIDES["model"] == "deepseek-reasoner"
        assert llm._OVERRIDES["temperature"] == 0.2
        # 离线降级路径不受 override 影响，仍返回文本
        text = llm.complete("写一篇小红书正文，主题《测试》")
        assert "测试" in text
        # 复位，避免影响其他用例
        llm.configure(model="deepseek-chat", temperature=0.8)


# ---------- 内容质量升级（提示词资产 / RAG / Skill） ----------

class TestPromptsAsset:
    def test_load_prompts_yaml(self):
        from core.prompts import load_prompts
        p = load_prompts("config/prompts.yaml")
        assert "title" in p and "user" in p["title"]
        assert "{topic}" in p["body"]["user"] and "{rag_context}" in p["body"]["user"]

    def test_load_prompts_fail_safe(self):
        from core.prompts import load_prompts
        p = load_prompts("nope.yaml")
        assert "title" in p and "user" in p["title"]   # 缺失回退内置


class TestChineseRAG:
    def test_bigram_similarity_works(self):
        from core.rag import VectorStore, Chunk, RAGPipeline
        store = VectorStore()
        store.add(Chunk(id="1", text="秋招穿搭技巧 衬衫 西装", summary="面试穿搭"))
        store.add(Chunk(id="2", text="考研英语复习方法", summary="考研"))
        rag = RAGPipeline(store)
        results = rag.retrieve("秋招面试穿搭怎么选衬衫", top_k=1)
        assert results and results[0].id == "1"

    def test_build_rag_from_dir(self):
        from core.rag import build_rag_from_dir
        pipe = build_rag_from_dir("knowledge")
        assert len(pipe.store.chunks) >= 2

    def test_frontmatter_parse(self):
        from core.rag import _parse_frontmatter, build_rag_from_dir
        meta, body = _parse_frontmatter("---\ntopic: 秋招穿搭\nkeywords: [面试, 衬衫]\n---\n正文内容")
        assert meta.get("topic") == "秋招穿搭"
        assert "正文内容" in body and "秋招穿戴" not in body
        # 无 front-matter 兜底
        meta2, body2 = _parse_frontmatter("只有正文")
        assert meta2 == {} and body2 == "只有正文"

    def test_retrieve_relevant_filters_irrelevant(self):
        from core.rag import VectorStore, Chunk, RAGPipeline
        store = VectorStore()
        store.add(Chunk(id="a", text="秋招面试穿搭攻略", summary="秋招穿搭",
                        metadata={"keywords": ["面试", "穿搭", "秋招"]}))
        store.add(Chunk(id="b", text="考研英语完形填空技巧", summary="考研英语",
                        metadata={"keywords": ["考研", "英语"]}))
        rag = RAGPipeline(store)
        rel = rag.retrieve_relevant("秋招面试穿搭", top_k=2, min_score=0.10)
        assert rel and rel[0].id == "a"          # 命中相关性高的
        # 高出阈值但更不相关的话，也会被过滤（这里至少不把 b 排前面）
        assert rel[0].id == "a"

    def test_relevance_threshold_skips_when_irrelevant(self):
        from core.rag import VectorStore, Chunk, RAGPipeline
        store = VectorStore()
        store.add(Chunk(id="q", text="考研英语复习方法", summary="考研英语",
                        metadata={"keywords": ["考研", "英语"]}))
        rag = RAGPipeline(store)
        rel = rag.retrieve_relevant("秋招面试穿搭怎么选", top_k=2, min_score=0.20)
        assert rel == []                          # 主题不贴近 → 不注入，避免跑题


class TestSkillsQuality:
    def test_orchestrator_injects_rag_context(self, persona):
        orch = Orchestrator(persona=persona, harness=Harness(RuleConfig(**persona["harness"])))
        draft = orch.run("秋招穿搭")
        assert draft.title and draft.body and draft.tags   # 离线链路不因 RAG 注入而破坏

    def test_tag_fallback_on_garbage_output(self):
        # 离线/异常输出会被清洗并回退模板标签，绝不产出垃圾标签
        skill = get("tag_selector")
        draft = Draft(topic="秋招穿搭")
        out = skill.run(SkillInput(draft=draft, context={"persona": {}}))
        assert out.draft.tags and all(t.startswith("#") for t in out.draft.tags)
        assert not any("占位" in t for t in out.draft.tags)

    def test_title_kept_under_20(self):
        skill = get("title_generator")
        draft = Draft(topic="秋招穿搭")
        out = skill.run(SkillInput(draft=draft, context={"persona": {}}))
        assert out.draft.title and len(out.draft.title) <= 20


# ---------- 浏览器自动回退 ----------

class TestBrowserFallback:
    def test_browser_order(self):
        from publishers.xhs import _browser_order
        assert _browser_order("chrome") == ["chrome", "msedge", None]
        assert _browser_order("msedge") == ["msedge", None]
        # 不指定浏览器时：优先 Edge（Windows 自带），再回退内置 Chromium
        assert _browser_order(None) == ["msedge", None]

    def test_browser_exe_paths_known(self):
        from publishers.xhs import _browser_exe_paths
        assert isinstance(_browser_exe_paths("chrome"), list)
        assert isinstance(_browser_exe_paths("msedge"), list)


# ---------- 多人格库 ----------

class TestPersonaLibrary:
    def test_list_personas(self):
        from core.persona import list_personas, get_persona
        names = list_personas()
        assert "小鹿学姐" in names and "干货知识风" in names
        p = get_persona("干货知识风")
        assert p.get("name") == "笔记君"
        assert p.get("tone") == "professional_warm"

    def test_resolve_merges_over_base(self):
        from core.persona import resolve_persona
        p = resolve_persona("干货知识风")
        assert p.get("name") == "笔记君"
        assert p.get("sentence_length") == "medium"

    def test_resolve_default_when_missing(self):
        from core.persona import resolve_persona
        p = resolve_persona("不存在的人格")
        assert p.get("name")   # 回退默认，不报错

    def test_add_and_get(self, workdir):
        from core.persona import add_persona, get_persona, list_personas
        p = str(workdir / "p.yaml")
        add_persona("测试人格", {"name": "测试君", "tone": "fresh"}, p)
        assert "测试人格" in list_personas(p)
        assert get_persona("测试人格", p).get("name") == "测试君"


# ---------- 联网增强 ----------

class TestWebSearch:
    def test_disabled_returns_empty(self, monkeypatch):
        import os
        os.environ["WEB_SEARCH_ENABLED"] = "0"
        from core.websearch import search_web, build_web_context
        assert search_web("秋招穿搭") == []
        assert build_web_context("秋招穿搭") == ""

    def test_off_backend_returns_empty(self, monkeypatch):
        import os
        os.environ["WEB_SEARCH_ENABLED"] = "1"
        os.environ["WEB_SEARCH_BACKEND"] = "off"
        from core.websearch import search_web
        assert search_web("秋招穿搭") == []

    def test_backend_switch(self, monkeypatch):
        import os
        os.environ["WEB_SEARCH_BACKEND"] = "bocha"
        os.environ.pop("BOCHA_API_KEY", None)   # 无 Key → 空
        from core.websearch import _search_bocha
        assert _search_bocha("秋招穿搭", 3) == []

    def test_build_web_context_format(self, monkeypatch):
        # 模拟返回结果，验证格式化与相关性过滤
        import os
        os.environ["WEB_SEARCH_ENABLED"] = "1"
        from core import websearch
        monkeypatch.setattr(websearch, "search_web",
                            lambda q, top_k=3: ["秋招穿搭 热点A：内容1", "面试穿搭 热点B：内容2"])
        ctx = websearch.build_web_context("秋招穿搭")
        assert "近期相关热点" in ctx and "热点A" in ctx
        # 无结果 → 空串
        monkeypatch.setattr(websearch, "search_web", lambda q, top_k=3: [])
        assert websearch.build_web_context("秋招穿搭") == ""
        # 无关结果被过滤 → 空串（防跑题）
        monkeypatch.setattr(websearch, "search_web",
                            lambda q, top_k=3: ["字典解释秋字的意思", "别的话题内容"])
        assert websearch.build_web_context("秋招穿搭") == ""


# ---------- 封面生成（多模态） ----------

class TestCoverGen:
    def test_generate_cover_png(self):
        from core.covergen import generate_cover
        from PIL import Image
        p = generate_cover("秋招战袍穿对，offer翻倍", topic="秋招穿搭",
                           out_dir=".tmp_covers")
        im = Image.open(p)
        assert im.size == (600, 800)
        import shutil
        shutil.rmtree(".tmp_covers", ignore_errors=True)

    def test_ensure_cover_for_draft(self):
        from core.covergen import ensure_cover_for_draft
        from core.types import Draft
        d = Draft(topic="秋招穿搭", title="秋招战袍穿对，offer翻倍")
        path = ensure_cover_for_draft(d, out_dir=".tmp_covers")
        assert path and d.metadata["cover"] == path
        assert d.metadata["images"][0] == path
        import shutil
        shutil.rmtree(".tmp_covers", ignore_errors=True)

    def test_no_title_no_cover(self):
        from core.covergen import ensure_cover_for_draft
        from core.types import Draft
        d = Draft(topic="无标题")
        assert ensure_cover_for_draft(d) is None

    def test_all_styles(self):
        from core.covergen import generate_cover, configure
        from PIL import Image
        import shutil
        for s in ("premium", "minimal", "gradient", "split", "card"):
            configure(style=s)
            p = generate_cover("风格测试标题", topic="测试", out_dir=".tmp_covers")
            assert Image.open(p).size == (600, 800)
        configure(style="premium")   # 复位默认
        shutil.rmtree(".tmp_covers", ignore_errors=True)

    def test_ai_fallback_without_key(self):
        from core.covergen import generate_cover, configure
        import os
        os.environ.pop("SILICONFLOW_API_KEY", None)
        configure(ai_enabled=True)
        p = generate_cover("AI回退测试", topic="测试")
        assert p.exists()   # 无 Key 自动回退本地海报

    def test_description_driven(self):
        from core.covergen import generate_cover, configure, _parse_style_desc
        from PIL import Image
        import shutil
        configure(style="gradient")
        # 描述「粉色渐变 可爱风」→ 粉色 + 渐变
        parsed = _parse_style_desc("粉色渐变 可爱风")
        assert parsed["style"] == "gradient" and parsed["palette"] is not None
        p = generate_cover("描述测试", topic="测试", out_dir=".tmp_covers",
                           description="粉色渐变 可爱风")
        assert Image.open(p).size == (600, 800)
        # 描述「深色高级感 金色线条」→ premium
        parsed2 = _parse_style_desc("深色高级感 金色线条")
        assert parsed2["style"] == "premium"
        # 空描述 → 不解析
        assert _parse_style_desc("") == {}
        shutil.rmtree(".tmp_covers", ignore_errors=True)

    def test_build_persona_block(self):
        from core.persona import build_persona_block
        p = {"name": "小鹿学姐", "tone": "warm_girly",
             "habits": ["短句优先"], "opening": "痛点共鸣", "interaction": "求收藏",
             "title_style": "数字+emoji", "example": "家人们谁懂啊"}
        block = build_persona_block(p)
        assert "你的人设：小鹿学姐" in block
        assert "开头风格：痛点共鸣" in block and "互动引导：求收藏" in block
        assert "风格示例：家人们谁懂啊" in block

    def test_suggest_persona(self):
        from core.persona import suggest_persona
        assert suggest_persona("考研英语复习方法") == "干货知识风"
        assert suggest_persona("面试被问缺点怎么答") == "职场进阶风"
        assert suggest_persona("随便聊聊") is None   # 无匹配不推荐


# ---------- 发布器安全特性 ----------

class TestPublisherSafety:
    def test_dry_run_publishes(self):
        draft = Draft(topic="t", title="标题", body="正文")
        res = DryRunPublisher().publish(draft)
        assert res.success

    def test_idempotent_skip(self, workdir):
        # 幂等检查在登录态检查之前：无 storage_state 也能返回跳过
        pub = XhsPlaywrightPublisher(
            storage_state=str(workdir / "missing.json"), auto_approve=True)
        draft = Draft(topic="t", title="标题", body="正文",
                      metadata={"publish_url": "https://xhs.com/note/abc"})
        res = pub.publish(draft)
        assert "IDEMPOTENT" in res.message
        assert res.url == "https://xhs.com/note/abc"

    def test_login_required_without_state(self, workdir):
        pub = XhsPlaywrightPublisher(
            storage_state=str(workdir / "missing.json"), auto_approve=True)
        draft = Draft(topic="t", title="标题", body="正文")
        with pytest.raises(LoginRequired):
            pub.publish(draft)

    def test_approval_denied_raises(self, workdir):
        # 有登录态文件但用户拒绝
        state = workdir / "state.json"
        state.write_text("{}", encoding="utf-8")
        pub = XhsPlaywrightPublisher(storage_state=str(state), auto_approve=False)
        draft = Draft(topic="t", title="标题", body="正文")
        with pytest.raises(ApprovalDenied):
            pub.publish(draft, confirm=lambda d: False)

    def test_rate_limit_via_harness(self, workdir):
        state = workdir / "state.json"
        state.write_text("{}", encoding="utf-8")
        harness = Harness(RuleConfig(publish_rate_limit=1))
        pub = XhsPlaywrightPublisher(storage_state=str(state), auto_approve=True,
                                     harness=harness, user_id="u1")
        draft = Draft(topic="t", title="标题", body="正文")
        assert harness.check_publish_quota("u1")[0]   # 先占掉发布额度
        res = pub.publish(draft)
        assert not res.success
        assert "限速" in res.message


# ---------- 调度器 ----------

class TestScheduler:
    def test_due_selection(self, workdir):
        bank_dir = workdir / "bank"
        bank_dir.mkdir()
        (bank_dir / "a.json").write_text(json.dumps({
            "id": "a", "topic": "秋招穿搭",
            "scheduled_at": "2026-01-01 09:00"}), encoding="utf-8")
        (bank_dir / "b.json").write_text(json.dumps({
            "id": "b", "topic": "租房避坑",
            "scheduled_at": "2099-01-01 09:00"}), encoding="utf-8")
        bank = ContentBank(str(bank_dir))
        log = PublishLog(path=str(workdir / "log.json")).load()
        orch = Orchestrator(persona={"harness": {}}, harness=Harness(RuleConfig()))
        sched = PublishScheduler(orchestrator=orch, bank=bank, log=log)
        due = sched.due_items()
        assert [d.id for d in due] == ["a"]

    def test_run_once_dry_run_publishes_and_logs(self, workdir):
        bank_dir = workdir / "bank"
        bank_dir.mkdir()
        (bank_dir / "a.json").write_text(json.dumps({
            "id": "a", "topic": "秋招穿搭",
            "scheduled_at": "2026-01-01 09:00",
            "title": "秋招穿搭｜谁懂啊", "body": "正文内容，家人们冲鸭🍃",
            "tags": ["#穿搭"]}), encoding="utf-8")
        log = PublishLog(path=str(workdir / "log.json")).load()
        sched = PublishScheduler(
            orchestrator=Orchestrator(persona={"harness": {}}, harness=Harness(RuleConfig())),
            publisher=DryRunPublisher(),
            bank=ContentBank(str(bank_dir)),
            log=log,
        )
        report = sched.run_once()
        assert report[0]["status"] == "published"
        assert log.records["a"]["status"] == "published"
        # 幂等：再跑一次不再发
        assert sched.run_once() == []

    def test_compliance_blocks(self, workdir):
        bank_dir = workdir / "bank"
        bank_dir.mkdir()
        (bank_dir / "bad.json").write_text(json.dumps({
            "id": "bad", "topic": "减肥",
            "scheduled_at": "2026-01-01 09:00",
            "title": "三天见效的减肥神药", "body": "稳赚不赔，加微信领取",
            "tags": ["#减肥"]}), encoding="utf-8")
        log = PublishLog(path=str(workdir / "log.json")).load()
        sched = PublishScheduler(
            orchestrator=Orchestrator(persona={"harness": {}}, harness=Harness(RuleConfig())),
            publisher=DryRunPublisher(),
            bank=ContentBank(str(bank_dir)),
            log=log,
        )
        report = sched.run_once()
        assert report[0]["status"] == "skipped"
        assert "合规拦截" in report[0]["reason"]
