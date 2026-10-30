"""Local document lifecycle orchestration; extraction-only answers are explicit."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .assistant_tools import KnowledgeSearchTool, sanitize_source_url
from .document_parser import DocumentError, PARSER_VERSION, parse_document_result, read_local_document
from .harness import Harness
from .knowledge_store import KnowledgeStore
from .rag import CachedEmbedder, RAGPipeline, _make_embedder, build_rag_from_documents
from .types import (
    DocumentParseOptions, KnowledgeChunk, KnowledgeDocument, KnowledgeImportRequest, KnowledgeImportResult, KnowledgeReviewedParse,
    KnowledgeSearchInput, KnowledgeSearchRequest, KnowledgeSearchResponse,
)


def _digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


class KnowledgeBaseService:
    def __init__(self, db_path: str | Path, source_root: str | Path, *, chunk_size: int = 600,
                 harness: Harness | None = None, parse_options: DocumentParseOptions | None = None,
                 ocr_backend=None):
        if not 100 <= chunk_size <= 2_000:
            raise ValueError("chunk_size must be between 100 and 2000")
        self.store = KnowledgeStore(db_path)
        self.source_root = Path(source_root)
        self.chunk_size = chunk_size
        self.harness = harness or Harness()
        self.parse_options = parse_options or DocumentParseOptions()
        self.ocr_backend = ocr_backend

    def _guard(self, action: str, kb_id: str):
        allowed, _ = self.harness.guard(action, user_id="local:" + kb_id)
        if not allowed:
            raise DocumentError("operation_blocked")

    def parser_context(self, suffix: str):
        """Shared policy identity for preparation and reviewed publication."""
        version = f"{PARSER_VERSION}:chars-{self.chunk_size}"
        backend = self.ocr_backend
        if self.parse_options.ocr_enabled and suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg"}:
            if backend is None:
                from .local_ocr import LocalPaddleOCR
                backend = LocalPaddleOCR(self.parse_options)
            policy = self.parse_options.model_dump(exclude={"model_root", "worker_python"})
            version += ":ocr-" + _digest(json.dumps([policy, backend.fingerprint], sort_keys=True))
        return version, backend

    def prepare_review(self, request: KnowledgeImportRequest) -> KnowledgeReviewedParse:
        """Prepare without indexing. Only a local operator/service may call this."""
        self._guard("knowledge_ingest", request.kb_id)
        relative, data = read_local_document(self.source_root, request.source_path)
        identity = request.source_path.replace("\\", "/")
        if relative != identity:
            raise DocumentError("noncanonical_path")
        document_id = "doc-" + _digest(request.kb_id + "\0" + identity)[:24]
        previous = self.store.get(request.kb_id, document_id)
        parser_version, backend = self.parser_context(Path(relative).suffix)
        parsed = parse_document_result(data, Path(relative).suffix, options=self.parse_options, ocr_backend=backend)
        return KnowledgeReviewedParse(content_hash=_digest(data), parser_version=parser_version,
            expected_revision=previous.revision if previous else 0, parsed=parsed)

    def ingest(self, request: KnowledgeImportRequest, *, reviewed_parse: KnowledgeReviewedParse | None = None) -> KnowledgeImportResult:
        self._guard("knowledge_ingest", request.kb_id)
        identity = request.source_path.replace("\\", "/")
        document_id = "doc-" + _digest(request.kb_id + "\0" + identity)[:24]
        job_id = self.store.start(request.kb_id, document_id, "ingest")
        try:
            relative, data = read_local_document(self.source_root, request.source_path)
            if relative != identity:
                raise DocumentError("noncanonical_path")
            previous = self.store.get(request.kb_id, document_id)
            content_hash = _digest(data)
            source_url = sanitize_source_url(request.source_url)
            if request.source_url and source_url != request.source_url:
                raise DocumentError("invalid_source_url")
            parser_version, backend = self.parser_context(Path(relative).suffix)
            if reviewed_parse is not None:
                if (reviewed_parse.content_hash != content_hash or reviewed_parse.parser_version != parser_version
                        or not request.accept_ocr_review):
                    raise DocumentError("review_stale")
                parser_version += ":review-" + _digest(reviewed_parse.parsed.model_dump_json())
            version = _digest(json.dumps([content_hash, parser_version, request.title,
                source_url, request.license], ensure_ascii=False))
            if previous and previous.status == "active" and previous.document_version == version:
                self.store.confirm_unchanged(job_id, previous)
                return KnowledgeImportResult(job_id=job_id, document_id=document_id,
                    status="unchanged", document_version=version, chunk_count=len(previous.chunks))
            if reviewed_parse is not None:
                if reviewed_parse.expected_revision != (previous.revision if previous else 0):
                    raise DocumentError("review_stale")
                parsed = reviewed_parse.parsed
            else:
                parsed = parse_document_result(data, Path(relative).suffix, options=self.parse_options, ocr_backend=backend)
            if parsed.requires_review and not request.accept_ocr_review:
                raise DocumentError("ocr_review_required")
            units = parsed.units
            title = request.title or next((u.section for u in units if u.section), Path(relative).stem)
            chunks = []
            for unit_index, unit in enumerate(units):
                parent_id = f"{document_id}:{version[:16]}:{unit_index}"
                offset = 0
                while offset < len(unit.text):
                    end_offset = min(offset + self.chunk_size, len(unit.text))
                    if end_offset < len(unit.text):
                        boundary = max(unit.text.rfind("\n", offset + self.chunk_size // 2, end_offset),
                            unit.text.rfind(" ", offset + self.chunk_size // 2, end_offset))
                        if boundary > offset:
                            end_offset = boundary + 1
                    part = unit.text[offset:end_offset]
                    if not part.strip():
                        offset = end_offset
                        continue
                    start = (unit.line_start + unit.text[:offset].count("\n")) if unit.line_start else None
                    end = (start + part.count("\n")) if start else None
                    if Path(relative).suffix.lower() in {".html", ".htm"}:
                        start, end = unit.line_start, unit.line_end
                    chunks.append(KnowledgeChunk(
                        chunk_id="chunk-" + _digest(parent_id + ":" + str(offset))[:32],
                        parent_id=parent_id,
                        parent_text=unit.text[max(0, offset - 300):min(len(unit.text), end_offset + 300)],
                        text=part,
                        section=unit.section, page=unit.page, line_start=start, line_end=end,
                        extraction_method=unit.extraction_method, bounding_box=unit.bounding_box,
                        ocr_confidence=unit.ocr_confidence, quality_flags=unit.quality_flags,
                        ocr_engine=unit.ocr_engine, ocr_model_fingerprint=unit.ocr_model_fingerprint,
                        ocr_reviewed=unit.extraction_method == "ocr" and request.accept_ocr_review,
                        human_edited=unit.human_edited))
                    if len(chunks) > 2_000:
                        raise DocumentError("too_many_chunks")
                    offset = end_offset
            if not chunks:
                raise DocumentError("empty_document")
            document = KnowledgeDocument(kb_id=request.kb_id, document_id=document_id,
                document_version=version, source_path=relative, title=title,
                source_url=source_url, license=request.license, content_hash=content_hash,
                parser_version=parser_version, chunks=chunks, page_reports=parsed.pages)
            self.store.publish(job_id, document, previous)
            return KnowledgeImportResult(job_id=job_id, document_id=document_id,
                status="updated" if previous else "created", document_version=version, chunk_count=len(chunks))
        except (DocumentError, OSError) as exc:
            reason = str(exc) if isinstance(exc, DocumentError) else "source_unavailable"
            self.store.finish(job_id, failed=True, reason=reason)
            return KnowledgeImportResult(job_id=job_id, document_id=document_id, status="failed", reason=reason)
        except Exception:
            self.store.finish(job_id, failed=True, reason="internal_error")
            raise

    def delete(self, kb_id: str, document_id: str) -> bool:
        self._guard("knowledge_delete", kb_id)
        previous = self.store.get(kb_id, document_id)
        if previous is None or previous.status == "deleted":
            return False
        job_id = self.store.start(kb_id, document_id, "delete")
        try:
            self.store.publish(job_id, previous.model_copy(update={"status": "deleted"}), previous)
        except DocumentError as exc:
            self.store.finish(job_id, failed=True, reason=str(exc))
            raise
        except Exception:
            self.store.finish(job_id, failed=True, reason="internal_error")
            raise
        return True

    def search(self, request: KnowledgeSearchRequest, *, backend: str = "hashing",
               model: str | None = None, retrieval_mode: str = "hybrid",
               vector_store: str = "memory", qdrant_path: str = ".rag_cache/qdrant_catalog") -> KnowledgeSearchResponse:
        self._guard("knowledge_search", request.kb_id)
        if backend not in {"hashing", "ollama", "sentence-transformers"}:
            raise ValueError("unsupported embedding backend")
        pipeline, document_count = self.pipeline(request.kb_id, backend=backend,
            model=model, retrieval_mode=retrieval_mode, vector_store=vector_store, qdrant_path=qdrant_path)
        try:
            output = KnowledgeSearchTool(pipeline).run(KnowledgeSearchInput(
                query=request.query, top_k=request.top_k, min_score=request.min_score))
        finally:
            close = getattr(pipeline.store, "close", None)
            if close:
                close()
        if output.sources:
            answer = "以下为原文证据摘录（检索式基线，不是模型生成或事实裁决）：\n" + "\n".join(
                f"- {source.excerpt} [{source.ref_id}]" for source in output.sources)
        else:
            answer = "当前知识库未检索到达到门槛的证据；请补充资料或澄清问题。"
        return KnowledgeSearchResponse(kb_id=request.kb_id, answer=answer, sources=output.sources,
            index_version=pipeline.index_version, document_count=document_count,
            embedding_backend=pipeline.store.embedder.name, retrieval_mode=pipeline.retrieval_mode)

    def pipeline(self, kb_id: str, *, backend: str = "hashing", model: str | None = None,
                 retrieval_mode: str = "hybrid", vector_store: str = "memory",
                 qdrant_path: str = ".rag_cache/qdrant_catalog") -> tuple[RAGPipeline, int]:
        """A fresh request-scoped snapshot; callers execute retrieval through Harness."""
        if backend not in {"hashing", "ollama", "sentence-transformers"}:
            raise ValueError("unsupported embedding backend")
        documents = self.store.documents(kb_id)
        embedder = _make_embedder(backend, model or ("bge-m3" if backend == "ollama" else "BAAI/bge-small-zh-v1.5"))
        if backend != "hashing":
            embedder = CachedEmbedder(embedder, self.store.path.with_suffix(".embeddings.sqlite3"))
        store = None
        if vector_store == "qdrant":
            from .qdrant_store import QdrantVectorStore
            store = QdrantVectorStore(qdrant_path, embedder=embedder)
        elif vector_store != "memory":
            raise ValueError("unsupported_vector_store")
        pipeline = build_rag_from_documents(documents, embedder=embedder, retrieval_mode=retrieval_mode, vector_store=store)
        return pipeline, len(documents)
