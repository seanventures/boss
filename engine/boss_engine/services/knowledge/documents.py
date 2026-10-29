"""Add a document to a KG project (KG Domain layer v1, A2): paste-only in v1 (SPEC resolution
#7 — fetch-from-URL is a follow-up), caller-segmented into clauses (SPEC resolution #1 — BOSS
does not run text-segmentation over a pasted blob). Clauses are facts from the source: no
approval is required to create them (PRD FR15); the document record stores who added it and
when.
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping, Optional

from sqlalchemy.engine import Connection

from ..core import now_utc
from .ontology_packs import get_project_pack
from .store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()


class DocumentRefused(ValueError):
    """A document cannot be added, for a reason a person has to change."""


def _key(*parts: str) -> str:
    return hashlib.sha1(":".join(parts).encode("utf-8")).hexdigest()[:24]


def add_document(
    connection: Connection,
    *,
    project_id: str,
    title: str,
    source_url: str,
    citation: str,
    clauses: list[Mapping[str, Any]],
    sensitivity: str = "public",
    actor_kind: str = "person",
    actor_ref: Optional[str] = None,
) -> dict[str, Any]:
    pack = get_project_pack(connection, project_id=project_id)
    if pack is None or "document" not in pack.kinds:
        raise DocumentRefused(
            f"project {project_id!r} has no active ontology pack granting 'document'; "
            "assign a pack (e.g. banking-regulatory) before adding a document."
        )
    title = str(title or "").strip()
    if not title:
        raise DocumentRefused("a document needs a title.")
    if not clauses:
        raise DocumentRefused("a document needs at least one clause.")
    now = now_utc().isoformat()
    document_key = "document:" + _key(title, str(source_url or ""))
    document_id = STORE.upsert_node(
        connection, project_id=project_id, kind="document", node_key=document_key,
        name=title, body=str(citation or ""), trust_tier="governed", actor_kind=actor_kind,
        actor_ref=actor_ref,
        attrs={"title": title, "source_url": source_url, "citation": citation,
              "sensitivity": sensitivity, "added_by": actor_ref or actor_kind, "added_at": now},
    )
    clause_ids: list[str] = []
    for clause in clauses:
        section = str(clause.get("section") or "").strip()
        text_body = str(clause.get("text") or "").strip()
        if not section or not text_body:
            raise DocumentRefused(
                "every clause needs a section and its exact text; "
                f"got {dict(clause)!r}."
            )
        clause_citation = str(clause.get("citation") or citation or "")
        clause_url = str(clause.get("url") or source_url or "")
        clause_key = "clause:" + _key(document_id, section)
        clause_id = STORE.upsert_node(
            connection, project_id=project_id, kind="clause", node_key=clause_key,
            name=section, body=text_body, trust_tier="governed", actor_kind=actor_kind,
            actor_ref=actor_ref,
            attrs={"document_id": document_id, "section": section, "text": text_body,
                  "citation": clause_citation, "url": clause_url},
        )
        clause_ids.append(clause_id)
    return {"document_id": document_id, "clause_ids": clause_ids}
