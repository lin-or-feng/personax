#!/usr/bin/env python3
"""PersonaX CLI —— 小红书内容生成 + 商用级自动发布

子命令：
  generate  生成一篇笔记（默认；含合规检查 + 干跑发布）
  publish   发布指定稿件（--real 真发 / 默认干跑）
  schedule  定时自动发布（--run-once 或 --daemon 无人值守）
  login     浏览器登录一次，导出登录态 storage_state.json
  eval      跑评测集，输出 eval_results.csv

示例：
  python main.py generate --topic "秋招穿搭"
  python main.py publish --draft content_bank/example.json --real
  python main.py schedule --run-once
  python main.py schedule --daemon --interval 60 --yes
  python main.py login
  python main.py eval
"""
from __future__ import annotations
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

# 读取 .env（若存在）：DEEPSEEK_API_KEY 等配置，系统环境变量优先
from core.envfile import load_env_file
load_env_file(os.path.join(os.path.dirname(__file__), ".env"))

# Windows 控制台默认 GBK，强制 UTF-8 输出（否则 emoji 打印直接崩）
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import yaml

import skills  # noqa: F401  触发 @register
from core.orchestrator import Orchestrator
from core.harness import Harness, RuleConfig, AuditLog
from core.compliance import ComplianceEngine, load_compliance_config
from publishers.xhs import (
    DryRunPublisher, XhsPlaywrightPublisher, export_login_state, debug_selectors,
    list_recent_notes, probe_publish_ui,
)


def load_persona(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_persona_arg(value: str) -> dict:
    """--persona 支持「人格库名字」或「yaml 文件路径」。

    默认人格：未指定/空/未知名字 → 都用 persona.yaml（默认人格），
    只有显式传入人格库中的名字时才切换为其他人格。
    """
    from core.persona import list_personas, resolve_persona
    if value and value in list_personas():
        return resolve_persona(value)
    if value and os.path.exists(value):
        return load_persona(value)
    return load_persona("config/persona.yaml")   # 默认人格兜底


def build_orchestrator(persona: dict, user: str) -> tuple[Orchestrator, Harness, AuditLog]:
    audit = AuditLog()
    harness = Harness(RuleConfig(**persona.get("harness", {})), audit=audit)
    orch = Orchestrator(persona=persona, harness=harness)
    return orch, harness, audit


def _print_draft(draft):
    print("\n" + "=" * 50)
    print(f"【{draft.title}】")
    print(f"标签: {draft.tags}")
    print("-" * 50)
    print(draft.body)
    print("-" * 50)
    print(f"封面: {draft.cover_text}")


def cmd_generate(args):
    persona = resolve_persona_arg(args.persona)
    orch, harness, audit = build_orchestrator(persona, args.user)
    compliance = ComplianceEngine(load_compliance_config(args.compliance))

    # 默认 Skill 链：路由 top-3 内容 Skill + 发布就绪门禁（触发 Harness 审批记录）
    if args.skill_chain:
        chain = args.skill_chain
    else:
        from core.registry import route as route_skills
        chain = [n for n, _ in route_skills(args.topic, top_k=3)] + ["xhs_publish"]

    draft = orch.run(topic=args.topic, user_id=args.user, skill_chain=chain)
    _print_draft(draft)

    # 合规门禁（商用：发布前必检）
    comp = compliance.check_draft(draft)
    print(f"\n[合规] {comp.summary}")
    for h in comp.hits:
        print(f"  - {h}")
    print(f"[就绪门禁] publish_ready={draft.metadata.get('publish_ready')} "
          f"issues={draft.metadata.get('publish_issues', [])}")

    # 发布（默认干跑；--publish/--real 才真发）
    if args.real:
        publisher = XhsPlaywrightPublisher(
            storage_state=args.storage_state,
            headless=not args.headed,
            channel=args.browser,
            harness=harness,
            user_id=args.user,
        )
    else:
        publisher = DryRunPublisher()
    try:
        result = publisher.publish(draft)
        print(f"\n发布: {result.message} (耗时{result.cost_ms}ms)")
    except Exception as e:  # noqa: BLE001 —— 审批拒绝/登录缺失等
        print(f"\n发布未执行: {e}")

    print(f"\n[审计] 共 {len(audit.entries)} 条记录")
    for e in audit.entries[-6:]:
        print(f"  - {e}")


def cmd_publish(args):
    """发布 content_bank 里的单篇稿件"""
    from publishers.scheduler import ContentBank, PublishLog

    data = json.load(open(args.draft, "r", encoding="utf-8-sig"))  # 容忍 BOM
    persona = resolve_persona_arg(args.persona)
    orch, harness, audit = build_orchestrator(persona, args.user)
    compliance = ComplianceEngine(load_compliance_config(args.compliance))

    from publishers.scheduler import BankItem
    item = BankItem(path=args.draft, data=data)
    draft = orch.run(topic=item.topic, user_id=args.user)
    # 稿件自带字段优先
    if data.get("title"):
        draft.title = data["title"]
    if data.get("body"):
        draft.body = data["body"]
    if data.get("tags"):
        draft.tags = [str(t) for t in data["tags"]]
    draft.metadata.update({"images": data.get("images", []), "cover": data.get("cover")})

    _print_draft(draft)
    comp = compliance.check_draft(draft)
    print(f"\n[合规] {comp.summary}")
    if not comp.ok:
        for h in comp.hits:
            print(f"  - {h}")
        print("发布已拦截（先修改内容再重试）")
        return

    publisher = DryRunPublisher() if not args.real else XhsPlaywrightPublisher(
        storage_state=args.storage_state,
        headless=not args.headed,
        channel=args.browser,
        auto_approve=args.yes,
        harness=harness,
        user_id=args.user,
    )
    try:
        result = publisher.publish(draft)
        print(f"\n发布: {result.message}")
        if result.success:
            log = PublishLog().load()
            log.record(item.id, "published", url=result.url or "")
        elif "未找到元素" in result.message:
            print("\n💡 提示：小红书页面改版导致选择器失效。"
                  "请运行 python main.py debug-selectors --browser msedge 导出真实页面结构，"
                  "把输出和 logs/selector_debug.* 发给我，即可更新选择器。")
    except Exception as e:  # noqa: BLE001
        print(f"\n发布未执行: {e}")


def cmd_schedule(args):
    from publishers.scheduler import PublishScheduler, ContentBank, PublishLog

    persona = resolve_persona_arg(args.persona)
    orch, harness, audit = build_orchestrator(persona, args.user)
    compliance = ComplianceEngine(load_compliance_config(args.compliance))

    publisher = DryRunPublisher() if not args.real else XhsPlaywrightPublisher(
        storage_state=args.storage_state,
        headless=not args.headed,
        channel=args.browser,
        auto_approve=args.yes,   # 无人值守必须显式 --yes
        harness=harness,
        user_id=args.user,
    )
    sched = PublishScheduler(
        orchestrator=orch,
        publisher=publisher,
        compliance=compliance,
        bank=ContentBank(args.bank),
        log=PublishLog(),
        auto_approve=args.yes,
    )
    if args.daemon:
        print(f"[调度] 无人值守循环启动，每 {args.interval}s 扫描一次 content_bank（Ctrl+C 退出）")
        try:
            sched.run_loop(interval=args.interval)
        except KeyboardInterrupt:
            print("\n[调度] 已停止")
    else:
        report = sched.run_once()
        if not report:
            print("[调度] 当前无到期待发稿件")
        for r in report:
            print(f"  [{r['id']}] {r['topic']} → {r['status']}: {r.get('reason', r.get('url', ''))}")


def cmd_login(args):
    path = export_login_state(args.storage_state, headless=False, channel=args.browser)
    print(f"登录态已保存: {path}")


def cmd_debug_selectors(args):
    shot = debug_selectors(
        state_path=args.storage_state,
        headless=not args.headed,
        channel=args.browser,
    )
    print(f"\n诊断完成。把上面的输出 + {shot} + logs/selector_debug.html 发给维护者即可定位新选择器。")


def cmd_notes(args):
    """打开「笔记管理」核实真实发布结果"""
    list_recent_notes(
        state_path=args.storage_state,
        headless=not args.headed,
        channel=args.browser,
        limit=args.limit,
    )


def cmd_personas(args):
    """列出人格库，展示各人格摘要"""
    from core.persona import list_personas, get_persona
    names = list_personas()
    if not names:
        print("人格库为空（config/personas.yaml）")
        return
    print(f"人格库（{len(names)} 个）：")
    for n in names:
        p = get_persona(n)
        print(f"  - {n}  [{p.get('tone', '-')}]  {p.get('description', '')[:44]}")
    print("\n用法：生成/发布时用 --persona <名字> 选择，如:")
    print('  python main.py generate --topic "考研英语" --persona 干货知识风')


def cmd_probe(args):
    """探测发布编辑器真实交互元素（找发布按钮）"""
    probe_publish_ui(
        state_path=args.storage_state,
        headless=not args.headed,
        channel=args.browser,
        mode=args.mode,
        upload_path=args.upload or "",
    )


def cmd_eval(args):
    """复用 eval/scorer.py 逻辑，输出 eval_results.csv"""
    from eval.scorer import main as eval_main
    eval_main()


def main():
    parser = argparse.ArgumentParser(
        prog="personax",
        description="PersonaX - 小红书内容生成与商用级自动发布",
    )
    parser.add_argument("--persona", default="config/persona.yaml",
                        help="人格库名字（如 干货知识风）或 persona.yaml 路径；python main.py personas 查看可用人格")
    parser.add_argument("--compliance", default="config/compliance.yaml")
    parser.add_argument("--storage-state", default="storage_state.json")
    sub = parser.add_subparsers(dest="command")

    # generate（默认）
    p_gen = sub.add_parser("generate", help="生成一篇笔记（默认命令）")
    p_gen.add_argument("--topic", default="秋招穿搭")
    p_gen.add_argument("--persona", default="config/persona.yaml",
                       help="人格库名字（如 干货知识风）或 persona.yaml 路径")
    p_gen.add_argument("--user", default="demo_user")
    p_gen.add_argument("--skill-chain", nargs="*", default=None,
                       help="指定 Skill 链，如: title_generator body_writer tag_selector cover_writer xhs_publish")
    p_gen.add_argument("--real", action="store_true", help="真实发布（默认干跑）")
    p_gen.add_argument("--publish", dest="real", action="store_true",
                       help="[旧用法别名] 等价于 --real")
    p_gen.add_argument("--headed", action="store_true", help="有头浏览器（调试用）")
    p_gen.add_argument("--browser", default=None, choices=["chromium", "chrome", "msedge"],
                       help="浏览器：chromium(默认) / chrome(系统Chrome) / msedge(系统Edge，免下载)")
    p_gen.set_defaults(func=cmd_generate)

    # publish
    p_pub = sub.add_parser("publish", help="发布 content_bank 里的单篇稿件")
    p_pub.add_argument("--draft", required=True, help="稿件 JSON 路径，如 content_bank/example.json")
    p_pub.add_argument("--persona", default="config/persona.yaml",
                       help="人格库名字（如 干货知识风）或 persona.yaml 路径")
    p_pub.add_argument("--user", default="demo_user")
    p_pub.add_argument("--real", action="store_true", help="真实发布（默认干跑）")
    p_pub.add_argument("--headed", action="store_true")
    p_pub.add_argument("--yes", action="store_true", help="跳过人工确认（无人值守）")
    p_pub.add_argument("--browser", default=None, choices=["chromium", "chrome", "msedge"],
                       help="浏览器：chromium(默认) / chrome(系统Chrome) / msedge(系统Edge，免下载)")
    p_pub.set_defaults(func=cmd_publish)

    # schedule
    p_sch = sub.add_parser("schedule", help="定时自动发布")
    p_sch.add_argument("--bank", default="content_bank", help="内容库目录")
    p_sch.add_argument("--persona", default="config/persona.yaml",
                       help="人格库名字（如 干货知识风）或 persona.yaml 路径")
    p_sch.add_argument("--user", default="demo_user")
    p_sch.add_argument("--real", action="store_true", help="真实发布（默认干跑）")
    p_sch.add_argument("--headed", action="store_true")
    p_sch.add_argument("--yes", action="store_true", help="无人值守（跳过人工确认，需已登录）")
    p_sch.add_argument("--daemon", action="store_true", help="循环模式")
    p_sch.add_argument("--run-once", action="store_true", help="执行一次到期任务（默认行为）")
    p_sch.add_argument("--interval", type=float, default=60.0, help="循环扫描间隔（秒）")
    p_sch.add_argument("--browser", default=None, choices=["chromium", "chrome", "msedge"],
                       help="浏览器：chromium(默认) / chrome(系统Chrome) / msedge(系统Edge，免下载)")
    p_sch.set_defaults(func=cmd_schedule)

    # login
    p_login = sub.add_parser("login", help="浏览器登录一次并导出登录态")
    p_login.add_argument("--browser", default=None, choices=["chromium", "chrome", "msedge"],
                         help="浏览器：chromium(默认) / chrome(系统Chrome) / msedge(系统Edge，免下载)")
    p_login.set_defaults(func=cmd_login)

    # debug-selectors（改版排查）
    p_dbg = sub.add_parser("debug-selectors", help="导出发布页真实元素结构（选择器失效时用）")
    p_dbg.add_argument("--headed", action="store_true", help="有头浏览器")
    p_dbg.add_argument("--browser", default=None, choices=["chromium", "chrome", "msedge"],
                       help="浏览器：chromium(默认) / chrome(系统Chrome) / msedge(系统Edge，免下载)")
    p_dbg.set_defaults(func=cmd_debug_selectors)

    # notes（核实发布结果）
    p_notes = sub.add_parser("notes", help="打开笔记管理，列出最近笔记核实发布")
    p_notes.add_argument("--headed", action="store_true", help="有头浏览器")
    p_notes.add_argument("--limit", type=int, default=10, help="最多列出条数")
    p_notes.add_argument("--browser", default=None, choices=["chromium", "chrome", "msedge"],
                         help="浏览器：chromium(默认) / chrome(系统Chrome) / msedge(系统Edge，免下载)")
    p_notes.set_defaults(func=cmd_notes)

    # personas（查看人格库）
    p_pers = sub.add_parser("personas", help="列出人格库")
    p_pers.set_defaults(func=cmd_personas)

    # probe（找发布按钮）
    p_probe = sub.add_parser("probe", help="探测发布编辑器真实按钮/输入框（定位发布按钮）")
    p_probe.add_argument("--mode", default="tuwen", choices=["tuwen", "article"],
                         help="tuwen=上传图文 / article=写长文")
    p_probe.add_argument("--upload", default="", help="图片路径（tuwen 模式先传图再探测编辑器）")
    p_probe.add_argument("--headed", action="store_true", help="有头浏览器")
    p_probe.add_argument("--browser", default=None, choices=["chromium", "chrome", "msedge"],
                         help="浏览器：chromium(默认) / chrome(系统Chrome) / msedge(系统Edge，免下载)")
    p_probe.set_defaults(func=cmd_probe)

    # eval
    p_eval = sub.add_parser("eval", help="跑评测集输出 CSV")
    p_eval.set_defaults(func=cmd_eval)

    argv = sys.argv[1:]

    # 兼容旧用法：`python main.py` / `python main.py --topic xxx [--publish]` → generate
    if not argv or argv[0].startswith("-"):
        if argv and argv[0] in ("--help", "-h"):
            parser.parse_args(argv)   # 顶层帮助（显示全部子命令）并退出
        ns = p_gen.parse_args(argv)
        # 补上父级默认选项（--persona/--compliance/--storage-state）
        parent_defaults = parser.parse_args([])
        for opt in ("persona", "compliance", "storage_state"):
            if not hasattr(ns, opt):
                setattr(ns, opt, getattr(parent_defaults, opt))
        cmd_generate(ns)
        return

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

