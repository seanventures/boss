"""Hybrid retrieval and role packs.

Inferred facts never outrank governed or approved facts. Packs stop at a hard
token budget and record the fact ids they included. Passport ceiling drops
tiers the reader is not allowed to see.
"""
from __future__ import annotations

import math
from typing import Any

from sqlalchemy.engine import Connection

from .store import PostgresKnowledgeStore, RRF_K

TIER_RANK = {"inferred": 1, "observed": 2, "approved": 3, "governed": 4}
PACKS = {
    ("design", "implement"): ("invariant", "coupling", "decision"),
    ("review", "review"): ("invariant", "decision", "risk"),
    ("sre", "incident"): ("coupling", "lesson", "risk"),
    ("jev", "decide"): ("decision", "lesson"),
}


def _tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4)) if text else 0


def visible(trust_tier: str, ceiling: str) -> bool:
    if trust_tier not in TIER_RANK or ceiling not in TIER_RANK:
        raise ValueError("unknown trust tier")
    return TIER_RANK[trust_tier] <= TIER_RANK[ceiling]


def rank_hits(hits: list[dict]) -> list[dict]:
    """Tier first. Inside a tier, reciprocal-rank score. Inferred stays last."""
    def key(item: dict) -> tuple:
        tier = TIER_RANK.get(str(item.get("trust_tier") or "inferred"), 1)
        return (-tier, -float(item.get("score") or 0), str(item.get("node_key") or ""))

    return sorted(hits, key=key)


def fuse(text_hits: list[dict], vector_hits: list[dict]) -> list[dict]:
    fused: dict[str, dict] = {}
    for hits in (text_hits, vector_hits):
        for rank, hit in enumerate(hits, start=1):
            slot = fused.setdefault(
                hit["node_id"],
                {
                    "node_id": hit["node_id"],
                    "node_key": hit.get("node_key"),
                    "trust_tier": hit.get("trust_tier") or "inferred",
                    "score": 0.0,
                    "body": hit.get("body") or "",
                },
            )
            slot["score"] += 1.0 / (RRF_K + rank)
            if hit.get("trust_tier"):
                slot["trust_tier"] = hit["trust_tier"]
            if hit.get("body"):
                slot["body"] = hit["body"]
    return rank_hits(list(fused.values()))


def fuse_lists(lists: dict[str, list[dict]]) -> list[dict]:
    """Reciprocal-rank fusion of named candidate lists (text, graph, vector). A node in several
    lists outranks one in a single list; each hit records which lists found it."""
    fused: dict[str, dict] = {}
    for name, hits in lists.items():
        for rank, hit in enumerate(hits, start=1):
            slot = fused.setdefault(hit["node_id"], {"node_id": hit["node_id"],
                                                    "node_key": hit.get("node_key"),
                                                    "score": 0.0, "via": []})
            slot["score"] += 1.0 / (RRF_K + rank)
            slot["via"].append(name)
    return sorted(fused.values(), key=lambda item: (-item["score"], str(item.get("node_key") or "")))


def knowledge_pack(
    *,
    role: str,
    stage: str,
    hits: list[dict],
    budget_tokens: int,
    ceiling: str = "governed",
) -> dict[str, Any]:
    if budget_tokens < 0:
        raise ValueError("budget_tokens must be >= 0")
    wanted = PACKS.get((role, stage))
    chosen: list[dict] = []
    used = 0
    for hit in rank_hits(hits):
        tier = str(hit.get("trust_tier") or "inferred")
        if not visible(tier, ceiling):
            continue
        if hit.get("archived"):
            continue
        kind = hit.get("fact_kind")
        if wanted is not None and kind is not None and kind not in wanted:
            continue
        cost = _tokens(str(hit.get("body") or hit.get("node_key") or ""))
        if used + cost > budget_tokens:
            continue
        used += cost
        chosen.append(hit)
    return {
        "role": role,
        "stage": stage,
        "budget_tokens": budget_tokens,
        "tokens_used": used,
        "fact_ids": [hit["node_id"] for hit in chosen],
        "items": chosen,
        "labeled_as": "quoted source material",
    }


def precedent_section(hits: list[dict]) -> str:
    lines = []
    for hit in hits:
        lines.append(
            f"- {hit.get('node_key')}: {hit.get('body')} "
            f"[fact_id={hit.get('node_id')}; tier={hit.get('trust_tier')}]"
        )
    return "\n".join(lines)


def blast_radius(
    connection: Connection,
    store: PostgresKnowledgeStore,
    *,
    project_id: str,
    node_id: str,
    max_depth: int = 3,
    policy=None,
) -> list[dict]:
    """Callers and callees. Depth is walked one hop at a time, both directions.

    Under a stage policy the walk stays inside the stage's code reach and its ``max_hops``.
    """
    if policy is not None:
        max_depth = min(max_depth, max(policy.max_hops, 0))
    seen = {node_id}
    frontier = [node_id]
    found: list[dict] = []
    for depth in range(1, max_depth + 1):
        nxt = []
        for current in frontier:
            for direction, kind in (("in", "calls"), ("out", "calls"), ("in", "dispatches_to"),
                                    ("out", "dispatches_to")):
                rows = store.neighbors(
                    connection,
                    project_id=project_id,
                    node_id=current,
                    kind=kind,
                    direction=direction,
                    policy=policy,
                )
                for row in rows:
                    if row["node_id"] in seen:
                        continue
                    seen.add(row["node_id"])
                    item = dict(row)
                    item["depth"] = depth
                    found.append(item)
                    nxt.append(row["node_id"])
        frontier = nxt
    return found
