"""多人格库：创建多种人格，按所选人格生成内容

- config/personas.yaml 存放人格库（名字/语气/习惯/禁用词/句长/可选生成参数）
- config/persona.yaml 存放系统配置（harness/rag/生成默认/路由）+ 兜底人格
- resolve_persona(name)：把「人格库里的某个人格」叠加到系统配置上，得到完整 persona dict
- name 为空 → 直接用 persona.yaml（保持旧行为）

示例：
    from core.persona import list_personas, resolve_persona
    print(list_personas())                 # ['小鹿学姐', '干货知识风', ...]
    persona = resolve_persona("干货知识风")  # 用该人格生成
    Orchestrator(persona=persona).run("考研英语")
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Optional

import yaml

# 人格身份字段：这些由人格库决定；其余（harness/rag/路由）来自系统配置
_IDENTITY_KEYS = ("name", "description", "tone", "habits", "forbidden", "sentence_length")

# 默认路径基于项目根目录（core/ 的上一级），与 cwd 无关
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
PERSONAS_PATH = str(_PROJECT_ROOT / "config" / "personas.yaml")
BASE_PERSONA_PATH = str(_PROJECT_ROOT / "config" / "persona.yaml")


def _load_yaml(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return {}


def list_personas(path: str | Path = PERSONAS_PATH) -> list[str]:
    """人格库里的所有人格名"""
    data = _load_yaml(path)
    return [k for k in data.keys()] if isinstance(data, dict) else []


def get_persona(name: str, path: str | Path = PERSONAS_PATH) -> dict:
    """取单个名人人格（无则空 dict）"""
    data = _load_yaml(path)
    if isinstance(data, dict):
        p = data.get(name)
        if isinstance(p, dict):
            return p
    return {}


def resolve_persona(
    name: Optional[str] = None,
    base_path: str | Path = BASE_PERSONA_PATH,
    personas_path: str | Path = PERSONAS_PATH,
) -> dict:
    """得到实际用于生成的完整 persona dict。

    逻辑：先读系统配置 persona.yaml（含 harness/rag/生成默认/路由），
    再把所选人格库里的人格身份字段叠加上去；name 为空则原样返回 persona.yaml。
    """
    base = _load_yaml(base_path)
    if not name:
        return base
    persona = get_persona(name, personas_path)
    if not persona:
        return base          # 库中无此人设 → 用默认，避免报错
    merged = dict(base)
    for k in _IDENTITY_KEYS:
        if k in persona:
            merged[k] = persona[k]
    # 生成参数允许人设覆盖（深合并）
    if isinstance(persona.get("generation"), dict):
        merged["generation"] = {**(base.get("generation") or {}), **persona["generation"]}
    return merged


def add_persona(
    name: str,
    persona: dict,
    path: str | Path = PERSONAS_PATH,
) -> None:
    """新增/覆盖一个人格到库中"""
    data = _load_yaml(path)
    if not isinstance(data, dict):
        data = {}
    data[name] = persona
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
