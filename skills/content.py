"""内容生成 Skills：标题 / 标签 / 正文 / 封面（提示词资产驱动）

质量升级点：
- 提示词来自资产（context.prompts，config/prompts.yaml 可热更新），回退内置模板
- 生成参数（model/temperature/max_tokens）贯通 persona.generation 配置
- 正文接入 RAG 优质范例（context.rag_examples）提升表达质量
- 离线降级防垃圾：过滤"占位"输出，长度超限自动截断
"""
from __future__ import annotations
import re
from core.registry import Skill, register
from core.types import SkillInput, SkillOutput
from core.llm import complete, build_system_prompt
from core.prompts import DEFAULT_PROMPTS

TITLE_MAX = 20
COVER_MAX = 15


def _tpl(inp: SkillInput, key: str) -> dict:
    """提示词资产模板：context 优先，回退内置"""
    prompts = inp.context.get("prompts") or {}
    return prompts.get(key) or DEFAULT_PROMPTS.get(key, {})


def _persona_short(persona: dict) -> str:
    """精简人格（标题用）：只给身份/语气/标题风格，不给示例，防止模型照抄示例句"""
    parts = []
    name = persona.get("name") or "博主"
    desc = persona.get("description") or ""
    parts.append(f"你是{name}" + (f"（{desc}）" if desc else ""))
    if persona.get("tone"):
        parts.append(f"语气：{persona['tone']}")
    if persona.get("title_style"):
        parts.append(f"标题风格：{persona['title_style']}")
    forbidden = persona.get("forbidden") or []
    if forbidden:
        parts.append("禁用：" + "、".join(str(f) for f in forbidden))
    return "；".join(parts)


def _persona_line(persona: dict) -> str:
    """人格 → 提示词指令块（含开头/互动/标题风格等，让模型稳定模仿）"""
    from core.persona import build_persona_block
    return build_persona_block(persona)


def _gen_kwargs(inp: SkillInput) -> dict:
    """生成参数贯通：persona.generation 配置（界面 configure() 覆盖优先级更高）"""
    gen = (inp.context.get("persona", {}) or {}).get("generation", {}) or {}
    return {
        "model": gen.get("model", "deepseek-chat"),
        "temperature": gen.get("temperature", 0.8),
        "max_tokens": gen.get("max_tokens", 1024),
    }


def _fmt_examples(examples) -> str:
    """RAG 召回样例 → 提示词片段（截断并引导「学而不抄」，让生成更自然）"""
    if not examples:
        return ""
    picked = []
    for e in examples[:2]:
        s = str(e).strip()
        if len(s) > 280:
            s = s[:280] + "…"
        picked.append(s)
    joined = "\n---\n".join(picked)
    return ("\n\n参考以下同主题范例的结构与表达（学习它的分段方式、语气、互动引导，"
            "但内容要贴合你自己的主题，不要照搬它的原句）：\n" + joined)


def _clean_lines(text: str) -> list[str]:
    """清洗 LLM 输出行：去序号/列表符号/空行/垃圾占位行"""
    out = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("0123456789.、·-–—)） ").strip()
        if not line:
            continue
        if "占位" in line or line.startswith("关于《"):
            continue
        out.append(line)
    return out


def _depure_markdown(text: str) -> str:
    """去掉 LLM 输出的 Markdown 标记 —— XHS 长文不渲染，会显示成字面符号。

    只去除加粗/斜体/删除线/行首标题/引用/行内代码，保留正文里的 #话题 与 emoji。
    """
    import re
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)   # **bold**
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.S)       # __bold__
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)  # *italic*
    text = re.sub(r"~~(.+?)~~", r"\1", text, flags=re.S)       # ~~del~~
    text = re.sub(r"`([^`]+)`", r"\1", text)                   # `code`
    text = re.sub(r"(?m)^> ?", "", text)                       # > 引用
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)                 # 行首标题
    return text


# emoji/符号区（含变体选择符、组合框符）
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF\u2B05-\u2B07]"
)


def _sanitize_body(text: str, max_emoji: int = 6) -> str:
    """清洗小模型常见输出问题：组合 emoji（①⃣）、带圈数字、段标签、emoji 泛滥。

    - 去掉变体选择符 U+FE0F 与组合框符 U+20E3（①⃣ → ①）
    - 去掉带圈数字 ①-⑳ 与 keycap 残留
    - 去掉「段一：」这类小节标签行
    - emoji 数量超过上限时删除多余
    - 合并多余空行
    """
    text = text.replace("\uFE0F", "").replace("\u20E3", "")
    text = re.sub(r"[\u2460-\u24FF]", "", text)
    # 段标签：独立行整行删；行内前缀（段一：xxx）删前缀保留内容
    text = re.sub(r"(?m)^\s*段[一二三四五六七八九十\d]+\s*[:：]?\s*$", "", text)
    text = re.sub(r"(?m)^\s*段[一二三四五六七八九十\d]+\s*[:：]\s*", "", text)
    # 去括号内单独 emoji（(🔍) 这种模型爱加的）
    text = re.sub(r"（([\U0001F000-\U0001FAFF\u2600-\u27BF])\）", r"\1", text)
    text = re.sub(r"\(([\U0001F000-\U0001FAFF\u2600-\u27BF])\)", r"\1", text)
    # emoji 数量上限
    emojis = _EMOJI_RE.findall(text)
    if len(emojis) > max_emoji:
        kept = 0
        out = []
        for ch in text:
            if _EMOJI_RE.match(ch):
                if kept >= max_emoji:
                    continue
                kept += 1
            out.append(ch)
        text = "".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@register
class TitleGenerator(Skill):
    name = "title_generator"
    description = "生成小红书爆款标题（数字/emoji/悬念/痛点）"
    triggers = ["标题", "title", "起名"]
    popularity = 0.9

    def run(self, inp: SkillInput) -> SkillOutput:
        draft = inp.draft
        persona = inp.context.get("persona", {})
        tpl = _tpl(inp, "title")
        prompt = tpl.get("user", DEFAULT_PROMPTS["title"]["user"]).format(
            topic=draft.topic, persona=_persona_short(persona))
        text = complete(prompt, system=build_system_prompt(persona), **_gen_kwargs(inp))
        lines = _clean_lines(text)
        chosen = next((l for l in lines if len(l) <= TITLE_MAX), None) or (
            lines[0] if lines else None) or f"{draft.topic}分享🍃"
        draft.title = _sanitize_body(chosen[:TITLE_MAX], max_emoji=2) or f"{draft.topic}分享🍃"
        return SkillOutput(draft=draft, notes=[f"生成{len(lines)}个候选标题"])


@register
class TagSelector(Skill):
    name = "tag_selector"
    description = "选择话题标签（LLM 推荐 + 离线兜底）"
    triggers = ["标签", "tag", "话题"]
    popularity = 0.7

    def run(self, inp: SkillInput) -> SkillOutput:
        draft = inp.draft
        tpl = _tpl(inp, "tags")
        prompt = tpl.get("user", DEFAULT_PROMPTS["tags"]["user"]).format(topic=draft.topic)
        text = complete(prompt, system="", **_gen_kwargs(inp))
        candidates = []
        for raw in _clean_lines(text):
            tag = raw.lstrip("#").strip()
            if tag and len(tag) <= 20 and tag not in candidates:
                candidates.append(f"#{tag}")
        if not candidates:
            # 离线/异常兜底：主题 + 通用热度标签
            candidates = [
                f"#{draft.topic.replace(' ', '')}",
                "#干货分享", "#笔记", "#学生党",
            ]
        draft.tags = candidates[:5]
        return SkillOutput(draft=draft, notes=[f"选定{len(draft.tags)}个标签"])


@register
class BodyWriter(Skill):
    name = "body_writer"
    description = "撰写正文（钩子开头/分段要点/数字清单/互动结尾）"
    triggers = ["正文", "body", "内容"]
    popularity = 1.0

    def run(self, inp: SkillInput) -> SkillOutput:
        draft = inp.draft
        persona = inp.context.get("persona", {})
        tpl = _tpl(inp, "body")
        rag = _fmt_examples(inp.context.get("rag_examples"))
        web = str(inp.context.get("web_context") or "")
        prompt = tpl.get("user", DEFAULT_PROMPTS["body"]["user"]).format(
            topic=draft.topic,
            title=draft.title or "无",
            persona=_persona_line(persona),
            rag_context=rag,
            web_context=web,
        )
        text = complete(prompt, system=build_system_prompt(persona), **_gen_kwargs(inp))
        if not text.strip() or "占位" in text:
            # 本地降级正文模板（短句 + 句号，符合 Persona 短句偏好）
            text = (
                f"家人们谁懂啊，今天必须聊聊{draft.topic}。🍃\n\n"
                f"刚开始我也是一头雾水。后来试了一圈，才发现几个超实用的点。\n\n"
                f"别贪多，先把基础打牢。\n"
                f"多看真实案例，比干看理论强太多。\n"
                f"动手练才是王道，光收藏等于没学。💡\n\n"
                f"你们有什么好方法？评论区一起交流呀。冲鸭🍃"
            )
        draft.body = _sanitize_body(_depure_markdown(text))
        return SkillOutput(draft=draft, notes=[f"正文已生成（RAG范例{len(inp.context.get('rag_examples') or [])}条）"])


@register
class CoverWriter(Skill):
    name = "cover_writer"
    description = "生成封面文案（≤15字，数字/悬念/痛点）"
    triggers = ["封面", "cover"]
    popularity = 0.6

    def run(self, inp: SkillInput) -> SkillOutput:
        draft = inp.draft
        tpl = _tpl(inp, "cover")
        prompt = tpl.get("user", DEFAULT_PROMPTS["cover"]["user"]).format(
            title=draft.title or draft.topic)
        text = complete(prompt, system="", **_gen_kwargs(inp))
        lines = _clean_lines(text)
        chosen = next((l for l in lines if len(l) <= COVER_MAX), None) or (
            lines[0] if lines else None)
        draft.cover_text = (chosen or (draft.title or draft.topic))[:COVER_MAX]
        return SkillOutput(draft=draft, notes=["封面文案已生成"])
