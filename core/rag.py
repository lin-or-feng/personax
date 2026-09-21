"""PersonaX 混合检索 RAG。

检索链路：BM25 稀疏召回 + dense 向量精确 kNN
→ RRF 融合 → 可选 cross-encoder 重排 → 相关性门控。

默认使用确定性的 HashingEmbedder，保证离线测试可运行；配置 Ollama 的
embedding 模型后才是正式的语义向量检索。查询增强默认使用规则实现，也可通过
统一的 core.llm.complete() 启用 LLM query rewriting / HyDE。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import urllib.request
from array import array
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence


@dataclass
class Chunk:
    id: str
    text: str
    summary: str = ""
    hypothetical_questions: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)


@dataclass
class RetrievalHit:
    chunk: Chunk
    score: float
    lexical_score: float = 0.0
    dense_score: float = 0.0
    rerank_score: float | None = None


def _tokenize(text: str) -> list[str]:
    """中文按 unigram+bigram、英文数字按词切分，供 BM25 与离线向量使用。"""
    tokens = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", text)]
    han = re.findall(r"[\u4e00-\u9fff]", text)
    tokens.extend(han)
    tokens.extend(han[i] + han[i + 1] for i in range(len(han) - 1))
    return tokens


def _chinese_tokens(text: str) -> set[str]:
    """兼容旧接口：返回去重后的中文友好 token。"""
    return set(_tokenize(text))


def _chunk_document_text(chunk: Chunk, index: str = "raw") -> str:
    if index == "summary":
        return chunk.summary or chunk.text
    if index == "hypo":
        return " ".join(chunk.hypothetical_questions) or chunk.text
    if index == "metadata":
        meta = chunk.metadata or {}
        return " ".join([
            str(meta.get("topic", "")),
            str(meta.get("style", "")),
            " ".join(str(x) for x in meta.get("keywords", [])),
        ])
    return chunk.text


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class Embedder(Protocol):
    name: str
    dimension: int

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """无依赖的 dense 特征兜底；用于测试，不应冒充语义 embedding 模型。"""

    name = "hashing"

    def __init__(self, dimension: int = 384):
        self.dimension = dimension

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dimension
            for token in _tokenize(text):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
                idx = int.from_bytes(digest[:8], "little") % self.dimension
                sign = 1.0 if int.from_bytes(digest[8:], "little") & 1 else -1.0
                vec[idx] += sign
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            vectors.append([x / norm for x in vec])
        return vectors


class OllamaEmbedder:
    """调用本地 Ollama `/api/embed` 的真实语义向量后端。"""

    def __init__(self, model: str = "bge-m3", base_url: str = "http://127.0.0.1:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.name = f"ollama:{model}"
        self.dimension = 0

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/embed", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Ollama embedding 调用失败（模型 {self.model}）。"
                f"请先启动 Ollama 并执行 `ollama pull {self.model}`：{exc}"
            ) from exc
        vectors = [[float(x) for x in row] for row in data.get("embeddings", [])]
        if len(vectors) != len(texts):
            raise RuntimeError("Ollama embedding 返回数量与输入不一致")
        if vectors:
            self.dimension = len(vectors[0])
        return vectors


class CachedEmbedder:
    """Persist document embeddings in a small SQLite cache on the local disk.

    Query vectors are intentionally not cached, so arbitrary user queries cannot
    make the cache grow without bound. ``VectorStore`` calls ``encode_documents``
    only while building the knowledge index.
    """

    def __init__(self, delegate: Embedder, cache_path: str | Path):
        self.delegate = delegate
        self.cache_path = Path(cache_path)
        self.name = delegate.name
        self.dimension = delegate.dimension
        self.cache_hits = 0
        self.cache_misses = 0

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self.delegate.encode(texts)
        self.dimension = self.delegate.dimension
        return vectors

    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]:
        rows = list(texts)
        if not rows:
            return []
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        keys = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in rows]
        found: dict[str, list[float]] = {}
        with sqlite3.connect(self.cache_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS embeddings ("
                "model TEXT NOT NULL, content_hash TEXT NOT NULL, "
                "dimension INTEGER NOT NULL, vector BLOB NOT NULL, "
                "PRIMARY KEY (model, content_hash))"
            )
            for key in dict.fromkeys(keys):
                cached = db.execute(
                    "SELECT dimension, vector FROM embeddings "
                    "WHERE model = ? AND content_hash = ?",
                    (self.delegate.name, key),
                ).fetchone()
                if cached:
                    dim, blob = int(cached[0]), cached[1]
                    values = array("f")
                    values.frombytes(blob)
                    if len(values) == dim:
                        found[key] = [float(value) for value in values]

            missing_keys: list[str] = []
            missing_texts: list[str] = []
            for key, text in zip(keys, rows):
                if key not in found and key not in missing_keys:
                    missing_keys.append(key)
                    missing_texts.append(text)
            self.cache_hits += len(rows) - len(missing_texts)
            self.cache_misses += len(missing_texts)

            if missing_texts:
                new_vectors = self.delegate.encode(missing_texts)
                for key, vector in zip(missing_keys, new_vectors):
                    values = array("f", (float(value) for value in vector))
                    found[key] = [float(value) for value in values]
                    db.execute(
                        "INSERT OR REPLACE INTO embeddings "
                        "(model, content_hash, dimension, vector) VALUES (?, ?, ?, ?)",
                        (self.delegate.name, key, len(values), values.tobytes()),
                    )
                db.commit()
                self.dimension = self.delegate.dimension
        return [found[key] for key in keys]


class SentenceTransformerEmbedder:
    """可选 sentence-transformers embedding 后端，首次使用会加载指定模型。"""

    def __init__(self, model: str = "BAAI/bge-small-zh-v1.5"):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("请先安装 `pip install -e .[rag]`") from exc
        self.model_name = model
        self.name = f"sentence-transformers:{model}"
        self._model = SentenceTransformer(model)
        self.dimension = int(self._model.get_sentence_embedding_dimension())

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        rows = self._model.encode(list(texts), normalize_embeddings=True)
        return [[float(x) for x in row] for row in rows]


class BM25Index:
    """标准 Okapi BM25 稀疏检索实现。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b

    def search(self, query: str, chunks: Sequence[Chunk], top_k: int = 5,
               index: str = "raw") -> list[tuple[Chunk, float]]:
        if not chunks:
            return []
        documents = [_tokenize(_chunk_document_text(c, index)) for c in chunks]
        avgdl = sum(len(doc) for doc in documents) / len(documents) or 1.0
        dfs: Counter[str] = Counter()
        for doc in documents:
            dfs.update(set(doc))
        query_terms = _tokenize(query)
        scored: list[tuple[Chunk, float]] = []
        n_docs = len(documents)
        for chunk, doc in zip(chunks, documents):
            tf = Counter(doc)
            score = 0.0
            for term in query_terms:
                freq = tf.get(term, 0)
                if not freq:
                    continue
                idf = math.log(1.0 + (n_docs - dfs[term] + 0.5) / (dfs[term] + 0.5))
                denom = freq + self.k1 * (1.0 - self.b + self.b * len(doc) / avgdl)
                score += idf * freq * (self.k1 + 1.0) / denom
            # BM25 没有任何词项命中时不要进入 RRF；否则零分文档也会因
            # “出现在排名列表里”而获得融合分，污染真正的召回结果。
            if score > 0.0:
                scored.append((chunk, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]


class VectorStore:
    """内存混合索引：BM25 + dense vector 精确 kNN。"""

    def __init__(self, embedder: Embedder | None = None):
        self.chunks: dict[str, Chunk] = {}
        self.embedder = embedder or HashingEmbedder()
        self.bm25 = BM25Index()
        self.last_dense_mode = "knn-exact"
        self.last_index_error = ""

    def add(self, chunk: Chunk):
        if not chunk.embedding:
            encode_documents = getattr(self.embedder, "encode_documents", self.embedder.encode)
            chunk.embedding = encode_documents([_chunk_document_text(chunk, "raw")])[0]
        self.chunks[chunk.id] = chunk

    def add_many(self, chunks: Sequence[Chunk], batch_size: int = 32) -> None:
        """批量生成 embedding，避免导入 N 个 chunk 时发起 N 次模型请求。"""
        rows = list(chunks)
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            encode_documents = getattr(self.embedder, "encode_documents", self.embedder.encode)
            vectors = encode_documents([_chunk_document_text(c, "raw") for c in batch])
            for chunk, vector in zip(batch, vectors):
                chunk.embedding = vector
                self.add(chunk)

    def similarity(self, query: str, chunk: Chunk) -> float:
        """兼容旧门控：主题/关键词与正文的中文 token Jaccard。"""
        q = _chinese_tokens(query)
        if not q:
            return 0.0
        theme = (chunk.summary or "") + " " + " ".join(
            str(k) for k in (chunk.metadata or {}).get("keywords", []))
        theme_tok = _chinese_tokens(theme)
        body_tok = _chinese_tokens(chunk.text)
        theme_score = len(q & theme_tok) / len(q | theme_tok) if theme_tok else 0.0
        body_score = len(q & body_tok) / len(q | body_tok) if body_tok else 0.0
        return round(0.6 * theme_score + 0.4 * body_score, 4)

    def search(self, query: str, top_k: int = 5, index: str = "raw") -> list[tuple[Chunk, float]]:
        """兼容旧接口，现由标准 BM25 实现。"""
        return self.bm25.search(query, list(self.chunks.values()), top_k, index)

    def dense_search(self, query: str, top_k: int = 5) -> list[tuple[Chunk, float]]:
        if not self.chunks:
            return []
        embedded_chunks = {
            cid: chunk for cid, chunk in self.chunks.items() if chunk.embedding
        }
        if not embedded_chunks:
            raise RuntimeError("dense 文档索引不可用")
        query_vector = self.embedder.encode([query])[0]
        self.last_dense_mode = "knn-exact"
        rows = [
            (cid, _cosine(query_vector, chunk.embedding))
            for cid, chunk in embedded_chunks.items()
        ]
        rows.sort(key=lambda item: item[1], reverse=True)
        rows = rows[:top_k]
        return [(self.chunks[cid], score) for cid, score in rows]


def reciprocal_rank_fusion(results_list: list[list[tuple[Chunk, float]]],
                           k: int = 60) -> list[tuple[str, float]]:
    """RRF：只依赖排名融合多路结果，避免 BM25 与 cosine 分数量纲冲突。"""
    scores: dict[str, float] = {}
    for results in results_list:
        for rank, (chunk, _) in enumerate(results):
            scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


def _diversify_sources(chunks: Sequence[Chunk], max_per_source: int = 2) -> list[Chunk]:
    """限制单一文档占满候选，保留同文档最多两个互补片段。"""

    counts: Counter[str] = Counter()
    diversified: list[Chunk] = []
    for chunk in chunks:
        source = str((chunk.metadata or {}).get("source") or chunk.id)
        if counts[source] >= max_per_source:
            continue
        counts[source] += 1
        diversified.append(chunk)
    return diversified


class RuleBasedQueryEnhancer:
    """低延迟、可解释的查询改写；短内容主题默认优先使用它。"""

    def rewrite(self, query: str, n: int = 3) -> list[str]:
        clean = re.sub(r"[？?！!，,。]+", " ", query).strip()
        compact = re.sub(r"(?:怎么|如何|请问|一下|有哪些|是什么)", "", clean).strip()
        variants = [query]
        if compact and compact != query:
            variants.append(compact)
        terms = [part for part in clean.split() if part]
        if len(terms) > 1:
            variants.extend(terms)
        return list(dict.fromkeys(v for v in variants if v))[:n]

    def hyde(self, query: str) -> str:
        return f"关于{query}的实用经验包括核心步骤、常见问题、避坑建议和真实案例。"


class LLMQueryEnhancer(RuleBasedQueryEnhancer):
    """通过注入的统一 LLM callable 完成 query rewriting 与 HyDE。"""

    def __init__(self, complete_fn: Callable[..., str]):
        self.complete_fn = complete_fn

    def rewrite(self, query: str, n: int = 3) -> list[str]:
        prompt = (
            f"把检索问题《{query}》改写成 {n} 个互补的中文搜索查询。"
            "每行一个，只输出查询，不要解释。"
        )
        lines = [line.strip(" -0123456789.、") for line in self.complete_fn(
            prompt, temperature=0.2, max_tokens=160).splitlines()]
        return list(dict.fromkeys([query] + [line for line in lines if line]))[:n]

    def hyde(self, query: str) -> str:
        prompt = (
            f"为问题《{query}》写一段约120字、可能出现在高质量资料中的假设答案。"
            "只用于向量检索，不要声明不确定性。"
        )
        return self.complete_fn(prompt, temperature=0.2, max_tokens=220).strip()


class CrossEncoderReranker:
    """可选的真正 cross-encoder：联合编码 query-document 后重新排序。"""

    def __init__(self, model: str = "BAAI/bge-reranker-base"):
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise RuntimeError("启用 cross-encoder 前请安装 `pip install -e .[rag]`") from exc
        self.model_name = model
        self._model = CrossEncoder(model)

    def rerank(self, query: str, chunks: Sequence[Chunk], top_k: int) -> list[tuple[Chunk, float]]:
        if not chunks:
            return []
        scores = self._model.predict([(query, c.text) for c in chunks])
        rows = [(chunk, float(score)) for chunk, score in zip(chunks, scores)]
        rows.sort(key=lambda item: item[1], reverse=True)
        return rows[:top_k]


class RAGPipeline:
    """查询增强 → BM25/dense 多路召回 → RRF → cross-encoder → 门控。"""

    def __init__(self, store: VectorStore | None = None,
                 enhancer: RuleBasedQueryEnhancer | None = None,
                 reranker: CrossEncoderReranker | None = None,
                 enable_hyde: bool = False,
                 retrieval_mode: str = "hybrid"):
        self.store = store or VectorStore()
        self.enhancer = enhancer or RuleBasedQueryEnhancer()
        self.reranker = reranker
        self.enable_hyde = enable_hyde
        normalized_mode = retrieval_mode.lower().replace("bm25", "lexical")
        if normalized_mode not in {"lexical", "dense", "hybrid"}:
            raise ValueError(
                "retrieval_mode 必须是 lexical/bm25、dense 或 hybrid"
            )
        self.retrieval_mode = normalized_mode
        self.last_trace: dict[str, Any] = {}

    def multi_query(self, query: str, n: int = 3) -> list[str]:
        return self.enhancer.rewrite(query, n=n)

    def decompose(self, query: str) -> list[str]:
        return [part.strip() for part in re.split(r"[、和及与]", query) if part.strip()]

    def retrieve_hits(self, query: str, top_k: int = 5) -> list[RetrievalHit]:
        degraded_components: list[str] = []
        degradation_reasons: dict[str, str] = {}

        def mark_degraded(component: str, exc: Exception) -> None:
            if component not in degraded_components:
                degraded_components.append(component)
            degradation_reasons[component] = type(exc).__name__

        try:
            rewritten = self.multi_query(query)
        except Exception as exc:  # noqa: BLE001 - LLM query rewrite 失败时回退规则改写
            mark_degraded("query_enhancer", exc)
            rewritten = RuleBasedQueryEnhancer().rewrite(query)
        decomposed = self.decompose(query)
        queries = list(dict.fromkeys(rewritten + decomposed))
        if self.enable_hyde:
            try:
                hyde_doc = self.enhancer.hyde(query)
                if hyde_doc:
                    queries.append(hyde_doc)
            except Exception as exc:  # noqa: BLE001 - HyDE 是可选增强，不应使基础检索失败
                mark_degraded("hyde", exc)

        result_lists: list[list[tuple[Chunk, float]]] = []
        fetch_k = max(top_k * 3, 10)
        if (
            self.retrieval_mode in {"dense", "hybrid"}
            and self.store.last_index_error
        ):
            mark_degraded("dense_index", RuntimeError(self.store.last_index_error))
        for variant in queries:
            if self.retrieval_mode in {"lexical", "hybrid"}:
                result_lists.append(self.store.search(variant, fetch_k, index="raw"))
                result_lists.append(self.store.search(variant, fetch_k, index="metadata"))
            if self.retrieval_mode in {"dense", "hybrid"}:
                if self.store.last_index_error:
                    continue
                try:
                    result_lists.append(self.store.dense_search(variant, fetch_k))
                except Exception as exc:  # noqa: BLE001 - embedding 故障时保留 BM25/RRF 结果
                    mark_degraded("dense_search", exc)

        fused = reciprocal_rank_fusion(result_lists)
        candidates = [self.store.chunks[cid] for cid, _ in fused[:fetch_k]]
        fused_scores = dict(fused)
        rerank_scores: dict[str, float] = {}
        if self.reranker:
            try:
                reranked = self.reranker.rerank(query, candidates, top_k=fetch_k)
                candidates = [chunk for chunk, _ in reranked]
                rerank_scores = {chunk.id: score for chunk, score in reranked}
            except Exception as exc:  # noqa: BLE001 - 重排失败时继续使用 RRF 顺序
                mark_degraded("reranker", exc)
        candidates = _diversify_sources(candidates, max_per_source=2)

        query_vector: list[float] = []
        if (
            candidates
            and self.retrieval_mode in {"dense", "hybrid"}
            and not any(
                component in degraded_components
                for component in ("dense_index", "dense_search")
            )
        ):
            try:
                query_vector = self.store.embedder.encode([query])[0]
            except Exception as exc:  # noqa: BLE001 - 门控仍可依赖 lexical score
                mark_degraded("dense_scoring", exc)
        hits = []
        for chunk in candidates[:top_k]:
            lexical = self.store.similarity(query, chunk)
            dense = max(0.0, _cosine(query_vector, chunk.embedding)) if query_vector else 0.0
            hits.append(RetrievalHit(
                chunk=chunk,
                score=rerank_scores.get(chunk.id, fused_scores.get(chunk.id, 0.0)),
                lexical_score=lexical,
                dense_score=dense,
                rerank_score=rerank_scores.get(chunk.id),
            ))
        self.last_trace = {
            "query": query,
            "queries": queries,
            "retrieval_mode": self.retrieval_mode,
            "embedding_backend": self.store.embedder.name,
            "dense_mode": (
                "disabled" if self.retrieval_mode == "lexical"
                else "unavailable" if any(
                    component in degraded_components
                    for component in ("dense_index", "dense_search")
                )
                else self.store.last_dense_mode
            ),
            "fusion": "rrf",
            "reranker": getattr(self.reranker, "model_name", "none"),
            "max_chunks_per_source": 2,
            "candidates": len(candidates),
            "embedding_cache_hits": getattr(self.store.embedder, "cache_hits", 0),
            "embedding_cache_misses": getattr(self.store.embedder, "cache_misses", 0),
            "degraded_components": degraded_components,
            "degradation_reasons": degradation_reasons,
        }
        return hits

    def retrieve(self, query: str, top_k: int = 5) -> list[Chunk]:
        return [hit.chunk for hit in self.retrieve_hits(query, top_k)]

    def retrieve_relevant(self, query: str, top_k: int = 2,
                          min_score: float = 0.10) -> list[Chunk]:
        """用 max(lexical, dense cosine) 门控，避免低相关内容进入提示词。"""
        hits = self.retrieve_hits(query, top_k=max(top_k * 3, top_k))
        kept = [hit.chunk for hit in hits if max(hit.lexical_score, hit.dense_score) >= min_score]
        self.last_trace["min_score"] = min_score
        self.last_trace["returned"] = len(kept[:top_k])
        return kept[:top_k]


def resolve_min_score(config: dict[str, Any], pipeline: RAGPipeline) -> float:
    """按 embedding 后端选择经过评测校准的相关性门槛。

    cosine 分数在 hashing、Ollama 和 sentence-transformers 之间不可直接比较，
    因此保留通用 ``min_score`` 作为回退，并允许配置后端级覆盖。
    """

    default = float(config.get("min_score", 0.10))
    overrides = config.get("min_score_by_backend", {}) or {}
    if not isinstance(overrides, dict):
        overrides = {}
    backend = str(pipeline.store.embedder.name).lower()
    family = backend.split(":", 1)[0]
    value = overrides.get(backend, overrides.get(family, default))
    score = float(value)
    if not 0.0 <= score <= 1.0:
        raise ValueError("RAG min_score 必须在 0 到 1 之间")
    return score


_FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_PIPE_CACHE: dict[tuple[Any, ...], RAGPipeline] = {}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONT_RE.match(text)
    if not m:
        return {}, text
    try:
        import yaml
        meta = yaml.safe_load(m.group(1)) or {}
    except Exception:  # noqa: BLE001
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, text[m.end():].strip()


def _split_text(text: str, chunk_size: int = 600, overlap: int = 80) -> list[str]:
    clean = text.strip()
    if not clean or len(clean) <= chunk_size:
        return [clean] if clean else []
    chunks = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + chunk_size)
        if end < len(clean):
            boundary = max(clean.rfind("\n", start, end), clean.rfind("。", start, end))
            if boundary > start + chunk_size // 2:
                end = boundary + 1
        chunks.append(clean[start:end].strip())
        if end >= len(clean):
            break
        start = max(end - overlap, start + 1)
    return [chunk for chunk in chunks if chunk]


def _make_embedder(backend: str, model: str) -> Embedder:
    if backend == "ollama":
        # 聊天后端使用 OpenAI 兼容地址 `/v1`，embedding 则调用 Ollama
        # 原生 `/api/embed`。复用同一个环境变量时需要移除末尾 `/v1`。
        base_url = os.getenv("RAG_OLLAMA_BASE_URL") or os.getenv(
            "OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        base_url = re.sub(r"/v1/?$", "", base_url.rstrip("/"))
        return OllamaEmbedder(model=model, base_url=base_url)
    if backend in {"sentence-transformers", "sentence_transformers", "st"}:
        return SentenceTransformerEmbedder(model=model)
    return HashingEmbedder()


def build_rag_from_dir(dir_path: str | Path, *, chunk_size: int = 600,
                       embedding_backend: str | None = None,
                       embedding_model: str | None = None,
                       query_enhancer: str | None = None,
                       enable_hyde: bool | None = None,
                       reranker: str | None = None,
                       reranker_model: str | None = None,
                       retrieval_mode: str | None = None,
                       environment_overrides: bool = True) -> RAGPipeline:
    """从 Markdown 目录构建并按文件指纹缓存混合索引。"""
    path = Path(dir_path)
    env = os.getenv if environment_overrides else lambda _key: None
    backend = (env("RAG_EMBEDDING_BACKEND") or embedding_backend or "hashing").lower()
    default_model = "bge-m3" if backend == "ollama" else "BAAI/bge-small-zh-v1.5"
    model = env("RAG_EMBEDDING_MODEL") or embedding_model or default_model
    enhancer_name = (env("RAG_QUERY_ENHANCER") or query_enhancer or "rule").lower()
    hyde_env = env("RAG_ENABLE_HYDE")
    hyde = (hyde_env == "1") if hyde_env is not None else bool(enable_hyde)
    reranker_name = (env("RAG_RERANKER") or reranker or "none").lower()
    rerank_model = env("RAG_RERANKER_MODEL") or reranker_model or "BAAI/bge-reranker-base"
    mode = (env("RAG_RETRIEVAL_MODE") or retrieval_mode or "hybrid").lower()
    files = sorted(path.rglob("*.md")) if path.exists() else []
    signature = tuple((str(f.resolve()), f.stat().st_mtime_ns, f.stat().st_size) for f in files)
    cache_key = (signature, chunk_size, backend, model,
                 enhancer_name, hyde, reranker_name, rerank_model, mode)
    if cache_key in _PIPE_CACHE:
        return _PIPE_CACHE[cache_key]

    embedder: Embedder = _make_embedder(backend, model)
    cache_enabled = os.getenv("RAG_EMBEDDING_CACHE", "1") != "0"
    if backend != "hashing" and cache_enabled:
        configured_cache = os.getenv("RAG_EMBEDDING_CACHE_PATH")
        cache_path = (Path(configured_cache) if configured_cache
                      else path.parent / ".rag_cache" / "embeddings.sqlite3")
        embedder = CachedEmbedder(embedder, cache_path)
    store = VectorStore(embedder=embedder)
    if enhancer_name == "llm":
        from .llm import complete
        enhancer: RuleBasedQueryEnhancer = LLMQueryEnhancer(complete)
    else:
        enhancer = RuleBasedQueryEnhancer()
    cross_encoder = CrossEncoderReranker(rerank_model) if reranker_name == "cross-encoder" else None
    pipe = RAGPipeline(
        store,
        enhancer=enhancer,
        reranker=cross_encoder,
        enable_hyde=hyde,
        retrieval_mode=mode,
    )

    pending_chunks: list[Chunk] = []
    for file_index, file_path in enumerate(files):
        try:
            raw_text = file_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not raw_text.strip():
            continue
        meta, body = _parse_frontmatter(raw_text)
        topic = str(meta.get("topic") or "").strip()
        if not topic:
            topic = next((line.strip() for line in body.splitlines() if line.strip()), "")[:40]
        keywords = meta.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [item.strip() for item in keywords.split(",")]
        for chunk_index, part in enumerate(_split_text(body or raw_text, chunk_size=chunk_size)):
            pending_chunks.append(Chunk(
                id=f"kb-{file_index}-{chunk_index}",
                text=part,
                summary=topic,
                hypothetical_questions=[f"关于{topic}有哪些经验和避坑建议"] if topic else [],
                metadata={
                    "source": file_path.relative_to(path).as_posix(),
                    "topic": topic,
                    "style": str(meta.get("style") or ""),
                    "keywords": [str(item) for item in keywords],
                    "retrieval_role": str(meta.get("retrieval_role") or "example"),
                    "source_name": str(meta.get("source") or ""),
                    "source_url": str(meta.get("source_url") or ""),
                    "license": str(meta.get("license") or ""),
                    "chunk_index": chunk_index,
                },
            ))
    if pipe.retrieval_mode == "lexical":
        # BM25 消融不应因为本地 embedding 服务不可用而无法建索引。
        store.chunks.update({chunk.id: chunk for chunk in pending_chunks})
    else:
        try:
            store.add_many(pending_chunks)
        except Exception as exc:  # noqa: BLE001 - 建索引失败时保留可用的 BM25 路径
            store.last_index_error = f"{type(exc).__name__}: {exc}"
            store.chunks.update({chunk.id: chunk for chunk in pending_chunks})
    # 失败索引不进入进程级缓存；Ollama 恢复后下一次构建可自动重试 dense。
    if not store.last_index_error:
        _PIPE_CACHE[cache_key] = pipe
    return pipe
