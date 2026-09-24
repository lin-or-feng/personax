"""轻量答案级事实支持度检查。

该模块只做可复现的词法支持度基线，不宣称替代 NLI、人工标注或
LLM-as-Judge。它适合 CI 捕获“引用编号存在但内容明显不相关”的回归。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .types import AssistantSource


_CITATION_RE = re.compile(r"\[(\d+)\]")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+.-]*|\d+(?:\.\d+)?")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")
_STOP_TOKENS = {
    "根据", "资料", "可以", "需要", "以及", "一个", "进行", "使用", "通过",
    "其中", "相关", "当前", "本地", "系统", "这是", "能够", "负责", "方面",
}
_NEGATION_RE = re.compile(r"尚未|未曾|没有|并非|不能|不会|不支持|未")
_POSITIVE_COMPLETION_RE = re.compile(r"已经|已实现|已支持|已替代|已完成|当前支持")


@dataclass(frozen=True)
class GroundingClaim:
    text: str
    citations: tuple[int, ...]
    score: float
    supported: bool
    reason: str


@dataclass(frozen=True)
class GroundingReport:
    claims: tuple[GroundingClaim, ...]
    supported_claims: int
    cited_claims: int
    support_rate: float


def _tokens(text: str) -> set[str]:
    cleaned = _CITATION_RE.sub("", text).lower()
    tokens = {item for item in _LATIN_RE.findall(cleaned) if len(item) > 1}
    han = _HAN_RE.findall(cleaned)
    tokens.update(han[index] + han[index + 1] for index in range(len(han) - 1))
    return {item for item in tokens if item not in _STOP_TOKENS}


def _claim_sentences(answer: str) -> list[str]:
    sentences: list[str] = []
    for raw in _SENTENCE_RE.split(answer or ""):
        sentence = raw.strip()
        if not sentence or sentence.lstrip().startswith(">"):
            continue
        # 兼容模型把引用写在句号之后的常见格式："结论。[1]"。
        if sentences and re.fullmatch(r"(?:\[\d+\][,，、 ]*)+[。！？!?；;]?", sentence):
            sentences[-1] += f" {sentence}"
        else:
            sentences.append(sentence)
    return sentences


def claim_support(
    claim: str,
    sources: Sequence[AssistantSource],
    *,
    min_score: float = 0.42,
) -> GroundingClaim:
    """判断单条带引用结论是否被对应来源词法支持。"""

    citations = tuple(dict.fromkeys(int(value) for value in _CITATION_RE.findall(claim)))
    if not citations:
        return GroundingClaim(claim, (), 0.0, False, "缺少引用编号")
    source_map = {int(source.ref_id): source for source in sources if source.ref_id.isdigit()}
    invalid = [value for value in citations if value not in source_map]
    if invalid:
        return GroundingClaim(claim, citations, 0.0, False, f"无效引用：{invalid}")
    claim_tokens = _tokens(claim)
    if not claim_tokens:
        return GroundingClaim(claim, citations, 0.0, False, "结论缺少可比较信息")
    best = 0.0
    for citation in citations:
        source = source_map[citation]
        evidence_text = f"{source.title} {source.excerpt}"
        evidence_tokens = _tokens(evidence_text)
        if _POSITIVE_COMPLETION_RE.search(claim) and _NEGATION_RE.search(evidence_text):
            return GroundingClaim(claim, citations, 0.0, False, "引用证据与结论存在否定冲突")
        best = max(best, len(claim_tokens & evidence_tokens) / len(claim_tokens))
    score = round(best, 4)
    return GroundingClaim(
        claim, citations, score, score >= min_score,
        "引用内容支持" if score >= min_score else "引用与结论词法重合不足",
    )


def evaluate_answer_grounding(
    answer: str,
    sources: Sequence[AssistantSource],
    *,
    min_score: float = 0.42,
) -> GroundingReport:
    """逐句检查回答；无来源的一般回答返回空报告而不是伪造通过。"""

    claims = tuple(
        claim_support(sentence, sources, min_score=min_score)
        for sentence in _claim_sentences(answer)
    )
    cited = sum(bool(item.citations) for item in claims)
    supported = sum(item.supported for item in claims)
    return GroundingReport(
        claims=claims,
        supported_claims=supported,
        cited_claims=cited,
        support_rate=round(supported / len(claims), 4) if claims else 0.0,
    )
