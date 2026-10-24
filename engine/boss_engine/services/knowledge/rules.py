"""Business rules: the Rules layer (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.3, §4.5).

Agents propose rules; only a person approves them (Rules review). A rule is a ``kind='rule'``
node, ``node_key='rule:<sha1(scope+statement)[:16]>'``, ``name`` = title, ``body`` = statement,
with the attrs below. Proposed rules are ``inferred`` and never injected into a pack; an approved
rule is ``approved`` (store-enforced: an agent cannot set it) and trusted input for every agent.

Phase 0 freezes the signature and the vocabulary; stream S2a implements it, and stream S3's
``scan_business_rules`` capability calls it.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from sqlalchemy.engine import Connection

CATEGORIES = frozenset({"validation", "calculation", "limit", "state_transition", "authorization", "other"})
STATUSES = frozenset({"proposed", "approved", "rejected", "superseded"})


def propose_rule(
    connection: Connection,
    *,
    project_id: str,
    statement: str,
    title: str,
    category: str,
    scope: Mapping[str, Sequence[str]],
    evidence: Sequence[Mapping[str, Any]],
    rationale: str,
    confidence: float,
    corrects: str | None = None,
) -> dict[str, Any]:
    """Write one proposed rule and its edges; return the rule as the view payload (``KgRule``).

    Args are the ``kg_propose_rule`` tool's (§4.5):

    - ``scope``: ``{areas[], packages[], symbols[]}`` — what it governs.
    - ``evidence``: at least one ``{symbol, lines, excerpt}``. Every ``symbol`` must resolve in
      the project (``store.find_by_key`` or a name lookup); otherwise the call refuses in words.
    - ``corrects``: the id of the approved rule this one corrects (a ``corrects`` edge; a person
      approves the correction).

    It writes an ``inferred`` rule (``attrs.status='proposed'``, ``attrs.version=1``,
    ``attrs.provenance`` from the LLM context — never from the arguments) plus ``evidenced_by``
    (attrs.lines, attrs.excerpt), ``proposed_by`` and ``produced`` edges, and ``corrects`` when
    given. It deduplicates by node_key: proposing the same scope + statement again returns the
    existing rule. Every call checks ``EntitlementPort.check(project_id, "knowledge.rules")``.
    """
    raise NotImplementedError("rules.propose_rule: stream S2a (BOSSAPP-768)")
