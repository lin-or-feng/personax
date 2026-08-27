"""内容合规引擎（商用级发布前门禁）

小红书/广告法发布红线检测，独立于业务（对齐 Harness 管控思想）：
- 广告法绝对化用语：最 / 第一 / 国家级 / 顶级 / 唯一 …
- 违禁医疗/金融承诺：根治 / 100% 有效 / 稳赚 …
- 平台规则：外链 / 微信 / 二维码导流 / 引流话术
- 自定义词表（config/compliance.yaml），可热更新，不侵入业务代码

用法：
    from core.compliance import ComplianceEngine, load_compliance_config
    engine = ComplianceEngine(load_compliance_config("config/compliance.yaml"))
    report = engine.check(title, body, tags)
    if not report.ok:
        print(report.hits)   # 命中词 + 建议替换
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import yaml

from .types import Draft

# 内置兜底词表：即便配置文件缺失也能工作（商用要求 fail-safe）
_BUILTIN = {
    "ad_law": [  # 广告法禁止的绝对化用语（正则变体，避免误伤「最后」「第一，第二」等）
        r"最(好|大|低|高|强|佳|优|新|快|便宜|火爆|受欢迎|值得|划算)",
        r"第一(品牌|销量|选择|推荐|口碑|名|位)",
        r"销量第[一二三]",
        "国家级", "世界级", "顶级", "极致", "绝对", "唯一",
        "首选", "独家", "万能", "百分百", "100%", "史无前例", "绝无仅有",
        "永久", "全网最低", "全网第一", "销量第一", "冠军",
    ],
    "medical": [  # 医疗/健康承诺（小红书高危）
        "根治", "治愈", "药到病除", "无副作用", "包治", "秘方",
        "减肥神药", "三天见效", "立刻见效", "保证有效",
    ],
    "finance": [  # 金融承诺
        "稳赚", "保本", "必赚", "零风险", "翻倍收益", "躺赚",
    ],
    "platform": [  # 平台导流规则
        "加微信", "vx", "weixin", "公众号", "二维码", "扫码",
        "私信我", "点击链接", "淘宝店", "下单链接",
    ],
}


@dataclass
class ComplianceHit:
    category: str          # ad_law / medical / finance / platform
    word: str
    suggestion: str = ""

    def __str__(self) -> str:
        return f"[{self.category}] 命中「{self.word}」{('→ ' + self.suggestion) if self.suggestion else ''}"


@dataclass
class ComplianceReport:
    ok: bool
    hits: list[ComplianceHit] = field(default_factory=list)
    checked_chars: int = 0

    @property
    def summary(self) -> str:
        if self.ok:
            return "合规 ✓"
        return f"不合规 ✗（{len(self.hits)} 处命中）"


def load_compliance_config(path: str | Path) -> dict:
    """读取 compliance.yaml；缺失时返回内置词表，保证 fail-safe"""
    p = Path(path)
    if not p.exists():
        return _BUILTIN
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    # 配置与内置合并：配置覆盖同 key 词表，缺失 key 用内置
    merged = dict(_BUILTIN)
    for k, words in (data.get("wordlists") or {}).items():
        merged[k] = [str(w) for w in words]
    return merged


class ComplianceEngine:
    """合规门禁：逐词命中检测 + 正则变体（如 vx / weixin）"""

    def __init__(self, wordlists: dict | None = None):
        self.wordlists = wordlists or _BUILTIN
        # 预编译正则：中文词直接包含，字母词忽略大小写
        self._patterns: dict[str, list[re.Pattern]] = {}
        for cat, words in self.wordlists.items():
            self._patterns[cat] = []
            for w in words:
                if not w:
                    continue
                flag = re.IGNORECASE if w.isascii() else 0
                self._patterns[cat].append(re.compile(re.escape(w), flag))

    def check(
        self,
        title: Optional[str] = None,
        body: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> ComplianceReport:
        parts: list[str] = [t for t in (title, body) if t]
        if tags:
            parts.append(" ".join(tags))
        text = "\n".join(parts)
        hits: list[ComplianceHit] = []
        for cat, pats in self._patterns.items():
            for p in pats:
                if p.search(text):
                    hits.append(ComplianceHit(category=cat, word=p.pattern))
        return ComplianceReport(ok=len(hits) == 0, hits=hits, checked_chars=len(text))

    def check_draft(self, draft: Draft) -> ComplianceReport:
        """对 Draft 全字段做门禁（商用：发布前必检）"""
        return self.check(draft.title, draft.body, draft.tags)
