"""Embeddings of distilled facts and node summaries (spec §3.4; Phase 2).

* Only facts and node summaries are embedded, never raw logs.
* Every row records its model and dimension. One HNSW index per (model, dimension) is created on
  first use, as a partial expression index, because one `vector` column holds every model.
* Embeddings are cached by content hash: the same text under the same model is never sent twice,
  in any project.
* Every call goes through the model gateway (`GovernedChatAdapter.create_embeddings`), in the
  budget category `knowledge`. An adapter without embeddings, an install without pgvector, or a
  refused budget means no embeddings; facts still resolve by subject and exact duplicates.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .store import Scope


def available(c: Connection) -> bool:
    return bool(c.execute(text("SELECT to_regclass('kg_embeddings')")).scalar())


def _vs(c: Connection) -> str:
    """The schema pgvector is installed in, quoted: its type, operator and opclass are qualified
    with it, so queries do not depend on the search_path."""
    name = c.execute(text("SELECT extnamespace::regnamespace::text FROM pg_extension WHERE extname = 'vector'")).scalar()
    return name or "public"


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def ensure_index(c: Connection, model: str, dims: int) -> str:
    """The HNSW index for one (model, dimension); DDL cannot bind, so the model is quoted here."""
    dims = int(dims)
    name = f"idx_kg_emb_hnsw_{hashlib.sha1(model.encode()).hexdigest()[:10]}_{dims}"
    quoted, vs = model.replace("'", "''"), _vs(c)
    c.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON kg_embeddings USING hnsw "
                   f"((embedding::{vs}.vector({dims})) {vs}.vector_cosine_ops) "
                   f"WHERE model = '{quoted}' AND dims = {dims}"))
    return name


class Embedder:
    """`adapter.create_embeddings(texts, model=...) -> (vectors, usage)`, governed by the caller."""

    def __init__(self, adapter: Any, model: str):
        if not re.fullmatch(r"[A-Za-z0-9._:/\-]{1,200}", model or ""):
            raise ValueError("an embedding model name is required")
        self.adapter = adapter
        self.model = model

    @property
    def usable(self) -> bool:
        return callable(getattr(self.adapter, "create_embeddings", None))

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors, _usage = self.adapter.create_embeddings(texts, model=self.model)
        return [list(v) for v in vectors]


def store(c: Connection, scope: Scope, embedder: Embedder, owner_kind: str, items: list[tuple[str, str]]) -> dict:
    """Embed `(owner_id, text)` pairs, reusing any cached vector of the same text and model."""
    if not items or not available(c) or not embedder.usable:
        return {"embedded": 0, "cached": 0}
    hashes = {oid: text_hash(t) for oid, t in items}
    cached = {r.content_hash: r.v for r in c.execute(text(
        "SELECT DISTINCT ON (content_hash) content_hash, embedding::text AS v FROM kg_embeddings "
        "WHERE model = :m AND content_hash = ANY(:h)"), {"m": embedder.model, "h": list(set(hashes.values()))})}
    todo = [(oid, t) for oid, t in items if hashes[oid] not in cached]
    if todo:
        for (oid, _), vec in zip(todo, embedder.embed([t for _, t in todo])):
            cached[hashes[oid]] = _literal(vec)
    dims, vs = None, _vs(c)
    for oid, _ in items:
        v = cached[hashes[oid]]
        dims = v.count(",") + 1
        c.execute(text(
            "INSERT INTO kg_embeddings (project_id, owner_kind, owner_id, model, dims, content_hash, embedding) "
            f"VALUES (:p, :k, :o, :m, :d, :h, CAST(:v AS {vs}.vector)) ON CONFLICT (project_id, owner_kind, owner_id, model) "
            "DO UPDATE SET content_hash = EXCLUDED.content_hash, embedding = EXCLUDED.embedding, dims = EXCLUDED.dims"),
            {"p": scope.project_id, "k": owner_kind, "o": oid, "m": embedder.model, "d": dims,
             "h": hashes[oid], "v": v})
    if dims:
        ensure_index(c, embedder.model, dims)
    return {"embedded": len(todo), "cached": len(items) - len(todo)}


def nearest(c: Connection, scope: Scope, model: str, vector: list[float], *, owner_kind: str = "fact",
            k: int = 5, exclude: Optional[str] = None) -> list[str]:
    if not available(c):
        return []
    dims, vs = len(vector), _vs(c)
    return [r[0] for r in c.execute(text(
        f"SELECT owner_id FROM kg_embeddings WHERE project_id = :p AND owner_kind = :ok AND model = :m "
        f"AND dims = {dims} AND owner_id <> COALESCE(:x, '') "
        f"ORDER BY embedding::{vs}.vector({dims}) OPERATOR({vs}.<=>) CAST(:v AS {vs}.vector({dims})) LIMIT :k"),
        {"p": scope.project_id, "ok": owner_kind, "m": model, "x": exclude, "v": _literal(vector), "k": k})]
