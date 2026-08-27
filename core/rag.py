"""RAG 模块：四索引 + 四查询 + RRF 融合（可运行骨架）

四索引：原始 / 摘要再索引 / 假设性问题 / 元数据
四查询：Enrich / Multi-query / Decomposition / Rerank(RRF)
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Chunk:
    id: str
    text: str
    summary: str = ""
    hypothetical_questions: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)


def _chinese_tokens(text: str) -> set[str]:
    """中文友好的 token 化：汉字按二元组（bigram），英文/数字按词。

    中文没有空格分词，原实现按空格 split 会把整句当一个 token，相似度近似失效。
    """
    import re
    tokens: set[str] = set()
    # 英文/数字词
    for w in re.findall(r"[A-Za-z0-9]+", text):
        tokens.add(w.lower())
    # 中文字符 bigram
    han = re.findall(r"[\u4e00-\u9fff]", text)
    for i in range(len(han) - 1):
        tokens.add(han[i] + han[i + 1])
    return tokens


_FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML front-matter，返回 (meta, 剩余正文)。无 front-matter 则返回 ({}, 原文)。"""
    m = _FRONT_RE.match(text)
    if not m:
        return {}, text
    try:
        import yaml
        meta = yaml.safe_load(m.group(1)) or {}
    except Exception:  # noqa: BLE001 —— front-matter 损坏不改主流程
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, text[m.end():].strip()


class VectorStore:
    """简易内存向量库（演示用，生产可换 Milvus/Qdrant）"""

    def __init__(self):
        self.chunks: dict[str, Chunk] = {}

    def add(self, chunk: Chunk):
        self.chunks[chunk.id] = chunk

    def similarity(self, query: str, chunk: Chunk) -> float:
        """中文 bigram 主题加权相似度。

        主题/关键词（summary+metadata.keywords）权重 0.6、正文 0.4：
        让「主题相同」的知识条目更容易命中，避免只靠正文撞词。
        """
        q = _chinese_tokens(query)
        if not q:
            return 0.0
        theme = (chunk.summary or "") + " " + " ".join(
            str(k) for k in (chunk.metadata or {}).get("keywords", []))
        theme_tok = _chinese_tokens(theme)
        body_tok = _chinese_tokens(chunk.text)
        theme_score = (len(q & theme_tok) / len(q | theme_tok)) if theme_tok else 0.0
        body_score = (len(q & body_tok) / len(q | body_tok)) if body_tok else 0.0
        return round(0.6 * theme_score + 0.4 * body_score, 4)

    def search(self, query: str, top_k: int = 5, index: str = "raw") -> list[tuple[Chunk, float]]:
        scored = []
        for c in self.chunks.values():
            text = c.text
            if index == "summary":
                text = c.summary or c.text
            elif index == "hypo":
                text = " ".join(c.hypothetical_questions) or c.text
            scored.append((c, self.similarity(query, c)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


def reciprocal_rank_fusion(results_list: list[list[tuple[Chunk, float]]], k: int = 60) -> list[Chunk]:
    """RRF：多路召回结果融合"""
    scores: dict[str, float] = {}
    for results in results_list:
        for rank, (chunk, _) in enumerate(results):
            scores[chunk.id] = scores.get(chunk.id, 0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


class RAGPipeline:
    """四查询编排 + RRF 重排"""

    def __init__(self, store: VectorStore | None = None):
        self.store = store or VectorStore()

    def multi_query(self, query: str, n: int = 3) -> list[str]:
        """多路召回：生成 n 种问法（演示用模板，生产调 LLM）"""
        variants = [query]
        for i in range(1, n):
            variants.append(f"{query} 第{i}种问法")
        return variants

    def decompose(self, query: str) -> list[str]:
        """问题拆解（演示用，生产调 LLM）"""
        if "和" in query or "、" in query:
            return query.replace("、", " ").split()
        return [query]

    def retrieve(self, query: str, top_k: int = 5) -> list[Chunk]:
        """四查询 → RRF 融合"""
        queries = self.multi_query(query)
        sub_queries = self.decompose(query)
        all_queries = list(dict.fromkeys(queries + sub_queries))

        result_lists: list[list[tuple[Chunk, float]]] = []
        for q in all_queries:
            # 多索引召回
            result_lists.append(self.store.search(q, top_k, index="raw"))
            result_lists.append(self.store.search(q, top_k, index="summary"))

        fused = reciprocal_rank_fusion(result_lists)
        return [self.store.chunks[cid] for cid, _ in fused[:top_k]]

    def retrieve_relevant(self, query: str, top_k: int = 2,
                          min_score: float = 0.10) -> list[Chunk]:
        """召回并过滤不相关条目。

        只有主题/关键词足够贴近（score >= min_score）才返回，否则返回空——
        避免不相干知识被硬塞进正文生成，导致跑题。让知识库只做「锦上添花」。
        """
        scored = self.store.search(query, top_k=len(self.store.chunks), index="raw")
        return [c for c, s in scored[:top_k * 3] if s >= min_score][:top_k]


def build_rag_from_dir(dir_path: str | Path) -> RAGPipeline:
    """从目录加载知识库（*.md 一篇一文件）→ RAGPipeline。

    推荐格式（YAML front-matter + 正文范例）：
        ---
        topic: 秋招穿搭          # 主题（用于检索）
        style: 口语化/短句/互动   # 风格描述
        keywords: [面试, 穿搭, 衬衫]
        ---
        <一篇高质量笔记正文：结构/语气/互动方式>

    无 front-matter 时，取正文首行为主题兜底。
    """
    pipe = RAGPipeline()
    p = Path(dir_path)
    if not p.exists():
        return pipe
    for i, f in enumerate(sorted(p.glob("*.md"))):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if not text.strip():
            continue
        meta, body = _parse_frontmatter(text)
        topic = str(meta.get("topic") or "").strip()
        if not topic:
            topic = next((l.strip() for l in body.splitlines() if l.strip()), "")[:40]
        summary = topic
        kws = meta.get("keywords") or []
        if isinstance(kws, str):
            kws = [k.strip() for k in kws.split(",")]
        metadata = {
            "source": f.name,
            "topic": topic,
            "style": str(meta.get("style") or ""),
            "keywords": [str(k) for k in kws],
        }
        pipe.store.add(Chunk(
            id=f"kb-{i}", text=body or text, summary=summary,
            hypothetical_questions=[f"关于{topic}的笔记怎么写"] if topic else [],
            metadata=metadata,
        ))
    return pipe
