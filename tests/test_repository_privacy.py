"""Publishable-file checks must report locations without exposing personal data."""
from __future__ import annotations

import json
import subprocess
import re
from urllib.parse import unquote, urlsplit

from scripts.check_privacy import audit_tree, findings


def test_sensitive_values_are_not_returned():
    phone = "151" + "23456789"
    email = "private" + "@" + "mail.vendor"
    profile = "C:" + "\\Users\\" + "someone"
    credential = "sk-" + "x" * 24
    report = findings("\n".join((phone, email, profile, credential)), "docs/sample.md")
    assert {item["kind"] for item in report} == {"phone", "email", "local_profile", "credential"}
    output = json.dumps(report)
    assert all(value not in output for value in (phone, email, profile, credential))


def test_reserved_example_domains_and_test_fixtures_are_not_private():
    assert not findings("user@example.com password@example.test test@test.invalid 13800138000", "tests/example.py")


def test_upstream_credits_are_preserved_as_information():
    report = findings("author" + "@" + "python.org", "examples/python_official/license.html")
    assert len(report) == 1 and report[0]["severity"] == "info"


def test_private_candidates_are_blocked_without_reading_their_content(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("local/\n", encoding="utf-8")
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "private.txt").write_bytes(b"\xff\xfe")
    (tmp_path / "storage_state.json").write_bytes(b"\xff\xfe")
    result = audit_tree(tmp_path)
    assert result["findings"] == [{"path": "storage_state.json", "line": 0,
        "kind": "private_file", "severity": "error"}]


def test_private_assets_and_resume_scripts_are_ignored_by_repository():
    from scripts.check_privacy import ROOT
    paths = ["artifacts/resume.pdf", "tools/update_resume_agent_focus.py",
        "docs/JOB_APPLICATION_BRIEF.md", ".env.production", ".streamlit/secrets.toml"]
    for path in paths:
        result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
        assert result.returncode == 0, path


def test_secret_filename_globs_and_safe_template():
    from scripts.check_secrets import _is_sensitive_name
    for name in ("client.pem", "private.key", "export.p12", ".env.production", "storage_state.backup.json", "secrets.toml"):
        assert _is_sensitive_name(name)
    assert not _is_sensitive_name(".env.example")


def test_docker_excludes_private_local_materials():
    from scripts.check_privacy import ROOT
    rules = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert all(path in rules for path in ("artifacts", "tools", "docs/JOB_APPLICATION_BRIEF.md",
        "docs/上下文摘要.md", ".streamlit/secrets.toml", "storage_state*.json"))


def test_publishable_document_links_have_existing_local_targets():
    from scripts.check_privacy import ROOT, git
    candidates = git(ROOT, "ls-files", "-co", "--exclude-standard", "-z").decode("utf-8").split("\0")
    broken = []
    for name in set(candidates):
        path = ROOT / name
        if path.suffix != ".md" or not path.is_file() or name.startswith("knowledge/"):
            continue
        text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
        for target in re.findall(r"\[[^\]\n]+\]\(([^\s)]+)\)", text):
            parsed = urlsplit(target.strip("<>"))
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            if not (path.parent / unquote(parsed.path)).resolve().exists():
                broken.append((name, parsed.path))
    assert not broken, broken


def test_consolidated_diagnostics_keep_failed_runs():
    from scripts.check_privacy import ROOT
    report = json.loads((ROOT / "docs" / "RETRIEVAL_AGENT_SMOKE.json").read_text(encoding="utf-8"))
    assert len(report["runs"]) == 5
    assert sum(run["passed"] for run in report["runs"]) == 1
    assert sum(not run["passed"] for run in report["runs"]) == 4


def test_owner_contact_is_allowed_only_in_readme():
    address = "3154433950" + "@qq.com"
    assert not findings(address, "README.md")
    assert any(hit["kind"] == "email" for hit in findings(address, "docs/sample.md"))
    assert any(hit["kind"] == "email" for hit in findings("other" + "@qq.com", "README.md"))
