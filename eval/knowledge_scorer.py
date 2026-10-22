"""Six authored fixture checks: engineering smoke tests, never a domain-quality score."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from core.knowledge import KnowledgeBaseService
from core.types import KnowledgeImportRequest, KnowledgeSearchRequest

ROOT = Path(__file__).resolve().parents[1]


def evaluate() -> dict:
    cases = json.loads((ROOT / "eval/knowledge_stage1_cases.json").read_text(encoding="utf-8"))
    results = []
    with tempfile.TemporaryDirectory(prefix="personax-knowledge-smoke-") as directory:
        service = KnowledgeBaseService(Path(directory) / "catalog.sqlite3", ROOT / "examples/ai_engineering")
        for file in ("rag.md", "context.html", "operations.txt"):
            imported = service.ingest(KnowledgeImportRequest(source_path=file, license="project-authored-demo"))
            if imported.status != "created":
                raise RuntimeError("fixture import failed")
        for case in cases:
            output = service.search(KnowledgeSearchRequest(query=case["query"]))
            actual = [source.source for source in output.sources]
            expected = case["expected_sources"]
            source_ok = bool(set(actual) & set(expected)) if expected else not actual
            provenance_ok = all(source.document_id and source.document_version and source.chunk_id
                and (source.page is not None or source.line_start is not None) for source in output.sources)
            results.append({"id": case["id"], "expected_sources": expected, "actual_sources": actual,
                "source_match": source_ok, "provenance_complete": provenance_ok,
                "passed": bool(source_ok and provenance_ok)})
    return {"scope": "authored_fixture_engineering_smoke_only", "mode": "extractive_baseline",
        "embedding_backend": "hashing", "independent_domain_evaluation": False,
        "cases": len(results), "passed_cases": sum(row["passed"] for row in results),
        "passed": all(row["passed"] for row in results), "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-json", default="")
    args = parser.parse_args()
    report = evaluate()
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report_json:
        path = Path(args.report_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
