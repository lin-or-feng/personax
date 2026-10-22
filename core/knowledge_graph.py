"""Small source-backed entity association graph, with bounded two-hop traversal.

Relations mean literal co-mention, not causality or biomedical assertions.
Every query validates edges against current active document/chunk versions.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from collections import deque
from contextlib import closing

from .harness import Harness
from .knowledge_store import KnowledgeStore
from .types import (KnowledgeGraphPath, KnowledgeGraphQuery, KnowledgeGraphResult,
    KnowledgeRelation, KnowledgeRelationInput)


def normalize_entity(text: str) -> str:
    return " ".join(text.casefold().split())


def contains_entity(text: str, term: str) -> bool:
    if term.isascii():
        return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])", text, re.I))
    return normalize_entity(term) in normalize_entity(text)


class KnowledgeGraphService:
    def __init__(self, db_path, *, harness: Harness | None = None):
        self.store = KnowledgeStore(db_path)
        self.harness = harness or Harness()
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS kb_relations (relation_id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS kb_relations_kb ON kb_relations(kb_id)")

    def _guard(self, action: str, kb_id: str):
        allowed, _ = self.harness.guard(action, user_id="local:" + kb_id)
        if not allowed:
            raise PermissionError("graph_operation_blocked")

    @staticmethod
    def _valid(relation, documents):
        document = documents.get(relation.document_id)
        if not document or document.document_version != relation.document_version:
            return False
        chunk = next((c for c in document.chunks if c.chunk_id == relation.chunk_id), None)
        if not chunk or relation.evidence not in chunk.text:
            return False
        return all(contains_entity(relation.evidence, term) for term in (relation.subject, relation.object))

    def add(self, request: KnowledgeRelationInput) -> KnowledgeRelation:
        self._guard("knowledge_graph_write", request.kb_id)
        if normalize_entity(request.subject) == normalize_entity(request.object):
            raise ValueError("self_relation_not_allowed")
        identity = "\0".join([request.kb_id, normalize_entity(request.subject), request.predicate,
            normalize_entity(request.object), request.document_id, request.document_version, request.chunk_id])
        relation = KnowledgeRelation(**request.model_dump(),
            relation_id="rel-" + hashlib.sha256(identity.encode()).hexdigest()[:32])
        documents = {d.document_id: d for d in self.store.documents(request.kb_id)}
        if not self._valid(relation, documents):
            raise ValueError("graph_evidence_not_current_or_not_literal")
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("INSERT OR REPLACE INTO kb_relations VALUES (?,?,?)",
                (relation.relation_id, relation.kb_id, relation.model_dump_json()))
        return relation

    def query(self, request: KnowledgeGraphQuery) -> KnowledgeGraphResult:
        self._guard("knowledge_graph_read", request.kb_id)
        documents = {d.document_id: d for d in self.store.documents(request.kb_id)}
        with closing(sqlite3.connect(self.store.path)) as db:
            rows = db.execute("SELECT payload FROM kb_relations WHERE kb_id=? ORDER BY relation_id LIMIT 5001",
                (request.kb_id,)).fetchall()
        if len(rows) > 5000:
            raise ValueError("graph_scope_too_large")
        relations = [KnowledgeRelation.model_validate_json(row[0]) for row in rows]
        active = [r for r in relations if self._valid(r, documents)]
        adjacent = {}
        for relation in active:
            for term, other in ((relation.subject, relation.object), (relation.object, relation.subject)):
                adjacent.setdefault(normalize_entity(term), []).append((other, relation))
        queue = deque([([request.entity], [])])
        paths = []
        while queue and len(paths) < request.limit:
            entities, edges = queue.popleft()
            if len(edges) >= request.max_hops:
                continue
            for other, relation in adjacent.get(normalize_entity(entities[-1]), []):
                if normalize_entity(other) in {normalize_entity(x) for x in entities}:
                    continue
                next_path = KnowledgeGraphPath(entities=entities + [other], relations=edges + [relation])
                paths.append(next_path)
                if len(paths) >= request.limit:
                    break
                queue.append((next_path.entities, next_path.relations))
        return KnowledgeGraphResult(kb_id=request.kb_id, paths=paths,
            excluded_stale_relations=len(relations) - len(active))
