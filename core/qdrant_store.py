"""Optional local persistent Qdrant adapter behind the existing VectorStore API.

Only current-snapshot point IDs are eligible for retrieval. Old persisted points
are not trusted as evidence. Local mode is exact search, NOT a server/ANN claim.
The caller must close request-scoped instances, including on failure.
"""
from __future__ import annotations

import hashlib
import threading
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from .rag import Chunk, Embedder, VectorStore, _chunk_document_text


@lru_cache(maxsize=32)
def _path_lock(path: str):
    return threading.RLock()


class QdrantVectorStore(VectorStore):
    def __init__(self, path: str | Path, *, embedder: Embedder | None = None):
        from qdrant_client import QdrantClient

        super().__init__(embedder=embedder)
        self.path = str(Path(path).resolve())
        self._lock = _path_lock(self.path)
        self._lock.acquire()
        self._closed = False
        self.collection = ""
        try:
            self.client = QdrantClient(path=self.path)
        except Exception:
            self._lock.release()
            raise

    def _point_id(self, chunk_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, self.collection + ":" + chunk_id))

    def _ensure_collection(self, dimension: int):
        from qdrant_client import models

        identity = f"{self.embedder.name}:{dimension}"
        self.collection = "personax_" + hashlib.sha256(identity.encode()).hexdigest()[:20]
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(self.collection,
                vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE))

    def add(self, chunk: Chunk):
        self.add_many([chunk])

    def add_many(self, chunks: Sequence[Chunk], batch_size: int = 32) -> None:
        from qdrant_client import models

        rows = list(chunks)
        kb_ids = {str((c.metadata or {}).get("kb_id", "")) for c in rows}
        if len(kb_ids) > 1 or (rows and not next(iter(kb_ids))):
            raise ValueError("qdrant_requires_single_catalog_snapshot")
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            encode = getattr(self.embedder, "encode_documents", self.embedder.encode)
            vectors = encode([_chunk_document_text(c) for c in batch])
            if len(vectors) != len(batch):
                raise ValueError("embedding_batch_length_mismatch")
            if vectors and len({len(vector) for vector in vectors}) != 1:
                raise ValueError("embedding_dimension_mismatch")
            points = []
            for chunk, vector in zip(batch, vectors):
                if not vector:
                    raise ValueError("empty_embedding")
                self._ensure_collection(len(vector))
                chunk.embedding = vector
                VectorStore.add(self, chunk)
                metadata = chunk.metadata or {}
                points.append(models.PointStruct(id=self._point_id(chunk.id), vector=vector,
                    payload={"chunk_id": chunk.id, "kb_id": metadata.get("kb_id"),
                        "document_id": metadata.get("document_id"),
                        "document_version": metadata.get("document_version")}))
            self.client.upsert(self.collection, points=points, wait=True)

    def dense_search(self, query: str, top_k: int = 5) -> list[tuple[Chunk, float]]:
        from qdrant_client import models

        if not self.chunks:
            return []
        kb_id = str(next(iter(self.chunks.values())).metadata["kb_id"])
        current_ids = [self._point_id(cid) for cid in self.chunks]
        result = self.client.query_points(self.collection, query=self.embedder.encode([query])[0],
            query_filter=models.Filter(must=[models.FieldCondition(key="kb_id", match=models.MatchValue(value=kb_id)),
                models.HasIdCondition(has_id=current_ids)]), limit=top_k, with_payload=True)
        self.last_dense_mode = "qdrant-local-exact"
        return [(self.chunks[point.payload["chunk_id"]], float(point.score))
            for point in result.points if point.payload.get("chunk_id") in self.chunks]

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self.client.close()
            finally:
                self._lock.release()
