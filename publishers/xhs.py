"""小红书发布实现（商用级）

两个发布器：
- DryRunPublisher             —— 干跑（测试/演示，不真发）
- XhsPlaywrightPublisher     —— 真实发布（Playwright 驱动创作者平台）

商用安全特性：
1. 登录态隔离    —— 必须提供 storage_state.json（cookie 隔离），缺失即拒绝发布
2. 登录检测      —— 打开发布页后校验登录态，未登录报错并提示 `python main.py login`
3. 人工审批      —— 默认发布前交互确认（对齐 Harness require_approval）；
                    无人值守模式需显式 auto_approve=True（定时任务）
4. 幂等保护      —— Draft.metadata["publish_url"] 已存在则跳过，防止重复发布
5. 失败重试      —— 指数退避重试（默认 3 次），每次失败截图存 logs/
6. 限速          —— 可挂 Harness quota，遵守每分钟发布上限
7. 选择器容错    —— 每个元素给多个候选选择器（小红书改版后自动换）
8. 图片上传      —— 支持封面/多图（draft.metadata["images"] / ["cover"]）
"""
from __future__ import annotations
import os
import time
from pathlib import Path
from typing import Callable, Optional

from .base import Publisher
from core.types import Draft, PublishResult

# 选择器候选表：按 XHS 创作者平台常见 DOM 写，改版后在此追加即可
DEFAULT_SELECTORS = {
    "title": [
        "textarea[placeholder*='标题']",
        "input[placeholder*='标题']",
        "div[contenteditable='true'] div[data-placeholder*='标题']",
        "div[contenteditable='true'][data-placeholder*='标题']",
        "div[class*='title'] input",
        "input[class*='title']",
        "div[class*='title'] div[contenteditable='true']",
    ],
    "body": [
        "div[contenteditable='true']",
        "div[class*='editor'] div[contenteditable='true']",
        "div[class*='content'] div[contenteditable='true']",
    ],
    "topic": [
        "button:has-text('话题')",
        "button[class*='topic']",
        "input[placeholder*='话题']",
        "input[placeholder*='标签']",
    ],
    "topic_option": [
        "div[class*='topic'] div[class*='item']",
        "li[class*='topic']",
        "div[class*='suggest'] div[class*='item']",
    ],
    "publish": [
        "xhs-publish-btn >> text=发布",
        "xhs-publish-btn >> button",
        "xhs-publish-btn",
        "xhs-publish-button >> text=发布",
        "button:has-text('发布')",
        "div[class*='publish'] button",
    ],
    "file_input": [
        "input[type='file']",
        "input.upload-input",
        "input[class*='upload']",
        "input[class*='file']",
    ],
    "login_indicator": [
        "a[href*='login']",
        "text=登录",
    ],
}


class LoginRequired(Exception):
    """未登录/登录态失效"""


class ApprovalDenied(Exception):
    """用户拒绝发布"""


class PublishUnconfirmed(Exception):
    """点击发布后未检测到成功证据（不返回假成功）"""


def _record_publish(mode: str, draft: Draft, result: "PublishResult", t0: float | None = None) -> None:
    """用量埋点：记录每次发布结果（success / cost_ms / mode），失败静默。"""
    try:
        from core.usage import record
        record(
            "publish",
            mode=mode,
            topic=draft.topic,
            title=draft.title,
            success=result.success,
            cost_ms=result.cost_ms,
            wall_ms=round((time.time() - t0) * 1000.0, 1) if t0 else None,
        )
    except Exception:  # noqa: BLE001 —— 埋点失败不影响发布
        pass


def _browser_exe_paths(channel: str) -> list[Path]:
    """返回指定浏览器通道的常见安装路径（存在的）"""
    pf = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    pf86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    if channel == "chrome":
        cands = [pf / "Google/Chrome/Application/chrome.exe",
                 pf86 / "Google/Chrome/Application/chrome.exe",
                 local / "Google/Chrome/Application/chrome.exe"]
    elif channel == "msedge":
        cands = [pf86 / "Microsoft/Edge/Application/msedge.exe",
                 pf / "Microsoft/Edge/Application/msedge.exe"]
    else:
        return []
    return [c for c in cands if c.exists()]


def _browser_order(channel: str | None) -> list[str | None]:
    """浏览器尝试顺序：指定 → msedge → 内置 chromium"""
    order: list[str | None] = []
    if channel:
        order.append(channel)
    if channel != "msedge":
        order.append("msedge")
    order.append(None)
    return order


def _launch_browser(playwright_instance, channel: str | None = None, headless: bool = True):
    """启动浏览器，自动回退：指定 channel → msedge → 内置 chromium。

    返回 (browser, notes)；notes 记录「检测到缺哪个浏览器 / 为何切换」的原因，
    前端可据此向用户展示。

    解决「选了 chrome 但机器没装 Google Chrome」时报错的问题。
    """
    notes: list[str] = []
    last_err: Exception | None = None
    for ch in _browser_order(channel):
        label = {None: "内置 Chromium", "chrome": "Google Chrome", "msedge": "Microsoft Edge"}.get(ch, ch)
        # 预检测：该浏览器是否已安装（给出明确原因）
        if ch:
            if not _browser_exe_paths(ch):
                notes.append(f"检测到未安装 {label}，尝试改用其他浏览器")
                last_err = RuntimeError(f"{label} 未安装")
                continue
        try:
            kwargs: dict = {"headless": headless}
            if ch:
                kwargs["channel"] = ch
            browser = playwright_instance.chromium.launch(**kwargs)
            if ch is not None and channel is not None and ch != channel:
                notes.append(f"「{channel}」不可用，已自动切换到「{label}」")
            return browser, notes
        except Exception as e:  # noqa: BLE001
            last_err = e
            notes.append(f"启动 {label} 失败：{str(e)[:100]}")
            continue
    raise RuntimeError(
        f"浏览器启动失败（尝试过 {[c or 'chromium' for c in _browser_order(channel)]}）：{last_err}\n"
        "请确认安装了 Edge/Chrome，或运行 python -m playwright install chromium 下载内置内核"
    ) from last_err


class DryRunPublisher(Publisher):
    """干跑发布（不真发，用于测试/演示）"""

    name = "dry_run"

    def publish(self, draft: Draft) -> PublishResult:
        time.sleep(0.05)
        result = PublishResult(
            success=True,
            url="https://www.xiaohongshu.com/explore/mock-123",
            message=f"[DRY] 发布《{draft.title}》 tags={draft.tags}",
            cost_ms=50,
        )
        _record_publish("dry", draft, result)
        return result


class XhsPlaywrightPublisher(Publisher):
    """小红书创作者平台真实发布器"""

    name = "xhs"

    def __init__(
        self,
        storage_state: str = "storage_state.json",
        headless: bool = True,
        channel: str | None = None,
        selectors: dict | None = None,
        max_retries: int = 3,
        retry_delays: tuple[float, ...] = (2.0, 5.0),
        wait_timeout_ms: int = 8000,
        screenshot_dir: str = "logs",
        auto_approve: bool = False,
        harness=None,
        user_id: str | None = None,
        scan_wait_seconds: int = 120,
        keep_browser_on_failure: bool = False,
    ):
        self.storage_state = storage_state
        self.headless = headless
        # channel: None/""/"chromium"=内置Chromium；"chrome"=系统Chrome（免下载）
        self.channel = None if channel in (None, "", "chromium") else channel
        self.selectors = selectors or DEFAULT_SELECTORS
        self.max_retries = max_retries
        self.retry_delays = retry_delays
        self.wait_timeout_ms = wait_timeout_ms
        self.screenshot_dir = Path(screenshot_dir)
        self.auto_approve = auto_approve
        self.harness = harness
        self.user_id = user_id
        self.scan_wait_seconds = scan_wait_seconds   # 有头模式扫码等待上限
        # 调试模式：失败后不由程序关闭有头浏览器，直到用户手动关闭窗口。
        # 便于直接查看平台提示、截图未覆盖到的浮层，并人工修改内容。
        self.keep_browser_on_failure = keep_browser_on_failure
        self.ai_generated = True   # 是否标注「内容由 AI 生成」（合规：默认标注，可被 draft.metadata 覆盖）
        self.browser_notes: list[str] = []   # 浏览器检测/切换原因（供前端展示）

    # ---------- 公共入口 ----------

    def publish(
        self,
        draft: Draft,
        confirm: Optional[Callable[[Draft], bool]] = None,
    ) -> PublishResult:
        _t0 = time.time()
        # 1) 幂等：已发布过则跳过
        if draft.metadata.get("publish_url"):
            result = PublishResult(
                success=True,
                url=draft.metadata["publish_url"],
                message=f"[IDEMPOTENT] 已发布过，跳过: {draft.metadata['publish_url']}",
            )
            _record_publish("real", draft, result, _t0)
            return result

        # 2) 限速：真实发布专用配额（与 Skill 调用预算分离）
        if self.harness is not None:
            ok, reason = self.harness.check_publish_quota(self.user_id)
            if not ok:
                result = PublishResult(success=False, message=f"限速拦截: {reason}")
                _record_publish("real", draft, result, _t0)
                return result

        # 3) 人工审批（默认开启，无人值守需 auto_approve）
        if not self.auto_approve:
            approved = confirm(draft) if confirm else self._ask_human(draft)
            if not approved:
                raise ApprovalDenied(f"用户拒绝发布《{draft.title}》")

        # 4) 登录态检查（发布前快速校验，避免进入重试浪费）
        missing = self._login_state_missing()
        if missing:
            raise LoginRequired(
                f"缺少登录态文件 {self.storage_state}。"
                f"请先执行: python main.py login（浏览器登录一次，自动保存会话）"
            )

        # 5) 带重试的真实发布
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                url = self._do_publish(draft)
                draft.metadata["publish_url"] = url
                result = PublishResult(success=True, url=url, message=f"发布成功: {url}")
                _record_publish("real", draft, result, _t0)
                return result
            except PublishUnconfirmed as e:
                # 未确认 ≠ 失败重试：直接返回，附真实现场证据
                self._screenshot("publish_unconfirmed")
                result = PublishResult(success=False, message=str(e))
                _record_publish("real", draft, result, _t0)
                return result
            except Exception as e:  # noqa: BLE001 —— 商用：吞异常并重试+截图
                last_err = e
                self._screenshot(f"fail_attempt{attempt}")
                # 调试时已等待用户手动关闭浏览器；不要关闭后又重新打开两次，
                # 否则失败现场既难观察，也可能触发平台的重复操作限制。
                if self.keep_browser_on_failure and not self.headless:
                    result = PublishResult(
                        success=False,
                        message=f"发布失败（调试现场已保留至手动关闭）：{e}",
                    )
                    _record_publish("real", draft, result, _t0)
                    return result
                if attempt < self.max_retries:
                    delay = self.retry_delays[min(attempt - 1, len(self.retry_delays) - 1)]
                    time.sleep(delay)
        result = PublishResult(success=False, message=f"发布失败（重试{self.max_retries}次）: {last_err}")
        _record_publish("real", draft, result, _t0)
        return result

    # ---------- 内部实现 ----------

    def _login_state_missing(self) -> bool:
        return not Path(self.storage_state).exists()

    def _ask_human(self, draft: Draft) -> bool:
        print("\n" + "=" * 60)
        print(f"【发布确认】标题: {draft.title}")
        print(f"标签: {draft.tags}")
        print(f"正文预览: {(draft.body or '')[:120]}…")
        print("=" * 60)
        ans = input("确认发布到小红书？[y/N]: ").strip().lower()
        return ans in ("y", "yes")

    def _screenshot(self, name: str):
        try:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            path = self.screenshot_dir / f"xhs_{name}_{int(time.time())}.png"
            self._page.screenshot(path=str(path))
            print(f"[截图] 已保存: {path}")
        except Exception:  # noqa: BLE001 —— 截图失败不影响主流程
            pass

    def _find(self, page, candidates: list[str], timeout: int | None = None):
        """候选选择器容错：返回第一个可见/存在的元素。

        性能策略：首个候选可等满预算（匹配时通常 1-2s 内返回），
        后续候选只快速探测 2.5s（不匹配时不再傻等满超时）。
        """
        fallback_budget = timeout if timeout is not None else 3000
        probe = min(2500, fallback_budget)
        for i, sel in enumerate(candidates):
            try:
                loc = page.locator(sel).first
                budget = fallback_budget if i == 0 else probe
                loc.wait_for(state="visible", timeout=budget)
                return loc
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError(f"未找到元素，候选选择器: {candidates}")

    def _dump_debug(self, page, name: str):
        """失败现场：截图 + HTML 快照，便于排查选择器/登录问题"""
        try:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            page.screenshot(path=str(self.screenshot_dir / f"xhs_{name}_{ts}.png"))
            html = page.content()
            (self.screenshot_dir / f"xhs_{name}_{ts}.html").write_text(html, encoding="utf-8")
            print(f"[debug] 已保存现场: logs/xhs_{name}_{ts}.png + .html")
            print(f"[debug] 当前 URL: {page.url}")
            print(f"[debug] 页面标题: {page.title()}")
        except Exception as e:  # noqa: BLE001 —— 快照失败不影响主流程
            print(f"[debug] 现场快照失败: {e}")

    def _ensure_logged_in(self, page):
        """打开发布页后检测登录态"""
        page.goto("https://creator.xiaohongshu.com/publish/publish", wait_until="domcontentloaded")
        for sel in self.selectors["login_indicator"]:
            try:
                # 登录入口可见 = 未登录
                page.locator(sel).first.wait_for(state="visible", timeout=2000)
                raise LoginRequired(
                    "检测到未登录（出现登录入口）。请执行: python main.py login"
                )
            except LoginRequired:
                raise
            except Exception:  # noqa: BLE001 —— 该选择器不存在，继续试下一个
                continue

    def _select_publish_type(self, page):
        """切到「上传图文」页签（小红书标准图文笔记；有标题/正文/话题/发布）。

        实测：长文编辑器无「发布」按钮；图文编辑器需先上传 ≥1 张图片，
        标题/正文/发布按钮（xhs-publish-btn）才出现。因此发布必须走图文模式。
        """
        try:
            ok = page.evaluate(
                """() => {
                    const tabs = [...document.querySelectorAll('.creator-tab')];
                    const t = tabs.find(x => !x.hasAttribute('aria-hidden')
                                          && x.textContent.trim() === '上传图文');
                    if (t) { t.click(); return true; }
                    return false;
                }""")
            if not ok:
                # 备选：选择器点击
                tab = page.locator(
                    "div.creator-tab:not([aria-hidden]), div[class*='tab']:not([aria-hidden])"
                ).filter(has_text="上传图文").first
                tab.click(timeout=4000)
            print("[info] 已切换到「上传图文」")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 切换「上传图文」失败: {e}")
        page.wait_for_timeout(2500)

    def _mark_ai_generated(self, page):
        """标注「笔记含AI合成内容」声明（合规，默认开启）。

        实际 UI（2026-08 截图核实）：发布页「内容设置」区有一个「添加内容类型声明」
        下拉，展开后含「笔记含AI合成内容」选项。点它即完成 AI 内容声明。
        依据：小红书社区公约 2.0 + 国家《人工智能生成合成内容标识办法》，
        AI 生成内容须主动标识，未标识会限流/封号；如实标识不影响流量。
        选择器按真实文案「添加内容类型声明 / 笔记含AI合成内容」精确匹配，可追加候选。
        """
        if not self.ai_generated:
            print("[info] 已关闭 AI 生成标注（ai_generated=False）")
            return

        def _click_first(match_sel: str, timeout: int = 2000) -> bool:
            """点击第一个可见候选（容错：匹配一列候选文本）"""
            try:
                loc = page.locator(match_sel).first
                loc.wait_for(state="visible", timeout=timeout)
                loc.click(timeout=timeout)
                return True
            except Exception:  # noqa: BLE001
                return False

        # 1) 打开「添加内容类型声明」下拉（先滚动到可见，再精确文案，再 JS 兜底）
        opened = False
        try:
            # 先 scroll_into_view：该入口在内容设置区（页面下半部），不可见时点不到
            trigger = page.locator("text=添加内容类型声明").first
            trigger.scroll_into_view_if_needed(timeout=3000)
            trigger.wait_for(state="visible", timeout=3000)
            trigger.click(timeout=3000)
            opened = True
        except Exception:  # noqa: BLE001
            opened = False
        if not opened:
            opened = _click_first(
                "text=添加内容类型声明, div:has-text('添加内容类型声明') span, "
                "div[class*='declare'] >> text=添加内容类型声明",
                timeout=1500,
            )
        if not opened:
            try:
                opened = page.evaluate(
                    """() => {
                        const els = [...document.querySelectorAll('div,span,button')];
                        const t = els.find(x => x.textContent.trim() === '添加内容类型声明'
                                          && x.offsetParent !== null);
                        if (t) { t.scrollIntoView({block:'center'}); t.click(); return true; }
                        return false;
                    }""")
            except Exception:  # noqa: BLE001
                opened = False
        page.wait_for_timeout(1000)
        if not opened:
            print("[warn] 未找到「添加内容类型声明」入口，跳过 AI 标注")
            return

        # 2) 在展开的选项里点「笔记含AI合成内容」（可先滚动到该选项再点）
        chosen = False
        for sel in ("text=笔记含AI合成内容", "text=含AI合成内容", "text=AI合成内容"):
            try:
                opt = page.locator(sel).first
                opt.scroll_into_view_if_needed(timeout=2000)
                opt.wait_for(state="visible", timeout=2000)
                opt.click(timeout=2000)
                chosen = True
                print(f"[info] 已标注「笔记含AI合成内容」（{sel}）")
                break
            except Exception:  # noqa: BLE001
                continue
        if not chosen:
            print("[warn] 未找到「笔记含AI合成内容」选项，AI 标注可能未生效")

    def _upload_images(self, page, images: list[str]) -> bool:
        """上传图片（图文模式必需 ≥1 张，图上传后标题/正文/发布按钮才出现）。

        注意：XHS 的文件输入框是隐藏元素（display:none），不能要求 visible；
        set_input_files 对隐藏文件框有效，故用 state=attached。
        """
        if not images:
            return False
        try:
            loc = page.locator(
                "input[type='file'], input.upload-input, input[class*='upload'], "
                "input[class*='file']"
            ).first
            loc.wait_for(state="attached", timeout=8000)
            loc.set_input_files(images)
            page.wait_for_timeout(4000)   # 等上传完成 + 编辑器出现
            print(f"[info] 已上传 {len(images)} 张图片")
            return True
        except Exception as e:  # noqa: BLE001
            # 兜底：点「上传图片」按钮再传
            try:
                btn = page.locator("button:has-text('上传图片'), div[class*='upload'] button").first
                btn.click(timeout=3000)
                page.wait_for_timeout(1000)
                loc = page.locator(
                    "input[type='file'], input.upload-input, input[class*='upload']"
                ).first
                loc.wait_for(state="attached", timeout=4000)
                loc.set_input_files(images)
                page.wait_for_timeout(4000)
                print(f"[info] 已通过按钮上传 {len(images)} 张图片")
                return True
            except Exception as e2:  # noqa: BLE001
                print(f"[warn] 图片上传失败: {e} / {e2}")
                return False

    def _editor_ready(self, page) -> bool:
        """标题输入框是否已出现（图文编辑器出现的标志）"""
        for cand in ("input[placeholder*='标题']", "input[placeholder*='填写标题']",
                     DEFAULT_SELECTORS["body"][0]):
            try:
                page.locator(cand).first.wait_for(state="visible", timeout=3500)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _dismiss_promo(self, page) -> bool:
        """点掉「支持千字长文」引导面板（按钮文案：新的创作 / 开始创作）"""
        for text in ("新的创作", "开始创作"):
            # 先按文本找，再 JS 兜底（避免命中隐藏克隆）
            for btn in (page.get_by_text(text, exact=True).first,
                        page.get_by_text(text, exact=True).last):
                try:
                    btn.click(timeout=2000)
                    page.wait_for_timeout(1500)
                    print(f"[info] 已点「{text}」进入编辑器")
                    return True
                except Exception:  # noqa: BLE001
                    continue
        try:
            ok = page.evaluate(
                """(labels) => {
                    const els = [...document.querySelectorAll('button, div[class*="button"]')];
                    const t = els.find(x => x.textContent.trim() === labels[0]
                                        || x.textContent.trim() === labels[1]);
                    if (t) { t.click(); return true; }
                    return false;
                }""", ("新的创作", "开始创作"))
            if ok:
                page.wait_for_timeout(1500)
                print("[info] 已通过 JS 点「新的创作」进入编辑器")
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _dismiss_permission_popup(self, page):
        """关掉浏览器权限/引导浮层（如「了解你的位置」定位请求）。

        这类浮层会遮挡编辑器，导致后续点击/发布按钮定位失败。按钮文案常见：
        阻止 / 仅此一次 / 允许 / 知道了 / 暂不开启 / 取消。
        尽力而为，找不到就静默跳过（不阻断主流程）。
        """
        for text in ("阻止", "仅此一次", "暂不", "知道了", "取消", "关闭"):
            try:
                loc = page.locator(f"button:has-text('{text}'), div[role='button']:has-text('{text}')").first
                loc.wait_for(state="visible", timeout=800)
                loc.click(timeout=1000)
                page.wait_for_timeout(600)
                print(f"[info] 已关闭权限/引导浮层:「{text}」")
                return True
            except Exception:  # noqa: BLE001 —— 浮层可能不存在/不可点，继续
                continue
        # 弹窗可能是浏览器原生权限条（不在 DOM），用 ESC 兜底
        try:
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        return False

    def _do_publish(self, draft: Draft) -> str:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser, notes = _launch_browser(p, self.channel, self.headless)
            self.browser_notes = notes   # 供前端展示（如「未装 Chrome，已切 Edge」）
            for n in notes:
                print(f"[browser] {n}")
            keep_open = False
            try:
                # 拒绝地理位置等敏感权限：从根上消除「了解你的位置」权限弹窗，
                # 避免它遮挡编辑器导致后续点击/发布按钮定位失败
                try:
                    context = browser.new_context(
                        storage_state=self.storage_state,
                        permissions=[],
                        geolocation=None,
                        locale="zh-CN",
                    )
                    # Chromium 系：显式拒绝授权弹窗（读取/位置/通知等自动 dismiss）
                    try:
                        context.grant_permissions([], origin="https://creator.xiaohongshu.com")
                    except Exception:  # noqa: BLE001
                        pass
                except TypeError:
                    # 旧版 Playwright 不支持部分参数，退化为仅存储态
                    context = browser.new_context(storage_state=self.storage_state)
                page = context.new_page()
                self._page = page
                t0 = time.time()
                self._ensure_logged_in(page)
                # 兜底：若仍有权限弹窗/引导浮层，尝试一键关闭（不阻断主流程）
                try:
                    self._dismiss_permission_popup(page)
                except Exception:  # noqa: BLE001
                    pass
                print(f"  [timing] 打开页面+登录检测: {int((time.time() - t0) * 1000)}ms")
                self._select_publish_type(page)   # 切到「上传图文」
                print(f"  [timing] 切换发布类型: {int((time.time() - t0) * 1000)}ms")

                try:
                    # 图片上传（图文模式必需 ≥1 张；缺图时自动生成标题封面）
                    images = self._resolve_images(draft)
                    if not images:
                        try:
                            from core.covergen import ensure_cover_for_draft
                            cover = ensure_cover_for_draft(draft)
                            if cover:
                                images = [cover]
                                print(f"[info] 已自动生成标题封面: {cover}")
                        except Exception as e:  # noqa: BLE001
                            print(f"[warn] 封面生成失败: {e}")
                    if not images:
                        default_cover = Path(__file__).resolve().parent.parent / "assets" / "note_cover.png"
                        if default_cover.exists():
                            images = [str(default_cover)]
                            print("[info] 稿件无配图，使用默认封面 assets/note_cover.png")
                    self._upload_images(page, images)

                    # 等图文编辑器出现（标题输入框）
                    if not self._editor_ready(page):
                        raise RuntimeError("上传图片后图文编辑器未出现（标题输入框未找到）")

                    # 标题
                    if draft.title:
                        el = self._find(page, self.selectors["title"], self.wait_timeout_ms)
                        el.fill(draft.title)

                    # 正文：发布前去掉游离 # 话题和所有空白行，规避编辑器的换行限制。
                    if draft.body:
                        clean_body = self._normalize_body(self._strip_inline_topics(draft.body))
                        el = self._find(page, self.selectors["body"], self.wait_timeout_ms)
                        el.click()
                        page.keyboard.type(clean_body, delay=1)

                    # 话题标签（图文模式点「话题」按钮添加，尽力而为，不阻断发布）
                    if draft.tags:
                        self._add_topics_via_button(page, draft.tags)

                    # AI 生成标注（合规：默认勾选「内容由 AI 生成」）
                    try:
                        self.ai_generated = bool(draft.metadata.get("ai_generated", True))
                        self._mark_ai_generated(page)
                    except Exception as e:  # noqa: BLE001
                        print(f"[warn] AI 生成标注失败（不影响发布）: {e}")

                    # 点击发布
                    self._click_publish(page)
                    page.wait_for_timeout(1500)

                    # 可能的二次确认弹窗（模态框内的「发布/确认」按钮）
                    for sel in ("div[class*='modal'] button:has-text('发布')",
                                "div[class*='dialog'] button:has-text('发布')",
                                "div[class*='confirm'] button:has-text('确定')",
                                "div[class*='modal'] button:has-text('确认')"):
                        try:
                            page.locator(sel).first.click(timeout=2500)
                            page.wait_for_timeout(1200)
                            print("[info] 已点击发布确认弹窗")
                            break
                        except Exception:  # noqa: BLE001
                            continue

                    # ===== 真实发布验证：必须拿到证据，否则不算成功 =====
                    published = False
                    real_url: str | None = None

                    # 风控/扫码验证处理：
                    #   headed 模式 → 停在浏览器等你扫码（最长 scan_wait_seconds），扫完继续
                    #   headless 模式 → 无法扫码，直接给明确提示
                    risk = self._handle_risk_control(page)
                    if risk == "blocked":
                        raise PublishUnconfirmed(
                            "小红书安全风控：要求「小红书APP 扫码验证身份」。\n"
                            "headless 模式无法扫码。请用有头模式发布：\n"
                            "  命令行：python main.py publish ... --real --headed\n"
                            "  可视化：勾选「有头模式」后发布，弹出窗口时用小红书 APP 扫码。"
                        )

                    published, real_url = self._verify_published(page)
                    if not published and risk == "scanned":
                        # 扫码完成后可能需重新触发发布
                        print("[info] 扫码验证完成，重新触发发布…")
                        self._click_publish(page)
                        page.wait_for_timeout(1500)
                        self._handle_risk_control(page)   # 若再次弹出则继续等待
                        published, real_url = self._verify_published(page)

                    # 无论成败，留真实现场证据（截图+HTML）
                    self._dump_debug(page, "publish_result")

                    if not published:
                        # 再查一次风控（可能弹窗稍后才出现）
                        risk2 = self._check_risk_control(page)
                        if risk2:
                            raise PublishUnconfirmed(
                                "小红书安全风控：要求扫码验证。请用有头模式（--headed 或界面勾选「有头模式」）发布并扫码。"
                            )
                        # 检查是否因「违反社区规范」被拒（页面停在编辑器，看起来像卡住）
                        blocked = self._check_content_blocked(page)
                        if blocked:
                            raise PublishUnconfirmed(
                                f"⚠️ 内容被小红书拦截：{blocked}。\n"
                                "请回到编辑页删掉可能违规的措辞（绝对化用语/导流/敏感词），"
                                "重新「检验」通过后再发布。现场已存 logs/。"
                            )
                        raise PublishUnconfirmed(
                            "点击发布后未检测到成功确认。现场已存 logs/xhs_publish_result_*，"
                            "请用 python main.py notes 查看「笔记管理」确认这篇是否已发布/草稿/审核中。"
                        )
                    # 发布成功后：把最新登录态（含「已信任设备」标记）写回，
                    # 下次少触发「新设备+扫码验证」风控
                    try:
                        context.storage_state(path=self.storage_state)
                        print(f"[info] 已保存最新登录态（含本设备信任标记）→ {self.storage_state}")
                    except Exception as e:  # noqa: BLE001
                        print(f"[warn] 保存登录态失败: {e}")

                    url = real_url or page.url
                except PublishUnconfirmed:
                    if self.keep_browser_on_failure and not self.headless:
                        keep_open = True
                        self._wait_for_manual_debug_close(browser, page)
                    raise
                except Exception:
                    # 失败现场：截图 + HTML，便于排查选择器/登录问题
                    self._dump_debug(page, "fail")
                    if self.keep_browser_on_failure and not self.headless:
                        keep_open = True
                        self._wait_for_manual_debug_close(browser, page)
                    raise
            finally:
                # 常规运行应立即回收浏览器；仅有头调试失败时交给用户手动关闭。
                if not keep_open:
                    browser.close()
        if real_url:
            return real_url
        return "https://www.xiaohongshu.com/explore/(已确认发布，链接见「笔记管理」)"

    def _wait_for_manual_debug_close(self, browser, page) -> None:
        """失败现场保留在桌面，直到用户关闭有头浏览器窗口。"""
        self._dump_debug(page, "failure_kept_open")
        print(
            "[debug] 发布失败，已保留浏览器窗口用于排查。"
            "请查看平台提示或手动修改；关闭浏览器窗口后本次发布流程才会结束。"
        )
        while browser.is_connected():
            time.sleep(0.5)

    def _click_publish(self, page):
        """点击真正的「发布」按钮（三层递进：像素定位 → 多坐标 → 文本穿透）。

        xhs-publish-btn 是闭合 shadow 的 Vue 自定义组件，内部按钮无法用选择器/
        无障碍树拿到。改为：
        1) 像素定位：截图宿主区域，扫描小红书品牌红(#ff2442)色块质心，点真实按钮中心
        2) 多坐标候选：若干常见比例点逐个试
        3) 文本穿透：兜底
        """
        loc = page.locator("xhs-publish-btn").first
        try:
            loc.wait_for(state="attached", timeout=4000)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 未找到发布按钮宿主元素: {e}")
            return self._click_publish_fallback(page)

        if self._click_publish_by_color(page, loc):
            return
        if self._click_publish_by_points(page, loc):
            return
        self._click_publish_fallback(page)

    def _click_publish_by_color(self, page, loc) -> bool:
        """像素定位红色「发布」按钮：截图宿主元素，扫红色像素簇质心点击。

        小红书「发布」按钮为品牌红 #ff2442（R 高、G/B 低）。扫描整个宿主区域，
        收集满足红色判定的像素，取质心作为按钮中心。比硬编码坐标更能自适应
        按钮实际位置与窗口尺寸变化。找到的红色像素太少（<阈值）视为失败。
        """
        try:
            import io
            from PIL import Image
            raw = loc.screenshot(timeout=5000)
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            w, h = img.size
            if w < 10 or h < 10:
                return False
            px = img.load()
            xs, ys = [], []
            for y in range(0, h, 2):
                for x in range(0, w, 2):
                    r, g, b = px[x, y]
                    # 品牌红判定：R 明显高于 G/B，且 R 足够亮（排除灰/白/黑）
                    if r >= 180 and (r - g) > 100 and (r - b) > 80:
                        xs.append(x)
                        ys.append(y)
            if len(xs) < 50:   # 红色像素太少 → 按钮可能未渲染/不可点
                print(f"[warn] 像素定位未找到红色按钮（红色像素仅 {len(xs)}）")
                return False
            cx, cy = int(sum(xs) / len(xs)), int(sum(ys) / len(ys))
            loc.click(position={"x": cx, "y": cy}, timeout=3000)
            print(f"[info] 像素定位红色「发布」按钮并点击 ({cx},{cy})（样本 {len(xs)} 像素）")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 像素定位发布失败: {e}")
            return False

    def _click_publish_by_points(self, page, loc) -> bool:
        """多坐标候选点逐个试（按钮常在宿主右半侧，覆盖多个比例位置）。"""
        try:
            bb = loc.bounding_box()
            if not bb:
                return False
            points = [(0.62, 0.5), (0.7, 0.5), (0.6, 0.6), (0.8, 0.5),
                      (0.55, 0.5), (0.75, 0.45)]
            for fx, fy in points:
                try:
                    x = int(bb["width"] * fx)
                    y = int(bb["height"] * fy)
                    loc.click(position={"x": x, "y": y}, timeout=2000)
                    print(f"[info] 坐标候选点击「发布」({x},{y})")
                    return True
                except Exception:  # noqa: BLE001
                    continue
            return False
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 多坐标点击失败: {e}")
            return False

    def _click_publish_fallback(self, page):
        """兜底：文本/穿透点击「发布」。"""
        for sel in ("xhs-publish-btn >> text=发布", "button:has-text('发布')"):
            try:
                pg_loc = page.locator(sel).first
                pg_loc.wait_for(state="attached", timeout=2000)
                pg_loc.click(timeout=2000)
                print(f"[info] 文本穿透点击「发布」成功: {sel}")
                return
            except Exception:  # noqa: BLE001
                continue
        print("[warn] 发布按钮点击失败（像素/坐标/文本三层均未命中）")

    def _check_risk_control(self, page) -> str | None:
        """检测小红书安全风控/扫码验证弹窗，命中则返回提示文案（否则 None）"""
        for kw in ("扫码验证", "验证身份", "请使用已登录该账号", "安全验证", "账号存在异常"):
            try:
                if page.get_by_text(kw).count() > 0:
                    return f"小红书安全风控：要求「{kw}」"
            except Exception:  # noqa: BLE001
                continue
        return None

    def _check_content_blocked(self, page) -> str | None:
        """检测「因违反社区规范禁止发笔记」类红色拦截提示，命中返回文案（否则 None）。

        小红书发布时若内容含违规词/敏感信息，会弹红色提示并禁止发布，
        但页面仍停留在编辑器（看起来像卡住）。此检测把「被拒」与「真卡住」区分开。
        """
        for kw in ("违反社区规范", "禁止发笔记", "发布失败", "内容违规", "审核不通过",
                   "无法发布", "违规内容", "请修改"):
            try:
                if page.get_by_text(kw, exact=False).count() > 0:
                    return f"小红书社区规范拦截：{kw}"
            except Exception:  # noqa: BLE001
                continue
        return None

    def _handle_risk_control(self, page) -> str:
        """处理扫码验证风控。返回：
        - "none"    未触发风控
        - "blocked" 触发风控且 headless（无法扫码）→ 调用方报错
        - "scanned" 触发风控，有头模式下已等待用户扫码完成
        """
        if not self._check_risk_control(page):
            return "none"
        if self.headless:
            return "blocked"
        deadline = time.time() + self.scan_wait_seconds
        print(f"\n⚠️ 小红书要求扫码验证：请在浏览器窗口中用「小红书APP」扫码。")
        print(f"   最长等待 {self.scan_wait_seconds} 秒，扫码完成后自动继续…\n")
        while time.time() < deadline:
            page.wait_for_timeout(2000)
            if not self._check_risk_control(page):
                print("[info] 扫码验证完成，继续发布…")
                return "scanned"
        raise PublishUnconfirmed(
            f"扫码验证超时（{self.scan_wait_seconds}s）。请重试，并在弹窗出现时尽快用小红书APP扫码。"
        )

    def _verify_published(self, page) -> tuple[bool, str | None]:
        """真实发布验证：URL 出现 published=true / 成功提示 / 查看笔记抓链接"""
        published = False
        real_url: str | None = None
        # 证据 1：URL 出现 published=true（真发布标志）或跳转到笔记页
        try:
            page.wait_for_url(
                lambda u: "published=true" in u or "published=1" in u
                          or (("explore" in u or "note" in u) and "creator" not in u),
                timeout=8000,
            )
            if "explore" in page.url or "note" in page.url:
                real_url = page.url
            published = True
        except Exception:  # noqa: BLE001
            pass
        # 证据 2：出现「发布成功」类提示
        if not published:
            for txt in ("发布成功", "已发布", "发布完成"):
                try:
                    page.get_by_text(txt).first.wait_for(state="visible", timeout=5000)
                    published = True
                    break
                except Exception:  # noqa: BLE001
                    continue
        # 证据 3：成功弹窗里点「查看笔记」拿真实链接（可能开新标签页）
        if published and real_url is None:
            for view_text in ("查看笔记", "查看详情"):
                try:
                    btn_v = page.get_by_text(view_text, exact=True).last
                    btn_v.click(timeout=3000)
                    page.wait_for_timeout(2500)
                    for pg in page.context.pages:
                        if "xiaohongshu.com/explore" in pg.url or "xiaohongshu.com/discovery" in pg.url:
                            real_url = pg.url
                            break
                    if real_url:
                        break
                except Exception:  # noqa: BLE001
                    continue
        return published, real_url

    def _add_topics_via_button(self, page, tags: list[str]):
        """图文模式：点「话题」按钮添加话题标签；失败时绝不改写正文。"""
        try:
            btn = self._find(page, self.selectors["topic"], 5000)
            btn.click()
            page.wait_for_timeout(1000)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 未找到「话题」按钮（{e}），已跳过话题添加（正文保持不变）")
            return
        added = 0
        for tag in tags:
            raw = tag.lstrip("#").strip()
            # 平台话题不接受 #、空格、标点等特殊字符；非法标签直接跳过。
            import re
            if not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff]{1,20}", raw):
                print(f"[warn] 跳过不符合平台格式的话题：{tag!r}")
                continue
            try:
                # 话题面板里的搜索/输入框
                inp = page.locator(
                    "div[class*='topic'] input, input[placeholder*='话题'], "
                    "input[placeholder*='搜索']"
                ).first
                inp.wait_for(state="visible", timeout=2500)
                inp.fill(raw)
                page.wait_for_timeout(600)
                # 点第一个搜索结果
                option = page.locator(
                    "div[class*='topic'] div[class*='item'], "
                    "div[class*='search'] div[class*='item'], "
                    "li[class*='topic']"
                ).first
                option.wait_for(state="visible", timeout=2000)
                option.click()
                page.wait_for_timeout(400)
                added += 1
            except Exception:  # noqa: BLE001 —— 单个话题失败不阻断
                continue
        if added:
            print(f"[info] 已添加 {added} 个话题标签")
        else:
            print("[warn] 话题面板添加未成功，已跳过话题（正文保持不变）")

    def _strip_inline_topics(self, body: str) -> str:
        """清理正文里游离的话题 # 行（发布前用）。

        生成链路本身不会在正文里加 #话题；这些一般是手动编辑或历史兜底逻辑
        混进去的。发布前把「独立成行的 #xxx」清掉，避免正文里一堆 # 显得乱、
        也降低被社区规范拦截的概率。真正的标签请走话题栏添加。
        """
        import re
        if not body:
            return body
        lines = body.splitlines()
        kept = [ln for ln in lines if not re.fullmatch(r"\s*#[\w\u4e00-\u9fff·-]+", ln)]
        return "\n".join(kept).strip()

    def _normalize_body(self, body: str) -> str:
        """规范化正文，规避小红书编辑器的「不支持连续空行输入」限制。

        小红书正文编辑器不允许连续（≥2）空行，会弹「不支持连续空行输入」并不接收
        多余空行，导致正文被截断/吞字（表现为「发布内容与生成的不一样」）。
        这里把连续空行压成单个空行，保留段落结构且不触发该限制。
        """
        if not body:
            return body
        import re
        # 统一 Windows/Unix 换行，删除空白行和行尾空格。小红书编辑器不接受
        # 自动化连续输入的空行，保留单换行即可表达分段。
        normalized = body.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.rstrip() for line in normalized.split("\n")]
        return "\n".join(line for line in lines if line.strip()).strip()

    def _add_topics_inline(self, page, tags: list[str]):
        """长文编辑器兜底：在正文末尾输入 #话题，触发小红书话题联想并选中。

        若联想未弹出，**不保留 # 文本**（避免污染正文），改为跳过该话题。
        宁可少几个话题，也不要「#话题」混进正文——那会显得乱且易被社区规范拦截。
        """
        body_el = None
        try:
            body_el = self._find(page, self.selectors["body"], 3000)
        except Exception:  # noqa: BLE001
            return
        for tag in tags:
            raw = tag.lstrip("#").strip()
            if not raw:
                continue
            try:
                body_el.click()
                page.keyboard.press("End")
                page.keyboard.type(f"\n#{raw}")
                page.wait_for_timeout(700)
                # 联想下拉：点第一个候选（常见结构兜底）
                sug = page.locator(
                    "div[class*='suggest'] li, "
                    "div[class*='topic'] div[class*='item'], "
                    "li[class*='suggest'], "
                    "div[class*='dropdown'] div[class*='item']"
                ).first
                sug.wait_for(state="visible", timeout=1800)
                sug.click()
                page.wait_for_timeout(400)
                print(f"[info] 话题「{raw}」已通过内联联想添加")
            except Exception:  # noqa: BLE001
                # 联想没弹出 → 删掉刚输入的 #raw，避免污染正文
                try:
                    body_el.click()
                    page.keyboard.press("End")
                    # 选中刚输入的行并删除（连 # 一起）
                    for _ in range(len(raw) + 2):
                        page.keyboard.press("Backspace")
                    page.keyboard.press("Enter")
                except Exception:  # noqa: BLE001 —— 清理失败不强求
                    pass
                print(f"[warn] 话题「{raw}」联想未触发，已从正文移除（不保留 #文本）")

    def _resolve_images(self, draft: Draft) -> list[str]:
        """解析图片路径：draft.metadata["images"]（列表）+ ["cover"]（封面，放最前）"""
        md = draft.metadata or {}
        images = [str(p) for p in md.get("images", []) if p]
        cover = md.get("cover")
        if cover:
            images.insert(0, str(cover))
        existing = [p for p in images if Path(p).exists()]
        if len(existing) != len(images):
            print(f"[warn] 有 {len(images) - len(existing)} 张图片不存在，已忽略")
        return existing


def export_login_state(
    state_path: str = "storage_state.json",
    headless: bool = False,
    channel: str | None = None,
) -> str:
    """人工登录一次并导出登录态（供无人值守使用）。

    用法:
        python main.py login            # 内置 Chromium
        python main.py login --browser chrome   # 用系统 Chrome（免下载）
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, notes = _launch_browser(p, None if channel in (None, "", "chromium") else channel, headless)
        for n in notes:
            print(f"[browser] {n}")
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://creator.xiaohongshu.com/publish/publish", wait_until="domcontentloaded")
        print("请在浏览器中完成扫码/登录。登录成功后回到这里按 Enter 保存会话…")
        input(">>> 登录完成？按 Enter 保存并退出: ")
        context.storage_state(path=state_path)
        browser.close()
    return state_path


def debug_selectors(
    state_path: str = "storage_state.json",
    headless: bool = True,
    channel: str | None = None,
    out_dir: str = "logs",
) -> str:
    """选择器诊断：打开发布页，导出真实元素结构 + 截图 + HTML。

    小红书改版导致「未找到元素」时，运行本命令把输出与 logs/selector_debug.*
    发给维护者，即可精确定位新选择器。

    用法:
        python main.py debug-selectors --browser msedge
    """
    from playwright.sync_api import sync_playwright

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser, notes = _launch_browser(p, None if channel in (None, "", "chromium") else channel, headless)
        for n in notes:
            print(f"[browser] {n}")
        context = browser.new_context(storage_state=state_path)
        page = context.new_page()
        page.goto("https://creator.xiaohongshu.com/publish/publish", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)   # 等 SPA 渲染

        print("=" * 60)
        print("【诊断】当前 URL :", page.url)
        print("【诊断】页面标题 :", page.title())
        body_text = page.locator("body").inner_text(timeout=5000)[:300].replace("\n", " | ")
        print("【诊断】可见文本 :", body_text)
        print("=" * 60)

        # 切到「写长文」编辑器（与发布流程同一套逻辑：JS 点页签 + 关引导面板）
        for attempt in range(2):
            try:
                ok = page.evaluate(
                    """() => {
                        const tabs = [...document.querySelectorAll('.creator-tab')];
                        const t = tabs.find(x => !x.hasAttribute('aria-hidden')
                                              && x.textContent.trim() === '写长文');
                        if (t) { t.click(); return true; }
                        return false;
                    }""")
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                page.wait_for_timeout(2500)
                for text in ("新的创作", "开始创作"):
                    try:
                        page.get_by_text(text, exact=True).first.click(timeout=2000)
                        page.wait_for_timeout(1500)
                        print(f"【诊断】已点「{text}」进入编辑器")
                        break
                    except Exception:  # noqa: BLE001
                        continue
                print("【诊断】已切换到「写长文」页签\n")
                break

        print("\n=== 输入/文本域元素（标题/话题候选） ===")
        for el in page.locator("input, textarea").all():
            ph = el.get_attribute("placeholder") or ""
            tp = el.get_attribute("type") or ""
            cls = (el.get_attribute("class") or "")[:80]
            tag = el.evaluate("e => e.tagName")
            print(f"  <{tag}> type={tp!r} placeholder={ph!r} class={cls!r}")

        print("\n=== 可编辑 div（正文候选） ===")
        for el in page.locator("div[contenteditable='true']").all():
            ph = el.get_attribute("data-placeholder") or el.get_attribute("placeholder") or ""
            cls = (el.get_attribute("class") or "")[:80]
            print(f"  <div contenteditable> placeholder={ph!r} class={cls!r}")

        print("\n=== 含「发布」的按钮 ===")
        for el in page.locator("button:has-text('发布'), div[class*='publish'] button").all():
            txt = (el.inner_text() or "").strip()[:30]
            cls = (el.get_attribute("class") or "")[:80]
            print(f"  文本={txt!r} class={cls!r}")

        shot = out / "selector_debug.png"
        page.screenshot(path=str(shot), full_page=False)
        html = page.content()
        (out / "selector_debug.html").write_text(html, encoding="utf-8")
        print(f"\n已保存截图: {shot}")
        print(f"已保存 HTML: {out / 'selector_debug.html'}")
        browser.close()
    return str(shot)


def probe_publish_ui(
    state_path: str = "storage_state.json",
    channel: str | None = None,
    headless: bool = True,
    mode: str = "tuwen",          # tuwen=上传图文 / article=写长文
    upload_path: str = "",        # 上传图文需先传图才出现编辑器；传图片路径
    out_dir: str = "logs",
) -> dict:
    """探测发布编辑器的真实交互元素（找真正的「发布」按钮）。

    上传图文模式需先上传 ≥1 张图片才出现标题/正文/发布按钮；
    传 upload_path 后可探测完整编辑器。

    用法:
        python main.py probe --mode tuwen --upload assets/note_cover.png
        python main.py probe --mode article
    """
    from playwright.sync_api import sync_playwright

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tab_label = "上传图文" if mode == "tuwen" else "写长文"
    found: dict[str, list[str]] = {"buttons": [], "inputs": [], "contenteditable": [], "publish_text": []}
    with sync_playwright() as p:
        browser, notes = _launch_browser(p, None if channel in (None, "", "chromium") else channel, headless)
        for n in notes:
            print(f"[browser] {n}")
        context = browser.new_context(storage_state=state_path)
        page = context.new_page()
        page.goto("https://creator.xiaohongshu.com/publish/publish", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        ok = page.evaluate(
            """(label) => {
                const tabs = [...document.querySelectorAll('.creator-tab')];
                const t = tabs.find(x => !x.hasAttribute('aria-hidden') && x.textContent.trim() === label);
                if (t) { t.click(); return true; } return false;
            }""", tab_label)
        print(f"【探字】已点击「{tab_label}」页签: {ok}")
        page.wait_for_timeout(3000)
        for btn_txt in ("新的创作", "开始创作"):
            try:
                page.get_by_text(btn_txt, exact=True).first.click(timeout=2000)
                page.wait_for_timeout(1500)
                print(f"【探字】已点「{btn_txt}」")
                break
            except Exception:
                continue

        # 上传图片（图文模式）：传图后编辑器才出现
        if mode == "tuwen" and upload_path and Path(upload_path).exists():
            try:
                page.locator("input[type='file'], input.upload-input").first.set_input_files(
                    str(Path(upload_path).resolve()))
                print(f"【探字】已上传图片: {upload_path}")
                page.wait_for_timeout(3000)   # 等上传+编辑器出现
            except Exception as e:  # noqa: BLE001
                print(f"【探字】上传失败: {e}")
        page.wait_for_timeout(2000)

        print("=" * 60)
        print(f"【探字】当前 URL: {page.url}")
        print("=" * 60)

        print("\n=== 所有 <button> 文本 ===")
        for el in page.locator("button").all():
            try:
                txt = (el.inner_text() or "").strip()
                cls = (el.get_attribute("class") or "")[:60]
                if txt:
                    found["buttons"].append(f"{txt[:30]} [{cls}]")
                    print(f"  {txt[:30]}  [{cls}]")
            except Exception:
                pass

        print("\n=== input/textarea ===")
        for el in page.locator("input, textarea").all():
            try:
                ph = el.get_attribute("placeholder") or ""
                cls = (el.get_attribute("class") or "")[:50]
                tag = el.evaluate("e => e.tagName")
                found["inputs"].append(f"<{tag}> ph={ph[:30]!r} [{cls}]")
                print(f"  <{tag}> ph={ph[:30]!r}  [{cls}]")
            except Exception:  # noqa: BLE001
                pass

        print("\n=== contenteditable ===")
        for el in page.locator("div[contenteditable='true']").all():
            try:
                ph = el.get_attribute("data-placeholder") or el.get_attribute("placeholder") or ""
                cls = (el.get_attribute("class") or "")[:60]
                found["contenteditable"].append(f"ph={ph[:40]!r} [{cls}]")
                print(f"  ph={ph[:40]!r}  [{cls}]")
            except Exception:  # noqa: BLE001
                pass

        print("\n=== 任意标签含「发布」文本的元素 ===")
        publish_hits = page.evaluate(
            """() => {
                const out = [];
                for (const el of document.querySelectorAll('*')) {
                    if (el.children.length === 0) {
                        const t = (el.textContent || '').trim();
                        if (t === '发布' || t.includes('发布')) {
                            const r = el.getBoundingClientRect();
                            out.push(`${el.tagName}  text=${t.slice(0,20)}  role=${el.getAttribute('role')||''}  cls=${(el.className||'').toString().slice(0,60)}  visible=${r.width>0&&r.height>0}`);
                        }
                    }
                }
                return out;
            }""")
        for hit in publish_hits:
            found["publish_text"].append(hit)
            print(f"  {hit}")

        # 深挖 xhs-publish-btn 内部（shadow DOM）结构
        print("\n=== xhs-publish-btn 内部按钮（穿透 shadow DOM） ===")
        try:
            inner = page.locator("xhs-publish-btn button").all()
            if inner:
                for b in inner:
                    txt = (b.inner_text() or "").strip()
                    cls = (b.get_attribute("class") or "")[:50]
                    print(f"  <button> text={txt[:20]!r} cls={cls}")
            else:
                print("  （没有可穿透的内部 button，尝试 shadowRoot 查询…）")
                shadow_btns = page.evaluate(
                    """() => {
                        const btn = document.querySelector('xhs-publish-btn');
                        if (!btn) return [];
                        const sh = btn.shadowRoot;
                        if (!sh) return [];
                        return [...sh.querySelectorAll('button, [role=button]')]
                            .map(x => (x.textContent||'').trim().slice(0,20));
                    }""")
                for t in shadow_btns:
                    print(f"  shadow button text={t!r}")
        except Exception as e:  # noqa: BLE001
            print(f"  xhs-publish-btn 内部分析失败: {e}")

        shot = out / f"probe_{mode}.png"
        page.screenshot(path=str(shot), full_page=False)
        (out / f"probe_{mode}.html").write_text(page.content(), encoding="utf-8")
        print(f"\n已保存截图: {shot}")
        browser.close()
    return found


def list_recent_notes(
    state_path: str = "storage_state.json",
    channel: str | None = None,
    headless: bool = True,
    limit: int = 10,
    out_dir: str = "logs",
) -> list[dict]:
    """打开创作者平台「笔记管理」，列出最近的笔记（标题/状态/链接）用于核实发布。

    用法:
        python main.py notes [--browser msedge]
    """
    from playwright.sync_api import sync_playwright

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    with sync_playwright() as p:
        browser, notes = _launch_browser(p, None if channel in (None, "", "chromium") else channel, headless)
        for n in notes:
            print(f"[browser] {n}")
        context = browser.new_context(storage_state=state_path)
        page = context.new_page()
        page.goto("https://creator.xiaohongshu.com/publish/publish", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        # 侧边栏点击「笔记管理」（URL note/manage 是 404，用导航进入）
        try:
            page.get_by_text("笔记管理").first.click(timeout=5000)
        except Exception:  # noqa: BLE001
            try:
                page.goto("https://creator.xiaohongshu.com/note/manage", wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                pass
        page.wait_for_timeout(6000)   # 等列表渲染

        print("=" * 60)
        print("【笔记管理】当前 URL:", page.url)
        print("=" * 60)

        # 真实笔记链接（xiaohongshu.com/explore/<id>）
        note_links: list[str] = []
        for a in page.locator("a[href*='/explore/'], a[href*='xiaohongshu.com/explore']").all():
            href = a.get_attribute("href") or ""
            if "/explore/" in href:
                note_links.append(href if href.startswith("http") else "https://www.xiaohongshu.com" + href)
        seen: set[str] = set()
        for u in note_links:
            if u not in seen:
                seen.add(u)
                results.append({"url": u})

        # 列表可见文本（含标题/状态）
        body_text = page.locator("body").inner_text(timeout=5000)
        lines = [l.strip() for l in body_text.splitlines() if l.strip()]
        status_words = ("已发布", "审核中", "未通过", "草稿", "待发布", "已隐藏")
        import re as _re
        printed = 0
        # 打印每条笔记的「标题 + 日期 + 状态」（日期形如 2024-06-16）
        for i, line in enumerate(lines):
            if _re.match(r"^\d{4}-\d{2}-\d{2}", line) and i > 0:
                title = lines[i - 1]
                if title and title not in ("全部", "笔记管理"):
                    # 找该条状态
                    status = next((w for w in status_words if any(w in l for l in lines[i:i+3])), "")
                    print(f"  · {title[:40]}  {line}  {status}")
                    printed += 1
                    if printed >= limit:
                        break

        shot = out / "notes_manage.png"
        page.screenshot(path=str(shot), full_page=False)
        print(f"\n已保存截图: {shot}")
        browser.close()

    print(f"\n共发现 {len(results)} 个笔记链接:")
    for r in results[:limit]:
        print(f"  - {r['url']}")
    return results
