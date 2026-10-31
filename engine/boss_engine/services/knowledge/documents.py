"""Add a document to a KG project (KG Domain layer v1, A2): paste-only in v1 (SPEC resolution
#7 — fetch-from-URL is a follow-up), caller-segmented into clauses (SPEC resolution #1 — BOSS
does not run text-segmentation over a pasted blob). Clauses are facts from the source: no
approval is required to create them (PRD FR15); the document record stores who added it and
when.
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..core import now_utc
from . import domain
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
    # Validate every clause before writing anything: a bad clause is refused up front, so a
    # refusal never leaves a half-written document node, clause or document→clause edge behind.
    checked: list[tuple[str, str, str, str]] = []
    for clause in clauses:
        section = str(clause.get("section") or "").strip()
        # Store the pasted clause text exactly as given; only check emptiness on a stripped copy.
        text_body = str(clause.get("text") or "")
        if not section or not text_body.strip():
            raise DocumentRefused(
                "every clause needs a section and its exact text; "
                f"got {dict(clause)!r}."
            )
        checked.append((
            section, text_body,
            str(clause.get("citation") or citation or ""),
            str(clause.get("url") or source_url or ""),
        ))
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
    for section, text_body, clause_citation, clause_url in checked:
        clause_key = "clause:" + _key(document_id, section)
        clause_id = STORE.upsert_node(
            connection, project_id=project_id, kind="clause", node_key=clause_key,
            name=section, body=text_body, trust_tier="governed", actor_kind=actor_kind,
            actor_ref=actor_ref,
            attrs={"document_id": document_id, "section": section, "text": text_body,
                  "citation": clause_citation, "url": clause_url},
        )
        # The document→clause link the graph reads (criterion 2): the clause belongs to its
        # document, so a clause opened in Graph shows its document and vice versa.
        STORE.upsert_edge(
            connection, project_id=project_id, src_id=document_id, dst_id=clause_id,
            kind="contains", actor_kind=actor_kind, actor_ref=actor_ref,
        )
        clause_ids.append(clause_id)
    return {"document_id": document_id, "clause_ids": clause_ids}


def list_documents(connection: Connection, *, project_id: str) -> list[dict[str, Any]]:
    """Every ``document`` node of the project, newest first, each with its ``clause`` children."""
    doc_rows = connection.execute(
        text(
            """
            SELECT id, name, body, attrs
            FROM kg_nodes
            WHERE project_id = :project_id AND kind = 'document'
            ORDER BY (attrs->>'added_at') DESC NULLS LAST
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    documents: list[dict[str, Any]] = []
    for row in doc_rows:
        attrs = dict(row["attrs"] or {})
        document_id = str(row["id"])
        clause_rows = connection.execute(
            text(
                """
                SELECT id, attrs
                FROM kg_nodes
                WHERE project_id = :project_id AND kind = 'clause'
                  AND attrs->>'document_id' = :document_id
                """
            ),
            {"project_id": project_id, "document_id": document_id},
        ).mappings().all()
        clauses = []
        for clause_row in clause_rows:
            clause_attrs = dict(clause_row["attrs"] or {})
            clauses.append({
                "id": str(clause_row["id"]),
                "section": clause_attrs.get("section", ""),
                "text": clause_attrs.get("text", ""),
                "citation": clause_attrs.get("citation", ""),
                "url": clause_attrs.get("url", ""),
            })
        documents.append({
            "id": document_id,
            "title": attrs.get("title", row["name"]),
            "source_url": attrs.get("source_url", ""),
            "citation": attrs.get("citation", row["body"]),
            "sensitivity": attrs.get("sensitivity", "public"),
            "added_by": attrs.get("added_by", ""),
            "added_at": attrs.get("added_at", ""),
            "clauses": clauses,
        })
    return documents


def linked_rules(connection: Connection, *, project_id: str, clause_id: str) -> list[dict[str, Any]]:
    """``rule`` nodes this clause is ``implemented_by``, deduped by target node."""
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT n.id AS node_id, n.name AS title
            FROM kg_edges e
            JOIN kg_nodes n ON n.id = e.dst_id
            WHERE e.project_id = :project_id AND e.kind = 'implemented_by'
              AND e.src_id = :clause_id AND n.kind = 'rule'
            """
        ),
        {"project_id": project_id, "clause_id": clause_id},
    ).mappings().all()
    return [{"node_id": str(row["node_id"]), "title": row["title"]} for row in rows]


def link_rule(
    connection: Connection, *, project_id: str, clause_id: str, rule_id: str, actor: str,
) -> dict[str, Any]:
    """An operator's explicit pick (the "Check compliance" missing-subject recovery): the
    clause is `implemented_by` `rule_id`, through the SAME edge `regulatory_trace` proposes
    (`domain.propose_implementation`) — never a one-off edge of its own — so `linked_rules`
    finds it on the clause's next read, same as a trace-proposed link."""
    row = connection.execute(
        text("SELECT id FROM kg_nodes WHERE project_id = :p AND id = :c AND kind = 'clause' "
             "AND valid_to IS NULL"), {"p": project_id, "c": clause_id}).mappings().first()
    if row is None:
        raise DocumentRefused(f"no clause '{clause_id}' in this project")
    rule_row = connection.execute(
        text("SELECT id FROM kg_nodes WHERE project_id = :p AND id = :r AND kind = 'rule' "
             "AND valid_to IS NULL"), {"p": project_id, "r": rule_id}).mappings().first()
    if rule_row is None:
        raise DocumentRefused(f"no rule '{rule_id}' in this project")
    return domain.propose_implementation(
        connection, project_id=project_id, clause_id=clause_id, target_id=rule_id,
        rationale=f"{actor} linked this rule from the compliance-check setup flow.",
        confidence=1.0, actor_ref=actor)
