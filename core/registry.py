"""Skill 注册中心 + 多信号加权路由（对齐商用框架 Skill manifest 标准）"""
from __future__ import annotations
from typing import Any, Callable
from .types import SkillInput, SkillOutput


class Skill:
    """所有 Skill 的基类。子类实现 run()，通过 @register 自动注册。"""

    name: str = ""
    description: str = ""
    triggers: list[str] = []       # 关键词触发
    popularity: float = 0.5        # 0~1，热度
    version: str = "0.1.0"

    def run(self, inp: SkillInput) -> SkillOutput:
        raise NotImplementedError


_REGISTRY: dict[str, Skill] = {}


def register(skill_cls: type[Skill]) -> type[Skill]:
    """装饰器：注册 Skill 到全局中心"""
    inst = skill_cls()
    if not inst.name:
        inst.name = skill_cls.__name__
    _REGISTRY[inst.name] = inst
    return skill_cls


def all_skills() -> dict[str, Skill]:
    return dict(_REGISTRY)


def get(name: str) -> Skill | None:
    return _REGISTRY.get(name)


def route(query: str, top_k: int = 3, weights: dict | None = None) -> list[tuple[str, float]]:
    """多信号加权路由：语义(0.5) + 关键词(0.3) + 热度(0.2)
    返回 [(skill_name, score), ...] 降序。
    """
    w = weights or {"semantic": 0.5, "keyword": 0.3, "popularity": 0.2}
    results: list[tuple[str, float]] = []
    q = query.lower()
    for name, skill in _REGISTRY.items():
        # 关键词信号
        kw_score = sum(1 for t in skill.triggers if t.lower() in q) / max(len(skill.triggers), 1)
        # 热度信号
        pop_score = skill.popularity
        # 语义信号（简化：用 triggers 与 query 的 token 重叠近似）
        tokens = set(q.split()) | {t.lower() for t in skill.triggers}
        sem_score = len(set(q.split()) & {t.lower() for t in skill.triggers}) / max(len(tokens), 1)
        score = w.get("semantic", 0.5) * sem_score + w.get("keyword", 0.3) * kw_score + w.get("popularity", 0.2) * pop_score
        results.append((name, round(score, 4)))
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_k]
