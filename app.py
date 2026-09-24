"""PersonaX 2.7 可视化工作台（Streamlit）

启动：
    pip install streamlit
    python -m streamlit run app.py

功能页：
- 📝 生成与编辑  生成标题/正文/标签 → 自由编辑 → 检验（合规+风格+就绪）→ 发布
- 🗓️ 内容库与定时  管理 content_bank 稿件、设置发布时间、执行到期任务
- 💬 AI 助手      有状态问答 → 按需 RAG → 引用与 Trace
- ⚙️ 设置        人格/生成参数（内容格式）、合规词表，可视化编辑
- 📊 状态与日志  发布留痕、审计摘要、一键评测
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st
import yaml

# ---------- 路径（与 cwd 无关，双击/任意目录启动都稳） ----------
BASE = Path(__file__).resolve().parent
PERSONA_PATH = BASE / "config" / "persona.yaml"
COMPLIANCE_PATH = BASE / "config" / "compliance.yaml"
BANK_DIR = BASE / "content_bank"
LOG_PATH = BASE / "publish_log.json"
STATE_PATH = BASE / "storage_state.json"
ASSISTANT_CHECKPOINT_PATH = BASE / "logs" / "assistant_checkpoints.sqlite3"
ASSISTANT_FEEDBACK_PATH = BASE / "logs" / "assistant_feedback.sqlite3"
LANGGRAPH_CHECKPOINT_PATH = BASE / "logs" / "langgraph_checkpoints.sqlite3"

st.set_page_config(
    page_title="PersonaX · Local Agent Studio",
    page_icon=":material/hub:",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 触发 Skill 注册
sys.path.insert(0, str(BASE))
from core.envfile import load_env_file
load_env_file(BASE / ".env")   # 读取 DEEPSEEK_API_KEY（若 .env 存在）
import skills  # noqa: F401
from core.orchestrator import Orchestrator
from core.harness import Harness, RuleConfig
from core.compliance import ComplianceEngine, load_compliance_config
from core.publish_safety import real_publish_enabled, real_publish_disabled_message
from core.approval import PublishApprovalStore, draft_fingerprint
from core.style import StyleEnforcer
from core.registry import get, route as route_skills
from core.llm import configure as llm_configure
from publishers.xhs import DryRunPublisher, XhsPlaywrightPublisher, LoginRequired, ApprovalDenied
from publishers.scheduler import ContentBank, PublishLog, PublishScheduler, BankItem

TIME_FMT = "%Y-%m-%d %H:%M"


# ---------- 通用工具 ----------

def load_persona() -> dict:
    with open(PERSONA_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_persona(data: dict):
    with open(PERSONA_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def build_orch(persona_name: str | None = None, engine: str = "python"):
    """构建编排器。persona_name 指定人格库中的某个人格，否则用 persona.yaml"""
    from core.persona import list_personas, resolve_persona
    if persona_name and persona_name in list_personas():
        persona = resolve_persona(persona_name)
    else:
        persona = load_persona()
    harness = Harness(RuleConfig(**persona.get("harness", {})))
    if engine == "langgraph":
        from core.graph import LangGraphOrchestrator

        return LangGraphOrchestrator(persona=persona, harness=harness), persona
    return Orchestrator(persona=persona, harness=harness), persona


def compliance() -> ComplianceEngine:
    return ComplianceEngine(load_compliance_config(str(COMPLIANCE_PATH)))


def default_chain(topic: str) -> list[str]:
    return [n for n, _ in route_skills(topic, top_k=3)] + ["xhs_publish"]


def fmt_tags(tags) -> str:
    return " ".join(tags or [])


def parse_tags(text: str) -> list[str]:
    parts = [p.strip() for p in text.replace("，", " ").replace(",", " ").split()]
    seen, out = set(), []
    for p in parts:
        raw = p.lstrip("#")
        if raw and raw not in seen:
            seen.add(raw)
            out.append(f"#{raw}")
    return out


def _md_table(headers: list[str], rows: list[list]) -> str:
    """无 pandas 的表格渲染（避免 numpy/pandas 版本不兼容崩溃）"""
    lines = ["| " + " | ".join(str(h) for h in headers) + " |",
             "|" + "---|" * len(headers)]
    for r in rows:
        cells = [str(c).replace("|", "/")[:80] for c in r]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


_FIELD_LABEL = {"title": "标题", "body": "正文", "tags": "标签"}


def _field_text(draft, field: str) -> str:
    """取草稿某字段的文本（tags 拼成空格分隔字符串）"""
    if field == "tags":
        return fmt_tags(draft.tags)
    return str(getattr(draft, field, "") or "")


def _highlight_match(text: str, match: str) -> str:
    """把命中的违规词用 <mark> 高亮（HTML 转义，防注入）"""
    if not match:
        return html.escape(text)
    return html.escape(text).replace(html.escape(match), f"<mark>{html.escape(match)}</mark>")


def _diff_highlight(before: str, after: str) -> str:
    """修改前后逐字 diff：删除标红、新增标绿，其余原样（HTML 转义防注入）"""
    import difflib
    sm = difflib.SequenceMatcher(None, before, after)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        b_seg = before[i1:i2]
        a_seg = after[j1:j2]
        if tag == "equal":
            out.append(html.escape(a_seg))
        elif tag == "delete":
            out.append(f"<del style='background:#ffe0e0;color:#c62828'>{html.escape(b_seg)}</del>")
        elif tag == "insert":
            out.append(f"<mark style='background:#d4f7d4'>{html.escape(a_seg)}</mark>")
        elif tag == "replace":
            out.append(f"<del style='background:#ffe0e0;color:#c62828'>{html.escape(b_seg)}</del>")
            out.append(f"<mark style='background:#d4f7d4'>{html.escape(a_seg)}</mark>")
    return "".join(out)


def _render_assistant_meta(message: dict) -> None:
    """渲染助手消息的来源与执行轨迹。"""
    sources = message.get("sources") or []
    if sources:
        with st.expander(f"📚 引用来源（{len(sources)}）"):
            for source in sources:
                ref = source.get("ref_id", "?")
                title = source.get("title", "未命名来源")
                url = source.get("source_url") or ""
                origin = source.get("source") or "本地知识库"
                score = float(source.get("score") or 0.0)
                if url:
                    st.link_button(
                        f"[{ref}] {title} · {origin} · 相关度 {score:.3f}",
                        url,
                        width="content",
                    )
                else:
                    st.text(f"[{ref}] {title} · {origin} · 相关度 {score:.3f}")
                st.text(source.get("excerpt") or "")
    trace = message.get("trace") or []
    if trace:
        with st.expander(f"🔎 Agent Trace（{len(trace)} 步）"):
            rows = [
                [item.get("step"), item.get("worker"), item.get("action"),
                 item.get("status"), item.get("duration_ms"), item.get("detail")]
                for item in trace
            ]
            st.markdown(_md_table(
                ["step", "worker", "action", "status", "ms", "detail"], rows))
    _render_assistant_feedback(message)


_FEEDBACK_REASONS = {
    "inaccurate": "事实不准确",
    "missing_evidence": "缺少或引用错误",
    "irrelevant": "答非所问",
    "incomplete": "回答不完整",
    "slow": "响应太慢",
    "other": "其他",
}


def _render_assistant_feedback(message: dict) -> None:
    """收集本地显式反馈；默认只存评分，不保存对话内容。"""

    trace_id = str(message.get("trace_id") or "").strip()
    if len(trace_id) < 8:
        return
    from core.feedback import AssistantFeedbackStore

    store = AssistantFeedbackStore(ASSISTANT_FEEDBACK_PATH)
    existing = store.get(trace_id)
    default = None
    if existing is not None:
        default = 1 if existing.rating == "helpful" else 0
    selected = st.feedback(
        "thumbs",
        key=f"assistant_feedback_{trace_id}",
        default=default,
    )
    if selected is None:
        st.caption("反馈默认只保存评分和 Trace；不会自动保存问题或回答正文。")
        return

    rating = "helpful" if selected == 1 else "not_helpful"
    if existing is None or existing.rating != rating:
        existing = store.upsert(
            trace_id=trace_id,
            thread_id=str(message.get("thread_id") or ""),
            rating=rating,
            answer=str(message.get("content") or ""),
            route=str(message.get("route") or ""),
            source_count=len(message.get("sources") or []),
            degraded=bool(message.get("degraded", False)),
            include_content=False,
        )
        st.toast("已记录回答反馈", icon=":material/check_circle:")
    if rating == "helpful":
        st.caption("已记录为有帮助；只保存评分、Trace 和回答哈希。")
        return

    with st.expander("补充失败原因并加入评测候选（可选）", icon=":material/bug_report:"):
        reason_key = f"assistant_feedback_reason_{trace_id}"
        note_key = f"assistant_feedback_note_{trace_id}"
        include_key = f"assistant_feedback_include_{trace_id}"
        st.session_state.setdefault(
            reason_key,
            existing.reason if existing and existing.reason in _FEEDBACK_REASONS else "inaccurate",
        )
        st.session_state.setdefault(note_key, existing.note if existing else "")
        st.session_state.setdefault(
            include_key, bool(existing and existing.content_included),
        )
        with st.form(f"assistant_feedback_form_{trace_id}", border=False):
            reason = st.selectbox(
                "主要原因",
                list(_FEEDBACK_REASONS),
                format_func=lambda value: _FEEDBACK_REASONS[value],
                key=reason_key,
            )
            note = st.text_area(
                "补充说明",
                max_chars=500,
                placeholder="只写需要改进的点，不要填写密钥或账号信息。",
                key=note_key,
            )
            include_content = st.checkbox(
                "同意在本机保存脱敏后的问题与回答摘要，用于人工构建回归集",
                key=include_key,
            )
            submitted = st.form_submit_button(
                "保存反馈详情",
                icon=":material/save:",
                type="primary",
            )
        if submitted:
            refs = [
                {
                    "ref_id": str(item.get("ref_id") or ""),
                    "title": str(item.get("title") or ""),
                    "source": str(item.get("source") or ""),
                }
                for item in (message.get("sources") or [])
            ]
            saved = store.upsert(
                trace_id=trace_id,
                thread_id=str(message.get("thread_id") or ""),
                rating="not_helpful",
                reason=reason,
                note=note,
                question=str(message.get("question") or ""),
                answer=str(message.get("content") or ""),
                source_refs=refs,
                route=str(message.get("route") or ""),
                source_count=len(refs),
                degraded=bool(message.get("degraded", False)),
                include_content=include_content,
            )
            if include_content:
                st.success(
                    "已保存脱敏评测候选，仍需人工标注后才能进入正式评测集。",
                    icon=":material/fact_check:",
                )
            else:
                st.success("已保存原因；未保存问题和回答内容。")
            if saved.redacted:
                st.caption("检测到常见手机号、邮箱或身份证号，已自动掩码。")


def _render_generation_trace(draft) -> None:
    """只在 LangGraph 模式展示可解释轨迹；不暴露提示词或正文 checkpoint。"""

    metadata = draft.metadata or {}
    if metadata.get("orchestration_engine") != "langgraph":
        return
    trace = metadata.get("graph_trace") or []
    status = str(metadata.get("graph_status") or "unknown")
    status_label = {"completed": "已完成", "blocked": "被门禁拦截", "error": "执行失败"}.get(
        status, status)
    with st.expander(
        f"LangGraph 执行轨迹 · {status_label} · {len(trace)} 个节点",
        icon=":material/account_tree:",
    ):
        with st.container(horizontal=True):
            st.metric("Skill 执行", int(metadata.get("graph_steps") or 0), border=True)
            st.metric("Checkpoint", int(metadata.get("graph_checkpoint_count") or 0), border=True)
            st.metric("总耗时", f"{float(metadata.get('graph_duration_ms') or 0):.0f} ms", border=True)
        st.caption(
            f"运行 ID：`{metadata.get('graph_thread_id', '-')}` · "
            f"存储：{metadata.get('graph_checkpoint_backend', '-')} · "
            "仅保存本地诊断状态，不代表已发布"
        )
        if trace:
            st.dataframe(
                trace,
                hide_index=True,
                width="stretch",
                key=f"generation_trace_{metadata.get('graph_thread_id', 'current')}",
            )


def _page_header(title: str, description: str, *, icon: str, eyebrow: str) -> None:
    """统一页面标题层级，避免每个页面重复堆叠 emoji 与分隔线。"""

    st.caption(eyebrow.upper())
    st.header(f":material/{icon}: {title}", anchor=False)
    st.caption(description)


def _workflow_stage() -> int:
    """根据当前草稿状态推导生成流程的展示阶段。"""

    if st.session_state.get("check_result"):
        return 2
    if st.session_state.get("draft"):
        return 1
    return 0


def _render_workflow_progress() -> None:
    labels = ["设定", "生成", "检验", "发布"]
    icons = ["tune", "auto_awesome", "verified", "publish"]
    current = _workflow_stage()
    with st.container(horizontal=True, gap="small"):
        for idx, (label, icon) in enumerate(zip(labels, icons, strict=True)):
            st.badge(
                f"{idx + 1} {label}",
                icon=f":material/{icon}:",
                color="primary" if idx == current else "gray",
            )


# ---------- 会话状态 ----------

def init_state():
    s = st.session_state
    s.setdefault("draft", None)          # 当前草稿 Draft
    s.setdefault("topic", "")            # 笔记主题（默认空，让用户输入）
    s.setdefault("check_result", None)   # 检验报告 dict
    s.setdefault("pub_confirm", False)   # 真实发布二次确认
    s.setdefault("pub_msg", "")
    s.setdefault("gen_ts", 0)            # 生成版本号：换 key 清掉旧编辑框输入
    s.setdefault("jump_target", None)    # 合规跳转定位目标 dict(field, match, suggestion)
    s.setdefault("last_fix", None)       # 最近一次就地修改 dict(field, before, after)
    s.setdefault("assistant_messages", [])
    s.setdefault("assistant_thread_id", f"ui-{uuid.uuid4().hex[:12]}")
    s.setdefault("assistant_memory_bootstrapped", False)
    s.setdefault("publish_approval_id", "")
    s.setdefault("memory_summary_candidate", "")
    s.setdefault("memory_delete_confirm", False)
    s.setdefault("feedback_delete_confirm", False)


init_state()

NAV_LABELS = {
    "generate": ":material/edit_note: 生成工作台",
    "assistant": ":material/smart_toy: AI 助手",
    "schedule": ":material/calendar_month: 内容与定时",
    "knowledge": ":material/database: 知识库",
    "settings": ":material/tune: 设置",
    "observability": ":material/monitoring: 状态与日志",
}
LEGACY_NAV = {
    "📝 生成与编辑": "generate",
    "💬 AI 助手": "assistant",
    "🗓️ 内容库与定时": "schedule",
    "📚 知识库": "knowledge",
    "⚙️ 设置": "settings",
    "📊 状态与日志": "observability",
}
if st.session_state.get("nav") in LEGACY_NAV:
    st.session_state.nav = LEGACY_NAV[st.session_state.nav]

with st.sidebar:
    st.markdown("## :material/hub: PersonaX")
    st.caption("Local agent studio · v2.7.0")
    page = st.radio(
        "导航",
        list(NAV_LABELS),
        format_func=lambda key: NAV_LABELS[key],
        label_visibility="collapsed",
        key="nav",
        width="stretch",
    )
    st.space("small")
    with st.container(border=True, gap="small"):
        if STATE_PATH.exists():
            st.badge("发布登录态已就绪", icon=":material/check_circle:", color="green")
        else:
            st.badge("发布前需要登录", icon=":material/info:", color="orange")
        st.caption("内容库 `content_bank/`\n\n发布留痕 `publish_log.json`")
    if st.button(
        "重置当前草稿",
        icon=":material/restart_alt:",
        type="tertiary",
        width="stretch",
    ):
        st.session_state.draft = None
        st.session_state.check_result = None
        st.session_state.pub_confirm = False
        st.rerun()


# ============================================================
# 页 1：生成与编辑 → 检验 → 发布
# ============================================================
if page == "generate":
    _page_header(
        "生成工作台",
        "从任务设定到发布检查，所有高风险动作都保留人工确认。",
        icon="edit_note",
        eyebrow="Create workflow",
    )
    _render_workflow_progress()
    st.space("small")

    # ---- 模型后端 + 模型切换（默认本地 Ollama，免费） ----
    from core.llm import _backend, _ollama_reachable
    task_panel = st.container(border=True)
    task_panel.subheader(":material/tune: 本次任务", anchor=False)
    task_panel.caption("选择模型、编排方式和写作人格；这些设置只影响本次生成。")
    be1, be2, be3 = task_panel.columns([2, 2, 1])
    backend_labels = ["ollama（本地免费）", "deepseek（云端）", "offline（离线模板）"]
    backend_index = {"ollama": 0, "deepseek": 1, "offline": 2}.get(_backend(), 2)
    backend_ch = be1.selectbox(
        "模型后端",
        backend_labels,
        index=backend_index,
        help="ollama=本地免费生成（需已运行 Ollama）；deepseek=云端（需 .env 填 Key）；offline=不调模型用模板")
    if backend_ch.startswith("ollama"):
        model_choices = ["qwen2.5:7b", "qwen2.5:3b", "qwen2.5:14b", "glm4:9b", "deepseek-r1:7b"]
        backend_key = "ollama"
    elif backend_ch.startswith("deepseek"):
        model_choices = ["deepseek-chat", "deepseek-reasoner"]
        backend_key = "deepseek"
    else:
        model_choices = ["离线模板"]
        backend_key = "offline"
    gen_model = be2.selectbox("模型", model_choices)
    gen_temp = be3.slider("温度", 0.0, 1.5, 0.8, 0.1)
    engine_label = task_panel.segmented_control(
        "编排引擎",
        ["Python（默认）", "LangGraph（状态图）"],
        default="Python（默认）",
        key="generation_engine",
        help="LangGraph 模式使用 LangChain Runnable + SQLite checkpoint；重启后仍可在状态页查看运行记录。",
    )
    generation_engine = "langgraph" if engine_label.startswith("LangGraph") else "python"
    from core.websearch import _enabled as ws_enabled
    task_panel.caption(
        f"联网增强：{'已开启' if ws_enabled() else '已关闭'}（可在设置页切换） · "
        "无配图时发布前自动生成封面"
    )

    # 未配 Key 时给清晰提示（而不是报错）
    if backend_key == "deepseek" and not os.getenv("DEEPSEEK_API_KEY"):
        task_panel.warning(
            "未检测到 DEEPSEEK_API_KEY：请在项目根目录 `.env` 填写 Key，"
            "或改选本地 Ollama。",
            icon=":material/key_off:",
        )

    c1, c2 = task_panel.columns([3, 1])
    topic = c1.text_input("笔记主题", st.session_state.topic,
                          placeholder="请输入笔记主题，如：秋招穿搭")
    from core.persona import list_personas, suggest_persona
    persona_names = ["默认(persona.yaml)"] + list_personas()
    rec_persona = suggest_persona(topic)

    # ---- 自动推荐人格：主题变了自动选推荐，可手动切换回来 ----
    st.session_state.setdefault("auto_persona", True)
    auto_on = c1.checkbox("自动按主题推荐人格", key="auto_persona",
                          help="勾选：改主题自动选推荐人格；取消或手动选＝用你选的")
    sel_key = "persona_choice"
    st.session_state.setdefault(sel_key, "默认(persona.yaml)")
    if auto_on and topic != st.session_state.get("last_topic", ""):
        # 主题变化 → 自动切到推荐人格
        st.session_state[sel_key] = rec_persona or "默认(persona.yaml)"
        st.session_state.last_topic = topic

    bp1, bp2 = task_panel.columns([3, 1])

    def _apply_recommended():
        # 回调在组件实例化前执行，允许修改 selectbox 的 session state
        st.session_state[sel_key] = rec_persona or "默认(persona.yaml)"
        st.session_state.last_topic = topic

    gen_persona = bp1.selectbox("人格（可手动切换）", persona_names, key=sel_key,
                                help="选哪个就用哪个语气生成；可到「⚙️ 设置 → 人设库」创建/编辑人格")
    bp2.button("回到推荐", icon=":material/replay:", width="stretch", on_click=_apply_recommended,
               disabled=not rec_persona or rec_persona == gen_persona)
    if rec_persona and gen_persona == rec_persona:
        task_panel.caption(f"已自动选择推荐人格：**{rec_persona}**。你仍可以手动切换。")
    elif rec_persona:
        task_panel.caption(f"推荐人格：**{rec_persona}**；当前手动选择：{gen_persona}。")
    st.session_state.topic = topic

    if task_panel.button(
        "生成发布内容",
        icon=":material/auto_awesome:",
        type="primary",
        width="stretch",
    ):
        if not (topic or "").strip():
            task_panel.warning("请先输入笔记主题，例如：秋招穿搭。", icon=":material/edit:")
        else:
            llm_configure(backend=backend_key,
                          model=None if backend_key == "offline" else gen_model,
                          temperature=gen_temp)
            chose = None if gen_persona.startswith("默认") else gen_persona
            orch, _ = build_orch(chose, engine=generation_engine)
            with st.spinner(f"生成中（{backend_key} / {chose or '默认人格'}）…"):
                try:
                    draft = orch.run(topic=topic, user_id="web_user", skill_chain=default_chain(topic))
                    draft.metadata["ai_generated"] = True   # 合规：默认标注 AI 生成
                    st.session_state.draft = draft
                    st.session_state.check_result = None
                    st.session_state.pub_confirm = False
                    st.session_state.gen_ts += 1   # 换 key，让编辑框展示新草稿
                    st.success("生成完成，可在下方编辑（发布时将标注「内容由 AI 生成」）")
                except Exception as e:  # noqa: BLE001
                    st.error(f"生成失败: {e}")

    st.space("medium")

    draft = st.session_state.draft
    if draft is None:
        st.info(
            "在上方完成任务设定并生成内容，或从“内容与定时”载入已有稿件。",
            icon=":material/lightbulb:",
        )
    else:
        _render_generation_trace(draft)
        st.subheader(":material/edit: 内容编辑与预览", anchor=False)
        ts = st.session_state.gen_ts
        col_a, col_b = st.columns([2, 1])
        with col_a.container(border=True):
            title = st.text_input("标题（≤20 字，含 emoji 更吸睛）", draft.title or "", key=f"e_title_{ts}")
            body = st.text_area("正文（短句分段，结尾互动引导）", draft.body or "",
                                height=260, key=f"e_body_{ts}")
        with col_b.container(border=True):
            tags = st.text_input("标签（空格分隔）", fmt_tags(draft.tags), key=f"e_tags_{ts}")
            cover_text = st.text_input("封面文案（可选）", draft.cover_text or "", key=f"e_cover_{ts}")
            images = st.text_input("配图路径（逗号分隔，可选）",
                                   " ".join((draft.metadata or {}).get("images", [])), key=f"e_images_{ts}")
            preview = st.text_area("发布预览", f"{title}\n\n{body}\n\n{fmt_tags(parse_tags(tags))}",
                                   height=200, disabled=True)

        if st.button("应用编辑", icon=":material/save:", width="stretch"):
            draft.title = title
            draft.body = body
            draft.tags = parse_tags(tags)
            draft.cover_text = cover_text or None
            draft.metadata["images"] = [p for p in images.replace("，", ",").split(",") if p.strip()]
            st.session_state.draft = draft
            st.session_state.gen_ts += 1   # 换 key，让编辑框展示编辑后的内容
            st.session_state.check_result = None
            st.success("已应用编辑，可继续「检验」")

        # ---- 封面生成（多模态 · 描述驱动） ----
        cover_desc = st.text_input("封面描述（可选，按你的描述生成）",
                                   (st.session_state.draft.metadata or {}).get("cover_desc", "")
                                   if st.session_state.draft else "",
                                   placeholder="如：粉色渐变 可爱风 / 深色高级感 金色线条 / 简约留白 黑白",
                                   icon=":material/palette:")
        if st.button("按描述生成封面", icon=":material/image:", type="secondary", width="stretch",
                     help="填写描述后点此生成；不填则用当前风格自动生成"):
            from core.covergen import ensure_cover_for_draft
            draft = st.session_state.draft
            if draft is None or not (draft.title or "").strip():
                st.warning("请先生成/填写标题，再生成封面")
            else:
                draft.metadata["cover_desc"] = cover_desc.strip() or None
                cover = ensure_cover_for_draft(draft)
                if cover:
                    st.session_state.draft = draft
                    st.session_state.cover_preview = cover
                    st.success("封面已生成 ✅（发布时自动上传，界面已预览）")
                else:
                    st.warning("生成封面失败")
        if st.session_state.get("cover_preview"):
            st.image(st.session_state.cover_preview, caption="当前封面预览（发布自动上传）", width=300)

        st.space("medium")

        ck1, ck2, ck3 = st.columns(3)
        # ---- 检验 ----
        if ck1.button(
            "检验（合规 + 风格 + 就绪）",
            icon=":material/fact_check:",
            width="stretch",
        ):
            draft = st.session_state.draft
            comp = compliance().check_draft(draft)
            style = StyleEnforcer(load_persona()).enforce(draft)
            gate = get("xhs_publish").run(
                __import__("core.types", fromlist=["SkillInput"]).SkillInput(draft=draft, context={})
            ).draft
            st.session_state.check_result = {
                "compliance": comp, "style": style,
                "ready": gate.metadata.get("publish_ready"),
                "issues": gate.metadata.get("publish_issues", []),
            }

        res = st.session_state.check_result
        if res:
            comp, style = res["compliance"], res["style"]
            with st.expander("检验报告", expanded=True):
                st.markdown(f"**合规**：{'✅ 通过' if comp.ok else f'❌ {len(comp.hits)} 处命中'}")
                for idx, h in enumerate(comp.hits):
                    hc1, hc2 = st.columns([6, 1])
                    field_name = _FIELD_LABEL.get(h.field, "")
                    with hc1:
                        st.warning(f"  - {h}")
                    with hc2:
                        if st.button("定位", icon=":material/my_location:", key=f"jump_{idx}",
                                     help=f"就地展开{field_name or '对应'}编辑框",
                                     disabled=not h.field):
                            st.session_state.jump_target = {
                                "field": h.field, "match": h.match,
                                "suggestion": h.suggestion, "hit_idx": idx,
                            }
                            st.rerun()

                # ---- 就地定位编辑：点「📍 定位」后在违规项下方展开 ----
                jump = st.session_state.get("jump_target")
                if jump and jump.get("field"):
                    fld = jump["field"]
                    st.markdown("---")
                    st.markdown(f"#### 📍 定位到「{_FIELD_LABEL.get(fld, fld)}」")
                    cur_text = _field_text(st.session_state.draft, fld)
                    # 高亮命中词预览
                    st.markdown(
                        f"命中词 <mark>{html.escape(jump.get('match') or '')}</mark>"
                        + (f"　→ 建议：{html.escape(jump.get('suggestion'))}" if jump.get("suggestion") else ""),
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        "<div style='background:#fff5f5;padding:8px 12px;border-radius:6px;border:1px solid #f5c6cb'>"
                        + _highlight_match(cur_text, jump.get("match") or "")
                        + "</div>",
                        unsafe_allow_html=True,
                    )
                    new_val = st.text_area(
                        f"就地修改「{_FIELD_LABEL.get(fld, fld)}」",
                        cur_text,
                        key=f"inline_fix_{fld}_{jump.get('hit_idx', 0)}",
                        height=160 if fld == "body" else 80,
                    )
                    b1, b2, b3 = st.columns([1, 1, 3])
                    if b1.button("保存修改", icon=":material/save:", key=f"save_fix_{fld}_{jump.get('hit_idx', 0)}",
                                 type="primary"):
                        d = st.session_state.draft
                        before = cur_text
                        if fld == "tags":
                            d.tags = parse_tags(new_val)
                            after_str = fmt_tags(d.tags)
                        else:
                            setattr(d, fld, new_val)
                            after_str = new_val
                        st.session_state.draft = d
                        # 换 key（gen_ts+1）让顶部编辑框重建并读到新值。
                        # 不要额外写 session_state[f"e_body_{..}"]，否则会与
                        # text_area 的 value 参数冲突触发 Streamlit 警告。
                        st.session_state.gen_ts += 1
                        st.session_state.last_fix = {"field": fld, "before": before, "after": after_str}
                        st.session_state.jump_target = None
                        st.session_state.check_result = None   # 需重新检验
                        st.rerun()
                    if b2.button("取消", key=f"cancel_fix_{fld}_{jump.get('hit_idx', 0)}"):
                        st.session_state.jump_target = None
                        st.rerun()

                # ---- 修改结果展示（保存后就地显示 diff 高亮） ----
                last_fix = st.session_state.get("last_fix")
                if last_fix:
                    st.markdown("---")
                    st.markdown(f"#### ✅ 已修改「{_FIELD_LABEL.get(last_fix['field'], last_fix['field'])}」")
                    st.caption("红 = 删掉　绿 = 新增（改完请重新点「🔍 检验」）")
                    st.markdown(
                        "<div style='background:#fafafa;padding:8px 12px;border-radius:6px;"
                        "border:1px solid #ddd;white-space:pre-wrap'>"
                        + _diff_highlight(last_fix["before"], last_fix["after"])
                        + "</div>",
                        unsafe_allow_html=True,
                    )
                st.markdown("---")
                st.markdown(f"**风格**：{'✅ 通过' if style.ok else '❌ 有超标项'}")
                for i in style.issues:
                    st.warning(f"  - {i}")
                st.markdown(f"**发布就绪**：{'✅ 可以发布' if res['ready'] else '❌ 未就绪'}")
                for i in res["issues"]:
                    st.warning(f"  - {i}")

        # ---- 持久化人工审批：草稿一变，旧审批立即失效 ----
        approval_store = PublishApprovalStore(LANGGRAPH_CHECKPOINT_PATH)
        approval = None
        if st.session_state.publish_approval_id:
            approval = approval_store.get(st.session_state.publish_approval_id)
            if approval and approval.draft_hash != draft_fingerprint(st.session_state.draft):
                st.session_state.publish_approval_id = ""
                approval = None
                st.warning("草稿已修改，原发布审批自动失效，请重新提交。", icon=":material/edit_note:")

        approval_box = st.container(border=True)
        approval_box.subheader(":material/approval: 发布审批", anchor=False)
        check_ready = bool(st.session_state.check_result and st.session_state.check_result.get("ready"))
        if approval is None:
            approval_box.caption("先完成检验，再提交人工审批。审批状态保存到 SQLite，重启后仍可恢复。")
            if approval_box.button(
                "提交发布审批",
                icon=":material/send:",
                disabled=not check_ready,
                key="submit_publish_approval",
            ):
                approval = approval_store.request(st.session_state.draft, "web_user")
                st.session_state.publish_approval_id = approval.request_id
                st.rerun()
        elif approval.status == "pending":
            approval_box.warning(f"等待人工决定 · `{approval.request_id}`")
            approve_col, reject_col = approval_box.columns(2)
            if approve_col.button("批准", icon=":material/check_circle:", type="primary"):
                approval_store.decide(approval.request_id, "approved", reason="Streamlit 人工确认")
                st.rerun()
            if reject_col.button("拒绝", icon=":material/cancel:"):
                approval_store.decide(approval.request_id, "rejected", reason="Streamlit 人工拒绝")
                st.rerun()
        elif approval.status == "approved":
            approval_box.success(f"审批已通过 · `{approval.request_id}`；仅当前草稿可使用。")
        elif approval.status == "consumed":
            approval_box.info("该审批已用于一次发布，不会被重复消费。")
        else:
            approval_box.error(f"审批已拒绝：{approval.reason or '未填写原因'}")
            if approval_box.button("重新提交审批", icon=":material/replay:"):
                st.session_state.publish_approval_id = ""
                st.rerun()

        approval_ready = bool(approval and approval.status == "approved")

        # ---- 发布 ----
        pub_c1, pub_c2, pub_c3 = st.columns([1, 1, 1])
        can_real_publish = real_publish_enabled()
        if not can_real_publish:
            st.info(f"🛡️ {real_publish_disabled_message()}")
        browser_ch = pub_c1.selectbox("浏览器", ["msedge", "chrome", "chromium"], index=0,
                                      help="系统 Edge/Chrome 免下载；chromium 需已装 Playwright 内核")
        headed_mode = pub_c1.checkbox("有头模式（弹出窗口，可扫码验证）", value=False,
                                      help="小红书风控要求扫码验证时勾选此项：会弹出浏览器，你用小红书APP扫码后发布")
        keep_failure_browser = pub_c1.checkbox(
            "调试：失败时保留浏览器", value=True, disabled=not headed_mode,
            help="仅有头模式有效。发布失败时不自动关闭窗口，关闭窗口后才返回结果。",
        )
        if pub_c2.button("干跑发布（不真发）", icon=":material/science:", width="stretch"):
            draft = st.session_state.draft
            r = DryRunPublisher().publish(draft)
            st.info(f"{r.message}（{r.cost_ms}ms）")

        if pub_c3.button("真实发布", icon=":material/rocket_launch:", width="stretch", type="primary",
                          disabled=not STATE_PATH.exists() or not can_real_publish or not approval_ready):
            if not st.session_state.pub_confirm:
                st.session_state.pub_confirm = True
                st.warning("⚠️ 二次确认：再次点击「真实发布」即真发到小红书")
            else:
                draft = st.session_state.draft
                orch, _ = build_orch()
                pub = XhsPlaywrightPublisher(
                    storage_state=str(STATE_PATH), headless=not headed_mode,
                    channel=None if browser_ch == "chromium" else browser_ch,
                    auto_approve=True, harness=orch.harness, user_id="web_user",
                    keep_browser_on_failure=keep_failure_browser,
                    approval_store=approval_store,
                    approval_request_id=approval.request_id if approval else "",
                    require_persisted_approval=True,
                )
                log_lines: list[str] = []
                with st.spinner("发布中…（上传图文 → 填内容 → 点发布）"):
                    try:
                        r = pub.publish(draft)
                        # 前端展示浏览器检测/切换原因（如「未装 Chrome，已切 Edge」）
                        for n in getattr(pub, "browser_notes", []):
                            st.warning(f"⚠️ {n}")
                        if r.success:
                            st.success(f"✅ {r.message}")
                            PublishLog(path=str(LOG_PATH)).load().record(
                                f"web-{datetime.now():%Y%m%d%H%M%S}", "published", url=r.url or "")
                        else:
                            st.error(f"发布失败: {r.message}")
                    except (LoginRequired, ApprovalDenied) as e:
                        st.error(f"未发布: {e}")
                    except Exception as e:  # noqa: BLE001
                        st.error(f"发布异常: {e}")
                st.session_state.pub_confirm = False

        # 核对已发布笔记（调用 notes 命令，界面直接看结果）
        if st.button("核对已发布笔记", icon=":material/inventory_2:", width="stretch"):
            with st.spinner("打开笔记管理核对…"):
                notes_args = [sys.executable, "main.py", "notes"]
                if browser_ch != "chromium":
                    notes_args.append(f"--browser={browser_ch}")
                proc = subprocess.run(notes_args, cwd=str(BASE),
                                      capture_output=True, text=True, encoding="utf-8")
            st.code(proc.stdout[-1200:] or proc.stderr[-800:])

        if not STATE_PATH.exists():
            st.caption("💡 真实发布前需先登录：`python main.py login --browser msedge`")


# ============================================================
# 页 2：AI 助手
# ============================================================
elif page == "assistant":
    _page_header(
        "AI 助手",
        "按需检索本地知识库，回答附来源与执行轨迹；助手没有发布权限。",
        icon="smart_toy",
        eyebrow="Read-only assistant",
    )

    from core.assistant import (
        AssistantCheckpointStore,
        AssistantMemoryStore,
        AssistantOrchestrator,
        get_session_harness,
    )
    from core.llm import _backend, _ollama_reachable
    from core.llm import complete as llm_complete
    from core.memory import propose_memory_summary
    from core.types import AssistantRequest, ChatMessage

    assistant_config = st.container(border=True)
    assistant_config.subheader(":material/tune: 对话设置", anchor=False)
    a1, a2, a3, a4 = assistant_config.columns([2, 2, 1, 1])
    backend_labels = ["ollama（本地）", "deepseek（云端）", "offline（离线模板）"]
    current_backend = _backend()
    backend_index = {"ollama": 0, "deepseek": 1, "offline": 2}.get(current_backend, 0)
    assistant_backend_label = a1.selectbox(
        "对话后端", backend_labels, index=backend_index, key="assistant_backend")
    assistant_backend = assistant_backend_label.split("（", 1)[0]
    if assistant_backend == "ollama":
        assistant_models = ["qwen2.5:3b", "qwen2.5:7b", "qwen2.5:14b", "deepseek-r1:7b"]
    elif assistant_backend == "deepseek":
        assistant_models = ["deepseek-chat", "deepseek-reasoner"]
    else:
        assistant_models = ["离线模板"]
    assistant_model = a2.selectbox("模型", assistant_models, key="assistant_model")
    use_knowledge = a3.checkbox(
        "本地知识库", value=True, key="assistant_use_kb", persist_state="session"
    )
    use_memory = a4.checkbox(
        "对话记忆", value=True, key="assistant_use_memory", persist_state="session",
        help="恢复最近会话，并把你主动保存的长期记忆加入上下文。",
    )
    llm_configure(
        backend=assistant_backend,
        model=None if assistant_backend == "offline" else assistant_model,
        temperature=0.3,
        max_tokens=700,
    )

    if assistant_backend == "ollama":
        assistant_config.caption(
            f"Ollama：{'已连接' if _ollama_reachable() else '未连接'} · "
            "对话最多 4 步 · 最近 8 条消息进入上下文"
        )
    elif assistant_backend == "deepseek" and not os.getenv("DEEPSEEK_API_KEY"):
        assistant_config.warning(
            "未配置 DEEPSEEK_API_KEY；请选择本地 Ollama，或在 `.env` 中配置 Key。",
            icon=":material/key_off:",
        )

    checkpoint_store = AssistantCheckpointStore(ASSISTANT_CHECKPOINT_PATH)
    memory_store = AssistantMemoryStore(ASSISTANT_CHECKPOINT_PATH)
    memory_user_id = "local_user"

    if use_memory and not st.session_state.assistant_memory_bootstrapped:
        latest_threads = checkpoint_store.list_threads(limit=1)
        if latest_threads:
            restored_thread = latest_threads[0].thread_id
            restored_messages = checkpoint_store.load(restored_thread)
            st.session_state.assistant_thread_id = restored_thread
            st.session_state.assistant_messages = [
                message.model_dump() for message in restored_messages
            ]
        st.session_state.assistant_memory_bootstrapped = True

    saved_memories = memory_store.list(memory_user_id, limit=8) if use_memory else []
    controls = st.container(horizontal=True, gap="small", vertical_alignment="center")
    if controls.button("新建会话", icon=":material/add_comment:"):
        st.session_state.assistant_messages = []
        st.session_state.assistant_thread_id = f"ui-{uuid.uuid4().hex[:12]}"
        st.rerun()
    if controls.button("清空当前", icon=":material/delete_sweep:"):
        checkpoint_store.delete(st.session_state.assistant_thread_id)
        st.session_state.assistant_messages = []
        st.session_state.assistant_thread_id = f"ui-{uuid.uuid4().hex[:12]}"
        st.rerun()

    history_popover = controls.popover("历史会话", icon=":material/history:")
    with history_popover:
        thread_summaries = checkpoint_store.list_threads(limit=12)
        if not thread_summaries:
            st.caption("还没有可恢复的历史会话。")
        for summary in thread_summaries:
            stamp = datetime.fromtimestamp(summary.updated_at).strftime("%m-%d %H:%M")
            if st.button(
                f"{stamp} · {summary.preview or '空会话'}",
                key=f"resume_{summary.thread_id}",
                width="stretch",
            ):
                st.session_state.assistant_thread_id = summary.thread_id
                st.session_state.assistant_messages = [
                    message.model_dump() for message in checkpoint_store.load(summary.thread_id)
                ]
                st.rerun()

    memory_popover = controls.popover(
        f"长期记忆 {len(saved_memories)}", icon=":material/psychology:"
    )
    with memory_popover:
        st.caption("只保存你主动确认的偏好或摘要；常见手机号、邮箱和身份证号会自动掩码。")
        memory_kind = st.selectbox(
            "记忆类型", ["preference", "summary"],
            format_func=lambda value: "稳定偏好" if value == "preference" else "对话摘要",
            key="assistant_memory_kind",
        )
        ttl_label = st.selectbox(
            "保留时间", ["不过期", "30 天", "90 天", "365 天"],
            index=2,
            key="assistant_memory_ttl",
        )
        ttl_days = {"不过期": None, "30 天": 30, "90 天": 90, "365 天": 365}[ttl_label]
        memory_text = st.text_input(
            "新增记忆",
            placeholder="例如：我正在准备武汉地区的 Agent 岗秋招",
            max_chars=500,
            key="assistant_memory_input",
        )
        if st.button("保存记忆", icon=":material/save:", type="primary"):
            try:
                memory_store.add(
                    memory_user_id,
                    memory_text,
                    kind=memory_kind,
                    source_thread_id=st.session_state.assistant_thread_id,
                    ttl_days=ttl_days,
                )
                st.rerun()
            except ValueError as exc:
                st.warning(str(exc))

        if st.button(
            "生成摘要候选",
            icon=":material/summarize:",
            disabled=not bool(st.session_state.assistant_messages),
            help="只生成候选，不会自动写入长期记忆。",
        ):
            try:
                candidate = propose_memory_summary(
                    [ChatMessage(role=item["role"], content=item["content"])
                     for item in st.session_state.assistant_messages],
                    llm_complete,
                )
                st.session_state.memory_summary_candidate = candidate
                st.session_state.assistant_summary_editor = candidate
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.warning(f"摘要候选生成失败：{exc}")
        if st.session_state.memory_summary_candidate:
            summary_candidate = st.text_area(
                "待确认摘要", max_chars=500, key="assistant_summary_editor"
            )
            if st.button("确认并保存摘要", icon=":material/task_alt:"):
                memory_store.add(
                    memory_user_id,
                    summary_candidate,
                    kind="summary",
                    source_thread_id=st.session_state.assistant_thread_id,
                    ttl_days=ttl_days,
                )
                st.session_state.memory_summary_candidate = ""
                st.session_state.pop("assistant_summary_editor", None)
                st.rerun()

        for memory in saved_memories:
            row = st.container(horizontal=True, vertical_alignment="center")
            expiry = (
                datetime.fromtimestamp(memory.expires_at).strftime("%Y-%m-%d")
                if memory.expires_at else "不过期"
            )
            masked = " · 已脱敏" if memory.redacted else ""
            row.caption(f"[{memory.kind}] {memory.content} · {expiry}{masked}")
            if row.button(
                "删除", icon=":material/delete:",
                key=f"delete_memory_{memory.memory_id}",
            ):
                memory_store.delete(memory_user_id, memory.memory_id)
                st.rerun()

        memory_export = json.dumps(
            memory_store.export(memory_user_id), ensure_ascii=False, indent=2
        )
        st.download_button(
            "导出全部记忆",
            data=memory_export,
            file_name="personax_memories.json",
            mime="application/json",
            icon=":material/download:",
            width="stretch",
        )
        if not st.session_state.memory_delete_confirm:
            if st.button("删除全部记忆", icon=":material/delete_forever:", width="stretch"):
                st.session_state.memory_delete_confirm = True
                st.rerun()
        else:
            st.warning("再次确认会永久删除当前用户的全部长期记忆。")
            delete_col, cancel_col = st.columns(2)
            if delete_col.button("确认删除", type="primary"):
                memory_store.delete_all(memory_user_id)
                st.session_state.memory_delete_confirm = False
                st.rerun()
            if cancel_col.button("取消"):
                st.session_state.memory_delete_confirm = False
                st.rerun()

    controls.caption(
        f"会话 `{st.session_state.assistant_thread_id}` · "
        f"最近 8 条消息 + {len(saved_memories)} 条长期记忆"
        if use_memory else
        f"会话 `{st.session_state.assistant_thread_id}` · 本轮不读写记忆"
    )

    if not st.session_state.assistant_messages:
        st.info(
            "可以问项目架构、RAG 检索、内容优化或使用方法。先从下面任选一个问题开始。",
            icon=":material/lightbulb:",
        )

    suggested_prompt = None
    if not st.session_state.assistant_messages:
        suggestion = st.pills(
            "快捷问题",
            ["解读 PersonaX 2.7 架构", "分析 RAG 检索链路", "给项目优化建议"],
            key="assistant_suggestion",
            width="stretch",
        )
        suggestion_map = {
            "解读 PersonaX 2.7 架构": "请解释 PersonaX 2.7 的持久化审批、父子检索、回答反馈闭环和一次对话的执行流程。",
            "分析 RAG 检索链路": "项目里的 BM25、dense kNN、RRF 和相关性门控分别解决什么问题？",
            "给项目优化建议": "结合当前 PersonaX 项目，给我 3 条优先级最高、可验证的优化建议。",
        }
        suggested_prompt = suggestion_map.get(suggestion)

    for message in st.session_state.assistant_messages:
        avatar = ":material/person:" if message["role"] == "user" else ":material/smart_toy:"
        with st.chat_message(message["role"], avatar=avatar):
            st.markdown(message["content"])
            if message["role"] == "assistant":
                _render_assistant_meta(message)

    typed_prompt = st.chat_input(
        "输入问题，Enter 发送（不会自动发布内容）",
        max_chars=2_000,
        submit_mode="stop",
    )
    prompt = suggested_prompt or typed_prompt
    if prompt:
        history = [
            ChatMessage(role=item["role"], content=item["content"])
            for item in st.session_state.assistant_messages
        ]
        user_message = {"role": "user", "content": prompt}
        st.session_state.assistant_messages.append(user_message)
        with st.chat_message("user", avatar=":material/person:"):
            st.markdown(prompt)

        persona = load_persona()
        harness = get_session_harness(st.session_state, persona)
        service = AssistantOrchestrator(
            persona,
            harness=harness,
            checkpoint_store=checkpoint_store if use_memory else None,
            prompt_path=BASE / "config" / "assistant_prompts.yaml",
        )
        with st.chat_message("assistant", avatar=":material/smart_toy:"):
            status = st.status(
                "Agent 正在检索、规划并流式回答…", expanded=False, type="compact"
            )
            response_stream = service.reply_stream(AssistantRequest(
                question=prompt,
                history=history,
                memories=[memory.content for memory in saved_memories],
                thread_id=st.session_state.assistant_thread_id,
                use_knowledge=use_knowledge,
                max_steps=4,
            ))
            st.write_stream(response_stream)
            response = response_stream.response
            if response is None:
                raise RuntimeError("回答流未正常结束")
            status.update(
                label="回答已生成" + ("，记忆已保存" if use_memory else ""),
                state="complete",
            )
            assistant_message = {
                "role": "assistant",
                "content": response.answer,
                "question": prompt,
                "sources": [source.model_dump() for source in response.sources],
                "trace": [item.model_dump() for item in response.trace],
                "route": response.route,
                "checkpoint_id": response.checkpoint_id,
                "trace_id": response.trace_id,
                "thread_id": st.session_state.assistant_thread_id,
                "degraded": response.degraded,
            }
            _render_assistant_meta(assistant_message)
        st.session_state.assistant_messages.append(assistant_message)


# ============================================================
# 页 3：内容库与定时
# ============================================================
elif page == "schedule":
    _page_header(
        "内容库与定时",
        "集中管理草稿、计划时间与发布留痕。真实发布仍受安全锁控制。",
        icon="calendar_month",
        eyebrow="Content operations",
    )

    bank = ContentBank(str(BANK_DIR))
    log = PublishLog(path=str(LOG_PATH)).load()
    items = bank.list_items()

    st.subheader(":material/add_task: 新建定时稿件", anchor=False)
    with st.form("new_draft", clear_on_submit=True):
        f1, f2, f3 = st.columns([2, 2, 1])
        n_topic = f1.text_input("主题", "秋招穿搭")
        n_time = f2.datetime_input("发布时间", datetime.now() + timedelta(hours=2), step=600)
        n_title = st.text_input("标题（可留空，发布时自动生成）")
        n_body = st.text_area("正文（可留空，发布时自动生成）", height=120)
        n_tags = st.text_input("标签（空格分隔，可留空）")
        n_img = st.text_input("配图路径（逗号分隔，可选）")
        submitted = st.form_submit_button(
            "存入内容库（定时）", icon=":material/save:", type="primary"
        )
    if submitted:
        BANK_DIR.mkdir(parents=True, exist_ok=True)
        fid = f"auto-{datetime.now():%Y%m%d-%H%M%S}"
        payload = {
            "id": fid, "topic": n_topic,
            "scheduled_at": n_time.strftime(TIME_FMT),
            "title": n_title or None, "body": n_body or None,
            "tags": parse_tags(n_tags),
            "images": [p.strip() for p in n_img.replace("，", ",").split(",") if p.strip()],
        }
        (BANK_DIR / f"{fid}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        st.success(f"已存入内容库，将于 {payload['scheduled_at']} 到期")
        st.rerun()

    st.space("medium")
    st.subheader(f":material/folder_open: 内容库稿件（{len(items)} 篇）", anchor=False)

    now = datetime.now()
    for it in items:
        due = it.scheduled_at and it.scheduled_at <= now
        rec = log.records.get(it.id)
        status = "✅ 已发布" if rec and rec["status"] == "published" else \
                 ("⛔ " + rec["status"] if rec else ("🕐 待发布" if not due else "🔔 已到期"))
        with st.expander(f"{it.topic} ｜ {status} ｜ {it.scheduled_at:%Y-%m-%d %H:%M}" if it.scheduled_at
                         else f"{it.topic} ｜ {status}"):
            st.code(it.data.get("title") or "（标题自动生成）")
            st.text(it.data.get("body") or "（正文自动生成）")
            st.caption(f"标签: {it.data.get('tags')}  配图: {it.data.get('images')}")
            if rec:
                st.caption(f"留痕: {rec}")
            b1, b2, b3 = st.columns(3)
            if b1.button("载入编辑", icon=":material/edit:", key=f"load_{it.id}"):
                st.session_state.draft = it.to_draft()
                st.session_state.check_result = None
                st.session_state.pub_confirm = False
                st.session_state.gen_ts += 1
                st.session_state.nav = "generate"
                st.rerun()
            if b2.button("干跑发布", icon=":material/science:", key=f"dry_{it.id}"):
                draft = it.to_draft()
                if not (draft.title and draft.body):
                    orch, _ = build_orch()
                    draft = orch.run(topic=it.topic, user_id="web_user")
                r = DryRunPublisher().publish(draft)
                st.info(r.message)
            if b3.button("删除", icon=":material/delete:", key=f"del_{it.id}"):
                Path(it.path).unlink(missing_ok=True)
                st.rerun()

    st.space("medium")
    st.subheader(":material/schedule: 执行定时任务", anchor=False)
    e1, e2 = st.columns(2)
    if e1.button("执行到期任务（干跑）", icon=":material/play_arrow:", width="stretch"):
        orch, persona = build_orch()
        sched = PublishScheduler(orchestrator=orch, publisher=DryRunPublisher(),
                                 compliance=compliance(), bank=bank,
                                 log=PublishLog(path=str(LOG_PATH)))
        report = sched.run_once()
        if not report:
            st.info("当前无到期待发稿件")
        for r in report:
            st.write(f"`[{r['id']}]` {r['topic']} → **{r['status']}**：{r.get('reason', r.get('url', ''))}")
        st.rerun()
    if e2.button("执行到期任务（真实发布，需已登录）", icon=":material/rocket_launch:", width="stretch",
                  disabled=not STATE_PATH.exists() or not real_publish_enabled()):
        orch, persona = build_orch()
        pub = XhsPlaywrightPublisher(storage_state=str(STATE_PATH), headless=True,
                                     auto_approve=True, harness=orch.harness, user_id="web_user")
        sched = PublishScheduler(orchestrator=orch, publisher=pub,
                                 compliance=compliance(), bank=bank,
                                 log=PublishLog(path=str(LOG_PATH)), auto_approve=True)
        report = sched.run_once()
        if not report:
            st.info("当前无到期待发稿件")
        for r in report:
            st.write(f"`[{r['id']}]` {r['topic']} → **{r['status']}**：{r.get('reason', r.get('url', ''))}")
        st.rerun()
    st.caption("无人值守常驻请用命令行：`python main.py schedule --daemon --interval 60 --real --yes --browser msedge`（需先在 .env 解锁真实发布）")


# ============================================================
# 页 4：知识库（喂优质范例 → 让生成更自然）
# ============================================================
elif page == "knowledge":
    _page_header(
        "知识库",
        "管理写作范例与可溯源公开参考，检索时保留来源、主题和角色信息。",
        icon="database",
        eyebrow="Retrieval knowledge",
    )

    KNOWLEDGE_DIR = BASE / "knowledge"
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)

    with st.form("kb_add", clear_on_submit=True):
        k1, k2, k3 = st.columns([2, 1, 2])
        kb_topic = k1.text_input("主题（必填，用于检索）", "秋招穿搭")
        kb_style = k2.text_input("风格描述", "口语化/短句/互动")
        kb_kw = k3.text_input("关键词（逗号分隔）", "面试, 穿搭, 秋招")
        kb_body = st.text_area("范例正文（粘贴你满意的笔记，体现结构/语气/互动）", height=280,
                               placeholder="家人们谁懂啊，……\n\n第一，……\n第二，……\n\n你们……评论区聊聊～")
        kb_submit = st.form_submit_button("存入知识库", icon=":material/save:", type="primary")
    if kb_submit:
        if not kb_topic.strip() or not kb_body.strip():
            st.warning("主题和范例正文必填")
        else:
            kws = [k.strip() for k in kb_kw.replace("，", ",").split(",") if k.strip()]
            fm = "---\ntopic: %s\nstyle: %s\nkeywords: [%s]\n---\n\n%s\n" % (
                kb_topic.strip(), kb_style.strip() or "口语化",
                ", ".join(kws), kb_body.strip())
            fname = (kb_topic.strip().replace("/", "_").replace("\\", "_")[:40]) + ".md"
            (KNOWLEDGE_DIR / fname).write_text(fm, encoding="utf-8")
            st.success(f"已存入知识库：knowledge/{fname}")
            st.rerun()

    st.space("medium")
    st.subheader(":material/library_books: 现有知识库", anchor=False)
    kb_files = sorted(KNOWLEDGE_DIR.rglob("*.md"))
    if not kb_files:
        st.info("知识库为空，先在上面粘贴一篇进去")
    public_count = sum("public" in f.relative_to(KNOWLEDGE_DIR).parts for f in kb_files)
    st.caption(
        f"共 {len(kb_files)} 篇：自建范例 {len(kb_files) - public_count} 篇，"
        f"可溯源公开参考 {public_count} 篇。公开资料只用于事实背景，不会被当作文风范例。"
    )
    for f in kb_files:
        content = f.read_text(encoding="utf-8", errors="ignore")
        from core.rag import _parse_frontmatter
        metadata, _ = _parse_frontmatter(content)
        topic = str(metadata.get("topic") or f.stem)
        role = str(metadata.get("retrieval_role") or "example")
        role_label = "公开参考" if role == "reference" else "自建范例"
        relative_path = f.relative_to(KNOWLEDGE_DIR)
        c1, c2 = st.columns([3, 1])
        c1.caption(f"**{topic}** ｜ {role_label} ｜ `{relative_path}` ｜ {len(content)} 字")
        item_key = relative_path.as_posix()
        details = st.expander(
            "查看/复制这篇资料",
            on_change="rerun",
            key=f"kbview_{item_key}",
        )
        if details.open:
            with details:
                if metadata.get("source_url"):
                    st.link_button("打开原始来源", str(metadata["source_url"]), icon=":material/open_in_new:")
                st.text(content)
        if c2.button("删除", icon=":material/delete:", key=f"kbdel_{item_key}", width="stretch"):
            f.unlink(missing_ok=True)
            st.rerun()


# ============================================================
# 页 5：设置（内容格式 / 人格 / 合规词表）
# ============================================================
elif page == "settings":
    _page_header(
        "设置",
        "管理人格、联网增强、封面生成与合规词表。高级配置保持本地可审计。",
        icon="tune",
        eyebrow="Workspace settings",
    )

    # ---------- 人设库（多人格） ----------
    st.subheader(":material/groups: 人设库", anchor=False)
    st.caption("生成时按所选人格控制语气、习惯与禁用表达。")
    from core.persona import list_personas, get_persona, add_persona, _load_yaml
    PERSONAS_PATH = str(BASE / "config" / "personas.yaml")
    names = list_personas()
    sel_name = st.selectbox("已有的人格", ["➕ 新增人格…"] + names)

    if sel_name == "➕ 新增人格…":
        new_name = st.text_input("新人格名（如：职场干货君）", key="np_name")
        new_persona_yaml = st.text_area(
            "人格定义（YAML，字段：name/tone/habits/forbidden/sentence_length/可选 generation）",
            "name: 职场干货君\ntone: professional_warm\nhabits:\n  - 开头给结论\n  - 分点讲\nforbidden:\n  - 绝对化用语\nsentence_length: medium",
            height=260, key="np_yaml")
        if st.button("新增人格", icon=":material/person_add:", type="primary"):
            try:
                import yaml as _y
                data = _y.safe_load(new_persona_yaml)
                if not isinstance(data, dict) or not data.get("name"):
                    st.error("人格定义需包含 name 字段")
                else:
                    add_persona(new_name.strip(), data)
                    st.success(f"已新增人格「{new_name}」，生成页可选用")
                    st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"YAML 解析失败: {e}")
    else:
        p = get_persona(sel_name)
        import yaml as _y
        edit_yaml = st.text_area(f"编辑「{sel_name}」的人格定义（YAML）",
                                 _y.safe_dump(p, allow_unicode=True, sort_keys=False),
                                 height=320, key=f"editp_{sel_name}")
        bc1, bc2 = st.columns(2)
        if bc1.button("保存修改", icon=":material/save:"):
            try:
                data = _y.safe_load(edit_yaml)
                add_persona(sel_name, data)
                st.success(f"已更新人格「{sel_name}」")
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"YAML 解析失败: {e}")
        if bc2.button("删除人格", icon=":material/person_remove:"):
            data = _load_yaml(PERSONAS_PATH)
            data.pop(sel_name, None)
            with open(PERSONAS_PATH, "w", encoding="utf-8") as f:
                _y.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            st.success(f"已删除人格「{sel_name}」")
            st.rerun()

    st.space("medium")
    st.subheader(":material/style: 人格模板", anchor=False)
    st.caption("将模板一键套用到当前默认 persona.yaml。")
    presets = {
        "温暖闺蜜风（默认）": {"name": "小鹿学姐", "tone": "warm_girly",
                             "habits": ["每3句一个emoji", "结尾常用\"冲鸭/绝绝子/家人们\"", "善用\"谁懂啊\"", "短句优先，口语化"],
                             "forbidden": ["书面语\"综上所述\"", "英文技术缩写"], "sentence_length": "short"},
        "干货知识风": {"name": "笔记君", "tone": "professional_warm",
                      "habits": ["开头直接给结论", "多用序号分点", "每段一个重点", "结尾总结+引导收藏"],
                      "forbidden": ["绝对化用语", "保证效果"], "sentence_length": "medium"},
        "种草测评风": {"name": "好物测评酱", "tone": "energetic",
                      "habits": ["开头抛痛点", "优缺点都要写", "价格/渠道透明", "结尾互动提问"],
                      "forbidden": ["虚假宣传", "夸大功效"], "sentence_length": "short"},
        "旅行探店风": {"name": "阿鹿的旅程", "tone": "fresh",
                      "habits": ["地点+体验叙事", "多场景描写", "交通/人均花费写明", "结尾给建议"],
                      "forbidden": ["广告硬广"], "sentence_length": "medium"},
    }
    preset_name = st.selectbox("选择模板", list(presets.keys()))
    if st.button("套用模板到 persona.yaml", icon=":material/palette:"):
        data = load_persona()
        data.update(presets[preset_name])
        save_persona(data)
        st.success(f"已套用「{preset_name}」，可在下方微调")

    st.space("medium")

    # ---------- 联网增强开关 ----------
    st.subheader(":material/public: 联网增强", anchor=False)
    st.caption("生成前获取热点和参考资料；搜不到内容时自动跳过。")
    from core.websearch import configure as ws_configure, _enabled as ws_enabled, _backend as ws_backend
    st.session_state.setdefault("ws_on", ws_enabled())
    st.session_state.setdefault("ws_be", ws_backend() if ws_backend() in ("bing", "bocha", "tavily") else "bing")
    ws_on = st.checkbox("开启联网（每篇多花 3-8 秒；搜到相关内容才注入，搜不到不影响）",
                        key="ws_on")
    be_idx = ["bing", "bocha", "tavily"].index(st.session_state.ws_be) \
        if st.session_state.ws_be in ("bing", "bocha", "tavily") else 0
    ws_be = st.selectbox("联网后端", ["bing", "bocha", "tavily"], index=be_idx, key="ws_be",
                         help="bing=免费免Key；bocha/tavily 需在 .env 配 BOCHA_API_KEY / TAVILY_API_KEY")
    # 每次渲染同步到运行时（界面改即生效，不用重启、不用改 .env）
    ws_configure(enabled=ws_on, backend=ws_be)
    st.caption(f"当前：{'✅ 已开启（' + ws_be + '）' if ws_on else '⏸ 已关闭'} —— 生成页立即可用")

    st.space("medium")

    # ---------- 封面设置 ----------
    st.subheader(":material/image: 封面设置", anchor=False)
    from core.covergen import configure as cg_configure, _style as cg_style, _ai_enabled as cg_ai
    st.session_state.setdefault("cover_style", cg_style())
    st.session_state.setdefault("cover_ai", cg_ai())
    cover_opts = ["premium（高级暗调）", "minimal（留白极简）",
                  "gradient（渐变大字）", "split（撞色几何）", "card（复古卡片）"]
    style_idx = next((i for i, o in enumerate(cover_opts)
                      if o.startswith(st.session_state.cover_style)), 0)
    cover_style = st.selectbox("封面风格（同主题保持一致的风格）", cover_opts,
                               index=style_idx, key="cover_style")
    cover_ai = st.checkbox("用 AI 背景生成封面（需在 .env 配 SILICONFLOW_API_KEY；无 Key 自动回退海报）",
                           key="cover_ai")
    cg_configure(style=cover_style.split("（")[0], ai_enabled=cover_ai)
    st.caption("发布时若稿件无图，会自动按此风格生成标题封面。")

    st.space("medium")
    st.subheader(":material/description: persona.yaml", anchor=False)
    yaml_text = st.text_area("内容格式配置（直接编辑 YAML）", PERSONA_PATH.read_text(encoding="utf-8"),
                             height=420, key="persona_yaml")
    if st.button("保存 persona.yaml", icon=":material/save:", type="primary"):
        try:
            data = yaml.safe_load(yaml_text)
            save_persona(data)
            st.success("已保存并生效")
        except yaml.YAMLError as e:
            st.error(f"YAML 语法错误，未保存: {e}")

    st.space("medium")
    st.subheader(":material/shield: compliance.yaml", anchor=False)
    comp_text = st.text_area("合规词表（直接编辑 YAML）", COMPLIANCE_PATH.read_text(encoding="utf-8"),
                             height=320, key="comp_yaml")
    if st.button("保存 compliance.yaml", icon=":material/save:"):
        try:
            yaml.safe_load(comp_text)
            COMPLIANCE_PATH.write_text(comp_text, encoding="utf-8")
            st.success("已保存")
        except yaml.YAMLError as e:
            st.error(f"YAML 语法错误，未保存: {e}")


# ============================================================
# 页 6：状态与日志
# ============================================================
else:
    _page_header(
        "状态与日志",
        "查看 LangGraph 运行、发布留痕和回归评测，定位失败节点与降级路径。",
        icon="monitoring",
        eyebrow="Agent console",
    )

    from core.llm import _ollama_reachable
    ollama_online = _ollama_reachable()
    with st.container(horizontal=True, gap="small"):
        st.badge(
            "Ollama 已连接" if ollama_online else "Ollama 未连接",
            icon=":material/memory:",
            color="green" if ollama_online else "orange",
        )
        st.badge(
            "发布登录态已就绪" if STATE_PATH.exists() else "发布登录态未配置",
            icon=":material/account_circle:",
            color="green" if STATE_PATH.exists() else "gray",
        )
        st.badge(
            "真实发布已解锁" if real_publish_enabled() else "真实发布安全锁已启用",
            icon=":material/lock_open:" if real_publish_enabled() else ":material/lock:",
            color="orange" if real_publish_enabled() else "blue",
        )

    st.subheader(":material/monitoring: Agent 遥测", anchor=False)
    from core.observability import (
        aggregate_usage,
        load_usage_events,
        safe_event_rows,
        trace_events,
    )

    usage_events, malformed_usage_lines = load_usage_events(
        BASE / "logs" / "usage.jsonl",
    )
    usage_summary = aggregate_usage(
        usage_events,
        malformed_lines=malformed_usage_lines,
    )
    tool_success = (
        f"{usage_summary.tool_success_rate:.1%}"
        if usage_summary.tool_success_rate is not None else "无样本"
    )
    degraded_rate = (
        f"{usage_summary.assistant_degraded_rate:.1%}"
        if usage_summary.assistant_degraded_rate is not None else "无样本"
    )
    cost_value = (
        f"¥{usage_summary.estimated_cost_cny:.4f}"
        if usage_summary.pricing_configured else "未配置"
    )
    with st.container(horizontal=True):
        st.metric("LLM 调用", usage_summary.llm_calls, border=True)
        st.metric("工具成功率", tool_success, border=True)
        st.metric(
            "助手 P95",
            f"{usage_summary.assistant_p95_ms:.1f} ms",
            border=True,
        )
        st.metric(
            "累计 Token",
            usage_summary.prompt_tokens + usage_summary.completion_tokens,
            border=True,
        )
        st.metric("估算成本", cost_value, border=True)
    st.caption(
        f"最近读取 {usage_summary.events} 条安全元数据 · "
        f"Trace {usage_summary.trace_count} 个 · 助手降级率 {degraded_rate}。"
        "成本需通过 LLM_INPUT_COST_CNY_PER_MILLION / "
        "LLM_OUTPUT_COST_CNY_PER_MILLION 配置；本地 Ollama 可保持为 0。"
    )
    if malformed_usage_lines:
        st.warning(
            f"已跳过 {malformed_usage_lines} 条损坏或超长遥测记录。",
            icon=":material/warning:",
        )
    trace_ids = list(dict.fromkeys(
        item.trace_id for item in reversed(usage_events) if item.trace_id
    ))
    with st.expander("查看 Trace 与最近安全事件"):
        if trace_ids:
            selected_trace_id = st.selectbox(
                "Trace ID",
                trace_ids,
                key="observability_trace_id",
            )
            st.dataframe(
                safe_event_rows(trace_events(usage_events, selected_trace_id)),
                hide_index=True,
                width="stretch",
                key="observability_trace_events",
            )
        else:
            st.info("完成一次助手对话后，这里会显示端到端 Trace。")
        st.caption("表格只展示模型、工具、状态、耗时等元数据，不展示提示词、正文或工具参数。")
        st.dataframe(
            safe_event_rows(usage_events[-50:]),
            hide_index=True,
            width="stretch",
            key="observability_recent_events",
        )

    st.subheader(":material/thumb_up: 回答反馈闭环", anchor=False)
    from core.feedback import AssistantFeedbackStore

    feedback_store = AssistantFeedbackStore(ASSISTANT_FEEDBACK_PATH)
    feedback_summary = feedback_store.summary()
    feedback_rows = feedback_store.list_recent(limit=100)
    helpful_rate = (
        f"{feedback_summary.helpful_rate:.1%}"
        if feedback_summary.helpful_rate is not None else "无样本"
    )
    with st.container(horizontal=True):
        st.metric("反馈总数", feedback_summary.total, border=True)
        st.metric("有帮助率", helpful_rate, border=True)
        st.metric("负反馈", feedback_summary.not_helpful, border=True)
        st.metric("待人工标注", feedback_summary.eval_candidates, border=True)
    st.caption(
        "评分默认只保存 Trace、回答哈希和运行元数据；只有用户显式勾选后，"
        "才在本机保存经常见 PII 掩码的问题与回答摘要。"
    )
    feedback_candidates = feedback_store.export_eval_candidates()
    with st.container(horizontal=True, vertical_alignment="center"):
        st.download_button(
            "导出评测候选",
            data=json.dumps(feedback_candidates, ensure_ascii=False, indent=2),
            file_name="personax_feedback_candidates.json",
            mime="application/json",
            icon=":material/download:",
            disabled=not bool(feedback_candidates),
        )
        if not st.session_state.feedback_delete_confirm:
            if st.button(
                "删除全部反馈",
                icon=":material/delete_forever:",
                disabled=not bool(feedback_rows),
                key="feedback_delete_start",
            ):
                st.session_state.feedback_delete_confirm = True
                st.rerun()
        if feedback_summary.reasons:
            st.caption(
                "失败原因：" + " · ".join(
                    f"{_FEEDBACK_REASONS.get(reason, reason)} {count}"
                    for reason, count in sorted(feedback_summary.reasons.items())
                )
            )
    if st.session_state.feedback_delete_confirm:
        with st.container(border=True):
            st.warning("此操作会永久删除本机全部回答反馈和评测候选。")
            with st.container(horizontal=True):
                if st.button(
                    "确认删除全部反馈",
                    type="primary",
                    icon=":material/delete_forever:",
                ):
                    feedback_store.delete_all()
                    st.session_state.feedback_delete_confirm = False
                    st.rerun()
                if st.button("取消", icon=":material/close:"):
                    st.session_state.feedback_delete_confirm = False
                    st.rerun()
    with st.expander("查看最近反馈元数据"):
        if feedback_rows:
            st.dataframe(
                [
                    {
                        "时间": datetime.fromtimestamp(item.updated_at).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "Trace ID": item.trace_id,
                        "评分": "有帮助" if item.rating == "helpful" else "需改进",
                        "原因": _FEEDBACK_REASONS.get(item.reason, item.reason or "-"),
                        "路由": item.route or "-",
                        "来源数": item.source_count,
                        "降级": item.degraded,
                        "已纳入候选": item.content_included,
                        "已脱敏": item.redacted,
                    }
                    for item in feedback_rows
                ],
                hide_index=True,
                width="stretch",
                key="observability_feedback_rows",
            )
        else:
            st.info("还没有回答反馈。到 AI 助手页面对任意回答点选赞或踩即可。")

    st.subheader(":material/account_tree: LangGraph 运行观测", anchor=False)
    from core.graph import GraphRunStore

    graph_runs = GraphRunStore(LANGGRAPH_CHECKPOINT_PATH).list_recent(limit=50)
    completed_runs = sum(item.status == "completed" for item in graph_runs)
    blocked_runs = sum(item.status == "blocked" for item in graph_runs)
    error_runs = sum(item.status == "error" for item in graph_runs)
    with st.container(horizontal=True):
        st.metric("最近运行", len(graph_runs), border=True)
        st.metric("完成", completed_runs, border=True)
        st.metric("门禁拦截", blocked_runs, border=True)
        st.metric("异常", error_runs, border=True)
    if not graph_runs:
        st.info(
            "还没有 LangGraph 运行记录。到“生成工作台”选择 LangGraph 后生成一次即可。",
            icon=":material/info:",
        )
    else:
        run_rows = [
            {
                "运行 ID": item.thread_id,
                "主题": item.topic,
                "状态": item.status,
                "Skill 步数": item.steps,
                "重试": item.retries,
                "耗时(ms)": item.duration_ms,
                "Checkpoint": item.checkpoint_count,
                "时间": datetime.fromtimestamp(item.updated_at).strftime("%Y-%m-%d %H:%M:%S"),
            }
            for item in graph_runs
        ]
        st.dataframe(run_rows, hide_index=True, width="stretch", key="graph_run_history")
        selected_run_id = st.selectbox(
            "查看单次节点轨迹",
            [item.thread_id for item in graph_runs],
            key="selected_graph_run",
        )
        selected_run = next(item for item in graph_runs if item.thread_id == selected_run_id)
        st.caption(
            f"Checkpoint ID：`{selected_run.checkpoint_id or '-'}` · "
            "运行索引不会保存完整提示词、正文或密钥"
        )
        if selected_run.error:
            st.error(selected_run.error, icon=":material/error:")
        st.dataframe(
            [item.model_dump(mode="json") for item in selected_run.trace],
            hide_index=True,
            width="stretch",
            key=f"graph_trace_{selected_run.thread_id}",
        )

    st.space("medium")
    st.subheader(":material/receipt_long: 发布留痕", anchor=False)
    st.caption("来源：publish_log.json；仅记录状态、链接和时间，不保存正文或密钥。")
    log = PublishLog(path=str(LOG_PATH)).load()
    if not log.records:
        st.info("暂无发布记录")
    else:
        rows = [{"id": k, **v} for k, v in log.records.items()]
        st.markdown(_md_table(["id", "status", "url", "ts"],
                              [[str(r.get(k, "")) for k in ("id", "status", "url", "ts")] for r in rows]))

    st.space("medium")
    st.subheader(":material/inventory_2: 内容库概览", anchor=False)
    bank = ContentBank(str(BANK_DIR))
    items = bank.list_items()
    if not items:
        st.info("内容库为空")
    else:
        rows = []
        for it in items:
            rec = log.records.get(it.id, {})
            rows.append([
                it.id, it.topic,
                it.scheduled_at.strftime(TIME_FMT) if it.scheduled_at else "-",
                rec.get("status", "待发布"), rec.get("url", ""),
            ])
        st.markdown(_md_table(["id", "topic", "scheduled_at", "status", "url"], rows))

    st.space("medium")
    st.subheader(":material/science: 质量评测", anchor=False)
    eval_col1, eval_col2, eval_col3, eval_col4 = st.columns(4)
    if eval_col1.button("内容生成评测", icon=":material/play_arrow:", width="stretch"):
        with st.spinner("评测中…"):
            proc = subprocess.run([sys.executable, "eval/scorer.py"], cwd=str(BASE),
                                  capture_output=True, text=True, encoding="utf-8")
        st.code(proc.stdout[-800:] if proc.stdout else proc.stderr[-800:])
        csv_path = BASE / "eval_results.csv"
        if csv_path.exists():
            st.text(csv_path.read_text(encoding="utf-8-sig"))
    if eval_col2.button("AI 助手离线回归", icon=":material/play_arrow:", width="stretch"):
        with st.spinner("验证路由、引用、步数与回答完整性…"):
            proc = subprocess.run(
                [sys.executable, "-m", "eval.assistant_scorer"], cwd=str(BASE),
                capture_output=True, text=True, encoding="utf-8")
        st.code(proc.stdout[-4_000:] if proc.stdout else proc.stderr[-1_200:])

    if eval_col3.button("RAG 质量门禁", icon=":material/play_arrow:", width="stretch"):
        with st.spinner("计算 Recall@1、MRR、延迟与降级状态…"):
            proc = subprocess.run(
                [sys.executable, "-m", "eval.rag_scorer", "--top-k", "1", "--backend", "hashing"],
                cwd=str(BASE), capture_output=True, text=True, encoding="utf-8",
            )
        output = proc.stdout[-6_000:] if proc.stdout else proc.stderr[-1_500:]
        if proc.returncode == 0:
            st.success("RAG 质量门禁通过", icon=":material/check_circle:")
        else:
            st.error("RAG 质量门禁未通过", icon=":material/error:")
        st.code(output)

    if eval_col4.button("答案支持度门禁", icon=":material/fact_check:", width="stretch"):
        with st.spinner("检查引用编号、证据重合与明显否定冲突…"):
            proc = subprocess.run(
                [sys.executable, "-m", "eval.grounding_scorer", "--summary-only"],
                cwd=str(BASE), capture_output=True, text=True, encoding="utf-8",
            )
        output = proc.stdout[-4_000:] if proc.stdout else proc.stderr[-1_500:]
        if proc.returncode == 0:
            st.success("答案事实支持度门禁通过", icon=":material/check_circle:")
        else:
            st.error("答案事实支持度门禁未通过", icon=":material/error:")
        st.code(output)

    st.caption(
        "离线回归不调用真实 LLM；RAG 门禁固定 hashing 后端以保证可复现。"
        "答案支持度是可复现的词法基线，不替代人工标注、NLI 或 LLM-as-Judge。"
    )
