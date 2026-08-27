#!/usr/bin/env python3
"""PersonaX 评估：对 eval_set 逐条打分，输出 CSV"""
from __future__ import annotations
import csv
import json
import yaml
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import skills  # noqa: F401  触发 @register
from core.orchestrator import Orchestrator
from core.harness import Harness, RuleConfig, AuditLog


def score(draft_text: str, case: dict) -> dict:
    traits = case.get("expected_traits", [])
    forbidden = case.get("forbidden", [])
    trait_hit = sum(1 for t in traits if _trait_in(draft_text, t))
    trait_score = trait_hit / len(traits) if traits else 1.0
    emoji_score = min(draft_text.count("🍃") + draft_text.count("!") + draft_text.count("～"), 5) / 5
    len_score = 1.0 if all(len(s) <= 40 for s in draft_text.split("。")) else 0.5
    forbidden_hit = [w for w in forbidden if w in draft_text]
    return {
        "trait_score": round(trait_score, 3),
        "emoji_score": round(emoji_score, 3),
        "len_score": round(len_score, 3),
        "forbidden_hit": forbidden_hit,
        "total": round((trait_score + emoji_score + len_score) / 3, 3),
    }


def _trait_in(text: str, trait: str) -> bool:
    mapping = {
        "口语化": lambda t: any(w in t for w in ["谁懂", "绝了", "冲鸭", "家人们", "真的会谢"]),
        "emoji": lambda t: any(c in t for c in ["🍃", "！", "～", "💡"]),
        "短句": lambda t: all(len(s) <= 40 for s in t.split("。")),
        "互动引导": lambda t: any(w in t for w in ["你觉得", "评论区", "你们", "分享"]),
    }
    fn = mapping.get(trait)
    return fn(text) if fn else trait in text


def main():
    persona = yaml.safe_load(open("config/persona.yaml", encoding="utf-8"))
    cases = json.load(open("eval/eval_set.json", encoding="utf-8"))
    audit = AuditLog()
    harness = Harness(RuleConfig(**persona.get("harness", {})), audit=audit)
    orch = Orchestrator(persona=persona, harness=harness)

    rows = []
    for case in cases:
        draft = orch.run(topic=case["topic"])
        text = f"{draft.title}\n{draft.body}"
        sc = score(text, case)
        rows.append({"topic": case["topic"], **sc})

    with open("eval_results.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    avg = sum(r["total"] for r in rows) / len(rows)
    print(f"评估完成，{len(rows)} 条，平均分: {avg:.3f}")
    for r in rows:
        print(f"  {r['topic']}: {r['total']} (forbidden={r['forbidden_hit']})")


if __name__ == "__main__":
    main()
