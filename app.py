"""PersonaX 可视化工作台（Streamlit）

启动：
    pip install streamlit
    python -m streamlit run app.py

功能页：
- 📝 生成与编辑  生成标题/正文/标签 → 自由编辑 → 检验（合规+风格+就绪）→ 发布
- 🗓️ 内容库与定时  管理 content_bank 稿件、设置发布时间、执行到期任务
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

st.set_page_config(page_title="PersonaX 小红书工作台", page_icon="🍃", layout="wide")

# 触发 Skill 注册
sys.path.insert(0, str(BASE))
from core.envfile import load_env_file
load_env_file(BASE / ".env")   # 读取 DEEPSEEK_API_KEY（若 .env 存在）
import skills  # noqa: F401
from core.orchestrator import Orchestrator
from core.harness import Harness, RuleConfig
from core.compliance import ComplianceEngine, load_compliance_config
from core.publish_safety import real_publish_enabled, real_publish_disabled_message
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


def build_orch(persona_name: str | None = None):
    """构建编排器。persona_name 指定人格库中的某个人格，否则用 persona.yaml"""
    from core.persona import list_personas, resolve_persona
    if persona_name and persona_name in list_personas():
        persona = resolve_persona(persona_name)
    else:
        persona = load_persona()
    harness = Harness(RuleConfig(**persona.get("harness", {})))
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


init_state()

with st.sidebar:
    st.title("🍃 PersonaX")
    st.caption("小红书内容生成 · 检验 · 定时发布")
    page = st.radio(
        "导航",
        ["📝 生成与编辑", "🗓️ 内容库与定时", "📚 知识库", "⚙️ 设置", "📊 状态与日志"],
        label_visibility="collapsed",
        key="nav",
    )
    st.divider()
    st.caption(f"内容库: `content_bank/`\n\n发布留痕: `publish_log.json`")
    st.caption(f"登录态: {'✅ 已就绪' if STATE_PATH.exists() else '⚠️ 未登录（发布前需先登录）'}")
    if st.button("🔁 重置当前草稿", width="stretch"):
        st.session_state.draft = None
        st.session_state.check_result = None
        st.session_state.pub_confirm = False
        st.rerun()


# ============================================================
# 页 1：生成与编辑 → 检验 → 发布
# ============================================================
if page == "📝 生成与编辑":
    st.header("📝 生成 · 编辑 · 检验 · 发布")

    # ---- 模型后端 + 模型切换（默认本地 Ollama，免费） ----
    from core.llm import _backend, _ollama_reachable
    be1, be2, be3 = st.columns([2, 2, 1])
    default_be = "ollama（本地免费）" if _backend() == "ollama" else "deepseek（云端）"
    backend_ch = be1.selectbox(
        "模型后端",
        ["ollama（本地免费）", "deepseek（云端）", "offline（离线模板）"],
        index=0 if default_be.startswith("ollama") else 1,
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
    from core.websearch import _enabled as ws_enabled
    st.caption(f"🌐 联网：{'✅ 开' if ws_enabled() else '⏸ 关'}（⚙️ 设置页可切换）　"
               f"🎨 封面：发布时自动生成")

    # 未配 Key 时给清晰提示（而不是报错）
    if backend_key == "deepseek" and not os.getenv("DEEPSEEK_API_KEY"):
        st.warning("⚠️ 未检测到 DEEPSEEK_API_KEY：请在项目根目录 `.env` 填写 Key，"
                   "或改选「ollama（本地免费）」后端（不用 Key、不花钱）。")

    c1, c2 = st.columns([3, 1])
    topic = c1.text_input("笔记主题", st.session_state.topic,
                          placeholder="请输入笔记主题，如：秋招穿搭")
    from core.persona import list_personas, suggest_persona
    persona_names = ["默认(persona.yaml)"] + list_personas()
    rec_persona = suggest_persona(topic)

    # ---- 自动推荐人格：主题变了自动选推荐，可手动切换回来 ----
    st.session_state.setdefault("auto_persona", True)
    auto_on = c1.checkbox("✨ 自动按主题推荐人格", key="auto_persona",
                          help="勾选：改主题自动选推荐人格；取消或手动选＝用你选的")
    sel_key = "persona_choice"
    st.session_state.setdefault(sel_key, "默认(persona.yaml)")
    if auto_on and topic != st.session_state.get("last_topic", ""):
        # 主题变化 → 自动切到推荐人格
        st.session_state[sel_key] = rec_persona or "默认(persona.yaml)"
        st.session_state.last_topic = topic

    bp1, bp2 = st.columns([3, 1])

    def _apply_recommended():
        # 回调在组件实例化前执行，允许修改 selectbox 的 session state
        st.session_state[sel_key] = rec_persona or "默认(persona.yaml)"
        st.session_state.last_topic = topic

    gen_persona = bp1.selectbox("人格（可手动切换）", persona_names, key=sel_key,
                                help="选哪个就用哪个语气生成；可到「⚙️ 设置 → 人设库」创建/编辑人格")
    bp2.button("↩️ 回到推荐", use_container_width=True, on_click=_apply_recommended,
               disabled=not rec_persona or rec_persona == gen_persona)
    if rec_persona and gen_persona == rec_persona:
        st.caption(f"💡 已自动选择推荐人格：**{rec_persona}**（想换就手动选，或点「↩️ 回到推荐」）")
    elif rec_persona:
        st.caption(f"💡 推荐人格：**{rec_persona}**（你当前手动选择了 {gen_persona}）")
    st.session_state.topic = topic

    if st.button("✨ 生成发布内容", type="primary", width="stretch"):
        if not (topic or "").strip():
            st.warning("⚠️ 请先输入笔记主题（如：秋招穿搭）")
        else:
            llm_configure(backend=backend_key,
                          model=None if backend_key == "offline" else gen_model,
                          temperature=gen_temp)
            chose = None if gen_persona.startswith("默认") else gen_persona
            orch, _ = build_orch(chose)
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

    st.divider()

    draft = st.session_state.draft
    if draft is None:
        st.info("👆 先点「✨ 生成发布内容」，或到「🗓️ 内容库与定时」选稿编辑。")
    else:
        st.subheader("✏️ 编辑内容")
        ts = st.session_state.gen_ts
        col_a, col_b = st.columns([2, 1])
        with col_a:
            title = st.text_input("标题（≤20 字，含 emoji 更吸睛）", draft.title or "", key=f"e_title_{ts}")
            body = st.text_area("正文（短句分段，结尾互动引导）", draft.body or "",
                                height=260, key=f"e_body_{ts}")
        with col_b:
            tags = st.text_input("标签（空格分隔）", fmt_tags(draft.tags), key=f"e_tags_{ts}")
            cover_text = st.text_input("封面文案（可选）", draft.cover_text or "", key=f"e_cover_{ts}")
            images = st.text_input("配图路径（逗号分隔，可选）",
                                   " ".join((draft.metadata or {}).get("images", [])), key=f"e_images_{ts}")
            preview = st.text_area("👀 预览", f"{title}\n\n{body}\n\n{fmt_tags(parse_tags(tags))}",
                                   height=200, disabled=True)

        if st.button("💾 应用编辑", width="stretch"):
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
        cover_desc = st.text_input("🎨 封面描述（可选，按你的描述生成）",
                                   (st.session_state.draft.metadata or {}).get("cover_desc", "")
                                   if st.session_state.draft else "",
                                   placeholder="如：粉色渐变 可爱风 / 深色高级感 金色线条 / 简约留白 黑白")
        if st.button("🎨 按描述生成封面", type="secondary", width="stretch",
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

        st.divider()

        ck1, ck2, ck3 = st.columns(3)
        # ---- 检验 ----
        if ck1.button("🔍 检验（合规+风格+就绪）", width="stretch"):
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
                        if st.button("📍 定位", key=f"jump_{idx}",
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
                    if b1.button("💾 保存修改", key=f"save_fix_{fld}_{jump.get('hit_idx', 0)}",
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
        if pub_c2.button("🧪 干跑发布（不真发）", width="stretch"):
            draft = st.session_state.draft
            r = DryRunPublisher().publish(draft)
            st.info(f"{r.message}（{r.cost_ms}ms）")

        if pub_c3.button("🚀 真实发布", width="stretch", type="primary",
                          disabled=not STATE_PATH.exists() or not can_real_publish):
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
        if st.button("🗂️ 核对已发布笔记", width="stretch"):
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
# 页 2：内容库与定时
# ============================================================
elif page == "🗓️ 内容库与定时":
    st.header("🗓️ 内容库 · 定时发布")

    bank = ContentBank(str(BANK_DIR))
    log = PublishLog(path=str(LOG_PATH)).load()
    items = bank.list_items()

    st.subheader("📦 新建定时稿件")
    with st.form("new_draft", clear_on_submit=True):
        f1, f2, f3 = st.columns([2, 2, 1])
        n_topic = f1.text_input("主题", "秋招穿搭")
        n_time = f2.datetime_input("发布时间", datetime.now() + timedelta(hours=2), step=600)
        n_title = st.text_input("标题（可留空，发布时自动生成）")
        n_body = st.text_area("正文（可留空，发布时自动生成）", height=120)
        n_tags = st.text_input("标签（空格分隔，可留空）")
        n_img = st.text_input("配图路径（逗号分隔，可选）")
        submitted = st.form_submit_button("💾 存入内容库（定时）")
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

    st.divider()
    st.subheader(f"📚 内容库稿件（{len(items)} 篇）")

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
            if b1.button("✏️ 载入编辑", key=f"load_{it.id}"):
                st.session_state.draft = it.to_draft()
                st.session_state.check_result = None
                st.session_state.pub_confirm = False
                st.session_state.gen_ts += 1
                st.session_state.nav = "📝 生成与编辑"
                st.rerun()
            if b2.button("🧪 干跑发布", key=f"dry_{it.id}"):
                draft = it.to_draft()
                if not (draft.title and draft.body):
                    orch, _ = build_orch()
                    draft = orch.run(topic=it.topic, user_id="web_user")
                r = DryRunPublisher().publish(draft)
                st.info(r.message)
            if b3.button("🗑️ 删除", key=f"del_{it.id}"):
                Path(it.path).unlink(missing_ok=True)
                st.rerun()

    st.divider()
    st.subheader("⏰ 执行定时任务")
    e1, e2 = st.columns(2)
    if e1.button("▶️ 执行到期任务（干跑）", width="stretch"):
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
    if e2.button("🚀 执行到期任务（真实发布，需已登录）", width="stretch",
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
# 页 3：知识库（喂优质范例 → 让生成更自然）
# ============================================================
elif page == "📚 知识库":
    st.header("📚 知识库（喂优质范例 · 让生成更自然）")
    st.caption("把「你觉得好的笔记」喂进来，生成时自动召回作范例参考。格式会自动加 front-matter。")

    KNOWLEDGE_DIR = BASE / "knowledge"
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)

    with st.form("kb_add", clear_on_submit=True):
        k1, k2, k3 = st.columns([2, 1, 2])
        kb_topic = k1.text_input("主题（必填，用于检索）", "秋招穿搭")
        kb_style = k2.text_input("风格描述", "口语化/短句/互动")
        kb_kw = k3.text_input("关键词（逗号分隔）", "面试, 穿搭, 秋招")
        kb_body = st.text_area("范例正文（粘贴你满意的笔记，体现结构/语气/互动）", height=280,
                               placeholder="家人们谁懂啊，……\n\n第一，……\n第二，……\n\n你们……评论区聊聊～")
        kb_submit = st.form_submit_button("💾 存入知识库", type="primary")
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

    st.divider()
    st.subheader("📚 现有知识库")
    kb_files = sorted(KNOWLEDGE_DIR.glob("*.md"))
    if not kb_files:
        st.info("知识库为空，先在上面粘贴一篇进去")
    for f in kb_files:
        content = f.read_text(encoding="utf-8", errors="ignore")
        topic = ""
        for line in content.splitlines():
            if line.startswith("topic:"):
                topic = line.split(":", 1)[1].strip()
                break
        c1, c2 = st.columns([3, 1])
        c1.caption(f"**{topic or f.stem}** ｜ `{f.name}` ｜ {len(content)} 字")
        with st.expander("查看/复制这篇范例"):
            st.text(content)
        if c2.button("🗑️ 删除", key=f"kbdel_{f.stem}", width="stretch"):
            f.unlink(missing_ok=True)
            st.rerun()


# ============================================================
# 页 4：设置（内容格式 / 人格 / 合规词表）
# ============================================================
elif page == "⚙️ 设置":
    st.header("⚙️ 设置：发布内容格式 · 人格 · 合规")

    # ---------- 人设库（多人格） ----------
    st.subheader("🎭 人设库（多人格，生成时按所选人格写）")
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
        if st.button("💾 新增人格", type="primary"):
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
        if bc1.button("💾 保存修改"):
            try:
                data = _y.safe_load(edit_yaml)
                add_persona(sel_name, data)
                st.success(f"已更新人格「{sel_name}」")
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"YAML 解析失败: {e}")
        if bc2.button("🗑️ 删除人格"):
            data = _load_yaml(PERSONAS_PATH)
            data.pop(sel_name, None)
            with open(PERSONAS_PATH, "w", encoding="utf-8") as f:
                _y.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            st.success(f"已删除人格「{sel_name}」")
            st.rerun()

    st.divider()
    st.subheader("🎭 人格模板（一键套用当前默认 persona.yaml）")
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
    if st.button("🎨 套用模板到 persona.yaml"):
        data = load_persona()
        data.update(presets[preset_name])
        save_persona(data)
        st.success(f"已套用「{preset_name}」，可在下方微调")

    st.divider()

    # ---------- 联网增强开关 ----------
    st.subheader("🌐 联网增强（生成前抓热点/参考）")
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

    st.divider()

    # ---------- 封面设置 ----------
    st.subheader("🎨 封面设置（多模态）")
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

    st.divider()
    st.subheader("📄 persona.yaml（人格 + 生成参数 + 规则）")
    yaml_text = st.text_area("内容格式配置（直接编辑 YAML）", PERSONA_PATH.read_text(encoding="utf-8"),
                             height=420, key="persona_yaml")
    if st.button("💾 保存 persona.yaml", type="primary"):
        try:
            data = yaml.safe_load(yaml_text)
            save_persona(data)
            st.success("已保存并生效")
        except yaml.YAMLError as e:
            st.error(f"YAML 语法错误，未保存: {e}")

    st.divider()
    st.subheader("🛡️ compliance.yaml（合规词表，发布前门禁）")
    comp_text = st.text_area("合规词表（直接编辑 YAML）", COMPLIANCE_PATH.read_text(encoding="utf-8"),
                             height=320, key="comp_yaml")
    if st.button("💾 保存 compliance.yaml"):
        try:
            yaml.safe_load(comp_text)
            COMPLIANCE_PATH.write_text(comp_text, encoding="utf-8")
            st.success("已保存")
        except yaml.YAMLError as e:
            st.error(f"YAML 语法错误，未保存: {e}")


# ============================================================
# 页 4：状态与日志
# ============================================================
else:
    st.header("📊 状态与日志")

    st.subheader("📜 发布留痕（publish_log.json）")
    log = PublishLog(path=str(LOG_PATH)).load()
    if not log.records:
        st.info("暂无发布记录")
    else:
        rows = [{"id": k, **v} for k, v in log.records.items()]
        st.markdown(_md_table(["id", "status", "url", "ts"],
                              [[str(r.get(k, "")) for k in ("id", "status", "url", "ts")] for r in rows]))

    st.subheader("🗂️ 内容库概览")
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

    st.subheader("🧪 评测")
    if st.button("▶️ 运行评测（eval/scorer.py → eval_results.csv）"):
        with st.spinner("评测中…"):
            proc = subprocess.run([sys.executable, "eval/scorer.py"], cwd=str(BASE),
                                  capture_output=True, text=True, encoding="utf-8")
        st.code(proc.stdout[-800:] if proc.stdout else proc.stderr[-800:])
        csv_path = BASE / "eval_results.csv"
        if csv_path.exists():
            st.text(csv_path.read_text(encoding="utf-8-sig"))

    st.caption("审计日志（AuditLog）为每次运行的进程内记录；跨进程审计以 publish_log.json 为准。")
