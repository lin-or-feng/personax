"""提示词资产：Skill 生成模板（商用标准——提示词作为可配置资产）

- 默认模板内置在 DEFAULT_PROMPTS，config/prompts.yaml 可覆盖/扩展（热更新）
- 模板占位符：{topic} 主题 / {title} 标题 / {persona} 人设摘要 / {rag_context} 知识库参考
"""
from __future__ import annotations
from pathlib import Path
from typing import Any

DEFAULT_PROMPTS: dict[str, dict[str, str]] = {
    "title": {
        "user": (
            "为小红书笔记《{topic}》生成 3 个爆款标题候选，每行一个。\n"
            "要求：\n"
            "1. 每个标题不超过 20 字，标题内 emoji 最多 2 个；\n"
            "2. 至少包含一个吸睛元素：数字 / 悬念 / 痛点 / 人群词（学生党、打工人…）；\n"
            "3. 口语化，符合账号人设：{persona}；\n"
            "4. 标题之间风格不重复，不夸大（禁止绝对化用语）；\n"
            "5. 禁止使用① ② ③ 等带圈数字和组合字符，只输出标题本身。"
        ),
    },
    "body": {
        "user": (
            "写一篇小红书正文，主题《{topic}》，标题参考《{title}》。\n"
            "结构要求：\n"
            "- 开头一句钩子（痛点/悬念/共鸣），例如\"谁懂啊\"\"我真的会谢\"；\n"
            "- 正文分 3-5 段，每段一个要点，短句为主；\n"
            "- 至少一处数字/清单式表达（如\"3 个方法\"\"第一/第二\"）；\n"
            "- 结尾互动引导（提问/求收藏/求评论）。\n"
            "字数 300-500 字，emoji 全篇不超过 5 个；"
            "禁止使用① ② ③ 等带圈数字与组合字符；"
            "不要输出「段一」「段二」之类的小节标签；符合人设：{persona}。\n"
            "{web_context}"
            "{rag_context}"
        ),
    },
    "tags": {
        "user": (
            "为主题《{topic}》推荐 3-5 个小红书话题标签，每行一个（不带 # 号）。\n"
            "组合要求：1 个核心主题词 + 1 个人群词 + 1-2 个热度词。\n"
            "只输出标签本身，不要解释。"
        ),
    },
    "cover": {
        "user": (
            "为标题《{title}》生成 1 句封面文案，不超过 15 字，只输出这一句。\n"
            "要求抓眼球：数字 / 悬念 / 痛点 三选一。"
        ),
    },
}


def load_prompts(path: str | Path | None = "config/prompts.yaml") -> dict[str, Any]:
    """读取提示词资产；缺失/损坏时回退内置模板（fail-safe）"""
    merged = {k: dict(v) for k, v in DEFAULT_PROMPTS.items()}
    if not path:
        return merged
    p = Path(path)
    if not p.exists():
        return merged
    try:
        import yaml
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 —— 资产损坏不阻断主流程
        return merged
    for k, v in (data or {}).items():
        if isinstance(v, dict):
            merged.setdefault(k, {}).update({kk: str(vv) for kk, vv in v.items()})
    return merged
