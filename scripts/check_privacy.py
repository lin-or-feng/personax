"""Check publishable files for personal data without echoing matched values.

Uses Git's tracked/untracked file inventory. Ignored local files are not opened.
--history additionally scans reachable text blobs, without rewriting history.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_secrets import SECRET_PATTERNS
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".py", ".ps1", ".cmd",
    ".toml", ".html", ".svg", ".mmd", ".mjs", ".js", ".css", ".ini", ".example"}
PHONE = re.compile(r"(?<![\dA-Za-z])1[3-9]\d{9}(?![\dA-Za-z])")
EMAIL = re.compile(r"(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
PROFILE = re.compile(r"(?i)\b[A-Z]:[\\/]+Users[\\/]+(?:Administrator|ADMINI~1|[A-Za-z0-9_.-]+)")
WORKSPACE = re.compile(r"(?i)\b[A-Z]:[\\/]+(?:deepseekworkspace|agents)[\\/]")
CONVERSATION = re.compile(r"按用户(?:要求|选择)|按你的|接下来我|我会先|面试(?:表达|话术)|简历一句话|供自己复盘与面试")
TEST_PHONES = {"13800138000", "13900000000", "13812345678"}
EXAMPLE_DOMAINS = {"example.com", "example.org", "example.net", "test.invalid"}
PRIVATE_NAMES = {"storage_state.json", "publish_log.json", "credentials.json", ".env", "secrets.toml"}


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-c", "core.quotepath=false", *args], cwd=root)


def findings(text: str, path: str) -> list[dict]:
    """Locations/categories only; safe to include the returned records in logs."""
    result = []
    upstream = path.startswith(("examples/python_official/", "knowledge/public/"))
    for number, line in enumerate(text.splitlines(), 1):
        kinds = []
        if any(pattern.search(line) for pattern in SECRET_PATTERNS):
            kinds.append(("credential", "error"))
        if any(match.group() not in TEST_PHONES for match in PHONE.finditer(line)):
            kinds.append(("phone", "info" if upstream else "error"))
        for match in EMAIL.finditer(line):
            domain = match.group().rsplit("@", 1)[-1].lower()
            if domain not in EXAMPLE_DOMAINS and not domain.endswith((".example", ".invalid", ".test")):
                kinds.append(("email", "info" if upstream else "error"))
                break
        if PROFILE.search(line):
            kinds.append(("local_profile", "info" if upstream else "error"))
        if path.endswith(".md"):
            if WORKSPACE.search(line):
                kinds.append(("local_workspace", "warning"))
            if CONVERSATION.search(line):
                kinds.append(("conversation_residue", "warning"))
        result.extend({"path": path, "line": number, "kind": kind, "severity": severity}
            for kind, severity in kinds)
    return result


def audit_tree(root: Path) -> dict:
    paths = set(git(root, "ls-files", "-co", "--exclude-standard", "-z").decode("utf-8").split("\0")) - {""}
    result, scanned, binary = [], 0, 0
    for name in sorted(paths):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            result.append({"path": name, "line": 0, "kind": "external_or_symlink", "severity": "error"})
            continue
        if not path.is_file():
            continue  # staged deletion / deleted working-tree file
        private = (path.name in PRIVATE_NAMES or path.suffix.lower() in {".key", ".pem", ".p12"}
            or path.name.startswith("storage_state")
            or (path.name.startswith(".env.") and path.name != ".env.example"))
        if private or name.startswith("artifacts/") or (name.startswith("tools/") and "resume" in name.lower()):
            safe_name = "artifacts/[private-file]" if name.startswith("artifacts/") else (
                "tools/[resume-script]" if name.startswith("tools/") else name)
            result.append({"path": safe_name, "line": 0, "kind": "private_file", "severity": "error"})
            continue  # Never open credentials or personal documents.
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {".gitignore", ".dockerignore", "Dockerfile"}:
            binary += 1
            continue
        result.extend(findings(path.read_text(encoding="utf-8", errors="replace"), name))
        scanned += 1
    return {"scope": "git_working_tree_publishable_files", "text_files": scanned,
        "binary_files_not_ocr_scanned": binary, "findings": result}


def audit_history(root: Path) -> dict:
    objects = []
    for line in git(root, "rev-list", "--objects", "--all").decode("utf-8").splitlines():
        oid, _, name = line.partition(" ")
        if name and Path(name).suffix.lower() in TEXT_SUFFIXES:
            objects.append((oid, name))
    result, scanned = [], 0
    # Batch protocol keeps Git process creation independent of history length.
    with subprocess.Popen(["git", "cat-file", "--batch"], cwd=root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
        for oid, name in objects:
            process.stdin.write((oid + "\n").encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3:
                raise RuntimeError("Unexpected git object header")
            data = process.stdout.read(int(header[2]))
            process.stdout.read(1)
            if header[1] == b"blob":
                hits = findings(data.decode("utf-8", errors="replace"), name)
                # History prose warnings are useful for editing but are not privacy errors.
                result.extend(dict(hit, object_id=oid[:12]) for hit in hits if hit["severity"] == "error")
                scanned += 1
        process.stdin.close()
    return {"scope": "reachable_git_text_blobs_not_commit_metadata", "text_blobs": scanned, "findings": result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit_tree(ROOT)
    if args.history:
        report["history"] = audit_history(ROOT)
    all_hits = report["findings"] + report.get("history", {}).get("findings", [])
    errors = sum(item["severity"] == "error" for item in all_hits)
    report["current_errors"] = sum(item["severity"] == "error" for item in report["findings"])
    report["history_errors"] = sum(item["severity"] == "error" for item in report.get("history", {}).get("findings", []))
    report["errors"] = errors
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"findings", "history"}}, ensure_ascii=False))
    for hit in all_hits:
        if hit["severity"] != "info":
            scope = "history" if "object_id" in hit else "current"
            print(f"{hit['severity']} ({scope}): {hit['path']}:{hit['line']} [{hit['kind']}]")
    return 1 if args.strict and errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
