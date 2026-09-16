#!/usr/bin/env python3
"""批量扫描 content_bank JSON 稿件的广告法、医疗金融承诺与平台导流词。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.compliance import ComplianceEngine, load_compliance_config  # noqa: E402
from core.types import Draft  # noqa: E402


def check_files(bank: Path, config: Path) -> tuple[int, list[dict]]:
    engine = ComplianceEngine(load_compliance_config(config))
    results: list[dict] = []
    failures = 0
    for path in sorted(bank.glob("*.json")) if bank.exists() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            draft = Draft(
                topic=str(data.get("topic") or path.stem),
                title=data.get("title"),
                body=data.get("body"),
                tags=[str(tag) for tag in (data.get("tags") or [])],
            )
            report = engine.check_draft(draft)
            hits = [str(hit) for hit in report.hits]
            results.append({"file": path.name, "ok": report.ok, "hits": hits})
            failures += int(not report.ok)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            failures += 1
            results.append({
                "file": path.name,
                "ok": False,
                "hits": [f"稿件无法解析: {type(exc).__name__}"],
            })
    return failures, results


def main() -> int:
    parser = argparse.ArgumentParser(description="PersonaX 稿件合规扫描")
    parser.add_argument("--bank", default=str(ROOT / "content_bank"))
    parser.add_argument("--config", default=str(ROOT / "config" / "compliance.yaml"))
    args = parser.parse_args()
    failures, results = check_files(Path(args.bank), Path(args.config))
    print(json.dumps({
        "checked": len(results),
        "passed": len(results) - failures,
        "failed": failures,
        "results": results,
    }, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
