"""Retrieval and context packs (KNOWLEDGE_GRAPH_SPEC.md §3.5, Phase 3).

Hybrid candidates, fused without a model:

1. **graph**: facts about the task's touched symbols and files, and about their neighbourhood
   (two hops, fan-out capped per hop so a hub node cannot blow the latency budget);
2. **vector**: the nearest facts by embedding, when the caller gives a (governed) embedder;
3. **text**: full text over the stored `kg_facts.search` tsvector (GIN);
4. **trusted**: the governed and approved facts of the kinds the recipe asks for.

They are fused with reciprocal-rank fusion, then weighted by tier, so an inferred fact never
outranks a governed one with the same evidence. An optional reranker runs only when the org's
knowledge budget allows it.

**One pack builder**, `knowledge_pack(role, stage, task, budget_tokens)`. Its recipes are the
spec's: design or implement, review, SRE, Jev. The rendered pack never exceeds `budget_tokens`, and
every pack logs the fact, node and decision ids it included (`kg_pack_log`, for §3.7). The MCP
tools (`kg_search`, `kg_precedent`) return the same `Pack` the injected path renders, so an engine
with tools and one without see the same content.

**Every read is scoped twice**: by org, project and team (`Reader.scope`, `Reader.team_id`), and by
the reading agent's Passport ceiling (`Reader.permissions`). A fact carries the permissions its
sources need (`kg_facts.read_permissions`, the union over its sources: the most restrictive);
a node needs the permission of its kind. Both filters run in SQL, inside every generator.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..token_efficiency import estimate_tokens
from .store import Scope, reciprocal_rank_fusion

#: The Passport permission a fact's source needs (the reader must hold every one of a fact's).
SOURCE_PERMISSION = {"run": "code_task:read", "review": "code_task:read", "ci": "code_task:read",
                     "incident": "reliability:read", "decision": "decision:read", "agent": "repository:read"}
#: ...and a node's kind. A kind not named here needs repository:read.
NODE_PERMISSION = {"incident": "reliability:read", "root_cause": "reliability:read", "service": "reliability:read",
                   "datastore": "reliability:read", "deployment": "deployment:read", "decision": "decision:read",
                   "person": "decision:read", "release": "decision:read", "run": "code_task:read",
                   "pr": "code_task:read", "spec": "code_task:read", "finding": "code_task:read"}
DEFAULT_NODE_PERMISSION = "repository:read"
DECISION_PERMISSION = "decision:read"

TIER_WEIGHT = {"governed": 1.0, "approved": 0.9, "observed": 0.8, "inferred": 0.5}
FANOUT = 50          # neighbours kept per node per hop
CANDIDATES = 40      # per generator


@dataclass(frozen=True)
class Reader:
    """Who is reading: the scope, and the reading agent's Passport ceiling.

    `permissions=None` is BOSS itself (the Archivist, a person's console read already checked by
    the route). An agent's read always carries its ceiling; an empty set reads nothing.
    """
    scope: Scope
    permissions: Optional[frozenset[str]] = None
    team_id: Optional[str] = None
    agent: Optional[str] = None

    def holds(self, permission: str) -> bool:
        return self.permissions is None or permission in self.permissions

    def node_kinds_denied(self) -> list[str]:
        if self.permissions is None:
            return []
        return sorted({k for k, p in NODE_PERMISSION.items() if p not in self.permissions})


class ScopeRefused(PermissionError):
    pass


def check_scope(c: Connection, reader: Reader) -> None:
    """The project must exist and, when the reader names an org, belong to it."""
    row = c.execute(text("SELECT w.org_id FROM projects p LEFT JOIN workspaces w ON w.id = p.workspace_id "
                         "WHERE p.id = :p"), {"p": reader.scope.project_id}).first()
    if row is None:
        raise ScopeRefused(f"unknown project {reader.scope.project_id!r}")
    if reader.scope.org_id and row[0] != reader.scope.org_id:
        raise ScopeRefused("the project is not in the reader's org")


# ── the fact filter every generator shares ───────────────────────────────────────────────────

def _fact_filter(reader: Reader, alias: str = "f") -> tuple[str, dict]:
    sql = (f" {alias}.project_id = :p AND {alias}.valid_to IS NULL"
           f" AND ({alias}.team_id IS NULL OR {alias}.team_id = CAST(:team AS TEXT))")
    params: dict[str, Any] = {"p": reader.scope.project_id, "team": reader.team_id}
    if reader.permissions is not None:
        sql += f" AND {alias}.read_permissions <@ CAST(:held AS TEXT[])"
        params["held"] = sorted(reader.permissions)
    return sql, params


def _node_filter(reader: Reader, alias: str = "n") -> tuple[str, dict]:
    sql = f" {alias}.project_id = :p AND {alias}.valid_to IS NULL"
    params: dict[str, Any] = {"p": reader.scope.project_id}
    if reader.permissions is not None:
        denied = reader.node_kinds_denied()
        if DEFAULT_NODE_PERMISSION not in reader.permissions:
            # only the kinds the reader holds a named permission for
            allowed = sorted(k for k, p in NODE_PERMISSION.items() if p in reader.permissions)
            sql += f" AND {alias}.kind = ANY(CAST(:nallow AS TEXT[]))"
            params["nallow"] = allowed
        elif denied:
            sql += f" AND NOT ({alias}.kind = ANY(CAST(:ndeny AS TEXT[])))"
            params["ndeny"] = denied
    return sql, params


FACT_COLS = ("f.id, f.subject, f.predicate, f.object, f.statement, f.kind, f.tier, f.confidence, "
             "f.valid_from, f.subject_node_id")


# ── candidate generators ─────────────────────────────────────────────────────────────────────

def seed_nodes(c: Connection, reader: Reader, touched: Sequence[str], *, overlay: Any = None) -> list[str]:
    """Node ids for the task's touched symbols and files: exact key, exact name, or the file's path."""
    names = [t for t in dict.fromkeys(str(x).strip() for x in touched or ()) if t]
    if overlay is not None:
        # the build's own changed files and symbols seed the query even when the plan names none
        names += [n for n in overlay.seed_names() if n not in names]
    if not names:
        return []
    where, params = _node_filter(reader)
    # Three indexed lookups, not one OR (which the planner can only answer with a scan).
    return [r[0] for r in c.execute(text(
        f"""SELECT id FROM (
              (SELECT n.id FROM kg_nodes n WHERE {where} AND n.node_key = ANY(:t) LIMIT 200)
              UNION (SELECT n.id FROM kg_nodes n WHERE {where} AND n.name = ANY(:t) LIMIT 200)
              UNION (SELECT n.id FROM kg_nodes n WHERE {where} AND n.repo IS NOT NULL AND n.path = ANY(:t) LIMIT 200)
            ) s LIMIT 200"""), {**params, "t": names})]


def neighbourhood(c: Connection, reader: Reader, seeds: Sequence[str], *, depth: int = 2,
                  fanout: int = FANOUT) -> dict[str, int]:
    """{node id: hops} within `depth`, both directions, current edges, at most `fanout` per node per hop."""
    out = {s: 0 for s in seeds}
    frontier = list(seeds)
    where, params = _node_filter(reader)
    for hop in range(1, depth + 1):
        if not frontier:
            break
        rows = c.execute(text(
            f"""SELECT DISTINCT nb.id FROM unnest(CAST(:f AS TEXT[])) AS s(id)
                CROSS JOIN LATERAL (
                  (SELECT e.dst_id AS id FROM kg_edges e WHERE e.src_id = s.id AND e.valid_to IS NULL LIMIT :fan)
                  UNION ALL
                  (SELECT e.src_id FROM kg_edges e WHERE e.dst_id = s.id AND e.valid_to IS NULL LIMIT :fan)) nb
                JOIN kg_nodes n ON n.id = nb.id AND {where}"""),
            {**params, "f": frontier, "fan": fanout}).all()
        frontier = [r[0] for r in rows if r[0] not in out]
        for nid in frontier:
            out[nid] = hop
    return out


def graph_candidates(c: Connection, reader: Reader, near: dict[str, int], touched: Sequence[str],
                     kinds: Optional[Sequence[str]] = None) -> list[str]:
    if not near and not touched:
        return []
    where, params = _fact_filter(reader)
    kinds_sql = " AND (CAST(:kinds AS TEXT[]) IS NULL OR f.kind = ANY(CAST(:kinds AS TEXT[])))"
    rows = c.execute(text(
        f"""SELECT * FROM (
              (SELECT f.id, f.subject_node_id, f.tier, f.confidence FROM kg_facts f
               WHERE {where} AND f.subject_node_id = ANY(:ids){kinds_sql} LIMIT 500)
              UNION (SELECT f.id, f.subject_node_id, f.tier, f.confidence FROM kg_facts f
               WHERE {where} AND lower(f.subject) = ANY(:names){kinds_sql} LIMIT 500)) s LIMIT 500"""),
        {**params, "ids": list(near), "names": [str(t).lower() for t in touched or ()],
         "kinds": list(kinds) if kinds else None}).all()
    rows = sorted(rows, key=lambda r: (near.get(r.subject_node_id, 0), -TIER_WEIGHT.get(r.tier, 0),
                                       -(r.confidence or 0), r.id))
    return [r.id for r in rows][:CANDIDATES]


def text_candidates(c: Connection, reader: Reader, query: str, kinds: Optional[Sequence[str]] = None) -> list[str]:
    if not (query or "").strip():
        return []
    where, params = _fact_filter(reader)
    # OR of the query's words: a task description is not a phrase anyone stored.
    words = [w for w in "".join(ch if ch.isalnum() or ch in "_" else " " for ch in query.lower()).split() if len(w) > 2]
    if not words:
        return []
    tsq = " | ".join(dict.fromkeys(words[:24]))
    return [r[0] for r in c.execute(text(
        f"""SELECT f.id FROM kg_facts f, to_tsquery('simple', :q) q
            WHERE {where} AND f.search @@ q
              AND (CAST(:kinds AS TEXT[]) IS NULL OR f.kind = ANY(CAST(:kinds AS TEXT[])))
            ORDER BY ts_rank(f.search, q) DESC, f.id LIMIT :k"""),
        {**params, "q": tsq, "kinds": list(kinds) if kinds else None, "k": CANDIDATES})]


def vector_candidates(c: Connection, reader: Reader, query: str, embedder: Any) -> list[str]:
    """Nearest facts by embedding. The embedder is the caller's, already governed; none, no vector."""
    if embedder is None or not getattr(embedder, "usable", True) or not (query or "").strip():
        return []
    from . import embeddings as emb

    if not emb.available(c):
        return []
    try:
        vector = embedder.embed([query])[0]
    except Exception:  # noqa: BLE001 - a refused or failed embedding drops this generator only
        return []
    near = emb.nearest(c, reader.scope, embedder.model, vector, owner_kind="fact", k=CANDIDATES * 2)
    if not near:
        return []
    where, params = _fact_filter(reader)
    allowed = {r[0] for r in c.execute(text(f"SELECT f.id FROM kg_facts f WHERE {where} AND f.id = ANY(:ids)"),
                                       {**params, "ids": near})}
    return [i for i in near if i in allowed][:CANDIDATES]


def trusted_candidates(c: Connection, reader: Reader, kinds: Optional[Sequence[str]]) -> list[str]:
    where, params = _fact_filter(reader)
    return [r[0] for r in c.execute(text(
        f"""SELECT f.id FROM kg_facts f WHERE {where} AND f.tier IN ('governed', 'approved')
              AND (CAST(:kinds AS TEXT[]) IS NULL OR f.kind = ANY(CAST(:kinds AS TEXT[])))
            ORDER BY f.confidence DESC, f.valid_from DESC LIMIT 20"""),   # idx_kg_facts_trusted; tier weighs later
        {**params, "kinds": list(kinds) if kinds else None})]


# ── search ───────────────────────────────────────────────────────────────────────────────────

Reranker = Callable[[str, list[dict]], list[str]]


def budget_allows(c: Connection, scope: Scope, estimate_usd_micros: int) -> bool:
    """The org's knowledge budget (daily and monthly caps) has room for one more call."""
    from .archivist import knowledge_spend, org_settings

    s = org_settings(c, scope)
    today, month = knowledge_spend(c, s["org_id"])
    return today + estimate_usd_micros <= s["daily"] and month + estimate_usd_micros <= s["monthly"]


def search(c: Connection, reader: Reader, query: str, *, touched: Sequence[str] = (),
           kinds: Optional[Sequence[str]] = None, k: int = 20, embedder: Any = None,
           reranker: Optional[Reranker] = None, rerank_estimate_usd_micros: int = 2000,
           overlay: Any = None) -> dict:
    """Hybrid search over facts. Returns {"facts": [...], "sources": {...}, "reranked": bool}."""
    check_scope(c, reader)
    seeds = seed_nodes(c, reader, touched, overlay=overlay)
    near = neighbourhood(c, reader, seeds) if seeds else {}
    ranked = {"graph": graph_candidates(c, reader, near, touched, kinds),
              "text": text_candidates(c, reader, query, kinds),
              "vector": vector_candidates(c, reader, query, embedder),
              "trusted": trusted_candidates(c, reader, kinds)}
    ranked = {name: ids for name, ids in ranked.items() if ids}
    fused = reciprocal_rank_fusion(ranked)
    facts = _load_facts(c, reader, [h.id for h in fused[: max(k * 3, k)]])
    by_id = {f["id"]: f for f in facts}
    items = []
    for h in fused:
        f = by_id.get(h.id)
        if f is None:
            continue
        items.append({**f, "score": round(h.score * TIER_WEIGHT.get(f["tier"], 0.5), 6), "via": sorted(set(h.sources))})
    items.sort(key=lambda f: (-f["score"], f["id"]))
    reranked = False
    if reranker is not None and items and budget_allows(c, reader.scope, rerank_estimate_usd_micros):
        order = [i for i in reranker(query, items[: k * 2]) if i in by_id]
        if order:
            pos = {fid: n for n, fid in enumerate(order)}
            # The reranker orders within a tier; it can never lift an inferred fact over a trusted one.
            items.sort(key=lambda f: (-TIER_WEIGHT.get(f["tier"], 0.5), pos.get(f["id"], len(pos)), f["id"]))
            reranked = True
    if overlay is not None:
        items = overlay.annotate(items)
    return {"facts": items[:k], "sources": {n: len(v) for n, v in ranked.items()}, "seeds": seeds,
            "neighbourhood": near, "reranked": reranked}


def _load_facts(c: Connection, reader: Reader, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    where, params = _fact_filter(reader)
    rows = c.execute(text(
        f"""SELECT {FACT_COLS}, (SELECT array_agg(DISTINCT s.source_kind) FROM kg_fact_sources s WHERE s.fact_id = f.id)
              AS source_kinds FROM kg_facts f WHERE {where} AND f.id = ANY(:ids)"""), {**params, "ids": ids}).mappings()
    return [{**dict(r), "source_kinds": list(r["source_kinds"] or []),
             "valid_from": str(r["valid_from"])} for r in rows]


# ── precedent (Jev, §3.6) ────────────────────────────────────────────────────────────────────

def precedent(c: Connection, reader: Reader, situation: str, *, k: int = 5,
              exclude: Optional[str] = None) -> list[dict]:
    """The top-k past decisions most like `situation`: what was asked, what was decided, the outcome.

    Full text over the decision's question and resolution, fused (RRF) with decision-kind facts about
    the same words. Outcome: the runs the decision started, by status. Needs decision:read.
    """
    check_scope(c, reader)
    if not reader.holds(DECISION_PERMISSION) or not (situation or "").strip():
        return []
    words = [w for w in "".join(ch if ch.isalnum() else " " for ch in situation.lower()).split() if len(w) > 2]
    if not words:
        return []
    tsq = " | ".join(dict.fromkeys(words[:24]))
    vec = "to_tsvector('simple', d.question || ' ' || COALESCE(d.resolution_note, ''))"
    by_text = [r[0] for r in c.execute(text(
        f"""SELECT d.id FROM decisions d, to_tsquery('simple', :q) q
            WHERE d.project_id = :p AND d.status NOT IN ('pending') AND {vec} @@ q
              AND d.id <> COALESCE(CAST(:x AS TEXT), '')
            ORDER BY ts_rank({vec}, q) DESC, d.updated_at DESC LIMIT :n"""),
        {"p": reader.scope.project_id, "q": tsq, "x": exclude, "n": CANDIDATES})]
    fused = [h.id for h in reciprocal_rank_fusion({"text": by_text})][:k]
    if not fused:
        return []
    rows = c.execute(text(
        """SELECT d.id, d.question, d.status, d.resolution_note, d.approved_by, d.risk_tier, d.updated_at,
                  d.cited_fact_ids,
                  (SELECT json_agg(json_build_object('run', r.id, 'status', r.status))
                     FROM code_task_runs r WHERE r.decision_id = d.id) AS runs
           FROM decisions d WHERE d.project_id = :p AND d.id = ANY(:ids)"""),
        {"p": reader.scope.project_id, "ids": fused}).mappings()
    found = {r["id"]: r for r in rows}
    out = []
    for did in fused:
        r = found.get(did)
        if r is None:
            continue
        runs = list(r["runs"] or [])
        statuses = [x.get("status") for x in runs]
        outcome = ("failed" if "failed" in statuses else "succeeded" if statuses and all(s == "completed" for s in statuses)
                   else "in progress" if statuses else "no run")
        out.append({"id": r["id"], "situation": r["question"][:400], "decided": r["status"],
                    "reason": (r["resolution_note"] or "")[:300], "by": r["approved_by"], "outcome": outcome,
                    "runs": runs[:5], "at": str(r["updated_at"]), "cited_fact_ids": list(r["cited_fact_ids"] or [])})
    return out


# ── the pack ─────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Section:
    key: str
    title: str
    kinds: tuple[str, ...] = ()         # fact kinds; empty for a node or precedent section
    from_sources: tuple[str, ...] = ()  # keep only facts with one of these source kinds
    node_kinds: tuple[str, ...] = ()    # a node section: these kinds near the touched area, or recent
    precedent: bool = False
    module_map: bool = False
    keywords: tuple[str, ...] = ()      # keep only facts whose statement names one of these
    limit: int = 8


SECTIONS = {
    "module_map": Section("module_map", "Module map around the change", module_map=True, limit=15),
    "invariants": Section("invariants", "Invariants", kinds=("invariant",)),
    "couplings": Section("couplings", "Couplings", kinds=("coupling",)),
    "decisions": Section("decisions", "Recent decisions", kinds=("decision",)),
    "findings": Section("findings", "Past review findings on these files", kinds=("lesson", "risk", "pattern"),
                        from_sources=("review",)),
    "security": Section("security", "Security decisions", kinds=("decision", "invariant", "risk"),
                        keywords=("auth", "secret", "token", "permission", "security", "csrf", "xss", "inject",
                                  "encrypt", "password", "passport", "privilege", "pii")),
    "topology": Section("topology", "Service topology", node_kinds=("service", "datastore", "external"), limit=12),
    "known_causes": Section("known_causes", "Known causes", kinds=("risk", "lesson"), from_sources=("incident",)),
    "runbooks": Section("runbooks", "Runbooks", kinds=("pattern", "lesson"), from_sources=("incident",)),
    "recent_incidents": Section("recent_incidents", "Recent incidents", node_kinds=("incident",), limit=6),
    "precedent": Section("precedent", "Precedent: similar past decisions and their outcomes", precedent=True, limit=5),
}

#: Spec §3.5's recipes. A recipe is an ordered list of sections; earlier sections are filled first.
RECIPES = {
    "build": ("invariants", "module_map", "couplings", "decisions"),
    "review": ("invariants", "findings", "security"),
    "sre": ("topology", "known_causes", "runbooks", "recent_incidents"),
    "jev": ("precedent", "decisions"),
}
REVIEW_ROLES = frozenset({"adversarial_reviewer", "security_reviewer", "code_reviewer", "reviewer", "rook", "sentry"})
SRE_ROLES = frozenset({"sre", "beacon", "operations_engineer", "infra_steward"})
JEV_ROLES = frozenset({"jev", "intent_router", "decider", "cao"})


def recipe_for(role: str, stage: str) -> str:
    role, stage = (role or "").lower(), (stage or "").lower()
    if role in JEV_ROLES or stage in ("decide", "decision", "route"):
        return "jev"
    if role in REVIEW_ROLES or "review" in stage or stage in ("security", "security_check"):
        return "review"
    if role in SRE_ROLES or stage in ("incident", "sre", "operate", "diagnose"):
        return "sre"
    return "build"


@dataclass
class Pack:
    recipe: str
    text: str
    budget_tokens: int
    tokens: int
    fact_ids: list[str] = field(default_factory=list)
    node_ids: list[str] = field(default_factory=list)
    decision_ids: list[str] = field(default_factory=list)
    sections: dict[str, list[str]] = field(default_factory=dict)
    sources: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"recipe": self.recipe, "text": self.text, "budget_tokens": self.budget_tokens, "tokens": self.tokens,
                "fact_ids": self.fact_ids, "node_ids": self.node_ids, "decision_ids": self.decision_ids,
                "sections": self.sections, "sources": self.sources}


def _fact_line(f: dict) -> str:
    tag = "inferred, unconfirmed" if f["tier"] == "inferred" else f["tier"]
    return f"- [{tag}] {' '.join(str(f['statement']).split())[:300]} (fact:{f['id']})"


def _pagerank(nodes: Iterable[str], edges: list[tuple[str, str]], *, iterations: int = 20, d: float = 0.85) -> dict:
    ids = list(dict.fromkeys(nodes))
    if not ids:
        return {}
    n = len(ids)
    rank = dict.fromkeys(ids, 1.0 / n)
    out: dict[str, list[str]] = {i: [] for i in ids}
    for a, b in edges:
        if a in out and b in rank:
            out[a].append(b)
    for _ in range(iterations):
        nxt = dict.fromkeys(ids, (1 - d) / n)
        dangling = sum(rank[i] for i in ids if not out[i])
        for i in ids:
            if out[i]:
                share = d * rank[i] / len(out[i])
                for j in out[i]:
                    nxt[j] += share
        for i in ids:
            nxt[i] += d * dangling / n
        rank = nxt
    return rank


def _module_map(c: Connection, reader: Reader, near: dict[str, int], limit: int, overlay: Any = None) -> list[tuple[str, str]]:
    """The symbols and files around the touched area, PageRank-ranked over their own subgraph (Aider-style)."""
    if not near:
        return []
    where, params = _node_filter(reader)
    nodes = c.execute(text(f"SELECT n.id, n.kind, n.name, n.path FROM kg_nodes n WHERE {where} AND n.id = ANY(:ids) "
                           f"AND n.kind IN ('symbol', 'file', 'module')"), {**params, "ids": list(near)}).mappings().all()
    ids = [r["id"] for r in nodes]
    edges = [(a, b) for a, b in c.execute(text(
        "SELECT src_id, dst_id FROM kg_edges WHERE src_id = ANY(:ids) AND dst_id = ANY(:ids) AND valid_to IS NULL"),
        {"ids": ids}).all()]
    pr = _pagerank(ids, edges)
    ranked = sorted(nodes, key=lambda r: (near.get(r["id"], 9), -pr.get(r["id"], 0), r["name"]))
    lines = []
    for r in ranked[:limit]:
        where_ = f" ({r['path']})" if r["path"] and r["kind"] == "symbol" else ""
        mark = " [changed in this build]" if overlay is not None and overlay.changes(r["name"], r["path"]) else ""
        lines.append((r["id"], f"- {r['kind']} {r['name']}{where_}{mark}"))
    if overlay is not None:
        lines += [(f"overlay:{n}", f"- symbol {n} [new in this build]") for n in overlay.added_names()][: max(0, limit - len(lines))]
    return lines


def _node_section(c: Connection, reader: Reader, sec: Section, near: dict[str, int]) -> list[tuple[str, str]]:
    where, params = _node_filter(reader)
    rows = c.execute(text(
        f"""SELECT n.id, n.kind, n.name, n.attrs FROM kg_nodes n WHERE {where} AND n.kind = ANY(:k)
            ORDER BY (n.id = ANY(:near)) DESC, n.last_seen DESC, n.id LIMIT :l"""),
        {**params, "k": list(sec.node_kinds), "near": list(near), "l": sec.limit}).mappings().all()
    lines = []
    for r in rows:
        attrs = r["attrs"] or {}
        extra = ", ".join(f"{k}: {attrs[k]}" for k in ("status", "severity", "depends_on") if attrs.get(k))
        lines.append((r["id"], f"- {r['kind']} {r['name']}" + (f" ({extra})" if extra else "")))
    return lines


def knowledge_pack(c: Connection, reader: Reader, role: str, stage: str, task: dict, budget_tokens: int, *,
                   run_id: Optional[str] = None, step: Optional[str] = None, embedder: Any = None,
                   reranker: Optional[Reranker] = None, overlay: Any = None, via: str = "pack",
                   log: bool = True) -> Pack:
    """The context pack for one role at one stage, never over `budget_tokens` (estimate_tokens).

    `task`: {"text": what the task is, "touched": [symbols or files from the plan or diff],
    "decision_id": for Jev, the decision being made (excluded from its own precedent)}.
    """
    recipe = recipe_for(role, stage)
    query = str(task.get("text") or "")
    touched = [str(t) for t in (task.get("touched") or [])]
    wanted = [SECTIONS[s] for s in RECIPES[recipe]]
    kinds = sorted({k for s in wanted for k in s.kinds}) or None
    found = search(c, reader, query, touched=touched, kinds=kinds, k=60, embedder=embedder, reranker=reranker,
                   overlay=overlay)
    facts, near = found["facts"], found["neighbourhood"]
    header = f"=== KNOWLEDGE ({recipe}) ===\n"
    footer = "=== END KNOWLEDGE ===\n"
    pack = Pack(recipe=recipe, text="", budget_tokens=int(budget_tokens), tokens=0, sources=found["sources"])
    if estimate_tokens(header + footer) > budget_tokens:
        return _logged(c, reader, pack, role, stage, run_id, step, via, log)
    body, used = [], set()
    for sec in wanted:
        if sec.precedent:
            lines = []
            for p in precedent(c, reader, query, k=sec.limit, exclude=task.get("decision_id")):
                lines.append((f"decision:{p['id']}",
                              f"- {p['situation'][:200]} -> {p['decided']}; outcome: {p['outcome']}"
                              + (f"; why: {p['reason'][:120]}" if p["reason"] else "") + f" (decision:{p['id']})"))
        elif sec.module_map:
            lines = _module_map(c, reader, near, sec.limit, overlay)
        elif sec.node_kinds:
            lines = _node_section(c, reader, sec, near)
        else:
            pick = [f for f in facts if f["kind"] in sec.kinds and f["id"] not in used
                    and (not sec.from_sources or set(f["source_kinds"]) & set(sec.from_sources))
                    and (not sec.keywords or any(w in str(f["statement"]).lower() for w in sec.keywords))]
            if sec.key == "decisions":
                pick.sort(key=lambda f: f["valid_from"], reverse=True)
            lines = [(f["id"], _fact_line(f)) for f in pick[: sec.limit]]
        if not lines:
            continue
        title = f"## {sec.title}\n"
        added = []
        for ident, line in lines:
            trial = header + "".join(body) + title + "".join(x for _, x in added) + line + "\n" + footer
            if estimate_tokens(trial) > budget_tokens:
                break
            added.append((ident, line + "\n"))
        if not added:
            continue
        body.append(title + "".join(x for _, x in added))
        pack.sections[sec.key] = [i for i, _ in added]
        for ident, _ in added:
            if ident.startswith("decision:"):
                pack.decision_ids.append(ident.split(":", 1)[1])
            elif ident.startswith("overlay:"):
                continue
            elif sec.module_map or sec.node_kinds:
                pack.node_ids.append(ident)
            else:
                pack.fact_ids.append(ident)
                used.add(ident)
    if body:
        pack.text = header + "".join(body) + footer
        pack.tokens = estimate_tokens(pack.text)
    assert pack.tokens <= budget_tokens
    return _logged(c, reader, pack, role, stage, run_id, step, via, log)


def _logged(c, reader, pack: Pack, role, stage, run_id, step, via, log) -> Pack:
    if log:
        c.execute(text(
            "INSERT INTO kg_pack_log (project_id, run_id, step, role, stage, via, fact_ids, node_ids, decision_ids, "
            "tokens, budget_tokens) VALUES (:p, :r, :s, :role, :stage, :via, :f, :n, :d, :t, :b)"),
            {"p": reader.scope.project_id, "r": run_id, "s": step, "role": role or "", "stage": stage or "", "via": via,
             "f": pack.fact_ids, "n": pack.node_ids, "d": pack.decision_ids, "t": pack.tokens, "b": pack.budget_tokens})
    return pack


def log_tool_answer(c: Connection, reader: Reader, tool: str, *, run_id: Optional[str], fact_ids: Sequence[str] = (),
                    decision_ids: Sequence[str] = ()) -> None:
    c.execute(text("INSERT INTO kg_pack_log (project_id, run_id, step, via, fact_ids, decision_ids) "
                   "VALUES (:p, :r, :s, 'tool', :f, :d)"),
              {"p": reader.scope.project_id, "r": run_id, "s": tool, "f": list(fact_ids), "d": list(decision_ids)})


def p95(samples: Sequence[float]) -> float:
    xs = sorted(samples)
    return xs[max(0, math.ceil(0.95 * len(xs)) - 1)] if xs else 0.0
