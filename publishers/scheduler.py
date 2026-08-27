"""小红书定时自动发布调度器（商用级）

内容库模型：content_bank/*.json 一稿一文件
{
  "id": "20260827-001",              # 必填，唯一
  "topic": "秋招穿搭",                # 必填
  "scheduled_at": "2026-08-28 09:00",# 必填，格式 %Y-%m-%d %H:%M
  "title": "...",                     # 可选：缺省由生成链路补齐
  "body": "...",
  "tags": ["#穿搭"],
  "images": ["assets/cover.png"],     # 可选：配图
  "cover": "assets/cover.png"         # 可选：封面（放最前）
}

调度流程（每篇）：
  到期检查 → 内容补齐（缺内容走生成链路） → 合规门禁 →
  Harness 限速/审批 → 真实发布 → 写 publish_log.json（审计留痕）

用法（详见 README「怎么开始」）：
  python main.py schedule --run-once            # 执行一次到期任务
  python main.py schedule --daemon --interval 60 --yes   # 无人值守循环
"""
from __future__ import annotations
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from core.types import Draft
from core.compliance import ComplianceEngine, load_compliance_config
from core.orchestrator import Orchestrator
from core.harness import Harness, AuditLog
from publishers.base import Publisher
from publishers.xhs import DryRunPublisher, XhsPlaywrightPublisher, LoginRequired, ApprovalDenied

TIME_FMT = "%Y-%m-%d %H:%M"
BANK_DIR = "content_bank"
LOG_PATH = "publish_log.json"


@dataclass
class BankItem:
    """内容库一条待发稿"""
    path: str
    data: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.data.get("id") or Path(self.path).stem)

    @property
    def topic(self) -> str:
        return str(self.data.get("topic", ""))

    @property
    def scheduled_at(self) -> datetime | None:
        raw = self.data.get("scheduled_at")
        if not raw:
            return None
        try:
            return datetime.strptime(str(raw), TIME_FMT)
        except ValueError:
            return None

    def to_draft(self) -> Draft:
        return Draft(
            topic=self.topic,
            title=self.data.get("title"),
            body=self.data.get("body"),
            tags=[str(t) for t in self.data.get("tags", [])],
            cover_text=self.data.get("cover_text"),
            metadata={
                "images": list(self.data.get("images", [])),
                "cover": self.data.get("cover"),
                "bank_id": self.id,
            },
        )


@dataclass
class PublishLog:
    """发布留痕：一稿一条记录，供审计/去重"""
    path: str = LOG_PATH
    records: dict[str, dict] = field(default_factory=dict)

    def load(self):
        p = Path(self.path)
        if p.exists():
            try:
                self.records = json.loads(p.read_text(encoding="utf-8-sig"))  # 容忍 BOM
            except (json.JSONDecodeError, OSError):
                self.records = {}
        return self

    def record(self, bank_id: str, status: str, url: str = "", reason: str = ""):
        self.records[bank_id] = {
            "status": status,          # published / skipped / failed / blocked
            "url": url,
            "reason": reason,
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.path).write_text(
            json.dumps(self.records, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class ContentBank:
    """扫描 content_bank/ 下的 JSON 稿件"""

    def __init__(self, bank_dir: str = BANK_DIR):
        self.bank_dir = Path(bank_dir)

    def list_items(self) -> list[BankItem]:
        if not self.bank_dir.exists():
            return []
        items = []
        for f in sorted(self.bank_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8-sig"))  # 容忍 BOM
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, dict):
                items.append(BankItem(path=str(f), data=data))
        return items


class PublishScheduler:
    """定时自动发布：到期检测 → 补齐内容 → 合规 → 审批 → 发布 → 留痕"""

    def __init__(
        self,
        orchestrator: Orchestrator,
        publisher: Publisher | None = None,
        compliance: ComplianceEngine | None = None,
        bank: ContentBank | None = None,
        log: PublishLog | None = None,
        auto_approve: bool = False,
    ):
        self.orch = orchestrator
        self.publisher = publisher or DryRunPublisher()
        self.compliance = compliance or ComplianceEngine(
            load_compliance_config("config/compliance.yaml")
        )
        self.bank = bank or ContentBank()
        self.log = (log or PublishLog()).load()
        self.auto_approve = auto_approve

    def _fill_content(self, item: BankItem) -> Draft:
        """已有内容直接用；缺标题/正文则走生成链路补齐（Skill 链）"""
        draft = item.to_draft()
        if not (draft.title and draft.body and draft.tags):
            generated = self.orch.run(topic=item.topic)
            if not draft.title:
                draft.title = generated.title
            if not draft.body:
                draft.body = generated.body
            if not draft.tags:
                draft.tags = generated.tags
            if not draft.cover_text:
                draft.cover_text = generated.cover_text
        return draft

    def due_items(self, now: datetime | None = None) -> list[BankItem]:
        now = now or datetime.now()
        return [
            it for it in self.bank.list_items()
            if it.scheduled_at and it.scheduled_at <= now
            and it.id not in self.log.records
        ]

    def run_once(self, now: datetime | None = None) -> list[dict]:
        """执行所有到期未发稿件，返回逐条结果报告"""
        report: list[dict] = []
        for item in self.due_items(now):
            entry = self._process(item)
            report.append(entry)
        return report

    def run_loop(self, interval: float = 60.0, max_rounds: int | None = None):
        """无人值守循环：每 interval 秒扫一次到期任务"""
        rounds = 0
        while max_rounds is None or rounds < max_rounds:
            rounds += 1
            done = self.run_once()
            for d in done:
                print(f"  [{d['id']}] {d['status']}: {d.get('reason', '')}")
            if max_rounds is None:
                time.sleep(interval)
            elif rounds < max_rounds:
                time.sleep(interval)

    # ---------- 单篇处理 ----------

    def _process(self, item: BankItem) -> dict:
        base = {"id": item.id, "topic": item.topic, "scheduled_at": item.scheduled_at}
        try:
            # 1) 内容补齐
            draft = self._fill_content(item)
            if not (draft.title and draft.body):
                return self._skip(base, "内容不完整（生成链路未产出）")

            # 2) 合规门禁
            comp = self.compliance.check_draft(draft)
            if not comp.ok:
                words = "、".join(h.word for h in comp.hits)
                return self._skip(base, f"合规拦截: {words}")

            # 3) 发布（含审批/限速/重试/幂等）
            publisher = self.publisher
            if self.auto_approve and isinstance(publisher, XhsPlaywrightPublisher):
                # 无人值守：显式关闭人工确认
                publisher.auto_approve = True
            result = publisher.publish(draft)
            if result.success:
                self.log.record(item.id, "published", url=result.url or "")
                return {**base, "status": "published", "url": result.url, "reason": result.message}
            return self._skip(base, f"发布失败: {result.message}", status="failed")
        except ApprovalDenied as e:
            return self._skip(base, f"审批拒绝: {e}", status="blocked")
        except LoginRequired as e:
            return self._skip(base, f"登录态缺失: {e}", status="blocked")
        except Exception as e:  # noqa: BLE001 —— 单篇失败不拖垮整批
            return self._skip(base, f"异常: {e}", status="failed")

    def _skip(self, base: dict, reason: str, status: str = "skipped") -> dict:
        self.log.record(base["id"], status, reason=reason)
        return {**base, "status": status, "reason": reason}
