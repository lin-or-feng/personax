"""批量稿件合规扫描脚本回归。"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.check_content_compliance import check_files


def test_batch_compliance_scan_reports_safe_and_blocked(tmp_path: Path):
    (tmp_path / "safe.json").write_text(json.dumps({
        "topic": "求职", "title": "秋招经验分享", "body": "记录我的准备方法。", "tags": ["#秋招"],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "blocked.json").write_text(json.dumps({
        "topic": "测试", "title": "最好的唯一选择", "body": "加微信领资料", "tags": [],
    }, ensure_ascii=False), encoding="utf-8")

    failed, rows = check_files(tmp_path, Path("config/compliance.yaml"))
    assert failed == 1
    by_name = {row["file"]: row for row in rows}
    assert by_name["safe.json"]["ok"] is True
    assert by_name["blocked.json"]["ok"] is False
