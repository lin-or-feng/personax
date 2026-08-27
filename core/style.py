"""StyleEnforcer：Persona 风格硬约束校验 + 打回机制"""
from __future__ import annotations
from pydantic import BaseModel
from .types import Draft


class StyleReport(BaseModel):
    ok: bool
    issues: list[str] = []


class StyleEnforcer:
    """硬约束（forbidden/句长）不通过则 ok=False；软约束仅记录。"""

    def __init__(self, persona: dict):
        self.forbidden = persona.get("forbidden", [])
        self.max_len = {"short": 45, "medium": 90, "long": 180}.get(
            persona.get("sentence_length", "short"), 40
        )

    def check(self, text: str) -> StyleReport:
        issues: list[str] = []
        if not text:
            return StyleReport(ok=True)
        # 硬约束：禁用词
        for word in self.forbidden:
            if word and word in text:
                issues.append(f"禁用词命中: {word}")
        # 硬约束：句长（按句号/感叹号/问号/分号/换行分句）
        normalized = text.replace("！", "!").replace("？", "?")
        sentences = []
        for para in normalized.splitlines():
            for sep in ["。", "!", "?", "～", "；", ";"]:
                para = para.replace(sep, "\n")
            sentences.extend(s.strip() for s in para.splitlines() if s.strip())
        for sent in sentences:
            if len(sent) > self.max_len:
                issues.append(f"句长超标({len(sent)}>{self.max_len}): {sent[:20]}...")
        return StyleReport(ok=len(issues) == 0, issues=issues)

    def enforce(self, draft: Draft) -> StyleReport:
        """对草稿全文做校验"""
        parts = [draft.title or "", draft.body or "", draft.cover_text or ""]
        combined = " ".join(parts)
        return self.check(combined)
