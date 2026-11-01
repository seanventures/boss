"""Knowledge Graph and Context Graph version numbers (design §4.2: "KG v17", "CG v4").

KNOWLEDGE GRAPH VERSION — the reference graph's version is ``1 + the number of approved change
sets`` in the project, counted at a point in time. A change set is ONE act by which a person
made something trusted input for every agent:

* a business rule approved (``kg_audit`` action ``knowledge.rule.approve``) — a correction that
  supersedes an older rule is still one act;
* a rule edited by its reviewer (``knowledge.rule.edit``): version n+1 approved in the same act,
  superseding the old one;
* a fact promoted to approved (``knowledge.promote``);
* a proposed trace link (clause ↔ rule/code) approved (``kg_edges`` with ``trust_tier =
  'approved'`` and the reviewer's ``attrs.reviewed_at``), one act per link.

Agent writes, scans and ingestion never bump it: they are observed or inferred, not approved.
A project with nothing approved yet is at v1. ``as_of`` gives the version at that moment, so a
decision recorded last week keeps the version it was made against.

CONTEXT GRAPH VERSION — the schema version of the context kinds a project's agents write: the
project's ontology pack's ``context_version`` when the pack declares one, else the pack's own
``version``; a project with no pack writes only the core context kinds, at
:data:`CORE_CONTEXT_VERSION`.

Both are written on every AgentDecision (``attrs.kg_version`` / ``attrs.cg_version``) and shown on
the agent read/write view's band titles ("Knowledge Graph v17 · read only").
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: The kg_audit actions that each count as one approved change set.
APPROVAL_ACTIONS = ("knowledge.rule.approve", "knowledge.rule.edit", "knowledge.promote")
#: The core Context Graph schema (agent_decision, coverage_gap, resolved_pattern).
CORE_CONTEXT_VERSION = 1


def kg_version(connection: Connection, project_id: str, *, as_of: Optional[datetime] = None) -> int:
    """The project's Knowledge Graph version, now or at ``as_of``."""
    params: dict[str, Any] = {"p": project_id, "actions": list(APPROVAL_ACTIONS), "at": as_of}
    acts = connection.execute(text(
        "SELECT count(*) FROM kg_audit WHERE project_id = :p AND action = ANY(:actions) "
        "AND (CAST(:at AS TIMESTAMPTZ) IS NULL OR created_at <= CAST(:at AS TIMESTAMPTZ))"
    ), params).scalar_one()
    links = connection.execute(text(
        "SELECT count(*) FROM kg_edges WHERE project_id = :p AND trust_tier = 'approved' "
        "AND attrs ? 'reviewed_at' AND (CAST(:at AS TIMESTAMPTZ) IS NULL "
        "OR CAST(attrs->>'reviewed_at' AS TIMESTAMPTZ) <= CAST(:at AS TIMESTAMPTZ))"
    ), params).scalar_one()
    return 1 + int(acts or 0) + int(links or 0)


def cg_version(connection: Connection, project_id: str) -> int:
    """The project's Context Graph schema version (its pack's context kinds)."""
    from .ontology_packs import get_project_pack

    try:
        with connection.begin_nested():
            pack = get_project_pack(connection, project_id=project_id)
    except Exception:  # noqa: BLE001 - no pack table yet: the core context kinds
        pack = None
    if pack is None:
        return CORE_CONTEXT_VERSION
    raw = getattr(pack, "context_version", None) or pack.version
    try:
        return max(1, int(str(raw).lstrip("vV")))
    except ValueError:
        return CORE_CONTEXT_VERSION


def versions(connection: Connection, project_id: str) -> dict[str, int]:
    """Both, as the console and an AgentDecision's provenance carry them."""
    return {"kg_version": kg_version(connection, project_id),
            "cg_version": cg_version(connection, project_id)}


def label(version: int | str | None) -> str:
    """``17`` → ``"v17"``; a value already spelt ``v17`` is kept."""
    if version in (None, ""):
        return ""
    value = str(version)
    return value if value.lower().startswith("v") else f"v{value}"
