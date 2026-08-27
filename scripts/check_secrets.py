#!/usr/bin/env python3
"""上传 GitHub 前安全校验：扫描密钥/敏感文件

用法：
    python scripts/check_secrets.py            # 扫描当前项目
    python scripts/check_secrets.py --strict   # 发现任何 🔴 即退出码 1（用于 CI/提交前钩子）

规则：
    🔴 RED   —— 会被提交的危险项（密钥明文 / 敏感文件未忽略）→ 必须处理
    🟡 YELLOW —— 敏感但已被 .gitignore 忽略（如 .env / storage_state.json）→ 本地保留安全
    ✅ 通过   —— 干净

内置忽略规则与项目 .gitignore 保持一致；在 git 仓库内运行时会叠加
`git check-ignore` 的真实判定。
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
from pathlib import Path

# Windows 控制台强制 UTF-8（脚本含 emoji 输出）
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent

# 密钥特征（只匹配「真实形态」的 Key，占位符如 sk-你的key 不会误报）
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),                   # DeepSeek/OpenAI 真实 key
    re.compile(r"DEEPSEEK_API_KEY\s*=\s*sk-[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),                      # AWS
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),               # Google
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]

# 敏感文件名（无论内容，只要会被提交就报警）
SENSITIVE_FILES = {
    "storage_state.json", "storage_state", ".env", ".env.local", ".env.prod",
    "publish_log.json", "eval_results.csv", "*.key", "*.pem", "*.p12", "id_rsa", "id_ed25519",
    "credentials.json", "config.json",
}

# 敏感目录（整目录忽略）
SENSITIVE_DIRS = {"logs", ".tmp", ".venv", "venv", "__pycache__", ".git", ".idea", ".pytest_cache"}

# 内置忽略前缀（与 .gitignore 保持一致）
IGNORED_DIR_PREFIX = ("pxtest_", "probe_", "px2_", "pytest-cache-files-")


def _load_gitignore() -> list[str]:
    """读取项目 .gitignore 规则（项目未 git init 时也能生效）"""
    gi = ROOT / ".gitignore"
    if not gi.exists():
        return []
    pats = []
    for line in gi.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        pats.append(line)
    return pats


_GITIGNORE_PATTERNS = _load_gitignore()


def _git_check_ignore(path: Path) -> bool:
    """git 仓库内用真实 ignore 判定"""
    try:
        r = subprocess.run(["git", "check-ignore", "-q", str(path)],
                           cwd=str(ROOT), capture_output=True)
        return r.returncode == 0
    except Exception:
        return False


def _ignored(path: Path, rel: str) -> bool:
    import fnmatch
    if any(rel.startswith(d) for d in SENSITIVE_DIRS):
        return True
    for p in path.parents:
        if any(p.name.startswith(pre) for pre in IGNORED_DIR_PREFIX):
            return True
    # .gitignore 规则
    for pat in _GITIGNORE_PATTERNS:
        p = pat.rstrip("/")
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(path.name, p):
            return True
        if "/" not in p:
            # 无斜杠模式匹配任意层级（目录名/文件名）
            for part in rel.split("/"):
                if fnmatch.fnmatch(part, p):
                    return True
    if _git_check_ignore(path):
        return True
    return False


def _is_sensitive_name(rel: str) -> bool:
    name = rel.split("/")[-1]
    for pat in SENSITIVE_FILES:
        if pat.endswith("*"):
            if name.endswith(pat[1:]):
                return True
        elif name == pat:
            return True
    return False


def main() -> int:
    red: list[str] = []
    yellow: list[str] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # 剪枝
        dirnames[:] = [d for d in dirnames
                       if d not in SENSITIVE_DIRS
                       and not any(d.startswith(p) for p in IGNORED_DIR_PREFIX)]
        for fn in filenames:
            fp = Path(dirpath) / fn
            rel = fp.relative_to(ROOT).as_posix()
            if rel.startswith(".git/"):
                continue
            scanned += 1
            ignored = _ignored(fp, rel)
            name_hit = _is_sensitive_name(rel)
            secret_hit = False
            if not ignored and name_hit:
                red.append(f"{rel}  ← 敏感文件名且未被忽略")
            try:
                data = fp.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for pat in SECRET_PATTERNS:
                m = pat.search(data)
                if m:
                    # 打码展示
                    val = m.group(0)
                    masked = val[:7] + "*" * min(10, max(0, len(val) - 7))
                    if ignored:
                        yellow.append(f"{rel}  ← 含疑似密钥(已忽略): {masked}")
                    else:
                        red.append(f"{rel}:{data[:m.start()].count(chr(10)) + 1}  ← 含疑似密钥: {masked}")
                    secret_hit = True
                    break
            if ignored and (name_hit or secret_hit):
                yellow.append(f"{rel}  ← 敏感但已忽略(本地保留安全)")

    print("=" * 62)
    print(f"扫描完成：{scanned} 个文件")
    print("=" * 62)
    if red:
        print(f"\n🔴 危险项 {len(red)} 个（会被提交，必须处理）：")
        for item in sorted(set(red)):
            print(f"  - {item}")
        print("\n处理方式：加入 .gitignore，或删除该文件后再提交。")
    if yellow:
        print(f"\n🟡 敏感但已忽略 {len(yellow)} 个（本地安全，不提交）：")
        for item in sorted(set(yellow)):
            print(f"  - {item}")
    if not red:
        print("\n✅ 未发现会被提交的密钥/敏感文件")
    print()
    return 1 if (red and "--strict" in sys.argv) else 0


if __name__ == "__main__":
    sys.exit(main())
