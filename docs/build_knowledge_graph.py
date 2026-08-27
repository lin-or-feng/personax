#!/usr/bin/env python3
"""生成 PersonaX 知识图谱 Word 版（.docx）

纯标准库实现（zipfile + XML），无需 python-docx。
运行：python docs/build_knowledge_graph.py
输出：docs/PersonaX知识图谱.docx
"""
from __future__ import annotations
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent / "PersonaX知识图谱.docx"

# ---------- 内容 ----------
TITLE = "PersonaX 知识图谱"
SUBTITLE = "小红书内容生成与自动发布 Agent —— 架构 / 机制 / 技术点 / 面试要点"

SECTIONS: list[tuple[str, list[str]]] = [
    ("一、架构分层", [
        "接入层：main.py（CLI：generate / publish / schedule / login / notes / probe / personas / eval）；app.py（Streamlit 可视化工作台 5 页）",
        "编排层：orchestrator.py（Skill 链 + Harness 拦截 + 风格校验闭环）；graph.py（LangGraph 图编排，可选）",
        "能力层：skills/（标题/正文/标签/封面/发布就绪门禁）；rag.py（知识库召回）；llm.py（DeepSeek）",
        "基础设施：types.py（契约）/ registry.py（路由）/ harness.py（规则引擎）/ compliance.py（合规）/ persona.py（多人格）/ prompts.py（提示词资产）/ style.py（风格约束）",
        "执行层：publishers/xhs.py（Playwright 真实发布）/ scheduler.py（定时调度）",
    ]),
    ("二、核心机制", [
        "Skill 系统：@register 自动注册 + 多信号加权路由（语义0.5/关键词0.3/热度0.2）+ manifest 标准",
        "三层解耦：跨层只走 Draft / SkillInput / ExecutionContext 契约，禁止反向依赖",
        "Harness 管控：allow/deny + 发布限速 + 敏感工具审批 + 审计留痕（独立规则引擎）",
        "合规引擎：四类词表（广告法绝对化用语/医疗金融承诺/平台导流）+ 正则变体 + fail-safe 兜底",
        "多人格：config/personas.yaml 人格库，--persona 切换，生成时按所选人格写作",
        "提示词资产：config/prompts.yaml 模板（{topic}/{persona}/{rag_context}），热更新",
        "RAG：front-matter 结构化知识 + 中文 bigram + 主题加权（0.6/0.4）+ 相关性门槛防跑题",
        "真实发布：Playwright + 登录态隔离 + 闭合 Shadow DOM 坐标点击 + URL published=true 判定 + 重试/幂等/截图",
        "定时调度：content_bank 到期检测 → 内容补齐 → 合规 → 限速 → 发布 → publish_log.json 留痕",
        "评估闭环：eval_set + trait/emoji/句长三维打分 + CSV 回归保护",
    ]),
    ("三、技术栈", [
        "Python 3.10+ / DeepSeek（OpenAI SDK）/ Playwright / Pydantic / Streamlit / LangGraph（可选）/ PyYAML / pytest",
        "自研 RAG：汉字 bigram 相似度（中文无空格分词适配）",
        "38 项 pytest 单测全绿",
    ]),
    ("四、调优方向", [
        "接真实 LLM（.env 配 Key）→ 模板文升级为 DeepSeek 现写",
        "调 config/prompts.yaml 提示词资产 → 改生成要求，不用改代码",
        "喂 knowledge/*.md 优质范例 → RAG 自动召回学习结构与语气",
        "config/personas.yaml 建多人格 → 不同语气人设",
        "平台改版 → python main.py probe / debug-selectors 诊断，更新选择器",
    ]),
    ("五、面试要点（可深挖）", [
        "为什么用 Playwright 而不用官方 API？风控与合规如何考虑？",
        "闭合 Shadow DOM 的发布按钮如何定位与点击？",
        "RAG 为什么用中文 bigram？相关性门槛如何防跑题？",
        "合规词表如何设计？广告法哪些词要拦？",
        "幂等 / 限流 / 人工审批 / 审计如何实现？",
        "平台改版后如何快速恢复？（probe 诊断 + 选择器资产化）",
        "换成抖音 / 公众号如何扩展？（Publisher 抽象）",
    ]),
    ("六、一句话总结", [
        "PersonaX = 提示词资产 × 多人格 × RAG 知识库 × 合规引擎 × 规则管控 × Playwright 真实发布 × 定时调度 × 可视化 —— LLM Agent 落地到内容运营的完整工程样例。",
    ]),
]

TABLE_HEAD = ["机制", "设计", "面试可讲点"]
TABLE_ROWS = [
    ["Skill 系统", "@register + 多信号路由", "资产化、可插拔"],
    ["三层解耦", "跨层 Draft 契约", "为什么分层"],
    ["Harness", "限流/审批/审计", "规则引擎与业务解耦"],
    ["合规引擎", "四类词表+正则变体", "发布红线、fail-safe"],
    ["多人格", "personas.yaml + 合并", "配置驱动、热更新"],
    ["提示词资产", "prompts.yaml 模板", "提示词工程化"],
    ["RAG", "bigram+主题加权+门槛", "中文检索、防跑题"],
    ["真实发布", "shadow DOM 坐标点击", "逆向排障、改版容错"],
    ["幂等/限流", "publish_log + 配额", "商用健壮性"],
    ["评估闭环", "eval_set 三维打分", "回归保护"],
]

# ---------- docx 构建 ----------

def _p(text: str, bold: bool = False, size: int = 22, mono: bool = False,
        heading: int | None = None, space_after: int = 120) -> str:
    rpr = []
    if bold:
        rpr.append("<w:b/>")
    if mono:
        rpr.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:eastAsia="宋体"/>')
    rpr.append(f'<w:sz w:val="{size}"/>')
    ppr = []
    if heading:
        ppr.append(f'<w:pStyle w:val="Heading{heading}"/>')
    ppr.append(f'<w:spacing w:after="{space_after}" w:line="300" w:lineRule="auto"/>')
    return (
        f'<w:p><w:pPr>{"".join(ppr)}</w:pPr>'
        f'<w:r><w:rPr>{"".join(rpr)}</w:rPr><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
    )


def _table(rows: list[list[str]]) -> str:
    xml = ['<w:tbl><w:tblPr>'
           '<w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/>'
           '<w:tblBorders><w:top w:val="single" w:sz="4"/><w:left w:val="single" w:sz="4"/>'
           '<w:bottom w:val="single" w:sz="4"/><w:right w:val="single" w:sz="4"/>'
           '<w:insideH w:val="single" w:sz="4"/><w:insideV w:val="single" w:sz="4"/></w:tblBorders>'
           '</w:tblPr>']
    for r_i, row in enumerate(rows):
        xml.append('<w:tr>')
        for cell in row:
            bold = r_i == 0
            xml.append('<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>'
                       f'<w:p><w:pPr><w:spacing w:after="40"/></w:pPr>'
                       f'<w:r><w:rPr>{"<w:b/>" if bold else ""}<w:sz w:val="20"/></w:rPr>'
                       f'<w:t xml:space="preserve">{escape(cell)}</w:t></w:r></w:p></w:tc>')
        xml.append('</w:tr>')
    xml.append('</w:tbl>')
    return "".join(xml)


def build() -> None:
    body = [
        _p(TITLE, bold=True, size=32, space_after=80),
        _p(SUBTITLE, size=20, space_after=240),
    ]
    for title, lines in SECTIONS:
        body.append(_p(title, bold=True, size=26, heading=1, space_after=100))
        for line in lines:
            body.append(_p("• " + line, size=21, space_after=60))
    body.append(_p("核心机制速查表", bold=True, size=26, heading=1, space_after=100))
    body.append(_table([TABLE_HEAD] + TABLE_ROWS))
    body.append(_p("", space_after=40))

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>' + "".join(body) +
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" '
        'w:bottom="1134" w:left="1134" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
        '</w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )

    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    print(f"已生成: {OUT}")


if __name__ == "__main__":
    build()
