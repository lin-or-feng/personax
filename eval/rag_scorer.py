"""可复现 RAG 质量门禁与检索消融报告。

评测同时覆盖正例召回、相关性门控后的召回、无答案拒检、延迟和组件降级。
默认使用 hashing 后端保证 CI 离线可运行；本机语义质量应另用 Ollama bge-m3
复测。评测集属于工程回归集，不冒充真实用户分布或线上 SLA。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from core.envfile import load_env_file
from core.rag import RetrievalHit, build_rag_from_dir


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_MODES = ("bm25", "dense", "hybrid")
DEFAULT_MIN_SCORES = {
    "hashing": 0.15,
    "ollama": 0.50,
    "sentence-transformers": 0.50,
}


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return round(ordered[index], 1)


def _normalize_source(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip()


def _load_cases(eval_path: str | Path) -> list[dict[str, Any]]:
    path = Path(eval_path)
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("RAG 评测集必须是非空 JSON 数组")
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    expanded_rows: list[dict[str, Any]] = []
    for group_index, raw in enumerate(rows, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {group_index} 条评测样例必须是对象")
        queries = raw.get("queries")
        if queries is None:
            expanded_rows.append(raw)
            continue
        if not isinstance(queries, list) or not queries:
            raise ValueError(f"第 {group_index} 组 queries 必须是非空数组")
        shared = {key: value for key, value in raw.items() if key != "queries"}
        if "source" in shared and "expected_sources" not in shared:
            shared["expected_sources"] = [shared.pop("source")]
        group_id = str(shared.pop("id", f"group_{group_index:02d}"))
        for query_index, query_row in enumerate(queries, 1):
            if isinstance(query_row, str):
                query_row = {"query": query_row}
            if not isinstance(query_row, dict):
                raise ValueError(
                    f"{group_id}.queries 第 {query_index} 条必须是字符串或对象"
                )
            merged = {**shared, **query_row}
            merged.setdefault("id", f"{group_id}_{query_index:02d}")
            expanded_rows.append(merged)

    for index, raw in enumerate(expanded_rows, 1):
        query = str(raw.get("query") or "").strip()
        if not query:
            raise ValueError(f"第 {index} 条评测样例缺少 query")
        case_id = str(raw.get("id") or f"case_{index:03d}").strip()
        if case_id in seen_ids:
            raise ValueError(f"评测样例 id 重复：{case_id}")
        seen_ids.add(case_id)
        expected_raw = raw.get("expected_sources") or []
        if not isinstance(expected_raw, list):
            raise ValueError(f"{case_id}.expected_sources 必须是数组")
        expected = sorted({_normalize_source(value) for value in expected_raw if value})
        answerable = bool(raw.get("answerable", bool(expected)))
        if answerable and not expected:
            raise ValueError(f"{case_id} 是可回答样例，但 expected_sources 为空")
        cases.append({
            "id": case_id,
            "query": query,
            "expected_sources": expected,
            "answerable": answerable,
            "category": str(raw.get("category") or "uncategorized"),
            "difficulty": str(raw.get("difficulty") or "standard"),
        })
    if not any(case["answerable"] for case in cases):
        raise ValueError("RAG 评测集至少需要一个可回答正例")
    return cases


def _unique_source_hits(hits: Iterable[RetrievalHit]) -> list[tuple[str, RetrievalHit]]:
    """按来源去重，避免同一长文的多个 chunk 挤占 Recall@K。"""

    rows: list[tuple[str, RetrievalHit]] = []
    seen: set[str] = set()
    for hit in hits:
        source = _normalize_source(hit.chunk.metadata.get("source"))
        if not source or source in seen:
            continue
        seen.add(source)
        rows.append((source, hit))
    return rows


def _rank(sources: list[str], expected: set[str]) -> int | None:
    return next((idx for idx, source in enumerate(sources, 1) if source in expected), None)


def _ndcg(sources: list[str], expected: set[str], top_k: int) -> float:
    """二元相关性 NDCG@K；支持一个问题对应多个正确来源。"""

    if not expected or top_k < 1:
        return 0.0
    dcg = sum(
        1.0 / math.log2(index + 1)
        for index, source in enumerate(sources[:top_k], 1)
        if source in expected
    )
    ideal_count = min(len(expected), top_k)
    ideal = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_count + 1))
    return round(dcg / ideal, 4) if ideal else 0.0


def _metric_slice(rows: list[dict[str, Any]]) -> dict[str, Any]:
    positives = [row for row in rows if row["answerable"]]
    negatives = [row for row in rows if not row["answerable"]]
    positive_count = len(positives)
    negative_count = len(negatives)
    return {
        "cases": len(rows),
        "positive_cases": positive_count,
        "negative_cases": negative_count,
        "recall_at_k": round(
            sum(row["rank"] is not None for row in positives) / positive_count, 4
        ) if positive_count else None,
        "gated_recall_at_k": round(
            sum(row["gated_rank"] is not None for row in positives) / positive_count, 4
        ) if positive_count else None,
        "mrr": round(
            sum(1.0 / row["rank"] if row["rank"] else 0.0 for row in positives)
            / positive_count,
            4,
        ) if positive_count else None,
        "ndcg_at_k": round(
            sum(row["ndcg_at_k"] for row in positives) / positive_count, 4
        ) if positive_count else None,
        "gated_ndcg_at_k": round(
            sum(row["gated_ndcg_at_k"] for row in positives) / positive_count, 4
        ) if positive_count else None,
        "no_hit_accuracy": round(
            sum(row["no_hit_ok"] for row in negatives) / negative_count, 4
        ) if negative_count else None,
    }


def _breakdown(details: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in details:
        groups[str(row[field])].append(row)
    return {name: _metric_slice(rows) for name, rows in sorted(groups.items())}


def _threshold_sweep(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """复用一次检索的原始分数，展示门槛对召回与拒检的权衡。"""

    rows: list[dict[str, Any]] = []
    thresholds = (0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80)
    positives = [row for row in details if row["answerable"]]
    negatives = [row for row in details if not row["answerable"]]
    for threshold in thresholds:
        positive_hits = 0
        for row in positives:
            expected = set(row["expected"])
            kept = [
                item["source"] for item in row["retrieved_scores"]
                if item["relevance"] >= threshold
            ][:row["top_k"]]
            positive_hits += any(source in expected for source in kept)
        negative_rejections = sum(
            not any(
                item["relevance"] >= threshold
                for item in row["retrieved_scores"]
            )
            for row in negatives
        )
        rows.append({
            "min_score": threshold,
            "gated_recall_at_k": round(positive_hits / len(positives), 4)
            if positives else None,
            "no_hit_accuracy": round(negative_rejections / len(negatives), 4)
            if negatives else None,
        })
    return rows


def evaluate(
    eval_path: str = "eval/rag_eval_set.json",
    knowledge_dir: str = "knowledge",
    top_k: int = 5,
    *,
    embedding_backend: str | None = "hashing",
    retrieval_mode: str = "hybrid",
    min_score: float | None = None,
    min_recall: float = 0.80,
    min_mrr: float = 0.75,
    min_no_hit: float = 0.75,
    require_no_degradation: bool = True,
    reranker: str | None = None,
    reranker_model: str | None = None,
    parent_context: bool = True,
) -> dict[str, Any]:
    if top_k < 1:
        raise ValueError("top_k 必须大于 0")
    if retrieval_mode not in SUPPORTED_MODES:
        raise ValueError(f"retrieval_mode 必须是：{', '.join(SUPPORTED_MODES)}")
    if min_score is None:
        min_score = DEFAULT_MIN_SCORES.get(str(embedding_backend), 0.15)
    if not 0.0 <= min_score <= 1.0:
        raise ValueError("min_score 必须在 0 到 1 之间")

    load_env_file(PROJECT_ROOT / ".env")
    cases = _load_cases(eval_path)
    pipeline = build_rag_from_dir(
        knowledge_dir,
        embedding_backend=embedding_backend,
        retrieval_mode=retrieval_mode,
        reranker=reranker,
        reranker_model=reranker_model,
        parent_context=parent_context,
        environment_overrides=False,
    )
    latencies_ms: list[float] = []
    details: list[dict[str, Any]] = []
    degraded_components: set[str] = set()
    degradation_reasons: dict[str, set[str]] = defaultdict(set)

    # 多取候选后按来源去重，保证 top_k 表示来源数而不是 chunk 数。
    fetch_k = max(top_k * 6, 20)
    for case in cases:
        started = time.perf_counter()
        hits = pipeline.retrieve_hits(case["query"], top_k=fetch_k)
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        latencies_ms.append(latency_ms)
        trace = dict(pipeline.last_trace)
        degraded = list(trace.get("degraded_components", []))
        degraded_components.update(degraded)
        for component, reason in (trace.get("degradation_reasons", {}) or {}).items():
            degradation_reasons[str(component)].add(str(reason))

        unique_hits = _unique_source_hits(hits)
        ranked_sources = [source for source, _ in unique_hits[:top_k]]
        candidate_scores = [
            {
                "source": source,
                "relevance": round(max(hit.lexical_score, hit.dense_score), 4),
                "lexical": round(hit.lexical_score, 4),
                "dense": round(hit.dense_score, 4),
            }
            for source, hit in unique_hits
        ]
        gated_hits = [
            (source, hit)
            for source, hit in unique_hits
            if max(hit.lexical_score, hit.dense_score) >= min_score
        ]
        gated_sources = [source for source, _ in gated_hits[:top_k]]
        expected = set(case["expected_sources"])
        rank = _rank(ranked_sources, expected) if case["answerable"] else None
        gated_rank = _rank(gated_sources, expected) if case["answerable"] else None
        no_hit_ok = (not gated_sources) if not case["answerable"] else False
        details.append({
            "id": case["id"],
            "query": case["query"],
            "category": case["category"],
            "difficulty": case["difficulty"],
            "answerable": case["answerable"],
            "expected": sorted(expected),
            "retrieved": ranked_sources,
            "retrieved_scores": candidate_scores,
            "gated_retrieved": gated_sources,
            "rank": rank,
            "gated_rank": gated_rank,
            "ndcg_at_k": _ndcg(ranked_sources, expected, top_k),
            "gated_ndcg_at_k": _ndcg(gated_sources, expected, top_k),
            "no_hit_ok": no_hit_ok,
            "latency_ms": latency_ms,
            "top_k": top_k,
            "degraded_components": degraded,
        })

    metrics = _metric_slice(details)
    recall_at_k = float(metrics["recall_at_k"] or 0.0)
    gated_recall_at_k = float(metrics["gated_recall_at_k"] or 0.0)
    mrr = float(metrics["mrr"] or 0.0)
    no_hit_accuracy = metrics["no_hit_accuracy"]
    no_hit_passed = no_hit_accuracy is None or float(no_hit_accuracy) >= min_no_hit
    degradation_passed = not degraded_components or not require_no_degradation
    passed = (
        recall_at_k >= min_recall
        and gated_recall_at_k >= min_recall
        and mrr >= min_mrr
        and no_hit_passed
        and degradation_passed
    )
    last_trace = dict(pipeline.last_trace)
    trace_summary = {
        "retrieval_mode": pipeline.retrieval_mode,
        "embedding_backend": pipeline.store.embedder.name,
        "dense_mode": last_trace.get("dense_mode", "unknown"),
        "fusion": last_trace.get("fusion", "rrf"),
        "reranker": last_trace.get("reranker", "none"),
        "chunk_strategy": getattr(pipeline, "chunk_strategy", "unknown"),
        "index_version": getattr(pipeline, "index_version", ""),
        "embedding_cache_hits": getattr(pipeline.store.embedder, "cache_hits", 0),
        "embedding_cache_misses": getattr(pipeline.store.embedder, "cache_misses", 0),
        "degraded_components": sorted(degraded_components),
        "degradation_reasons": {
            component: sorted(reasons)
            for component, reasons in sorted(degradation_reasons.items())
        },
    }
    return {
        "schema_version": "2.7",
        "dataset": str(eval_path),
        "cases": len(cases),
        "positive_cases": metrics["positive_cases"],
        "negative_cases": metrics["negative_cases"],
        "top_k": top_k,
        "min_score": min_score,
        "retrieval_mode": pipeline.retrieval_mode,
        "embedding_backend": pipeline.store.embedder.name,
        "recall_at_k": recall_at_k,
        "gated_recall_at_k": gated_recall_at_k,
        "mrr": mrr,
        "ndcg_at_k": metrics["ndcg_at_k"],
        "gated_ndcg_at_k": metrics["gated_ndcg_at_k"],
        "no_hit_accuracy": no_hit_accuracy,
        "latency_ms": {
            "p50": _percentile(latencies_ms, 0.50),
            "p95": _percentile(latencies_ms, 0.95),
            "p99": _percentile(latencies_ms, 0.99),
            "max": round(max(latencies_ms), 1) if latencies_ms else 0.0,
        },
        "thresholds": {
            "min_recall": min_recall,
            "min_mrr": min_mrr,
            "min_no_hit": min_no_hit,
            "require_no_degradation": require_no_degradation,
        },
        "breakdown": {
            "category": _breakdown(details, "category"),
            "difficulty": _breakdown(details, "difficulty"),
        },
        "threshold_sweep": _threshold_sweep(details),
        "passed": passed,
        "trace": trace_summary,
        "details": details,
    }


def compare_modes(**kwargs: Any) -> dict[str, Any]:
    """用相同数据和 embedding 对 BM25、dense、hybrid 做公平消融。"""

    reports = {
        mode: evaluate(retrieval_mode=mode, **kwargs)
        for mode in SUPPORTED_MODES
    }
    passing_modes = [mode for mode, report in reports.items() if report["passed"]]
    candidates = passing_modes or list(reports)
    winner = max(
        candidates,
        key=lambda mode: (
            reports[mode]["gated_recall_at_k"],
            reports[mode]["gated_ndcg_at_k"],
            reports[mode]["mrr"],
            reports[mode]["no_hit_accuracy"] or 0.0,
            -reports[mode]["latency_ms"]["p95"],
        ),
    )
    return {
        "schema_version": "2.7",
        "kind": "retrieval_ablation",
        "winner": winner,
        "passed": reports["hybrid"]["passed"],
        "reports": reports,
    }


def compare_rerankers(**kwargs: Any) -> dict[str, Any]:
    """固定召回链路，对比 RRF 基线与 Cross-Encoder 重排。"""

    reports = {
        "rrf": evaluate(reranker="none", **kwargs),
        "cross_encoder": evaluate(reranker="cross-encoder", **kwargs),
    }
    winner = max(
        reports,
        key=lambda name: (
            reports[name]["gated_recall_at_k"],
            reports[name]["gated_ndcg_at_k"],
            reports[name]["mrr"],
            reports[name]["no_hit_accuracy"] or 0.0,
            -reports[name]["latency_ms"]["p95"],
        ),
    )
    cross_trace = reports["cross_encoder"]["trace"]
    available = "reranker" not in cross_trace["degraded_components"]
    return {
        "schema_version": "2.7",
        "kind": "reranker_ablation",
        "winner": winner,
        "cross_encoder_available": available,
        "passed": reports["rrf"]["passed"] and reports["cross_encoder"]["passed"],
        "reports": reports,
    }


def render_markdown(report: dict[str, Any]) -> str:
    if report.get("kind") == "reranker_ablation":
        lines = [
            "# PersonaX Cross-Encoder 消融报告",
            "",
            "| 重排方式 | 门控 Recall@K | 门控 NDCG@K | MRR | 无答案拒检 | P95(ms) | 门禁 |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
        for name in ("rrf", "cross_encoder"):
            row = report["reports"][name]
            lines.append(
                f"| {name} | {row['gated_recall_at_k']:.4f} | "
                f"{row['gated_ndcg_at_k']:.4f} | {row['mrr']:.4f} | "
                f"{row['no_hit_accuracy'] if row['no_hit_accuracy'] is not None else 'n/a'} | "
                f"{row['latency_ms']['p95']:.1f} | {'通过' if row['passed'] else '未通过'} |"
            )
        lines.extend([
            "",
            f"综合最优：**{report['winner']}**；Cross-Encoder "
            f"{'可用' if report['cross_encoder_available'] else '已降级'}。",
            "",
        ])
        return "\n".join(lines)
    if report.get("kind") == "retrieval_ablation":
        lines = [
            "# PersonaX RAG 消融报告",
            "",
            "| 模式 | Recall@K | 门控 Recall@K | 门控 NDCG@K | MRR | 无答案拒检 | P95(ms) | 门禁 |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        for mode in SUPPORTED_MODES:
            row = report["reports"][mode]
            no_hit = row["no_hit_accuracy"]
            lines.append(
                f"| {mode} | {row['recall_at_k']:.4f} | "
                f"{row['gated_recall_at_k']:.4f} | {row['gated_ndcg_at_k']:.4f} | "
                f"{row['mrr']:.4f} | "
                f"{no_hit if no_hit is not None else 'n/a'} | "
                f"{row['latency_ms']['p95']:.1f} | "
                f"{'通过' if row['passed'] else '未通过'} |"
            )
        lines.extend(["", f"综合最优模式：**{report['winner']}**。", ""])
        return "\n".join(lines)

    no_hit = report["no_hit_accuracy"]
    lines = [
        "# PersonaX RAG 评测报告",
        "",
        f"- 样例：{report['cases']}（正例 {report['positive_cases']} / 负例 {report['negative_cases']}）",
        f"- 检索：{report['retrieval_mode']} + {report['embedding_backend']}，Top-K={report['top_k']}",
        f"- Recall@K：{report['recall_at_k']:.4f}",
        f"- 门控 Recall@K：{report['gated_recall_at_k']:.4f}",
        f"- NDCG@K / 门控 NDCG@K：{report['ndcg_at_k']:.4f} / "
        f"{report['gated_ndcg_at_k']:.4f}",
        f"- MRR：{report['mrr']:.4f}",
        f"- 无答案拒检准确率：{no_hit if no_hit is not None else 'n/a'}",
        f"- P50 / P95 / P99：{report['latency_ms']['p50']:.1f} / "
        f"{report['latency_ms']['p95']:.1f} / {report['latency_ms']['p99']:.1f} ms",
        f"- 降级组件：{', '.join(report['trace']['degraded_components']) or '无'}",
        f"- 门禁：{'通过' if report['passed'] else '未通过'}",
        "",
        "## 分类切片",
        "",
        "| 分类 | 样例 | Recall@K | 门控 Recall@K | MRR | 无答案拒检 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for category, row in report["breakdown"]["category"].items():
        lines.append(
            f"| {category} | {row['cases']} | {row['recall_at_k'] if row['recall_at_k'] is not None else 'n/a'} | "
            f"{row['gated_recall_at_k'] if row['gated_recall_at_k'] is not None else 'n/a'} | "
            f"{row['mrr'] if row['mrr'] is not None else 'n/a'} | "
            f"{row['no_hit_accuracy'] if row['no_hit_accuracy'] is not None else 'n/a'} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict[str, Any], *, json_path: str = "", markdown_path: str = "") -> None:
    for raw_path, content in (
        (json_path, json.dumps(report, ensure_ascii=False, indent=2)),
        (markdown_path, render_markdown(report)),
    ):
        if not raw_path:
            continue
        path = Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + "\n", encoding="utf-8")


def compact_report(report: dict[str, Any]) -> dict[str, Any]:
    """移除逐条明细，避免 CI 日志被 120 条样例淹没。"""

    if report.get("kind") in {"retrieval_ablation", "reranker_ablation"}:
        compact = dict(report)
        compact["reports"] = {
            mode: {key: value for key, value in child.items() if key != "details"}
            for mode, child in report["reports"].items()
        }
        return compact
    return {key: value for key, value in report.items() if key != "details"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PersonaX RAG retrieval evaluation")
    parser.add_argument("--eval", default="eval/rag_eval_set.json")
    parser.add_argument("--knowledge", default="knowledge")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--backend",
        choices=["hashing", "ollama", "sentence-transformers"],
        default="hashing",
        help="hashing 可复现且无需模型；ollama 可实测本地 bge-m3",
    )
    parser.add_argument("--mode", choices=SUPPORTED_MODES, default="hybrid")
    parser.add_argument("--compare", action="store_true", help="依次比较 BM25、dense、hybrid")
    parser.add_argument(
        "--compare-reranker", action="store_true",
        help="固定当前召回模式，对比 RRF 与 Cross-Encoder（需要 sentence-transformers）",
    )
    parser.add_argument("--reranker-model", default="BAAI/bge-reranker-base")
    parser.add_argument(
        "--flat-chunks", action="store_true",
        help="关闭 2.5 标题感知父子 Chunk，用旧版固定切块做对照",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="默认 hashing=0.15，Ollama/sentence-transformers=0.50",
    )
    parser.add_argument("--min-recall", type=float, default=0.80)
    parser.add_argument("--min-mrr", type=float, default=0.75)
    parser.add_argument("--min-no-hit", type=float, default=0.75)
    parser.add_argument("--allow-degraded", action="store_true")
    parser.add_argument("--report-json", default="")
    parser.add_argument("--report-md", default="")
    parser.add_argument("--summary-only", action="store_true")
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args()
    kwargs = {
        "eval_path": args.eval,
        "knowledge_dir": args.knowledge,
        "top_k": args.top_k,
        "embedding_backend": args.backend,
        "min_score": args.min_score,
        "min_recall": args.min_recall,
        "min_mrr": args.min_mrr,
        "min_no_hit": args.min_no_hit,
        "require_no_degradation": not args.allow_degraded,
        "parent_context": not args.flat_chunks,
    }
    if args.compare and args.compare_reranker:
        raise SystemExit("--compare 与 --compare-reranker 不能同时使用")
    if args.compare:
        report = compare_modes(**kwargs)
    elif args.compare_reranker:
        report = compare_rerankers(
            retrieval_mode=args.mode,
            reranker_model=args.reranker_model,
            **kwargs,
        )
    else:
        report = evaluate(retrieval_mode=args.mode, **kwargs)
    write_report(report, json_path=args.report_json, markdown_path=args.report_md)
    printable = compact_report(report) if args.summary_only else report
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
