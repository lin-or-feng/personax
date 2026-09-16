#!/usr/bin/env python3
"""PersonaX 本地发布门禁：任何一步失败都阻止版本提交。"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    checks = [
        ("完整单元测试", [sys.executable, "-m", "pytest"]),
        ("内容库合规", [sys.executable, "scripts/check_content_compliance.py"]),
        ("密钥与敏感文件", [sys.executable, "scripts/check_secrets.py", "--strict"]),
        ("AI 助手离线回归", [sys.executable, "-m", "eval.assistant_scorer"]),
        (
            "RAG 质量门禁",
            [
                sys.executable,
                "-m",
                "eval.rag_scorer",
                "--top-k",
                "1",
                "--backend",
                "hashing",
                "--min-recall",
                "0.80",
                "--min-mrr",
                "0.80",
            ],
        ),
    ]
    started = time.perf_counter()
    for label, command in checks:
        print(f"\n[检查] {label}", flush=True)
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode != 0:
            print(f"\n[失败] {label}，已停止后续检查。", flush=True)
            return result.returncode
        print(f"[通过] {label}", flush=True)
    elapsed = time.perf_counter() - started
    print(f"\n[通过] PersonaX 发布门禁全部通过（{elapsed:.2f}s）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
