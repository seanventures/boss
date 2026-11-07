"""The Graph area's read path (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §3): overview, neighbourhood,
node detail, lazy tree and type-ahead search, in the wire shapes of packages/api-client's
"KNOWLEDGE GRAPH VIEW" block (apps/web/tests/fixtures/kg-*.json are real rows in them).

Code is read from the rollups (view_rollup.py: ``kg_view_tree``, ``kg_view_edges``); rules and
learning are small and read live from ``kg_nodes``/``kg_edges``, by product layer, so rows written
by the rules and learning streams appear here without a change. Every function takes the kinds
the caller may not read (``withheld``: decisions and precedents without ``decision:read``) and
treats them as absent — never counted, never drawn.

Budgets (p95, warm, Fineract 51k nodes / 234k edges): overview < 300 ms, neighbourhood < 200 ms,
node detail < 150 ms, tree children < 100 ms, search < 150 ms (test_knowledge_view_perf_db.py).
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import view_context, view_rollup
from .titles import repair_cut

log = logging.getLogger(__name__)

#: The chips (design §4.2, §4.7.3): the Knowledge Graph's code, rules, learning and domain
#: (regulations, clauses, documents and a pack's other reference kinds), and the Context Graph —
#: what agents did and decided. All of them is "All".
PRODUCT_LAYERS = ("code", "rules", "learning", "domain", "context")
DEFAULT_LAYERS = frozenset(PRODUCT_LAYERS)

#: Product layer kinds (spec §2.1). Learned items are what agents write while they work; anchors
#: are what they hang on (drawn, never counted as learned).
CODE_KINDS = frozenset(view_rollup.CODE_KINDS)
RULE_KINDS = frozenset({"rule"})
LEARNED_KINDS = ("investigation", "test_failure", "fix", "workaround", "review_finding",
                 "decision", "root_cause", "precedent", "fact")
ANCHOR_KINDS = ("run", "incident", "pull_request", "release", "spec", "runbook", "alert_rule", "topic")
DOMAIN_KINDS = view_context.CORE_DOMAIN_KINDS
LEARNING_KINDS = frozenset(LEARNED_KINDS) | frozenset(ANCHOR_KINDS)

#: The read permission a kind needs beyond knowledge:read (spec §3 "Permissions per layer").
KIND_PERMISSIONS: dict[str, str] = {"decision": "decision:read", "precedent": "decision:read"}

RULE_STATUSES = ("proposed", "approved", "rejected", "superseded")
RULE_CATEGORIES = ("validation", "calculation", "limit", "state_transition", "authorization", "other")
TREE_PAGE = 200
CONNECTION_PAGE = 20
OVERVIEW_EDGE_CAP = 1500
MAX_DEPTH = 3
MAX_LIMIT = 200

_PLURAL = {
    "investigation": "Investigations", "test_failure": "Test failures", "fix": "Fixes",
    "workaround": "Workarounds", "review_finding": "Review findings", "decision": "Decisions",
    "root_cause": "Root causes", "precedent": "Precedents", "fact": "Facts", "run": "Runs",
    "incident": "Incidents", "pull_request": "Pull requests", "release": "Releases", "spec": "Specs",
    "runbook": "Runbooks", "alert_rule": "Alert rules", "topic": "Topics",
}
#: (kind, direction) -> the panel's group label.
_GROUP_LABELS = {
    ("calls", "out"): "Calls", ("calls", "in"): "Called by",
    ("dispatches_to", "out"): "Dispatches to", ("dispatches_to", "in"): "Dispatched from",
    ("overrides", "out"): "Overrides", ("overrides", "in"): "Overridden by",
    ("references", "out"): "References", ("references", "in"): "Referenced by",
    ("defines", "out"): "Defines", ("defines", "in"): "Defined in",
    ("imports", "out"): "Imports", ("imports", "in"): "Imported by",
    ("extends", "out"): "Extends", ("extends", "in"): "Extended by",
    ("implements", "out"): "Implements", ("implements", "in"): "Implemented by",
    ("exposes", "out"): "Exposes", ("exposes", "in"): "Exposed by",
    ("reads", "out"): "Reads", ("reads", "in"): "Read by",
    ("writes", "out"): "Writes", ("writes", "in"): "Written by",
    ("evidenced_by", "out"): "Evidence", ("evidenced_by", "in"): "Evidence for",
    ("governs", "out"): "Governs", ("governs", "in"): "Governed by",
    ("about", "out"): "About", ("about", "in"): "Learned about it",
    ("consulted", "out"): "Consulted", ("consulted", "in"): "Consulted by",
    ("reused", "out"): "Reused", ("reused", "in"): "Reused by",
    ("produced", "out"): "Produced", ("produced", "in"): "Produced by",
    ("proposed_by", "out"): "Proposed by", ("proposed_by", "in"): "Proposed",
    ("approved_by", "out"): "Approved by", ("approved_by", "in"): "Approved",
    ("supersedes", "out"): "Supersedes", ("supersedes", "in"): "Superseded by",
    ("corrects", "out"): "Corrects", ("corrects", "in"): "Corrected by",
    ("caused_by", "out"): "Caused by", ("caused_by", "in"): "Causes",
    ("fixed_by", "out"): "Fixed by", ("fixed_by", "in"): "Fixes",
    ("ruled_out", "out"): "Ruled out", ("ruled_out", "in"): "Ruled out by",
    ("changed", "out"): "Changed", ("changed", "in"): "Changed by",
    # The Context Graph's links (view_context.load_trace): a case file, a decision's citations.
    ("decided", "out"): "Decisions", ("decided", "in"): "Case",
    ("remedied_by", "out"): "Remediation", ("remedied_by", "in"): "Case",
    ("worked_on", "out"): "Worked on", ("worked_on", "in"): "Runs that worked on it",
    ("concerns", "out"): "Concerns", ("concerns", "in"): "Cases about it",
    ("cites", "out"): "Cites", ("cites", "in"): "Cited by",
    ("precedent", "out"): "Precedent", ("precedent", "in"): "Case",
}
#: Edge weight nouns for the pre-formatted label ("16,749 calls").
_NOUNS = {"calls": ("call", "calls"), "imports": ("import", "imports"),
          "extends": ("subtype", "subtypes"), "governs": ("rule", "rules"),
          "evidenced_by": ("rule", "rules"), "about": ("learning", "learnings"),
          "changed": ("learning", "learnings")}
#: Code attrs too large or internal for the panel (a file's parse facts).
#: source_id is a run's raw id: the run is named, and linked by "Open in Factory run".
_HIDDEN_ATTRS = frozenset({"facts", "names", "content_hash", "source_id"})
#: A file node's attrs are its parse facts: never read them where only the node is drawn.
_NODE_COLS = ("n.id, n.kind, n.node_key, n.name, n.trust_tier, n.path, n.repo, n.valid_to, "
              "n.created_by, n.recorded_at, n.first_seen, "
              "CASE WHEN n.kind = 'file' THEN '{}'::jsonb ELSE n.attrs END AS attrs")


# ── vocabulary ───────────────────────────────────────────────────────────────────────────────

def product_layer(kind: str) -> str:
    """The product layer of a kg_nodes kind (or a view-only kind: area, package, layer). The
    engine's scopes.product_layer when the rules stream has landed it; this mapping otherwise.
    People, agents and teams are the provenance learnings hang on: Learning."""
    from . import scopes

    shared = getattr(scopes, "product_layer", None)
    if callable(shared):
        try:
            layer = shared(kind)
        except Exception:  # noqa: BLE001 - fall back to the mapping below
            layer = None
        if layer in PRODUCT_LAYERS:
            return layer
    # An ontology pack's kind sits in the layer its pack names (design §4.3): a compliance case
    # in Context, a regulatory report in Domain, a system in Code.
    packed = (view_context.kind_meta(kind) or {}).get("layer")
    if packed in PRODUCT_LAYERS:
        return str(packed)
    if kind in ("area", "package") or kind in CODE_KINDS:
        return "code"
    if kind in RULE_KINDS:
        return "rules"
    if kind in DOMAIN_KINDS:
        return "domain"
    return "learning"


def parse_layers(value: Optional[str | Iterable[str]]) -> frozenset[str]:
    """The `layers=` chips: a comma list of code, rules, learning, domain, context. Empty or
    missing: all of them. An unknown name is refused in words."""
    if value is None:
        return DEFAULT_LAYERS
    names = [v.strip() for v in (value.split(",") if isinstance(value, str) else value) if v and v.strip()]
    if not names:
        return DEFAULT_LAYERS
    unknown = sorted(set(names) - DEFAULT_LAYERS)
    if unknown:
        raise ValueError(f"unknown layer {', '.join(unknown)}: use code, rules, learning, domain or context")
    return frozenset(names)


def layer_kinds(layer: str) -> frozenset[str]:
    """The kinds one chip shows: core kinds, and for Domain and Context every pack kind in it."""
    if layer in ("domain", "context"):
        return view_context.kinds_in(layer)
    return {"code": CODE_KINDS, "rules": RULE_KINDS, "learning": LEARNING_KINDS}.get(layer, frozenset())


def kinds_of(layers: Iterable[str], withheld: Iterable[str] = ()) -> list[str]:
    out: set[str] = set()
    for layer in layers:
        out |= layer_kinds(layer)
    return sorted(out - set(withheld))


def withheld_for(held_permissions: Iterable[str]) -> frozenset[str]:
    """The kinds a caller may not read, given the permissions they already hold.

    A kind listed in ``KIND_PERMISSIONS`` whose required permission is absent is withheld —
    never counted, never drawn (spec §3 "Permissions per layer"). ``knowledge:read`` is assumed
    (the route gates on it); only the per-kind extras here withhold rows. This is the pure form
    of the route's ``_withheld`` (apps/api/app/routers/knowledge_view.py), so the decision/
    precedent withholding mapping is pinned under the engine suite the code-task gate runs, not
    only in the apps/api wire test the gate cannot execute."""
    held = set(held_permissions)
    return frozenset(
        kind for kind, permission in KIND_PERMISSIONS.items() if permission not in held
    )


def _readable(kind: str, layers: Iterable[str], withheld: Iterable[str]) -> bool:
    if kind in set(withheld):
        return False
    layers = set(layers)
    # The runs, precedents and incidents a trace hangs on are part of the Context picture too.
    return product_layer(kind) in layers or ("context" in layers and kind in view_context.CONTEXT_ANCHORS)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def _attrs(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def edge_label(kind: str, weight: int, *, rollup: bool = True) -> Optional[str]:
    """ "16,749 calls"; None for a single raw edge, where there is nothing to say."""
    if not rollup and weight <= 1:
        return None
    if kind in view_context.TRACE_EDGES:
        return None     # a trace link's weight ranks it; it counts nothing

    one, many = _NOUNS.get(kind, (kind.replace("_", " "), kind.replace("_", " ")))
    return f"{weight:,} {one if weight == 1 else many}"


def rule_status(row: Mapping[str, Any]) -> str:
    status = _attrs(row.get("attrs")).get("status")
    if status in RULE_STATUSES:
        return str(status)
    if row.get("valid_to") is not None:
        return "rejected"
    return "approved" if row.get("trust_tier") == "approved" else "proposed"


def _symbol_kind(attrs: Mapping[str, Any]) -> Optional[str]:
    kind = attrs.get("type_kind") or attrs.get("symbol_kind")
    return str(kind) if kind in ("class", "interface", "enum", "record", "method", "function") else None


def node_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """A kg_nodes row as KgViewNode."""
    kind = str(row["kind"])
    attrs = _attrs(row.get("attrs"))
    layer = product_layer(kind)
    name = str(row.get("name") or row.get("node_key") or row["id"])
    out: dict[str, Any] = {"id": row["id"], "key": row.get("node_key") or row["id"], "kind": kind,
                           "layer": layer, "label": name,
                           "trust_tier": row.get("trust_tier") or "observed"}
    if kind == "symbol":
        sk = _symbol_kind(attrs)
        if sk:
            out["symbol_kind"] = sk
        if attrs.get("symbol_kind") in ("method", "function"):
            out["label"] = name.rsplit(".", 1)[-1] + "()"
        if row.get("path"):
            out["sub"] = row["path"]
    elif kind == "file":
        out["label"] = name.rsplit("/", 1)[-1] or name
        if row.get("path"):
            out["sub"] = row["path"]
    elif layer == "rules":
        status = rule_status(row)
        out["status"] = status
        category = attrs.get("category")
        out["sub"] = f"{status} · {category}" if category else status
    elif kind == "person":
        # Tester round 4: an "Approved by" node literally named "ceo" (the legacy member key).
        named = str(attrs.get("name") or "").strip()
        if named:
            out["label"] = named
        elif name == str(attrs.get("ref") or name) or name.startswith(("usr_", "prn_")) or name == "ceo":
            out["label"] = OPERATOR
    elif kind == "run" and attrs.get("subtype") == "conversation":
        # A conversation is a run of its own (learning_conversations), named after its question
        # and when it started: "Conversation with Site Reliability Engineer".
        who = attrs.get("agent_name") or attrs.get("agent_role")
        out["sub"] = f"Conversation with {who}" if who else "Conversation"
    elif layer == "learning":
        prov = _attrs(attrs.get("provenance"))
        who = prov.get("agent_name") or prov.get("agent_role")
        parts = [str(p) for p in (who, prov.get("step")) if p]
        out["sub"] = " · ".join(parts) if parts else kind.replace("_", " ")
    if kind == "run":
        # A run node keeps the name it was given: one the old builder cut mid-word is mended.
        out["label"] = repair_cut(name)
        if out.get("sub") == "run":
            out["sub"] = "Workflow run" if str(row.get("node_key") or "").startswith("run:wfr_") else "Run"
    if layer == "context":
        label, sub = view_context.display(kind, name, attrs)
        out["label"] = label
        if sub:
            out["sub"] = sub
        elif "sub" in out and out["sub"] == kind.replace("_", " "):
            out.pop("sub")
    return out


def _rollup_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """An area or package tree row as KgViewNode (kind area | package; id = the rollup key)."""
    level = row["level"]
    classes, methods = int(row["class_count"]), int(row["method_count"])
    counts = {"classes": classes, "methods": methods}
    if level == "area":
        counts["packages"] = int(row["child_count"])
    return {"id": row["key"], "key": row["key"], "kind": level, "layer": "code",
            "label": row["label"], "sub": rollup_sub(classes, methods, int(row["child_count"] or 0), level),
            "trust_tier": "observed", "counts": counts}


def rollup_sub(classes: int, functions: int, children: int, level: str) -> str:
    """What an area or package holds, in one phrase. A Python or JavaScript area with no classes
    says its functions ("24 functions"), never "0 classes" (Graph area round 2)."""
    def n(v: int, one: str, many: str) -> str:
        return f"{v:,} {one if v == 1 else many}"
    if classes:
        return n(classes, "class", "classes")
    if functions:
        return n(functions, "function", "functions")
    if children:
        return n(children, "package", "packages") if level == "area" else n(children, "module", "modules")
    return "empty"


def _nodes_by_id(connection: Connection, project_id: str, ids: Iterable[str]) -> dict[str, dict]:
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        return {}
    rows = connection.execute(text(
        f"SELECT {_NODE_COLS} FROM kg_nodes n WHERE n.project_id = :p AND n.id = ANY(:ids)"),
        {"p": project_id, "ids": ids}).mappings().all()
    return {r["id"]: dict(r) for r in rows}


def _tree_rows(connection: Connection, project_id: str, keys: Iterable[str]) -> dict[str, dict]:
    keys = list(dict.fromkeys(k for k in keys if k))
    if not keys:
        return {}
    rows = connection.execute(text(
        "SELECT * FROM kg_view_tree WHERE project_id = :p AND key = ANY(:k)"),
        {"p": project_id, "k": keys}).mappings().all()
    return {r["key"]: dict(r) for r in rows}


def _tree_row_of_node(connection: Connection, project_id: str, node: Mapping[str, Any]) -> Optional[dict]:
    """A symbol's tree row: class and method rows are keyed by the symbol's node_key."""
    if node.get("kind") != "symbol":
        return None
    row = connection.execute(text(
        "SELECT * FROM kg_view_tree WHERE project_id = :p AND key = :k"),
        {"p": project_id, "k": node["node_key"]}).mappings().first()
    return dict(row) if row else None


def _area_key(row: Mapping[str, Any]) -> str:
    return f"area:{row['repo']}:{row['area']}"


def _package_key(row: Mapping[str, Any]) -> str:
    return f"pkg:{row['repo']}:{row['area']}:{row['package']}"


def _is_rollup_key(value: str) -> bool:
    return value.startswith("area:") or value.startswith("pkg:")


# ── overview ─────────────────────────────────────────────────────────────────────────────────

def overview(connection: Connection, project_id: str, *, layers: Iterable[str] = DEFAULT_LAYERS,
             repo: Optional[str] = None, area: Optional[str] = None,
             withheld: Iterable[str] = ()) -> dict[str, Any]:
    """Whole repo (KgOverviewResponse): area bubbles with weighted call edges, the Rules and
    Learning bubbles and their edges to areas. With ``area``, the packages of that area."""
    layers, withheld = frozenset(layers), frozenset(withheld)
    p = {"p": project_id}
    # Code in the graph and no map of it (a project set up without an index's end, a rebuild that
    # failed): the map is built now, before it is read — never "No code yet" over 51k nodes.
    mapped = view_rollup.ensure_built(connection, project_id)
    repos = [r[0] for r in connection.execute(text(
        "SELECT repo FROM kg_view_tree WHERE project_id = :p AND parent_key IS NULL AND level = 'area' "
        "GROUP BY repo ORDER BY sum(class_count) DESC, repo"), p).all()]
    if repo is not None and repo != "" and repo not in repos:
        raise LookupError(f"repository {repo} has no code graph in this project")
    chosen = repo or (repos[0] if repos else None)
    level, parent = "area", None
    if area:
        key = area if area.startswith("area:") else f"area:{chosen or ''}:{area}"
        found = _tree_rows(connection, project_id, [key]).get(key)
        if not found or found["level"] != "area":
            raise LookupError(f"area {area} is not in this project's code graph")
        level, parent, chosen = "package", key, found["repo"]
        rows = connection.execute(text(
            "SELECT * FROM kg_view_tree WHERE project_id = :p AND parent_key = :k AND level = 'package' "
            "ORDER BY class_count DESC, sort_key"), {**p, "k": key}).mappings().all()
    else:
        rows = connection.execute(text(
            "SELECT * FROM kg_view_tree WHERE project_id = :p AND parent_key IS NULL AND level = 'area' "
            "AND repo = :r ORDER BY class_count DESC, sort_key"), {**p, "r": chosen or ""}).mappings().all()
    areas = [_rollup_payload(r) for r in rows] if "code" in layers else []
    keys = [a["id"] for a in areas]
    edges: list[dict[str, Any]] = []
    if keys:
        for src, dst, kind, weight in connection.execute(text(
                "SELECT src_key, dst_key, kind, weight FROM kg_view_edges WHERE project_id = :p "
                "AND level = :l AND kind = 'calls' AND src_key = ANY(:k) AND dst_key = ANY(:k) "
                "ORDER BY weight DESC, src_key, dst_key LIMIT :cap"),
                {**p, "l": level, "k": keys, "cap": OVERVIEW_EDGE_CAP}).all():
            edges.append({"src": src, "dst": dst, "kind": kind, "weight": int(weight),
                          "label": edge_label(kind, int(weight))})

    bubbles: list[dict[str, Any]] = []
    target_sql = "'area:' || t.repo || ':' || t.area" if level == "area" else \
        "'pkg:' || t.repo || ':' || t.area || ':' || t.package"
    if "rules" in layers:
        # The one count (rules.rule_counts): business and data-quality rules, as Rules review.
        from .rules import rule_count_words, rule_counts

        found = rule_counts(connection, project_id)
        counts = {s: int(found.get(s) or 0) for s in RULE_STATUSES}
        bubbles.append({"id": "layer:rules", "key": "layer:rules", "kind": "layer", "layer": "rules",
                        "label": "Rules",
                        "sub": rule_count_words(counts) if sum(counts.values()) else "No rules yet",
                        "trust_tier": "observed", "counts": counts})
        edges += _layer_edges(connection, project_id, "layer:rules", ["rule"], ("governs", "evidenced_by"),
                              target_sql, keys)
    if "learning" in layers:
        learned = [k for k in LEARNED_KINDS if k not in withheld]
        found = dict(connection.execute(text(
            "SELECT kind, count(*) FROM kg_nodes WHERE project_id = :p AND kind = ANY(:k) "
            "AND valid_to IS NULL GROUP BY kind"), {**p, "k": learned}).all())
        counts = {k: int(found[k]) for k in learned if found.get(k)}
        total = sum(counts.values())
        bubbles.append({"id": "layer:learning", "key": "layer:learning", "kind": "layer",
                        "layer": "learning", "label": "Learning",
                        "sub": f"{total:,} learned" if total else "Nothing learned yet",
                        "trust_tier": "observed", "counts": counts})
        edges += _layer_edges(connection, project_id, "layer:learning",
                              [k for k in LEARNING_KINDS if k not in withheld], ("about", "changed"),
                              target_sql, keys)
    kinds = view_context.legend(connection, project_id)
    for layer in ("domain", "context"):
        if layer not in layers:
            continue
        bubble = _pack_bubble(connection, project_id, layer, withheld, kinds,
                              alone=layers == frozenset({layer}))
        if bubble:
            bubbles.append(bubble)
    edges += _bubble_links(connection, project_id, {b["id"] for b in bubbles}, withheld)

    sync = view_rollup.sync_row(connection, project_id)
    live = view_rollup.live_code_count(connection, project_id)
    edges_total = int(sync["edges"]) if sync else int(connection.execute(text(
        "SELECT count(*) FROM kg_edges WHERE project_id = :p AND valid_to IS NULL"), p).scalar() or 0)
    return {"project_id": project_id, "repo": chosen, "repos": repos, "level": level, "parent": parent,
            "areas": areas, "layers": bubbles, "edges": edges,
            "totals": {"nodes": live, "edges": edges_total},
            "built_at": _iso(sync["built_at"]) if sync else None,
            "stale": (sync is None and live > 0) or (sync is not None and int(sync["nodes"]) != live),
            "kinds": kinds,
            "map": mapped}


_BUBBLE_LABEL = {"domain": "Domain", "context": "Context"}
#: What a project holding a pack sees first in the Domain and Context bubbles: the pack's own
#: story, not whichever kind has the most rows (a DQ project opened on "190 DQ thresholds ·
#: 173 database columns" and on AML cases from another pack). Core kinds a pack's story hangs
#: on (a DQ incident) are counted in its layer here.
PACK_HEADLINES: dict[str, dict[str, tuple[str, ...]]] = {
    "banking-dq": {
        "domain": ("data_quality_rule", "cde", "database_table", "database_column", "system", "feed"),
        "context": ("incident", "exception_group", "remediation_package", "agent_decision"),
    },
}
#: How many kinds a bubble names: a pack's headline in full, else the three largest.
HEADLINE_PARTS = 6


def _headline(connection: Connection, project_id: str, layer: str) -> tuple[str, ...]:
    """The headline kinds of every pack the project holds, for ``layer`` (in pack order)."""
    from .ontology_packs import get_project_pack

    try:
        with connection.begin_nested():
            pack = get_project_pack(connection, project_id=project_id)
    except Exception:  # noqa: BLE001 - no pack read: the bubble orders by size
        return ()
    held = (pack.pack_ids or (pack.pack_id,)) if pack is not None else ()
    out: list[str] = []
    for pack_id in held:
        for kind in PACK_HEADLINES.get(pack_id, {}).get(layer, ()):
            if kind not in out:
                out.append(kind)
    return tuple(out)
_BUBBLE_EMPTY = {"domain": "No regulations or documents yet", "context": "No agent decisions yet"}


def _mid_sentence(label: str) -> str:
    """A pack's label as it reads inside a sentence: "Compliance case" → "compliance case", but
    an acronym keeps its capitals ("AML case" stays "AML case")."""
    head, _, rest = label.partition(" ")
    if len(head) > 1 and head.isupper():
        return label
    return label[:1].lower() + label[1:]


def _pack_bubble(connection: Connection, project_id: str, layer: str, withheld: frozenset[str],
                 kinds: Mapping[str, Mapping[str, Any]], *, alone: bool) -> Optional[dict[str, Any]]:
    """The Domain or the Context bubble: how many of each of its kinds the project holds, said
    by the pack's plural ("2 clauses · 1 document"). Drawn when it holds something, or when it is
    the one chip on (so the empty state can say so)."""
    headline = tuple(k for k in _headline(connection, project_id, layer) if k not in withheld)
    members = sorted((layer_kinds(layer) | set(headline)) - withheld)
    found = dict(connection.execute(text(
        "SELECT kind, count(*) FROM kg_nodes WHERE project_id = :p AND kind = ANY(:k) "
        "AND valid_to IS NULL GROUP BY kind"), {"p": project_id, "k": members}).all()) if members else {}
    counts = {k: int(found[k]) for k in members if found.get(k)}
    if not counts and not alone:
        return None

    def said(kind: str, n: int) -> str:
        meta = kinds.get(kind) or view_context.kind_meta(kind) or {}
        one = _mid_sentence(str(meta.get("label") or kind.replace("_", " ")))
        many = _mid_sentence(str(meta.get("plural") or one + "s"))
        return f"{n:,} {one if n == 1 else many}"

    # The kinds a person looks for first: cases and documents, then the agent decisions that
    # worked them, then the rest by size ("2 AML cases · 2 compliance cases · 94 agent decisions").
    def first(k: str) -> int:
        return 0 if k.endswith("_case") or k in ("document", "regulation") else 1 if k == "agent_decision" else 2

    if any(k in counts for k in headline):
        # The pack's story first, in its order; the rest by size after it.
        lead = [k for k in headline if k in counts]
        order = lead + sorted((k for k in counts if k not in lead), key=lambda k: (first(k), -counts[k], k))
        shown = order[:max(HEADLINE_PARTS, len(lead))]
    else:
        order = sorted(counts, key=lambda k: (first(k), -counts[k], k))
        shown = order[:3]
    sub = " · ".join(said(k, counts[k]) for k in shown) or _BUBBLE_EMPTY[layer]
    return {"id": f"layer:{layer}", "key": f"layer:{layer}", "kind": "layer", "layer": layer,
            "label": _BUBBLE_LABEL[layer], "sub": sub, "trust_tier": "observed", "counts": counts}


def _bubble_links(connection: Connection, project_id: str, drawn: set[str],
                  withheld: frozenset[str]) -> list[dict[str, Any]]:
    """The two graphs joined at Whole repo: how many links run between the Domain, Context,
    Rules and Learning bubbles (a clause implemented by a rule, a decision citing a rule, a case
    about a clause, a run that worked a case)."""
    pairs = [(a, b) for a in ("domain", "context") for b in ("rules", "learning", "domain", "context")
             if a != b and f"layer:{a}" in drawn and f"layer:{b}" in drawn]
    if not pairs:
        return []
    counts: dict[tuple[str, str], int] = defaultdict(int)
    by_kind = {k: layer for layer in ("rules", "learning", "domain", "context") for k in layer_kinds(layer) - withheld}
    for src_kind, dst_kind, n in connection.execute(text(
            "SELECT s.kind, d.kind, count(*) FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id AND s.valid_to IS NULL "
            "JOIN kg_nodes d ON d.id = e.dst_id AND d.valid_to IS NULL WHERE e.project_id = :p AND e.valid_to IS NULL "
            "AND s.kind = ANY(:k) AND d.kind = ANY(:k) GROUP BY 1, 2"), {"p": project_id, "k": sorted(by_kind)}).all():
        a, b = by_kind.get(src_kind), by_kind.get(dst_kind)
        if a and b and a != b and {a, b} & {"domain", "context"}:
            counts[tuple(sorted((a, b)))] += int(n)
    if "layer:context" in drawn:
        trace = view_context.load_trace(connection, project_id, withheld)
        other_ids = [d for _s, d, k, _w in trace.edges if k in ("concerns", "cites")]
        known = dict(connection.execute(text(
            "SELECT id, kind FROM kg_nodes WHERE project_id = :p AND id = ANY(:i)"),
            {"p": project_id, "i": sorted(set(other_ids))}).all()) if other_ids else {}
        for _s, d, k, _w in trace.edges:
            b = by_kind.get(known.get(d, ""))
            if k in ("concerns", "cites") and b and b != "context":
                counts[tuple(sorted(("context", b)))] += 1
            elif k == "worked_on" and "learning" in by_kind.values():
                counts[("context", "learning")] += 1
    out = []
    for (a, b), n in sorted(counts.items(), key=lambda x: -x[1]):
        if f"layer:{a}" in drawn and f"layer:{b}" in drawn and n:
            src, dst = (f"layer:{a}", f"layer:{b}") if a in ("domain", "context") else (f"layer:{b}", f"layer:{a}")
            out.append({"src": src, "dst": dst, "kind": "about", "weight": n,
                        "label": f"{n:,} {'link' if n == 1 else 'links'}"})
    return out


def _layer_edges(connection: Connection, project_id: str, bubble: str, kinds: list[str],
                 edge_kinds: tuple[str, ...], target_sql: str, keys: list[str]) -> list[dict[str, Any]]:
    """Layer bubble -> the drawn areas (or packages): how many of the layer's live items link to
    code inside each, through ``edge_kinds`` (either direction)."""
    if not keys or not kinds:
        return []
    rows = connection.execute(text(
        f"""
        SELECT target, kind, count(DISTINCT item) AS n FROM (
          SELECT {target_sql} AS target, e.kind, i.id AS item
          FROM kg_nodes i
          JOIN kg_edges e ON e.src_id = i.id AND e.valid_to IS NULL AND e.kind = ANY(:ek)
          JOIN kg_nodes s ON s.id = e.dst_id AND s.kind = 'symbol' AND s.valid_to IS NULL
          JOIN kg_view_tree t ON t.project_id = :p AND t.key = s.node_key
          WHERE i.project_id = :p AND i.kind = ANY(:k) AND i.valid_to IS NULL
          UNION ALL
          SELECT {target_sql}, e.kind, i.id
          FROM kg_nodes i
          JOIN kg_edges e ON e.dst_id = i.id AND e.valid_to IS NULL AND e.kind = ANY(:ek)
          JOIN kg_nodes s ON s.id = e.src_id AND s.kind = 'symbol' AND s.valid_to IS NULL
          JOIN kg_view_tree t ON t.project_id = :p AND t.key = s.node_key
          WHERE i.project_id = :p AND i.kind = ANY(:k) AND i.valid_to IS NULL
        ) x WHERE target = ANY(:keys) GROUP BY target, kind
        """), {"p": project_id, "k": kinds, "ek": list(edge_kinds), "keys": keys}).all()
    merged: dict[str, dict[str, int]] = defaultdict(dict)
    for target, kind, n in rows:
        merged[target][kind] = int(n)
    out = []
    for target in sorted(merged, key=lambda t: -max(merged[t].values())):
        kind = max(merged[target], key=lambda k: merged[target][k])
        weight = max(merged[target].values())
        out.append({"src": bubble, "dst": target, "kind": kind, "weight": weight,
                    "label": edge_label(kind, weight)})
    return out


# ── neighbourhood ────────────────────────────────────────────────────────────────────────────

def neighbourhood(connection: Connection, project_id: str, node: str, *, depth: int = 1,
                  layers: Iterable[str] = DEFAULT_LAYERS, limit: int = 80,
                  withheld: Iterable[str] = ()) -> dict[str, Any]:
    """Focus (KgNeighbourhoodResponse): the node and what it connects to within ``depth`` hops,
    at most ``limit`` neighbours, the strongest first; edges to layers outside ``layers`` dropped.

    - an area, package or class walks ``kg_view_edges`` at its level, both directions;
    - a method walks ``kg_edges`` calls / dispatches_to / overrides, with its class as context;
    - anything else (rules, learnings, files, endpoints) walks ``kg_edges`` of any kind.
    Rules and learnings attached to a code centre (or to anything inside it) join at hop 1."""
    if not node or not str(node).strip():
        raise ValueError("node is required")
    depth, limit = int(depth), int(limit)
    if not 1 <= depth <= MAX_DEPTH:
        raise ValueError(f"depth is 1 to {MAX_DEPTH}")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit is 1 to {MAX_LIMIT}")
    layers, withheld = frozenset(layers), frozenset(withheld)
    walk = _Walk(connection, project_id, depth=depth, limit=limit, layers=layers, withheld=withheld)

    if _is_rollup_key(node):
        row = _tree_rows(connection, project_id, [node]).get(node)
        if not row or row["level"] not in ("area", "package"):
            raise LookupError(f"{node} is not in this project's code graph")
        center = _rollup_payload(row)
        walk.rollup(row["level"], row["key"], center)
        walk.attached(row, center["id"])
        return walk.result(center, row["level"])

    if node.startswith(view_context.VIRTUAL_RUN):
        center = view_context.virtual_run_payload(walk.context(), node)
        if not center:
            raise LookupError("that run is no longer in this project's graph")
        walk.graph(center, kinds=None)
        return walk.result(center, "graph")
    found = _nodes_by_id(connection, project_id, [node]).get(node)
    if not found:
        raise LookupError(f"node {node} is not in this project's graph")
    if found["kind"] in withheld:
        raise PermissionError(f"reading a {found['kind']} needs {KIND_PERMISSIONS.get(found['kind'], 'more access')}")
    center = node_payload(found)
    tree = _tree_row_of_node(connection, project_id, found)
    if tree and tree["level"] == "class":
        walk.rollup("class", tree["key"], center)
        walk.attached(tree, center["id"])
        out = walk.result(center, "class")
        # What it holds, so a class whose methods only call each other still says so.
        counts = {"methods": int(tree.get("method_count") or 0)}
        out["center"]["counts"] = counts
        for n in out["nodes"]:
            if n["id"] == center["id"]:
                n["counts"] = counts
        return out
    if tree and tree["level"] == "method":
        owner = None
        if tree["parent_key"] and not _is_rollup_key(tree["parent_key"]):
            owner = _tree_rows(connection, project_id, [tree["parent_key"]]).get(tree["parent_key"])
        walk.graph(center, kinds=("calls", "dispatches_to", "overrides"),
                   context=owner["node_id"] if owner else None)
        return walk.result(center, "method")
    walk.graph(center, kinds=None)
    return walk.result(center, "graph")


class _Walk:
    """One neighbourhood walk: the kept nodes with their hop, the edges, what the limit hid."""

    def __init__(self, connection: Connection, project_id: str, *, depth: int, limit: int,
                 layers: frozenset[str], withheld: frozenset[str]) -> None:
        self.c, self.p = connection, project_id
        self.depth, self.limit, self.layers, self.withheld = depth, limit, layers, withheld
        self.hops: dict[str, int] = {}
        self.payloads: dict[str, dict[str, Any]] = {}
        self.edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.hidden_nodes = 0
        self.hidden_edges = 0
        #: consulted edges whose use was a citation (attrs.how = cited): drawn as "cited".
        self.cited: set[tuple[str, str, str]] = set()
        self._trace: Optional[view_context.Trace] = None

    def context(self) -> view_context.Trace:
        """The project's Context Graph and the links it implies, read once per walk."""
        if self._trace is None:
            self._trace = view_context.load_trace(self.c, self.p, self.withheld)
        return self._trace

    def _trace_rows(self, frontier: set[str]) -> list[tuple[str, str, str, int, str, None]]:
        """The trace's links touching the frontier, as kg_edges rows (src, dst, kind, weight,
        other kind, how): a case's runs, decisions and remediation, a decision's citations."""
        trace = self.context()
        rows = trace.touching(frontier)
        if not rows:
            return []
        unknown = {o for s, d, _k, _w in rows for o in (s, d) if o not in trace.kinds}
        if unknown:
            for nid, kind in self.c.execute(text(
                    "SELECT id, kind FROM kg_nodes WHERE project_id = :p AND id = ANY(:i) AND valid_to IS NULL"),
                    {"p": self.p, "i": sorted(unknown)}).all():
                trace.kinds[nid] = kind
        out = []
        for s, d, k, w in rows:
            other = d if s in frontier else s
            kind = trace.kinds.get(other)
            if kind:
                out.append((s, d, k, w, kind, None))
        return out

    @property
    def room(self) -> int:
        return self.limit - (len(self.hops) - 1)

    def _keep(self, frontier: set[str], found: list[tuple[str, str, str, int]], hop: int,
              *, rollup: bool, per_hop: int) -> list[str]:
        """From the edges found around ``frontier``, keep the strongest new neighbours."""
        weight: dict[str, int] = defaultdict(int)
        by_other: dict[str, list[tuple[str, str, str, int]]] = defaultdict(list)
        for src, dst, kind, w in found:
            if src in frontier and dst in frontier:
                self._edge(src, dst, kind, w, rollup)
                continue
            other = dst if src in frontier else src
            if other in self.hops:
                self._edge(src, dst, kind, w, rollup)
                continue
            weight[other] += w
            by_other[other].append((src, dst, kind, w))
        ranked = sorted(weight, key=lambda o: (-weight[o], o))
        take = ranked[:max(0, min(per_hop, self.room))]
        for dropped in ranked[len(take):]:
            self.hidden_nodes += 1
            self.hidden_edges += len(by_other[dropped])
        for other in take:
            self.hops[other] = hop
            for src, dst, kind, w in by_other[other]:
                self._edge(src, dst, kind, w, rollup)
        return take

    def _edge(self, src: str, dst: str, kind: str, weight: int, rollup: bool) -> None:
        key = (src, dst, kind)
        if key in self.edges:
            return
        self.edges[key] = {"src": src, "dst": dst, "kind": kind, "weight": int(weight),
                           "label": edge_label(kind, int(weight), rollup=rollup)}

    # A rollup level: kg_view_edges, keys mapped to payload ids at the end.
    def rollup(self, level: str, center_key: str, center: dict[str, Any]) -> None:
        self.level_keys: dict[str, str] = {center_key: center["id"]}
        self.hops[center_key] = 0
        if "code" not in self.layers:
            self._finish_rollup(level)
            return
        frontier = {center_key}
        for hop in range(1, self.depth + 1):
            if not frontier or self.room <= 0:
                break
            found = [(s, d, k, int(w)) for s, d, k, w in self.c.execute(text(
                "SELECT src_key, dst_key, kind, weight FROM kg_view_edges WHERE project_id = :p "
                "AND level = :l AND src_key = ANY(:f) UNION ALL "
                "SELECT src_key, dst_key, kind, weight FROM kg_view_edges WHERE project_id = :p "
                "AND level = :l AND dst_key = ANY(:f) AND NOT (src_key = ANY(:f))"),
                {"p": self.p, "l": level, "f": list(frontier)}).all()]
            # What a hop leaves unused is the next hop's (tester round 4: Show 200 read 147 of 809
            # because each of 2 hops was held to 100 while the first had only 47).
            per_hop = max(1, self.room // (self.depth - hop + 1))
            frontier = set(self._keep(frontier, found, hop, rollup=True, per_hop=per_hop))
        self._finish_rollup(level)

    def _finish_rollup(self, level: str) -> None:
        keys = list(self.hops)
        rows = _tree_rows(self.c, self.p, keys)
        if level == "class":
            nodes = _nodes_by_id(self.c, self.p, [r["node_id"] for r in rows.values()])
            for key, row in rows.items():
                found = nodes.get(row["node_id"])
                if found:
                    self.level_keys[key] = found["id"]
                    self.payloads[found["id"]] = node_payload(found)
        else:
            for key, row in rows.items():
                self.level_keys[key] = key
                self.payloads[key] = _rollup_payload(row)
        # Re-key hops and edges from rollup keys to payload ids; drop what did not resolve.
        hops = {}
        for key, hop in self.hops.items():
            if key in self.level_keys and self.level_keys[key] in self.payloads or hop == 0:
                hops[self.level_keys.get(key, key)] = hop
        edges = {}
        for (src, dst, kind), edge in self.edges.items():
            s, d = self.level_keys.get(src), self.level_keys.get(dst)
            if s in hops and d in hops:
                edges[(s, d, kind)] = {**edge, "src": s, "dst": d}
        self.hops, self.edges = hops, edges

    def attached(self, row: Mapping[str, Any], center_id: str) -> None:
        """Rules and learnings linked to the centre or to anything inside it (a class's methods,
        an area's or package's symbols), lifted onto the centre, at hop 1."""
        kinds = [k for k in kinds_of(self.layers - {"code"}, self.withheld)]
        if not kinds or self.room <= 0:
            return
        if row["level"] == "area":
            where, params = "t.repo = :r AND t.area = :a", {"r": row["repo"], "a": row["area"]}
        elif row["level"] == "package":
            where, params = ("t.repo = :r AND t.area = :a AND t.package = :pk",
                             {"r": row["repo"], "a": row["area"], "pk": row["package"]})
        else:
            where, params = "(t.key = :k OR t.parent_key = :k)", {"k": row["key"]}
        # Driven from the rules and learnings (few) through the edge indexes to the tree rows.
        found = self.c.execute(text(
            f"""
            SELECT i.id, e.kind, count(*) AS n, true AS outgoing FROM kg_nodes i
            JOIN kg_edges e ON e.src_id = i.id AND e.valid_to IS NULL
            JOIN kg_nodes s ON s.id = e.dst_id AND s.kind = 'symbol' AND s.valid_to IS NULL
          JOIN kg_view_tree t ON t.project_id = :p AND t.key = s.node_key
            WHERE i.project_id = :p AND i.kind = ANY(:kinds) AND i.valid_to IS NULL AND {where}
            GROUP BY i.id, e.kind
            UNION ALL
            SELECT i.id, e.kind, count(*), false FROM kg_nodes i
            JOIN kg_edges e ON e.dst_id = i.id AND e.valid_to IS NULL
            JOIN kg_nodes s ON s.id = e.src_id AND s.kind = 'symbol' AND s.valid_to IS NULL
          JOIN kg_view_tree t ON t.project_id = :p AND t.key = s.node_key
            WHERE i.project_id = :p AND i.kind = ANY(:kinds) AND i.valid_to IS NULL AND {where}
            GROUP BY i.id, e.kind
            ORDER BY 3 DESC LIMIT 400
            """), {"p": self.p, "kinds": kinds, **params}).all()
        ranked: dict[str, int] = defaultdict(int)
        for item, _kind, n, _out in found:
            ranked[item] += int(n)
        order = sorted(ranked, key=lambda i: (-ranked[i], i))
        take = order[:self.room]
        self.hidden_nodes += len(order) - len(take)
        nodes = _nodes_by_id(self.c, self.p, take)
        for item, kind, n, outgoing in found:
            if item not in nodes:
                continue
            self.hops.setdefault(item, 1)
            self.payloads[item] = node_payload(nodes[item])
            src, dst = (item, center_id) if outgoing else (center_id, item)
            self._edge(src, dst, kind, int(n), rollup=True)

    # kg_edges: a method's call graph, or any kind around a rule, learning or other node.
    def graph(self, center: dict[str, Any], *, kinds: Optional[tuple[str, ...]],
              context: Optional[str] = None) -> None:
        cid = center["id"]
        self.hops[cid] = 0
        self.payloads[cid] = center
        if context:
            owner = _nodes_by_id(self.c, self.p, [context]).get(context)
            if owner and _readable(owner["kind"], self.layers, self.withheld):
                self.hops[context] = 1
                self.payloads[context] = node_payload(owner)
                self._edge(context, cid, "defines", 1, rollup=False)
        frontier = {cid}
        kind_sql = "AND e.kind = ANY(:k)" if kinds else ""
        for hop in range(1, self.depth + 1):
            if not frontier or self.room <= 0:
                break
            rows = self.c.execute(text(
                f"""
                SELECT e.src_id, e.dst_id, e.kind, e.weight, o.kind AS other_kind, e.attrs->>'how' AS how FROM kg_edges e
                JOIN kg_nodes o ON o.id = e.dst_id AND o.valid_to IS NULL
                WHERE e.project_id = :p AND e.valid_to IS NULL AND e.src_id = ANY(:f) {kind_sql}
                UNION ALL
                SELECT e.src_id, e.dst_id, e.kind, e.weight, o.kind, e.attrs->>'how' FROM kg_edges e
                JOIN kg_nodes o ON o.id = e.src_id AND o.valid_to IS NULL
                WHERE e.project_id = :p AND e.valid_to IS NULL AND e.dst_id = ANY(:f)
                  AND NOT (e.src_id = ANY(:f)) {kind_sql}
                """), {"p": self.p, "f": list(frontier), "k": list(kinds or ())}).all()
            if kinds is None:
                rows = list(rows) + self._trace_rows(frontier)
            # A consultation the agent CITED in its answer says so on the canvas ("cited").
            self.cited.update((s, d, k) for s, d, k, _w, _o, how in rows if k == "consulted" and how == "cited")
            found = [(s, d, k, max(1, int(round(float(w or 1)))))
                     for s, d, k, w, other, _how in rows if _readable(other, self.layers, self.withheld)
                     or (s in frontier and d in frontier)]
            # What a hop leaves unused is the next hop's (tester round 4: Show 200 read 147 of 809
            # because each of 2 hops was held to 100 while the first had only 47).
            per_hop = max(1, self.room // (self.depth - hop + 1))
            frontier = set(self._keep(frontier, found, hop, rollup=False, per_hop=per_hop))
        missing = [i for i in self.hops if i not in self.payloads]
        for nid in [i for i in missing if i.startswith(view_context.VIRTUAL_RUN)]:
            virtual = view_context.virtual_run_payload(self.context(), nid)
            if virtual:
                self.payloads[nid] = virtual
        for nid, row in _nodes_by_id(self.c, self.p, missing).items():
            self.payloads[nid] = node_payload(row)

    def result(self, center: dict[str, Any], level: str) -> dict[str, Any]:
        cid = center["id"]
        self.payloads.setdefault(cid, center)
        self.hops[cid] = 0
        ordered = sorted((i for i in self.hops if i in self.payloads), key=lambda i: (self.hops[i], i != cid))
        nodes = [{**self.payloads[i], "hop": self.hops[i]} for i in ordered]
        ids = {n["id"] for n in nodes}
        edges = sorted(({**e, "how": "cited"} if (e["src"], e["dst"], e["kind"]) in self.cited else e
                        for e in self.edges.values() if e["src"] in ids and e["dst"] in ids),
                       key=lambda e: (-e["weight"], e["src"], e["dst"], e["kind"]))
        return {"center": {**self.payloads[cid], "hop": 0}, "nodes": nodes, "edges": edges,
                "depth": self.depth, "limit": self.limit, "level": level,
                "truncated": self.hidden_nodes > 0,
                "hidden": {"nodes": self.hidden_nodes, "edges": self.hidden_edges}}


# ── node detail ──────────────────────────────────────────────────────────────────────────────

def node_detail(connection: Connection, project_id: str, node_id: str, *, edge: Optional[str] = None,
                direction: Optional[str] = None, offset: int = 0, withheld: Iterable[str] = (),
                settings: Any = None) -> dict[str, Any]:
    """The right panel (KgNodeDetailResponse). ``edge`` + ``direction`` + ``offset`` page one
    connection group (20 at a time)."""
    withheld = frozenset(withheld)
    if direction not in (None, "", "out", "in"):
        raise ValueError("direction is out or in")
    if edge and not direction:
        raise ValueError("a connection page names its direction: out or in")
    offset = max(0, int(offset or 0))
    if _is_rollup_key(node_id):
        return _rollup_detail(connection, project_id, node_id, edge=edge, direction=direction, offset=offset)
    if node_id.startswith(view_context.VIRTUAL_RUN):
        return _virtual_run_detail(connection, project_id, node_id, withheld)
    rows = connection.execute(text(
        "SELECT n.*, CASE WHEN n.kind = 'file' THEN n.attrs - 'facts' - 'names' ELSE n.attrs END AS attrs "
        "FROM kg_nodes n WHERE n.project_id = :p AND n.id = :id"),
        {"p": project_id, "id": node_id}).mappings().first()
    if not rows:
        raise LookupError(f"node {node_id} is not in this project's graph")
    row = dict(rows)
    if row["kind"] in withheld:
        raise PermissionError(f"reading a {row['kind']} needs {KIND_PERMISSIONS.get(row['kind'], 'more access')}")
    attrs = _attrs(row.get("attrs"))
    payload = node_payload(row)
    tree = _tree_row_of_node(connection, project_id, row)
    layer = payload["layer"]
    return {
        "node": payload,
        "statement": (row.get("body") or None) if layer in ("rules", "learning", "domain", "context") else None,
        "attrs": {k: v for k, v in attrs.items() if k not in _HIDDEN_ATTRS},
        "repo": row.get("repo"),
        "path": row.get("path"),
        "line": _int(attrs.get("line")),
        "end_line": _int(attrs.get("end_line")),
        "area": tree["area"] if tree else None,
        "package": (tree["package"] if tree else None) or (attrs.get("module") if layer == "code" else None),
        "language": attrs.get("lang") or attrs.get("language"),
        "connections": _class_connections(connection, project_id, tree, node_id, edge=edge, direction=direction,
                                          offset=offset, withheld=withheld)
        if tree and tree["level"] == "class" else
        _connections(connection, project_id, node_id, edge=edge, direction=direction,
                     offset=offset, withheld=withheld),
        "vector": _vector(connection, project_id, row, layer, withheld, settings),
        "usage": _usage(connection, project_id, node_id),
        "provenance": _provenance(connection, project_id, row, attrs),
        "lineage": _lineage(connection, project_id, node_id, attrs) if row["kind"] in LEARNED_KINDS else None,
        "context": _context_card(connection, project_id, row, withheld),
    }


def _payloads(connection: Connection, project_id: str, withheld: frozenset[str]):
    def read(ids: list[str]) -> dict[str, dict[str, Any]]:
        return {i: node_payload(r) for i, r in _nodes_by_id(connection, project_id, ids).items()
                if r["kind"] not in withheld and r.get("valid_to") is None}
    return read


def _context_card(connection: Connection, project_id: str, row: Mapping[str, Any],
                  withheld: frozenset[str]) -> Optional[dict[str, Any]]:
    """The Context panel (design §4.7.3) for a decision, a case, a remediation or a run of the
    trace. Words, never a reason to refuse the panel."""
    kind = str(row["kind"])
    if product_layer(kind) != "context" and kind not in ("run", "precedent", "clause"):
        return None
    try:
        with connection.begin_nested():
            card = view_context.context_card(connection, project_id, {**row, "attrs": _attrs(row.get("attrs"))},
                                             _payloads(connection, project_id, withheld), withheld)
    except Exception:  # noqa: BLE001 - the rest of the panel still answers
        log.exception("context card for %s in %s could not be read", row.get("id"), project_id)
        return None
    if card is None or (kind in ("run", "precedent", "clause") and not card.get("links")):
        return None
    return card


def _virtual_run_detail(connection: Connection, project_id: str, node_id: str,
                        withheld: frozenset[str]) -> dict[str, Any]:
    """A run of the trace with no run node of its own: named, linked, never "not found"."""
    trace = view_context.load_trace(connection, project_id, withheld)
    payload = view_context.virtual_run_payload(trace, node_id)
    if not payload:
        raise LookupError("that run is no longer in this project's graph")
    rid = node_id[len(view_context.VIRTUAL_RUN):]
    run = trace.runs[rid]
    card = view_context.context_card(connection, project_id, {"id": node_id, "kind": "run", "node_key": f"run:{rid}",
                                                              "attrs": {}, "recorded_at": run["at"]},
                                     _payloads(connection, project_id, withheld), withheld)
    at = _iso(run["at"])
    return {"node": payload, "statement": None, "attrs": {}, "repo": None, "path": None, "line": None,
            "end_line": None, "area": None, "package": None, "language": None, "connections": [],
            "vector": {"embedded": False, "reason": "Not embedded: a run is a record of work, not knowledge"},
            "usage": {"shown": 0, "consulted_runs": 0, "reused_runs": 0},
            "provenance": {"created_by": "system", "created_by_kind": "system", "at": at, "who": "BOSS",
                           "workflow_run_id": rid if rid.startswith("wfr_") else None,
                           "run_title": run["title"], "run_at": at,
                           "events": [{"at": at, "what": "started", "by": None}]},
            "lineage": None, "context": card}


def _group_label(kind: str, direction: str) -> str:
    label = _GROUP_LABELS.get((kind, direction))
    if label:
        return label
    words = kind.replace("_", " ")
    return words.capitalize() if direction == "out" else f"{words.capitalize()} (incoming)"


def _connections(connection: Connection, project_id: str, node_id: str, *, edge: Optional[str],
                 direction: Optional[str], offset: int, withheld: frozenset[str]) -> list[dict[str, Any]]:
    params = {"p": project_id, "id": node_id, "w": sorted(withheld)}
    totals: dict[tuple[str, str], int] = {}
    for kind, way, n in connection.execute(text(
            """
            SELECT e.kind, 'out', count(*) FROM kg_edges e JOIN kg_nodes o ON o.id = e.dst_id
            WHERE e.src_id = :id AND e.project_id = :p AND e.valid_to IS NULL AND o.valid_to IS NULL
              AND NOT (o.kind = ANY(:w)) GROUP BY e.kind
            UNION ALL
            SELECT e.kind, 'in', count(*) FROM kg_edges e JOIN kg_nodes o ON o.id = e.src_id
            WHERE e.dst_id = :id AND e.project_id = :p AND e.valid_to IS NULL AND o.valid_to IS NULL
              AND NOT (o.kind = ANY(:w)) GROUP BY e.kind
            """), params).all():
        totals[(kind, way)] = int(n)
    if edge:
        wanted = [(edge, direction)] if (edge, direction) in totals else []
    else:
        wanted = sorted(totals, key=lambda g: (g[1] != "out", -totals[g], g[0]))
    if not wanted:
        return [{"kind": edge, "direction": direction, "label": _group_label(edge, direction),
                 "total": 0, "items": []}] if edge else []
    items: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for way, (near, far) in (("out", ("src_id", "dst_id")), ("in", ("dst_id", "src_id"))):
        kinds = [k for k, d in wanted if d == way]
        if not kinds:
            continue
        rows = connection.execute(text(
            f"""
            SELECT * FROM (
              SELECT e.kind AS edge_kind, {_NODE_COLS},
                     row_number() OVER (PARTITION BY e.kind ORDER BY e.weight DESC, n.name, n.id) AS rn
              FROM kg_edges e JOIN kg_nodes n ON n.id = e.{far}
              WHERE e.{near} = :id AND e.project_id = :p AND e.valid_to IS NULL AND n.valid_to IS NULL
                AND e.kind = ANY(:k) AND NOT (n.kind = ANY(:w))
            ) x WHERE rn > :lo AND rn <= :hi ORDER BY edge_kind, rn
            """), {**params, "k": kinds, "lo": offset if edge else 0,
                   "hi": (offset if edge else 0) + CONNECTION_PAGE}).mappings().all()
        for r in rows:
            items[(r["edge_kind"], way)].append(node_payload(r))
    return [{"kind": kind, "direction": way, "label": _group_label(kind, way),
             "total": totals[(kind, way)], "items": items.get((kind, way), [])} for kind, way in wanted]


#: The class-level rollup kinds and the direct edge kinds each one already counts.
_ROLLED_UP = {"calls": ("calls", "dispatches_to"), "imports": ("imports",), "extends": ("extends", "implements")}


def _class_connections(connection: Connection, project_id: str, tree: Mapping[str, Any], node_id: str, *,
                       edge: Optional[str], direction: Optional[str], offset: int,
                       withheld: frozenset[str]) -> list[dict[str, Any]]:
    """A class's (interface's, enum's, record's) connections, from what it CONTAINS.

    The index draws ``defines`` from the file to every symbol, never from a class to its methods
    (code_index.py), and a class's calls are its methods' calls. So a class's panel reads its
    members from the tree rollup (``kg_view_tree.parent_key``: the "Defines" group, in source
    order) and its calls, imports and subtypes from the class-level rollup (``kg_view_edges``,
    method edges rolled up to their classes), the same rows its Focus walks. Direct edges the
    rollup does not cover (references, rules, learnings, the file that defines it) stay as
    ``_connections`` reads them. Paging one group is ``edge`` + ``direction`` + ``offset``.
    """
    key = tree["key"]
    lo = offset if edge else 0
    hi = lo + CONNECTION_PAGE
    wanted = (edge, direction) if edge else None
    groups: list[dict[str, Any]] = []
    # 1. Its members: the tree's children of this class row.
    if wanted in (None, ("defines", "out")):
        total = int(connection.execute(text(
            "SELECT count(*) FROM kg_view_tree WHERE project_id = :p AND parent_key = :k AND level = 'method'"),
            {"p": project_id, "k": key}).scalar() or 0)
        rows = connection.execute(text(
            f"SELECT {_NODE_COLS} FROM kg_view_tree t JOIN kg_nodes n ON n.id = t.node_id AND n.valid_to IS NULL "
            "WHERE t.project_id = :p AND t.parent_key = :k AND t.level = 'method' "
            "ORDER BY t.sort_key, t.key OFFSET :lo LIMIT :n"),
            {"p": project_id, "k": key, "lo": lo, "n": CONNECTION_PAGE}).mappings().all() if total else []
        if total or wanted:
            groups.append({"kind": "defines", "direction": "out", "label": _group_label("defines", "out"),
                           "total": total, "items": [node_payload(r) for r in rows]})
    # 2. Its methods' calls, imports and subtypes, rolled up to the classes at the other end.
    rolled: set[tuple[str, str]] = set()
    if "symbol" not in withheld:
        totals: dict[tuple[str, str], int] = {}
        for kind, way, n in connection.execute(text(
                "SELECT kind, 'out', count(*) FROM kg_view_edges WHERE project_id = :p AND level = 'class' "
                "AND src_key = :k GROUP BY kind UNION ALL "
                "SELECT kind, 'in', count(*) FROM kg_view_edges WHERE project_id = :p AND level = 'class' "
                "AND dst_key = :k GROUP BY kind"), {"p": project_id, "k": key}).all():
            totals[(kind, way)] = int(n)
        for (kind, way), total in sorted(totals.items(), key=lambda g: (g[0][1] != "out", -g[1], g[0][0])):
            if wanted and wanted != (kind, way):
                continue
            near, far = ("src_key", "dst_key") if way == "out" else ("dst_key", "src_key")
            rows = connection.execute(text(
                f"SELECT {_NODE_COLS} FROM kg_view_edges e "
                f"JOIN kg_view_tree t ON t.project_id = e.project_id AND t.key = e.{far} "
                "JOIN kg_nodes n ON n.id = t.node_id AND n.valid_to IS NULL "
                f"WHERE e.project_id = :p AND e.level = 'class' AND e.kind = :kind AND e.{near} = :k "
                f"ORDER BY e.weight DESC, n.name, n.id OFFSET :lo LIMIT :n"),
                {"p": project_id, "k": key, "kind": kind, "lo": lo, "n": CONNECTION_PAGE}).mappings().all()
            groups.append({"kind": kind, "direction": way, "label": _group_label(kind, way), "total": total,
                           "items": [node_payload(r) for r in rows]})
            rolled.update((k, way) for k in _ROLLED_UP.get(kind, (kind,)))
    if wanted and any((g["kind"], g["direction"]) == wanted for g in groups):
        return [g for g in groups if (g["kind"], g["direction"]) == wanted]
    # 3. Everything else it touches directly (rules, learnings, references, its file).
    direct = _connections(connection, project_id, node_id, edge=edge, direction=direction,
                          offset=offset, withheld=withheld)
    return groups + [g for g in direct if (g["kind"], g["direction"]) not in rolled
                     and (g["kind"], g["direction"]) != ("defines", "out")]


def _kind_plural(kind: str) -> str:
    """A kind's plural as a sentence says it: the pack's plural, else the kind's words + "s"."""
    meta = view_context.kind_meta(kind) or {}
    one = _mid_sentence(str(meta.get("label") or kind.replace("_", " ")))
    return _mid_sentence(str(meta.get("plural") or one + "s"))


def _vector(connection: Connection, project_id: str, row: Mapping[str, Any], layer: str,
            withheld: frozenset[str], settings: Any) -> dict[str, Any]:
    found = connection.execute(text(
        "SELECT model_id, dimensions, embedding FROM kg_embeddings WHERE project_id = :p AND node_id = :id "
        "ORDER BY created_at DESC LIMIT 1"), {"p": project_id, "id": row["id"]}).mappings().first()
    if not found:
        if layer == "code":
            return {"embedded": False, "reason": "Not embedded: code symbols are matched by structure, not meaning"}
        from .embedding_slot import read, search_state
        from .embeddings import EMBEDDED_KINDS

        # SAY THE TRUTH PER KIND (rulebook source G1). "Not reached it yet" was shown for every
        # node without a vector, clauses and pack kinds included, which are never embedded.
        if row["kind"] not in EMBEDDED_KINDS:
            return {"embedded": False, "waiting": False,
                    "reason": f"Not embedded: {_kind_plural(row['kind'])} are found by name, words and "
                              "links, not by meaning"}
        on, reason = search_state(read(connection, project_id))
        return {"embedded": False, "waiting": bool(on),
                "reason": "Waiting to be embedded: the project's embedding model embeds it on its "
                          "next pass (about a minute)"
                if on else f"Not embedded: {reason}" if reason else "Not embedded"}
    from .store import PostgresKnowledgeStore

    neighbours: list[dict[str, Any]] = []
    try:
        with connection.begin_nested():
            hits = PostgresKnowledgeStore().vector_search(
                connection, project_id=project_id, model_id=found["model_id"],
                embedding=[float(x) for x in found["embedding"]], limit=12)
        nodes = _nodes_by_id(connection, project_id, [h["node_id"] for h in hits])
        for hit in hits:
            other = nodes.get(hit["node_id"])
            if not other or other["id"] == row["id"] or other["kind"] in withheld or other["valid_to"]:
                continue
            neighbours.append({"node": node_payload(other), "score": round(float(hit["cosine"]), 4)})
            if len(neighbours) == 5:
                break
    except Exception:  # noqa: BLE001 - the panel shows the vector without its neighbours
        neighbours = []
    return {"embedded": True, "model_id": found["model_id"], "dimensions": int(found["dimensions"]),
            "neighbours": neighbours}


_USAGE_HAS_KIND: Optional[bool] = None


def _usage(connection: Connection, project_id: str, node_id: str) -> dict[str, int]:
    """Shown in a pack (kg_usage), and the runs that consulted or reused it (their edges)."""
    global _USAGE_HAS_KIND
    if _USAGE_HAS_KIND is None:
        _USAGE_HAS_KIND = bool(connection.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'kg_usage' AND column_name = 'use_kind'"
        )).first())
    shown_sql = "AND coalesce(use_kind, 'shown') = 'shown'" if _USAGE_HAS_KIND else ""
    row = connection.execute(text(
        f"""
        SELECT (SELECT count(*) FROM kg_usage WHERE project_id = :p AND fact_id = :id {shown_sql}),
               (SELECT count(DISTINCT src_id) FROM kg_edges WHERE dst_id = :id AND kind = 'consulted'
                  AND valid_to IS NULL),
               (SELECT count(DISTINCT src_id) FROM kg_edges WHERE dst_id = :id AND kind = 'reused'
                  AND valid_to IS NULL)
        """), {"p": project_id, "id": node_id}).one()
    # A lesson's incidents: the ones that taught it (`produced`) and the ones that applied it
    # (`reused`), oldest first, by their short name ("INC-1").
    incidents: list[dict[str, Any]] = []
    for r in connection.execute(text(
            "SELECT s.id, s.name, s.attrs->>'incident_id' AS incident_id, e.kind, "
            "min(e.recorded_at) AS at FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id "
            "WHERE e.dst_id = :id AND e.project_id = :p AND e.kind IN ('produced', 'reused') "
            "AND s.kind = 'incident' AND e.valid_to IS NULL "
            "GROUP BY s.id, s.name, s.attrs->>'incident_id', e.kind ORDER BY at"),
            {"p": project_id, "id": node_id}).mappings():
        ref = None
        if r["incident_id"]:
            from ..incident_squad import incident_ref

            ref = incident_ref(connection, project_id, r["incident_id"])
        incidents.append({"id": r["id"], "label": ref or str(r["name"] or "an incident")[:60],
                          "how": "taught" if r["kind"] == "produced" else "reused"})
    return {"shown": int(row[0]), "consulted_runs": int(row[1]), "reused_runs": int(row[2]),
            "incidents": incidents}


#: An audit action key said as a person reads it: knowledge.rule.approve → "approved".
_DONE_WORD = {"record": "recorded", "propose": "proposed", "approve": "approved", "edit": "edited",
              "reject": "rejected", "supersede": "superseded", "promote": "promoted",
              "forget": "forgotten", "index": "indexed", "indexed": "indexed",
              "index_failed": "failed to index"}
#: The audit actions that ARE the creation the first event already says.
_CREATION = frozenset({"record", "propose", "index", "indexed"})
#: Said for an item recorded before BOSS kept who recorded it (BOSSAPP-768 round 2; round 4:
#: in words, never "(not recorded)").
NOT_RECORDED = "an agent, before BOSS kept agents' names"
#: A person whose display name was never recorded (a legacy "ceo" review): never the key.
OPERATOR = "the operator"
#: Who indexed code, when the repository's index record is gone.
CODE_WHO = "BOSS, from the repository"


def action_words(action: str) -> str:
    last = str(action or "").rsplit(".", 1)[-1]
    return _DONE_WORD.get(last, last.replace("_", " "))


def _who(layer: Optional[str], by_kind: str, prov: Mapping[str, Any], created_by: str) -> str:
    """WHO ADDED IT, in words, for every layer: BOSS from the repository for code; an agent by its name
    (and role) for what an agent wrote — "BOSS (general)" when nobody was addressed, "an agent (not
    recorded)" for an item written before BOSS kept the name; a person otherwise. Never an event
    key, and never the indexer for a rule or a learning."""
    if layer == "code":
        return CODE_WHO
    name = str(prov.get("agent_name") or "").strip()
    role = str(prov.get("agent_role") or "").strip()
    if by_kind == "person":
        return created_by
    if name:
        return name
    return role.replace("_", " ") if role and role != "boss" else NOT_RECORDED


def _provenance(connection: Connection, project_id: str, row: Mapping[str, Any],
                attrs: Mapping[str, Any]) -> dict[str, Any]:
    prov = _attrs(attrs.get("provenance"))
    review = _attrs(attrs.get("review"))
    created_by = str(row.get("created_by") or "system")
    layer = product_layer(str(row["kind"]))
    if layer == "code":
        by_kind = "system"
    elif prov.get("agent_role") or prov.get("agent_name") or created_by == "system":
        # Rules and learnings are written by agents (BOSS itself included); one without a recorded
        # name predates attribution and is said so, never as the code index.
        by_kind = "agent"
    elif created_by == prov.get("agent_role"):
        by_kind = "agent"
    else:
        by_kind = "person"
    who = _who(layer, by_kind, prov, created_by)
    at = prov.get("at") or prov.get("proposed_at") or _iso(row.get("first_seen") or row.get("recorded_at"))
    events: list[dict[str, Any]] = [{
        "at": at, "by": None if layer == "code" else who,
        "what": "indexed" if layer == "code" else "proposed" if layer == "rules" else "recorded"}]
    reviewed = str(review.get("decision") or rule_status(row)) if review.get("at") else None
    for action, when, detail, actor in connection.execute(text(
            "SELECT action, created_at, detail, actor_kind FROM kg_audit WHERE project_id = :p AND entity_id = :id "
            "ORDER BY created_at LIMIT 50"), {"p": project_id, "id": row["id"]}).all():
        if str(action or "").rsplit(".", 1)[-1] in _CREATION:
            continue
        detail = _attrs(detail)
        by = detail.get("actor_ref") or detail.get("actor")
        what = action_words(str(action))
        event = {"at": _iso(when), "what": what,
                 # A kind ("person", "agent") is never a name: a person's event without its ref is
                 # the review's person when it is the review; otherwise unnamed.
                 "by": by if by and by != "system" else (who if actor in ("agent", "system") else None)}
        if what == reviewed and (event["by"] in (None, review.get("by"))):
            event.update(by=review.get("by"), by_name=review.get("by_name"))
        events.append(event)
    # The review once: "approved by person" and then "approved by an owner" were the same act
    # (tester round 4) — the audit row and the review record.
    if reviewed and not any(e["at"] == review.get("at") or e["what"] == reviewed for e in events):
        events.append({"at": review.get("at"), "what": reviewed,
                       "by": review.get("by"), "by_name": review.get("by_name")})
    events.sort(key=lambda e: str(e["at"] or ""))
    approved = layer == "rules" and rule_status(row) == "approved"
    out: dict[str, Any] = {"created_by": created_by, "created_by_kind": by_kind, "at": at, "events": events,
                           "who": who}
    for key in ("agent_role", "agent_name", "agent_id", "run_id", "workflow_run_id", "code_task_run_id", "step", "model_id"):
        if prov.get(key) is not None:
            out[key] = prov.get(key)
    out.update(_where_from(connection, project_id, row, prov, layer))
    if approved:
        out["approved_by"] = review.get("by")
        out["approved_by_name"] = review.get("by_name")
        out["approved_at"] = review.get("at")
    if attrs.get("version") is not None:
        out["version"] = _int(attrs.get("version"))
        out["previous_id"] = attrs.get("previous_id")
    return out


def _where_from(connection: Connection, project_id: str, row: Mapping[str, Any], prov: Mapping[str, Any],
                layer: Optional[str]) -> dict[str, Any]:
    """WHERE IT CAME FROM, as the words need it (tester round 4: "Indexed by the code index",
    "Run wfr_6267c5d3… ↗"): for code, the repository, commit and time of the index that wrote it;
    for an agent's item, the run's title and start, or the conversation's title and start."""
    out: dict[str, Any] = {}
    if layer == "code":
        repo = row.get("repo")
        if repo:
            src = None
            try:
                with connection.begin_nested():
                    src = connection.execute(text(
                        "SELECT repo, branch, indexed_sha, indexed_at FROM kg_repo_index "
                        "WHERE project_id = :p AND repo = :r ORDER BY indexed_at DESC NULLS LAST LIMIT 1"),
                        {"p": project_id, "r": repo}).mappings().first()
            except Exception:  # noqa: BLE001 — provenance is words, never a reason to refuse
                src = None
            out["source"] = {"repo": repo, "branch": (src or {}).get("branch") or None,
                             "sha": str((src or {}).get("indexed_sha") or "")[:7] or None,
                             "at": _iso((src or {}).get("indexed_at")) or _iso(row.get("first_seen"))}
        return out
    from .learning_writeback import run_name

    ref = prov.get("code_task_run_id") or prov.get("workflow_run_id") or prov.get("run_id")
    if ref:
        title, started = run_name(connection, str(ref))
        out["run_title"] = title
        out["run_at"] = _iso(started)
    thread = prov.get("agent_thread_id")
    if thread and not ref:
        found = None
        try:
            with connection.begin_nested():
                found = connection.execute(text(
                    "SELECT name, attrs->>'started_at' AS at FROM kg_nodes WHERE project_id = :p AND kind = 'run' "
                    "AND node_key = :k AND valid_to IS NULL LIMIT 1"), {"p": project_id, "k": f"run:{thread}"}).mappings().first()
        except Exception:  # noqa: BLE001
            found = None
        if found:
            # The conversation node is named "<its question> · Oct 25, 07:13" (UTC): the words
            # say its question; the console shows the time in the viewer's zone.
            from .learning_conversations import is_placeholder, proper_names

            name = str(found["name"])
            if is_placeholder(name):     # still named by its id: named on read (mend_names)
                name = proper_names(connection, [str(thread)]).get(str(thread), name)
            mended = repair_cut(name)
            out["conversation_title"] = mended.rsplit(" · ", 1)[0] if " · " in mended else mended
            out["conversation_at"] = found["at"]
    return out


def _lineage(connection: Connection, project_id: str, node_id: str, attrs: Mapping[str, Any]) -> dict[str, Any]:
    prov = _attrs(attrs.get("provenance"))
    used = []
    for kind, eattrs, at, run_key, run_attrs, run_name in connection.execute(text(
            "SELECT e.kind, e.attrs, e.recorded_at, s.node_key, s.attrs, s.name FROM kg_edges e "
            "JOIN kg_nodes s ON s.id = e.src_id WHERE e.project_id = :p AND e.dst_id = :id "
            "AND e.kind IN ('consulted', 'reused') AND e.valid_to IS NULL ORDER BY e.recorded_at LIMIT 100"),
            {"p": project_id, "id": node_id}).all():
        ea, ra = _attrs(eattrs), _attrs(run_attrs)
        how = "reused" if kind == "reused" else "cited" if ea.get("how") == "cited" else "consulted"
        used.append({"run_id": str(ea.get("run_id") or ra.get("run_id") or str(run_key).split(":", 1)[-1]),
                     "agent_role": ea.get("agent_role") or ra.get("agent_role") or ra.get("role"),
                     "agent_name": ea.get("agent_name") or ra.get("agent_name"),
                     # The run by its name ("<title> · <when>", its time dropped: the console
                     # shows times in the viewer's zone), never its id (tester round 4).
                     "run_title": repair_cut(str(run_name or "")).rsplit(" · ", 1)[0] or None,
                     "how": how, "at": _iso(at)})
    return {"learned": {k: prov.get(k) for k in ("run_id", "agent_role", "agent_name", "step")} | {"at": prov.get("at")},
            "used": used}


def _rollup_detail(connection: Connection, project_id: str, key: str, *, edge: Optional[str],
                   direction: Optional[str], offset: int) -> dict[str, Any]:
    row = _tree_rows(connection, project_id, [key]).get(key)
    if not row or row["level"] not in ("area", "package"):
        raise LookupError(f"{key} is not in this project's code graph")
    level = row["level"]
    totals: dict[tuple[str, str], int] = {}
    for kind, way, n in connection.execute(text(
            "SELECT kind, 'out', count(*) FROM kg_view_edges WHERE project_id = :p AND level = :l AND src_key = :k "
            "GROUP BY kind UNION ALL SELECT kind, 'in', count(*) FROM kg_view_edges WHERE project_id = :p "
            "AND level = :l AND dst_key = :k GROUP BY kind"), {"p": project_id, "l": level, "k": key}).all():
        totals[(kind, way)] = int(n)
    wanted = [(edge, direction)] if edge else sorted(totals, key=lambda g: (g[1] != "out", -totals[g], g[0]))
    groups = []
    for kind, way in wanted:
        near, far = ("src_key", "dst_key") if way == "out" else ("dst_key", "src_key")
        others = connection.execute(text(
            f"SELECT {far}, weight FROM kg_view_edges WHERE project_id = :p AND level = :l AND {near} = :k "
            f"AND kind = :kind ORDER BY weight DESC, {far} OFFSET :o LIMIT :n"),
            {"p": project_id, "l": level, "k": key, "kind": kind, "o": offset if edge else 0,
             "n": CONNECTION_PAGE}).all()
        rows = _tree_rows(connection, project_id, [o[0] for o in others])
        groups.append({"kind": kind, "direction": way, "label": _group_label(kind, way),
                       "total": totals.get((kind, way), 0),
                       "items": [_rollup_payload(rows[o[0]]) for o in others if o[0] in rows]})
    sync = view_rollup.sync_row(connection, project_id)
    built = _iso(sync["built_at"]) if sync else None
    payload = _rollup_payload(row)
    return {"node": payload, "statement": None, "attrs": dict(payload["counts"]), "repo": row["repo"] or None,
            "path": None, "line": None, "end_line": None, "area": row["area"],
            "package": row["package"] if level == "package" else None, "language": None,
            "connections": groups,
            "vector": {"embedded": False, "reason": "Not embedded: areas and packages are rollups of the code graph"},
            "usage": {"shown": 0, "consulted_runs": 0, "reused_runs": 0},
            "provenance": {"created_by": "system", "created_by_kind": "system", "at": built,
                           "events": [{"at": built, "what": "rolled up", "by": None}]},
            "lineage": None}


# ── tree ─────────────────────────────────────────────────────────────────────────────────────

_LEVEL_ORDER = {"area": 0, "package": 1, "class": 2, "method": 3, "group": 0, "item": 2}


def _tree_payload(row: Mapping[str, Any], match: Optional[bool] = None) -> dict[str, Any]:
    level = row["level"]
    kind = {"area": "area", "package": "package", "method": "method"}.get(level) or row.get("symbol_kind") or "class"
    out = {"key": row["key"], "parent_key": row["parent_key"], "level": level, "node_id": row["node_id"],
           "label": row["label"], "kind": kind, "layer": "code", "count": int(row["child_count"]),
           "has_children": int(row["child_count"]) > 0}
    if match is not None:
        out["match"] = match
    return out


def tree_children(connection: Connection, project_id: str, *, tab: str = "project",
                  parent: Optional[str] = None, q: Optional[str] = None, offset: int = 0,
                  withheld: Iterable[str] = ()) -> dict[str, Any]:
    """The lazy tree (KgTreeChildrenResponse): ``parent``'s children, paged 200; with ``q``, the
    matching rows plus their ancestors so the tree opens to them."""
    if tab not in ("project", "rules", "learning", "domain", "context"):
        raise ValueError("tab is project, rules, learning, domain or context")
    q = (q or "").strip() or None
    parent = parent or None
    offset = max(0, int(offset or 0))
    withheld = frozenset(withheld)
    if tab == "project":
        rows, total, more = _project_tree(connection, project_id, parent, q, offset)
    else:
        rows, total, more = _item_tree(connection, project_id, tab, parent, q, offset, withheld)
    return {"tab": tab, "parent": parent, "q": q, "rows": rows, "total": total,
            "next_offset": offset + TREE_PAGE if more else None}


def _project_tree(connection: Connection, project_id: str, parent: Optional[str], q: Optional[str],
                  offset: int) -> tuple[list[dict], int, bool]:
    p = {"p": project_id, "o": offset, "n": TREE_PAGE}
    if q is None:
        where = "parent_key = :parent" if parent else "parent_key IS NULL AND level = 'area'"
        rows = connection.execute(text(
            f"SELECT *, count(*) OVER () AS total FROM kg_view_tree WHERE project_id = :p AND {where} "
            "ORDER BY repo, sort_key, key OFFSET :o LIMIT :n"), {**p, "parent": parent}).mappings().all()
        total = int(rows[0]["total"]) if rows else int(connection.execute(text(
            f"SELECT count(*) FROM kg_view_tree WHERE project_id = :p AND {where}"),
            {**p, "parent": parent}).scalar() or 0)
        return [_tree_payload(r) for r in rows], total, offset + len(rows) < total
    ql = q.lower()
    matches = connection.execute(text(
        """
        SELECT *, count(*) OVER () AS total FROM kg_view_tree
        WHERE project_id = :p AND label ILIKE :c ESCAPE '\\'
        ORDER BY (lower(label) = :ql) DESC, (lower(label) LIKE :pre ESCAPE '\\') DESC,
                 CASE level WHEN 'class' THEN 0 WHEN 'method' THEN 1 WHEN 'package' THEN 2 ELSE 3 END,
                 length(label), sort_key, key
        OFFSET :o LIMIT :n
        """), {**p, "c": f"%{_like(q)}%", "pre": f"{_like(ql)}%", "ql": ql}).mappings().all()
    total = int(matches[0]["total"]) if matches else 0
    found = {r["key"]: dict(r) for r in matches}
    ancestors: dict[str, dict] = {}
    wanted = {r["parent_key"] for r in matches if r["parent_key"]} - set(found)
    for _ in range(3):
        if not wanted:
            break
        rows = _tree_rows(connection, project_id, wanted)
        ancestors.update(rows)
        wanted = {r["parent_key"] for r in rows.values() if r["parent_key"]} - set(found) - set(ancestors)
    out = [_tree_payload(r, False) for r in ancestors.values() if r["key"] not in found]
    out += [_tree_payload(r, True) for r in matches]
    out.sort(key=lambda r: (_LEVEL_ORDER.get(r["level"], 9), r["label"].lower(), r["key"]))
    return out, total, offset + len(matches) < total


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _item_tree(connection: Connection, project_id: str, tab: str, parent: Optional[str], q: Optional[str],
               offset: int, withheld: frozenset[str]) -> tuple[list[dict], int, bool]:
    """Rules: status group → category group → rule. Learning: kind group → item, newest first."""
    if tab == "rules":
        kinds, layer = ["rule"], "rules"
    elif tab in ("domain", "context"):
        # Cases and documents first, then the rest of the layer's kinds (the pack's order).
        members = layer_kinds(tab) - withheld
        kinds = sorted(members, key=lambda k: (not (k.endswith("_case") or k in ("document", "regulation")),
                                                k != "agent_decision", k))
        layer = tab
    else:
        kinds = [k for k in (*LEARNED_KINDS, *ANCHOR_KINDS) if k not in withheld]
        layer = "learning"
    match_sql = "AND (name ILIKE :c ESCAPE '\\' OR body ILIKE :c ESCAPE '\\')" if q else ""
    live_sql = "" if tab == "rules" else "AND valid_to IS NULL"
    rows = connection.execute(text(
        f"SELECT id, kind, node_key, name, trust_tier, valid_to, recorded_at, attrs->>'status' AS status, "
        f"attrs->>'category' AS category, attrs->>'decision_type' AS decision_type, "
        f"attrs->>'step_id' AS step_id FROM kg_nodes "
        f"WHERE project_id = :p AND kind = ANY(:k) {live_sql} {match_sql} "
        f"ORDER BY recorded_at DESC, id LIMIT 5000"),
        {"p": project_id, "k": kinds, "c": f"%{_like(q or '')}%"}).mappings().all()

    def group_of(r: Mapping[str, Any]) -> list[str]:
        if tab == "rules":
            status = rule_status({"attrs": {"status": r["status"]}, "valid_to": r["valid_to"],
                                  "trust_tier": r["trust_tier"]})
            category = r["category"] if r["category"] in RULE_CATEGORIES else "other"
            return [f"rules:{status}", f"rules:{status}:{category}"]
        return [f"{layer}:{r['kind']}"]

    def group_row(key: str, count: int, match: Optional[bool]) -> dict[str, Any]:
        parts = key.split(":")
        if tab == "rules" and len(parts) == 3:
            label, parent_key = parts[2].replace("_", " ").capitalize(), f"rules:{parts[1]}"
        elif tab in ("domain", "context"):
            meta = view_context.kind_meta(parts[1]) or {}
            label, parent_key = str(meta.get("plural") or parts[1].replace("_", " ").capitalize()), None
        else:
            label = parts[1].capitalize() if tab == "rules" else _PLURAL.get(parts[1], parts[1])
            if tab == "learning" and parts[1] in ANCHOR_KINDS:
                # A run (an incident, a PR) is where something was learned, not a learning
                # (tester round 2: "3 learned" beside four groups).
                label = f"{label} it came from"
            parent_key = None
        out = {"key": key, "parent_key": parent_key, "level": "group", "node_id": None, "label": label,
               "kind": "group", "layer": layer, "count": count, "has_children": count > 0}
        if match is not None:
            out["match"] = match
        return out

    def item_row(r: Mapping[str, Any], match: Optional[bool]) -> dict[str, Any]:
        label = repair_cut(r["name"]) if r["kind"] == "run" else r["name"]
        if tab == "context":
            words, sub = view_context.display(r["kind"], label, {"decision_type": r["decision_type"],
                                                                  "step_id": r["step_id"], "status": r["status"]})
            label = f"{words} · {sub}" if sub and r["kind"] == "agent_decision" else words
        out = {"key": r["node_key"], "parent_key": group_of(r)[-1], "level": "item", "node_id": r["id"],
               "label": label, "kind": r["kind"], "layer": layer,
               "count": 0, "has_children": False}
        if match is not None:
            out["match"] = match
        return out

    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        for g in group_of(r):
            counts[g] += 1
    order = (["rules:" + s for s in RULE_STATUSES] if tab == "rules"
             else [f"{layer}:" + k for k in kinds])
    if q:
        groups = [g for g in counts if g.count(":") == 1] + [g for g in counts if g.count(":") == 2]
        out = [group_row(g, counts[g], False) for g in groups]
        items = [item_row(r, True) for r in rows]
        return out + items[offset:offset + TREE_PAGE], len(items), offset + TREE_PAGE < len(items)
    if parent is None:
        top = [group_row(g, counts[g], None) for g in order if counts.get(g)]
        return top, len(top), False
    if tab == "rules" and parent.count(":") == 1:
        subs = sorted((g for g in counts if g.count(":") == 2 and g.startswith(parent + ":")),
                      key=lambda g: RULE_CATEGORIES.index(g.rsplit(":", 1)[1]))
        page = [group_row(g, counts[g], None) for g in subs]
        return page, len(page), False
    members = [r for r in rows if group_of(r)[-1] == parent]
    page = [item_row(r, None) for r in members[offset:offset + TREE_PAGE]]
    return page, len(members), offset + TREE_PAGE < len(members)


# ── search ───────────────────────────────────────────────────────────────────────────────────

#: Paths of tests: ranked after the code they test at an equal match, never hidden.
_TEST_PATH = r"(^|/)(src/test|src/[A-Za-z]*[Tt]est[A-Za-z]*|tests?|__tests__|[^/]*-tests?(-[^/]*)?)(/|$)"


def search(connection: Connection, project_id: str, q: str, *, layers: Iterable[str] = DEFAULT_LAYERS,
           limit: int = 20, meaning: bool = False, withheld: Iterable[str] = (),
           settings: Any = None) -> dict[str, Any]:
    """Type-ahead (KgSearchResponse): names and ``search_tsv`` in one query — no embedding call,
    no per-hit lookup. Exact name > prefix of the last segment > text rank; classes before methods
    at an equal rank. ``meaning`` opts into the hybrid search (it embeds the query)."""
    from .embedding_slot import read, search_state

    q = (q or "").strip()
    if not q:
        raise ValueError("q is required")
    limit = max(1, min(int(limit), 50))
    withheld = frozenset(withheld)
    layers = frozenset(layers)
    kinds = kinds_of(layers, withheld)
    available, reason = search_state(read(connection, project_id))
    if meaning:
        if not available:
            raise ValueError(reason or "meaning-based search is off for this project")
        return {"q": q, "hits": _meaning_hits(connection, project_id, q, limit, kinds), "meaning": True,
                "meaning_available": True}
    ql = q.lower()
    params = {"p": project_id, "k": kinds, "q": q, "ql": ql, "contains": f"%{_like(q)}%",
              "prefix": f"{_like(ql)}%", "test": _TEST_PATH, "limit": limit,
              "items": ["rule", *LEARNED_KINDS]}
    # 1. By name: exact, then a prefix of the last segment, then anywhere in the name. A common
    #    word fills the page here, and no text rank is computed for its thousands of matches.
    rows = connection.execute(text(
        r"""
        WITH c AS (
          SELECT n.id, n.kind, n.name, n.path,
                 lower(regexp_replace(n.name, '^.*[.:/#]', '')) AS last,
                 CASE WHEN n.kind = 'symbol' THEN n.attrs->>'symbol_kind' END AS sk
          FROM kg_nodes n
          WHERE n.project_id = :p AND n.valid_to IS NULL AND n.kind = ANY(:k)
            AND n.name ILIKE :contains ESCAPE ''
        )
        SELECT id, CASE WHEN last = :ql OR lower(name) = :ql THEN 0
                        WHEN last LIKE :prefix ESCAPE '' THEN 1 ELSE 2 END AS tier
        FROM c
        ORDER BY 2,
                 CASE WHEN kind = 'symbol' AND sk IN ('method', 'function') THEN 1
                      WHEN kind = 'symbol' OR kind = ANY(:items) THEN 0 ELSE 2 END,
                 coalesce(path, '') ~ :test, length(name), name, id
        LIMIT :limit
        """), params).all()
    # 2. Room left: the words of the query in names and bodies (search_tsv), by text rank.
    if len(rows) < limit:
        rows += connection.execute(text(
            r"""
            SELECT n.id, 2 FROM kg_nodes n, plainto_tsquery('english', :q) AS tq
            WHERE n.project_id = :p AND n.valid_to IS NULL AND n.kind = ANY(:k)
              AND n.search_tsv @@ tq AND NOT (n.name ILIKE :contains ESCAPE '')
            ORDER BY CASE WHEN n.kind = 'symbol' AND n.attrs->>'symbol_kind' IN ('method', 'function') THEN 1
                          WHEN n.kind = 'symbol' OR n.kind = ANY(:items) THEN 0 ELSE 2 END,
                     coalesce(n.path, '') ~ :test, ts_rank(n.search_tsv, tq) DESC, length(n.name), n.id
            LIMIT :room
            """), {**params, "room": limit - len(rows)}).all()
    # 3. Room left: what agents and people WROTE — rules, learnings and the runs that produced
    #    them, by exact id, phrase or words in their text and attributes, and the learnings a run
    #    or conversation produced when its brief matches (find.find_written: the one search the
    #    agent tools use too, so the console and the agents cannot disagree). Tester round 4:
    #    INC-2041 (a root cause's evidence) and INC-2088 (only in the brief) were not found.
    from .find import find_written

    written = sorted(set(kinds) & ({"rule", *LEARNED_KINDS} | set(ANCHOR_KINDS)))
    if len(rows) < limit and written:
        seen = {r[0] for r in rows}
        rows += [(f["id"], 2) for f in find_written(connection, project_id, q, kinds=written,
                                                    limit=limit + len(seen))
                 if f["id"] not in seen][:limit - len(rows)]
    nodes = _nodes_by_id(connection, project_id, [r[0] for r in rows])
    hits = []
    n = len(rows)
    for i, (nid, tier) in enumerate(rows):
        found = nodes.get(nid)
        if not found:
            continue
        hits.append({"node": node_payload(found), "path": found.get("path"),
                     "match": ("exact", "prefix", "text")[int(tier)], "score": round((n - i) / n, 4)})
    return {"q": q, "hits": hits, "meaning": False, "meaning_available": bool(available)}


def _meaning_hits(connection: Connection, project_id: str, q: str, limit: int,
                  kinds: list[str]) -> list[dict[str, Any]]:
    from .service import KnowledgeService

    found = KnowledgeService().search(connection, project_id=project_id, query=q, limit=max(limit, 20))
    ids = [h["node_id"] for h in found.get("hits") or []]
    nodes = _nodes_by_id(connection, project_id, ids)
    allowed = set(kinds)
    hits = []
    for h in found.get("hits") or []:
        row = nodes.get(h["node_id"])
        if not row or row["kind"] not in allowed or row["valid_to"] is not None:
            continue
        hits.append({"node": node_payload(row), "path": row.get("path"), "match": "meaning",
                     "score": round(float(h.get("score") or h.get("rrf") or 0), 4)})
        if len(hits) == limit:
            break
    return hits


def names(connection: Connection, project_id: str, ids: Iterable[str], *,
          withheld: Iterable[str] = ()) -> dict[str, dict[str, Any]]:
    """What an answer's citations (``[R:kgn_…]``, ``[D:kgn_…]``, ``[L:kgn_…]``) are, by name: the
    console draws each as a link to the node, never as its id. A node of a kind the reader may not
    see, or of another project, is left out."""
    hidden = frozenset(withheld)
    wanted = [str(i).strip() for i in ids if str(i or "").strip()][:100]
    out: dict[str, dict[str, Any]] = {}
    for node_id, row in _nodes_by_id(connection, project_id, wanted).items():
        if row["kind"] in hidden:
            continue
        meta = view_context.kind_meta(row["kind"]) or {}
        out[node_id] = {"title": str(row.get("name") or row.get("node_key") or "a graph item"),
                        "kind": row["kind"],
                        "kind_label": str(meta.get("label") or row["kind"].replace("_", " ").capitalize()),
                        "layer": product_layer(row["kind"])}
    return out


def compare_search(connection: Connection, project_id: str, q: str, *,
                   layers: Iterable[str] = DEFAULT_LAYERS, limit: int = 10,
                   withheld: Iterable[str] = (), embedder: Any = None) -> dict[str, Any]:
    """The same query two ways, side by side (rulebook source G9): ``words`` is the type-ahead
    search (names and the words of names and bodies), ``meaning`` is the embedding model's nearest
    nodes alone — no words at all — so a person sees what meaning finds that words miss
    (``only_by_meaning``). Meaning needs the project's embedding model to have passed its check;
    without it ``meaning`` is empty and ``reason`` says why. The query's embedding is one governed
    call."""
    from .embedding_slot import read, search_state
    from .embeddings import configured_embedder
    from .store import PostgresKnowledgeStore

    q = (q or "").strip()
    if not q:
        raise ValueError("q is required")
    limit = max(1, min(int(limit), 25))
    withheld = frozenset(withheld)
    words = search(connection, project_id, q, layers=layers, limit=limit, withheld=withheld)["hits"]
    allowed = set(kinds_of(frozenset(layers), withheld))
    meaning: list[dict[str, Any]] = []
    available, reason = search_state(read(connection, project_id))
    if embedder is None and available:
        from ...config import get_settings

        embedder, why = configured_embedder(connection, get_settings(), project_id)
        reason = reason or why
    if embedder is not None:
        try:
            with connection.begin_nested():
                vector = embedder.embed([q])[0]
                found = PostgresKnowledgeStore().vector_search(
                    connection, project_id=project_id, model_id=embedder.model_id,
                    embedding=[float(x) for x in vector], limit=max(limit * 3, 20))
        except Exception as error:  # noqa: BLE001 — the words column still answers
            found = []
            reason = ("meaning-based search did not answer this time: the embedding model "
                      f"refused or failed ({type(error).__name__})")
        nodes = _nodes_by_id(connection, project_id, [h["node_id"] for h in found])
        for hit in found:
            row = nodes.get(hit["node_id"])
            if not row or row["kind"] not in allowed or row["valid_to"] is not None:
                continue
            meaning.append({"node": node_payload(row), "path": row.get("path"), "match": "meaning",
                            "score": round(float(hit.get("cosine") or 0), 4)})
            if len(meaning) == limit:
                break
        available = True
    worded = {h["node"]["id"] for h in words}
    return {"q": q, "words": words, "meaning": meaning, "meaning_available": bool(available),
            "reason": None if meaning or not reason else reason,
            "only_by_meaning": [h["node"]["id"] for h in meaning if h["node"]["id"] not in worded]}
