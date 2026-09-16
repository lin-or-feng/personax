"""Download a small, licensed Chinese Wikimedia knowledge pack for PersonaX.

The importer deliberately fetches a curated topic list instead of a multi-GB
Wikipedia dump. Each Markdown document keeps source, URL, retrieval date and
license metadata so RAG results remain attributable.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_URL = "https://zh.wikipedia.org/w/api.php"
SUMMARY_URL = "https://zh.wikipedia.org/api/rest_v1/page/summary/{}"
USER_AGENT = "PersonaXKnowledgeBot/1.0 (local educational RAG project)"
LICENSE = "CC BY-SA 4.0 / GFDL; see source page for details"

TOPIC_GROUPS: dict[str, list[str]] = {
    "career": [
        "职业", "招聘", "求职", "履历表", "面试", "实习",
        "职业生涯规划", "劳动合同",
    ],
    "campus": [
        "大学", "高等教育", "大学生", "学习", "学习方法", "时间管理",
        "全国硕士研究生招生考试", "学生社团",
    ],
    "wuhan_life": [
        "武汉市", "武汉科技大学", "武汉大学", "华中科技大学",
        "中国光谷", "租赁", "社会保险", "住房公积金",
    ],
    "agent_rag": [
        "人工智能", "机器学习", "深度学习", "自然语言处理", "信息检索",
        "BM25", "最近邻搜索", "大型语言模型", "检索增强生成", "提示工程",
    ],
    "content_marketing": ["社交媒体", "内容营销", "市场营销", "网络营销"],
}


def _request_json(url: str, timeout: int = 45, max_attempts: int = 4) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 502, 503, 504} or attempt + 1 >= max_attempts:
                raise
            retry_after = exc.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 3.0 * (2 ** attempt)
            time.sleep(min(max(delay, 1.0), 30.0))
    raise RuntimeError("Wikimedia request failed after retries")


def _clean_extract(text: str, max_chars: int) -> str:
    text = re.sub(r"\r\n?", "\n", text or "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # Bibliographies and link lists add retrieval noise but little reusable context.
    stop = re.search(
        r"(?m)^\s*(?:参见|参考资料|参考文献|外部链接|延伸阅读)\s*$",
        text,
    )
    if stop:
        text = text[:stop.start()].rstrip()
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars]
    boundary = max(clipped.rfind("\n"), clipped.rfind("。"))
    return clipped[:boundary + 1].rstrip() if boundary > max_chars // 2 else clipped.rstrip()


def fetch_page(title: str, max_chars: int = 4500) -> dict[str, Any] | None:
    params = urllib.parse.urlencode({
        "action": "query",
        "prop": "extracts|info",
        "explaintext": "1",
        "exsectionformat": "plain",
        "inprop": "url",
        "redirects": "1",
        "titles": title,
        "format": "json",
        "formatversion": "2",
        "origin": "*",
    })
    payload = _request_json(f"{API_URL}?{params}")
    pages = ((payload.get("query") or {}).get("pages") or [])
    if not pages or pages[0].get("missing"):
        return None
    page = pages[0]
    extract = _clean_extract(str(page.get("extract") or ""), max_chars)
    source_url = str(page.get("fullurl") or "")

    if len(extract) < 120:
        try:
            summary = _request_json(SUMMARY_URL.format(urllib.parse.quote(title, safe="")))
            extract = _clean_extract(str(summary.get("extract") or ""), max_chars)
            source_url = str(
                (((summary.get("content_urls") or {}).get("desktop") or {}).get("page"))
                or source_url
            )
        except Exception:  # noqa: BLE001 - the full extract may still be usable
            pass
    if len(extract) < 80:
        return None
    return {
        "title": str(page.get("title") or title),
        "page_id": page.get("pageid"),
        "source_url": source_url,
        "text": extract,
    }


def _safe_name(title: str) -> str:
    name = re.sub(r"[<>:\"/\\|?*]+", "_", title).strip(" .")
    return name[:80] or "untitled"


def _markdown(page: dict[str, Any], group: str, retrieved_at: str) -> str:
    metadata = {
        "topic": page["title"],
        "style": "neutral_reference",
        "keywords": [page["title"], group, "Wikipedia", "中文百科"],
        "retrieval_role": "reference",
        "source": "Wikipedia 中文",
        "source_url": page["source_url"],
        "license": LICENSE,
        "retrieved_at": retrieved_at,
        "page_id": page["page_id"],
        "modified": "Plain-text extraction and length truncation only",
    }
    frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
    attribution = (
        "\n\n---\n"
        f"来源：Wikipedia 中文《{page['title']}》（{page['source_url']}）\n\n"
        f"许可：{LICENSE}\n"
    )
    return f"---\n{frontmatter}\n---\n\n# {page['title']}\n\n{page['text']}{attribution}"


def inventory_pack(output_dir: Path) -> dict[str, Any]:
    """Rebuild a manifest from already downloaded Markdown files."""
    imported: list[dict[str, Any]] = []
    known_groups = set(TOPIC_GROUPS)
    for path in sorted(output_dir.glob("*.md")):
        raw = path.read_text(encoding="utf-8", errors="ignore")
        match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", raw, re.S)
        if not match:
            continue
        metadata = yaml.safe_load(match.group(1)) or {}
        if not isinstance(metadata, dict) or not metadata.get("source_url"):
            continue
        keywords = metadata.get("keywords") or []
        group = next((str(item) for item in keywords if str(item) in known_groups), "other")
        imported.append({
            "requested_title": str(metadata.get("topic") or path.stem),
            "title": str(metadata.get("topic") or path.stem),
            "group": group,
            "path": str(path),
            "source_url": str(metadata["source_url"]),
            "chars": len(raw[match.end():].strip()),
        })
    manifest = {
        "dataset": "PersonaX curated Wikipedia Chinese starter pack",
        "retrieved_at": date.today().isoformat(),
        "license": LICENSE,
        "source_api": API_URL,
        "imported": imported,
        "skipped": [],
    }
    (output_dir / "_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def import_pack(output_dir: Path, max_chars: int = 4500,
                delay_seconds: float = 1.2) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    retrieved_at = date.today().isoformat()
    manifest_path = output_dir / "_manifest.json"
    previous: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    imported_by_url = {
        str(item.get("source_url")): item
        for item in previous.get("imported", [])
        if item.get("source_url") and Path(str(item.get("path", ""))).exists()
    }
    skipped: list[dict[str, str]] = []
    for group, titles in TOPIC_GROUPS.items():
        for title in titles:
            try:
                page = fetch_page(title, max_chars=max_chars)
                if not page:
                    skipped.append({"title": title, "reason": "missing or too short"})
                    continue
                destination = output_dir / f"{_safe_name(page['title'])}.md"
                destination.write_text(_markdown(page, group, retrieved_at), encoding="utf-8")
                imported_by_url[page["source_url"]] = {
                    "requested_title": title,
                    "title": page["title"],
                    "group": group,
                    "path": str(destination),
                    "source_url": page["source_url"],
                    "chars": len(page["text"]),
                }
            except Exception as exc:  # noqa: BLE001 - keep the batch resumable
                skipped.append({"title": title, "reason": str(exc)[:240]})
            time.sleep(max(0.0, delay_seconds))

    manifest = {
        "dataset": "PersonaX curated Wikipedia Chinese starter pack",
        "retrieved_at": retrieved_at,
        "license": LICENSE,
        "source_api": API_URL,
        "imported": sorted(imported_by_url.values(), key=lambda item: (item["group"], item["title"])),
        "skipped": skipped,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "knowledge" / "public" / "wikipedia_zh",
    )
    parser.add_argument("--max-chars", type=int, default=4500)
    parser.add_argument("--delay", type=float, default=1.2)
    parser.add_argument("--embed", action="store_true", help="Build bge-m3 embeddings after import")
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Only rebuild the manifest and/or embeddings from existing files",
    )
    args = parser.parse_args()

    manifest = (inventory_pack(args.output) if args.skip_download else
                import_pack(args.output, max_chars=args.max_chars, delay_seconds=args.delay))
    print(f"Imported {len(manifest['imported'])} documents; skipped {len(manifest['skipped'])}.")
    print(f"Manifest: {args.output / '_manifest.json'}")

    if args.embed:
        sys.path.insert(0, str(PROJECT_ROOT))
        from core.envfile import load_env_file
        from core.rag import build_rag_from_dir

        load_env_file(PROJECT_ROOT / ".env")
        started = time.perf_counter()
        pipeline = build_rag_from_dir(PROJECT_ROOT / "knowledge")
        elapsed = time.perf_counter() - started
        embedder = pipeline.store.embedder
        print(
            f"Embedded {len(pipeline.store.chunks)} chunks with {embedder.name} "
            f"in {elapsed:.1f}s (cache hits={getattr(embedder, 'cache_hits', 0)}, "
            f"misses={getattr(embedder, 'cache_misses', 0)})."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
