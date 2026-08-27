#!/usr/bin/env python3
"""安装 git pre-commit 钩子：提交前自动跑安全校验（发现危险项阻止提交）

用法：python scripts/install_hooks.py
"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = ROOT / ".git" / "hooks"
HOOK = HOOKS_DIR / "pre-commit"

HOOK_CONTENT = """#!/bin/sh
# PersonaX 提交前安全校验：扫描密钥/敏感文件，发现危险项阻止提交
python "$(dirname "$0")/../../scripts/check_secrets.py" --strict || {
    echo ""
    echo "❌ 安全校验未通过：存在会被提交的密钥/敏感文件，已阻止提交。"
    echo "   处理方式：把文件加入 .gitignore 或删除后重试。"
    exit 1
}
"""


def main() -> int:
    if not (ROOT / ".git").exists():
        print("⚠️  当前目录不是 git 仓库，先执行: git init")
        return 1
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    HOOK.write_text(HOOK_CONTENT, encoding="utf-8")
    try:
        # Windows 下 git 钩子需要可执行（git 在 Windows 用 sh 运行，通常无需 chmod）
        subprocess.run(["git", "config", "core.hooksPath", ".git/hooks"],
                       cwd=str(ROOT), check=True)
    except Exception:  # noqa: BLE001
        pass
    print(f"✅ 已安装提交前安全校验钩子: {HOOK}")
    print("   之后每次 git commit 都会自动扫描密钥（危险项直接阻止提交）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
