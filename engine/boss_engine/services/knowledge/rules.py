"""Business rules: the Rules layer (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.3, §4.5).

Agents propose rules; only a person approves them (Rules review). A rule is a ``kind='rule'``
node, ``node_key='rule:<sha1(scope+statement)[:16]>'`` (a later version appends ``.v<n>``),
``name`` = title, ``body`` = statement, with the attrs below. Proposed rules are ``inferred`` and
never reach a stage's reads (``store._scope_sql``); an approved rule is ``approved``
(store-enforced: an agent cannot set it) and trusted input for every agent.

The life of a rule:

* ``propose_rule`` (the ``kg_propose_rule`` tool, the S3 scan): every evidence symbol must resolve
  to a real code node of the project, and the cited lines must lie inside it. Writes the
  ``inferred`` rule with ``evidenced_by`` (lines, excerpt), ``proposed_by`` (the agent) and
  ``produced`` (the run) edges, and ``corrects`` when it corrects an approved rule. Provenance is
  the LLM context's (role, run, workflow run, step, model), never the arguments. The same scope +
  statement proposed again is the same rule.
* ``approve_rule`` (a person, ``knowledge:review``): ``approved`` via ``set_trust``, an
  ``approved_by`` edge to the person, ``attrs.review``, ``governs`` edges from the scope, a
  ``kg_audit`` row with ``actor_ref``. Approving a correction supersedes the rule it corrects.
* ``edit_rule``: version n+1, approved in the same act, ``supersedes`` the old version (closed).
* ``reject_rule``: closes the rule with the reviewer's note.

Every write checks ``EntitlementPort.check(project_id, "knowledge.rules")`` (Community allows).

RULES FROM A DOCUMENT (rulebook source G4/G5). A rule may cite a CLAUSE as its evidence instead of
code: ``{clause, quote}``, where the quote must be a verbatim passage of the clause's text (or the
proposal is refused). A bank's data-quality rule is a ``data_quality_rule`` node (the banking-dq
pack's kind), proposed by ``propose_dq_rule`` with its ``dq_threshold`` nodes (Warning/Error), the
clause it was read from as evidence, and links to the project's CDE, column, table and report-line
nodes by name. It goes through the same review: listed in Rules review, approved, edited or
rejected by a person (approving it approves its thresholds too).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .scrub import contains_secret_marker, scrub
from .store import PostgresKnowledgeStore

CATEGORIES = frozenset({"validation", "calculation", "limit", "state_transition", "authorization", "other"})
STATUSES = frozenset({"proposed", "approved", "rejected", "superseded"})
#: A bank's data-quality rule and its grading thresholds (the banking-dq ontology pack's kinds).
DQ_RULE_KIND = "data_quality_rule"
DQ_THRESHOLD_KIND = "dq_threshold"
#: Every kind Rules review lists and a person reviews.
RULE_KINDS = ("rule", DQ_RULE_KIND)
DQ_LEVELS = ("warning", "error")
ENTITLEMENT = "knowledge.rules"

MAX_TITLE = 200
MAX_STATEMENT = 2000
MAX_RATIONALE = 4000
MAX_EXCERPT = 1200
MAX_EVIDENCE = 20
MAX_SCOPE_ITEMS = 50

_STORE = PostgresKnowledgeStore()
_SPACES = re.compile(r"\s+")


def rule_status_sql(alias: str = "") -> str:
    """A rule's status in SQL — the same reading as view.rule_status: its recorded status, else
    rejected when closed, approved when a person approved it, proposed otherwise."""
    a = f"{alias}." if alias else ""
    known = ", ".join(f"'{s}'" for s in sorted(STATUSES))
    return (f"CASE WHEN {a}attrs->>'status' IN ({known}) THEN {a}attrs->>'status' "
            f"WHEN {a}valid_to IS NOT NULL THEN 'rejected' "
            f"WHEN {a}trust_tier = 'approved' THEN 'approved' ELSE 'proposed' END")


def rule_counts(connection: Connection, project_id: str, *, kinds: Optional[Sequence[str]] = None,
                where: str = "", params: Optional[Mapping[str, Any]] = None) -> dict[str, int]:
    """THE RULE COUNT every surface shows — Rules review, the setup checklist, the graph
    canvas's Rules bubble, the rulebook sample, the DQ platform. Business rules and data-quality
    rules alike (``RULE_KINDS``) unless ``kinds`` narrows it; ``where`` (" AND …", on kg_nodes
    columns) narrows by scan, area or document. {status: n} for every status, zeros included.

    Four surfaces each counted their own way (only `rule`; only open rows; a missing status as
    proposed or as approved), and the console said 0, 50, 96 and 146 about the same rules."""
    counts = {s: 0 for s in sorted(STATUSES)}
    for value, n in connection.execute(text(
            f"SELECT {rule_status_sql()} AS status, count(*) FROM kg_nodes "
            f"WHERE project_id = :p AND kind = ANY(:kinds){where} GROUP BY 1"),
            {**dict(params or {}), "p": project_id, "kinds": list(kinds or RULE_KINDS)}).all():
        if value in counts:
            counts[value] = int(n)
    return counts


def rule_count_words(counts: Mapping[str, int]) -> str:
    """The count in words, the same everywhere: "93 approved · 53 waiting for review"."""
    parts = [f"{int(counts.get('approved') or 0):,} approved",
             f"{int(counts.get('proposed') or 0):,} waiting for review"]
    if counts.get("rejected"):
        parts.append(f"{int(counts['rejected']):,} rejected")
    return " · ".join(parts)


class RuleRefused(ValueError):
    """A proposal or review refused in words (a 422 on the API, an ``error`` from a tool)."""


# --- helpers ------------------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _clean(value: Any, *, field: str, limit: int, required: bool = True) -> str:
    raw = _SPACES.sub(" ", str(value or "")).strip()
    if required and not raw:
        raise RuleRefused(f"{field} is required.")
    if len(raw) > limit:
        raise RuleRefused(f"{field} is {len(raw)} characters; at most {limit}.")
    if contains_secret_marker(raw):
        raise RuleRefused(f"{field} holds something shaped like a secret; nothing was stored.")
    return scrub(raw)[0]


def entitlement(project_id: str) -> dict[str, Any]:
    """The entitlement port's answer for ``knowledge.rules`` (spec §3.7), as the console reads it
    from /v1/knowledge/graph-status: ``{allowed, reason}``. Community allows everything."""
    from ...ports import get_engine_ports

    ports = get_engine_ports()
    if ports is None:
        from ...defaults import AllowAllEntitlementPort

        port = AllowAllEntitlementPort()
    else:
        port = ports.entitlement
    verdict = port.check(project_id, ENTITLEMENT) or {}
    allowed = bool(verdict.get("allowed", False))
    reason = str(verdict.get("reason") or (
        "Business rules are included in this edition." if allowed else
        "Business rules are not included in this edition (knowledge.rules)."))
    return {"allowed": allowed, "reason": reason}


def _entitled(project_id: str) -> None:
    verdict = entitlement(project_id)
    if not verdict["allowed"]:
        raise PermissionError(verdict["reason"])


def _content_key(scope: Mapping[str, Any], statement: str) -> str:
    material = json.dumps({
        "repo": scope.get("repo"),
        "areas": sorted(scope.get("areas") or []),
        "packages": sorted(scope.get("packages") or []),
        "symbols": sorted(scope.get("symbols") or []),
        "statement": statement.lower(),
    }, sort_keys=True)
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def _node_key(content_key: str, version: int) -> str:
    return f"rule:{content_key}" if version <= 1 else f"rule:{content_key}.v{version}"


def _context() -> dict[str, Any]:
    try:
        from ..llm_telemetry import get_llm_context
    except Exception:  # noqa: BLE001
        return {}
    return get_llm_context() or {}


def _as_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise RuleRefused(f"{field} must be a list.")
    items = [str(v).strip() for v in value if str(v or "").strip()]
    if len(items) > MAX_SCOPE_ITEMS:
        raise RuleRefused(f"{field} names {len(items)} items; at most {MAX_SCOPE_ITEMS}.")
    return list(dict.fromkeys(items))


# --- resolving code ------------------------------------------------------------------------------
_CODE_COLUMNS = "id, kind, node_key, name, repo, path, attrs"


def resolve_code(connection: Connection, project_id: str, ref: str) -> dict[str, Any]:
    """One real, live code node for ``ref``: a node id, a node key, or a name (exact, else the
    last dotted part of a symbol). Refuses in words when nothing, or more than one thing, matches."""
    ref = str(ref or "").strip()
    if not ref:
        raise RuleRefused("An evidence symbol is empty.")
    live = "project_id = :p AND knowledge_layer = 'code' AND (valid_to IS NULL OR valid_to > now())"
    params = {"p": project_id, "r": ref}
    row = connection.execute(text(
        f"SELECT {_CODE_COLUMNS} FROM kg_nodes WHERE {live} AND (id = :r OR node_key = :r) LIMIT 1"),
        params).mappings().first()
    if row is not None:
        return dict(row)
    rows = connection.execute(text(
        f"SELECT {_CODE_COLUMNS} FROM kg_nodes WHERE {live} AND name = :r ORDER BY node_key LIMIT 4"),
        params).mappings().all()
    if not rows and "." not in ref and ":" not in ref:
        # A bare member name: the symbols whose last part it is.
        rows = connection.execute(text(
            f"SELECT {_CODE_COLUMNS} FROM kg_nodes WHERE {live} AND kind = 'symbol' "
            "AND name LIKE '%.' || :r ORDER BY node_key LIMIT 4"), params).mappings().all()
    if len(rows) == 1:
        return dict(rows[0])
    if not rows:
        raise RuleRefused(f"'{ref}' is not a symbol in this project's code graph. Cite a symbol "
                          "kg_symbol or kg_search found (its node key, id or exact name).")
    keys = ", ".join(r["node_key"] for r in rows[:3])
    raise RuleRefused(f"'{ref}' names more than one symbol ({keys}{', ...' if len(rows) > 3 else ''}); "
                      "cite its node key.")


def _lines(value: Any, node: Mapping[str, Any]) -> Optional[list[int]]:
    if value in (None, "", []):
        return None
    if isinstance(value, str):
        parts = [p for p in re.split(r"[^0-9]+", value) if p]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    elif isinstance(value, int) and not isinstance(value, bool):
        parts = [value]
    else:
        raise RuleRefused("lines must be [first, last].")
    try:
        numbers = [int(p) for p in parts][:2]
    except (TypeError, ValueError) as error:
        raise RuleRefused("lines must be [first, last].") from error
    if not numbers:
        return None
    first, last = numbers[0], numbers[-1]
    if first < 1 or last < first:
        raise RuleRefused(f"lines {first}-{last} are not a range.")
    attrs = node.get("attrs") or {}
    start, end = attrs.get("line"), attrs.get("end_line")
    if isinstance(start, int) and isinstance(end, int) and (first < start or last > end):
        raise RuleRefused(f"lines {first}-{last} are outside {node['name']} ({start}-{end}). "
                          "Cite lines inside the symbol.")
    return [first, last]


def _area_exists(connection: Connection, project_id: str, area: str) -> bool:
    if connection.execute(text("SELECT to_regclass('kg_view_tree') IS NOT NULL")).scalar():
        found = connection.execute(text(
            "SELECT 1 FROM kg_view_tree WHERE project_id = :p AND level = 'area' "
            "AND (key = :a OR label = :a) LIMIT 1"), {"p": project_id, "a": area}).first()
        if found is not None:
            return True
    name = area.split(":")[-1] if area.startswith("area:") else area
    return connection.execute(text(
        "SELECT 1 FROM kg_nodes WHERE project_id = :p AND kind = 'file' "
        "AND (valid_to IS NULL OR valid_to > now()) "
        "AND (path LIKE :prefix OR path LIKE :inner) LIMIT 1"),
        {"p": project_id, "prefix": f"{name}/%", "inner": f"%/{name}/%"}).first() is not None


def _package_node(connection: Connection, project_id: str, package: str) -> dict[str, Any]:
    """A package scope entry: a tree key ``pkg:<repo>:<area>:<package>``, a ``module:`` key, or a
    package name. Resolves to the module node."""
    ref = package.strip()
    repo = None
    if ref.startswith("pkg:"):
        parts = ref.split(":")
        if len(parts) < 4:
            raise RuleRefused(f"'{ref}' is not a package key (pkg:<repo>:<area>:<package>).")
        repo, ref = parts[1], parts[-1]
    row = connection.execute(text(
        "SELECT id, node_key, name, repo FROM kg_nodes WHERE project_id = :p AND kind = 'module' "
        "AND (valid_to IS NULL OR valid_to > now()) AND (node_key = :r OR name = :r) "
        "AND (CAST(:repo AS TEXT) IS NULL OR node_key = 'module:' || :repo || ':' || :r) "
        "ORDER BY node_key LIMIT 2"), {"p": project_id, "r": ref, "repo": repo}).mappings().all()
    if not row:
        raise RuleRefused(f"'{package}' is not a package in this project's code graph.")
    return dict(row[0])


def _owner_class_key(node: Mapping[str, Any]) -> Optional[str]:
    attrs = node.get("attrs") or {}
    if attrs.get("symbol_kind") == "method" and "." in node["node_key"].rsplit(":", 1)[-1]:
        return re.sub(r"\.[^.:]*$", "", node["node_key"])
    return None


def _area_name(area: str) -> str:
    """An area as a rule's scope stores it: the plain area (``fineract-provider``), from a tree key
    ``area:<repo>:<area>`` or the name itself."""
    area = str(area or "").strip()
    return area.rsplit(":", 1)[-1] if area.startswith("area:") else area


def _area_of(path: Optional[str]) -> Optional[str]:
    if not path or "/" not in path:
        return None
    return path.split("/", 1)[0]


# --- reading ---------------------------------------------------------------------------------------
def _rule_row(connection: Connection, project_id: str, rule_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT id, project_id, node_key, name, body, attrs, trust_tier, created_by, valid_to, "
        "recorded_at, first_seen, kind FROM kg_nodes WHERE id = :id AND kind = ANY(:kinds)"),
        {"id": rule_id, "kinds": list(RULE_KINDS)}).mappings().first()
    if row is None:
        raise LookupError(f"rule not found: {rule_id}")
    if row["project_id"] != project_id:
        raise PermissionError(f"rule {rule_id} is not in project {project_id}")
    return dict(row)


def _sub(status: str, category: str) -> str:
    return f"{status} · {category.replace('_', ' ')}"


def _evidence(connection: Connection, rule_id: str) -> list[dict[str, Any]]:
    """The CODE a rule cites (a clause it cites is ``_clause_evidence``)."""
    rows = connection.execute(text(
        "SELECT e.attrs AS edge_attrs, n.id, n.node_key, n.name, n.path FROM kg_edges e "
        "JOIN kg_nodes n ON n.id = e.dst_id "
        "WHERE e.src_id = :id AND e.kind = 'evidenced_by' AND n.kind <> 'clause' "
        "ORDER BY e.first_seen, n.node_key"),
        {"id": rule_id}).mappings().all()
    out = []
    for row in rows:
        attrs = row["edge_attrs"] or {}
        lines = attrs.get("lines")
        out.append({
            "symbol_id": row["id"], "symbol_key": row["node_key"], "label": row["name"],
            "path": row["path"],
            "lines": [int(lines[0]), int(lines[1])] if isinstance(lines, list) and len(lines) == 2 else None,
            "excerpt": attrs.get("excerpt"),
        })
    return out


def _events(connection: Connection, project_id: str, rule: Mapping[str, Any]) -> list[dict[str, Any]]:
    attrs = rule["attrs"] or {}
    provenance = attrs.get("provenance") or {}
    events = [{"at": provenance.get("proposed_at") or _iso(rule["first_seen"]), "what": "proposed",
               "by": provenance.get("agent_name") or provenance.get("agent_role") or rule["created_by"]}]
    for row in connection.execute(text(
            "SELECT action, actor_ref, actor_kind, created_at, detail->>'by_name' AS by_name "
            "FROM kg_audit "
            "WHERE project_id = :p AND entity_id = :id AND action LIKE 'knowledge.rule.%' "
            "AND action <> 'knowledge.rule.propose' "
            "ORDER BY created_at"), {"p": project_id, "id": rule["id"]}).mappings():
        what = row["action"].rsplit(".", 1)[-1]
        what = {"approve": "approved", "edit": "edited", "reject": "rejected",
                "supersede": "superseded"}.get(what, what)
        events.append({"at": _iso(row["created_at"]), "what": what,
                       "by": row["actor_ref"] or row["actor_kind"], "by_name": row["by_name"]})
    return events


def _clause_evidence(connection: Connection, rule_id: str) -> list[dict[str, Any]]:
    """The document clauses a rule cites: the passage quoted, the clause's section and page, and
    its document's title — what Rules review draws as a clause chip."""
    rows = connection.execute(text(
        "SELECT e.attrs AS edge_attrs, n.id, n.name, n.attrs AS clause_attrs, d.name AS document "
        "FROM kg_edges e JOIN kg_nodes n ON n.id = e.dst_id AND n.kind = 'clause' "
        "LEFT JOIN kg_nodes d ON d.id = n.attrs->>'document_id' "
        "WHERE e.src_id = :id AND e.kind = 'evidenced_by' AND e.valid_to IS NULL "
        "ORDER BY e.first_seen, n.name"), {"id": rule_id}).mappings().all()
    out = []
    for row in rows:
        edge = row["edge_attrs"] or {}
        clause = row["clause_attrs"] or {}
        out.append({
            "clause_id": row["id"], "section": clause.get("section") or row["name"],
            "document": row["document"], "citation": clause.get("citation"),
            "page": clause.get("page"), "page_end": clause.get("page_end"),
            "quote": edge.get("quote"),
        })
    return out


def _thresholds(connection: Connection, rule: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A data-quality rule's Warning and Error thresholds, as proposed with it."""
    attrs = rule["attrs"] or {}
    rows = {r["level"]: r for r in connection.execute(text(
        "SELECT id, attrs->>'level' AS level, trust_tier FROM kg_nodes WHERE project_id = :p "
        "AND kind = :k AND attrs->>'rule_id' = :rid AND (valid_to IS NULL OR valid_to > now())"),
        {"p": rule["project_id"], "k": DQ_THRESHOLD_KIND,
         "rid": (attrs.get("dq") or {}).get("rule_id") or ""}).mappings()}
    out = []
    for t in attrs.get("thresholds") or []:
        found = rows.get(t.get("level"))
        out.append({"level": t.get("level"), "text": t.get("text"), "metric": t.get("metric"),
                    "value": t.get("value"), "node_id": found["id"] if found else None})
    return out


def rule_payload(connection: Connection, project_id: str, rule_id: str) -> dict[str, Any]:
    """A rule as the view payload (``KgRule``). A data-quality rule also carries ``dq`` (its
    rulebook fields, thresholds and what it links to); every rule carries ``clause_evidence``."""
    rule = _rule_row(connection, project_id, rule_id)
    attrs = rule["attrs"] or {}
    status = str(attrs.get("status") or "proposed")
    category = str(attrs.get("category") or "other")
    provenance = dict(attrs.get("provenance") or {})
    review = attrs.get("review")
    scope = attrs.get("scope") or {}
    kind = str(rule.get("kind") or "rule")
    dq = None
    if kind == DQ_RULE_KIND:
        dq = {**dict(attrs.get("dq") or {}), "thresholds": _thresholds(connection, rule),
              "links": dict(attrs.get("links") or {})}
    return {
        "node": {"id": rule["id"], "key": rule["node_key"], "kind": kind,
                 "layer": "rules" if kind == "rule" else "domain",
                 "label": rule["name"], "sub": _sub(status, category),
                 "trust_tier": rule["trust_tier"], "status": status},
        "kind": kind,
        "dq": dq,
        "clause_evidence": _clause_evidence(connection, rule["id"]),
        "title": attrs.get("title") or rule["name"],
        "statement": attrs.get("statement") or rule["body"],
        "rationale": attrs.get("rationale") or None,
        "category": category,
        "scope": {"repo": scope.get("repo"), "areas": list(scope.get("areas") or []),
                  "packages": list(scope.get("packages") or []),
                  "symbols": list(scope.get("symbols") or [])},
        "status": status,
        "confidence": float(attrs.get("confidence") or 0.0),
        "provenance": {
            "created_by": rule["created_by"],
            # An edited version is written by the person who edited it; its lineage (the agent
            # that proposed the rule, its run) stays in the other fields.
            "created_by_kind": "person" if attrs.get("edited") else (provenance.get("actor_kind") or "agent"),
            "agent_role": provenance.get("agent_role"),
            "agent_name": provenance.get("agent_name"),
            "agent_id": provenance.get("agent_id"),
            "run_id": provenance.get("run_id"),
            "workflow_run_id": provenance.get("workflow_run_id"),
            "code_task_run_id": provenance.get("code_task_run_id"),
            "step": provenance.get("step"),
            "model_id": provenance.get("model_id"),
            "at": provenance.get("proposed_at") or _iso(rule["first_seen"]),
            "approved_by": (review or {}).get("by") if status in ("approved", "superseded") else None,
            "approved_by_name": (review or {}).get("by_name")
            if status in ("approved", "superseded") else None,
            "approved_at": (review or {}).get("at") if status in ("approved", "superseded") else None,
            "version": int(attrs.get("version") or 1),
            "previous_id": attrs.get("previous_id"),
            "events": _events(connection, project_id, rule),
        },
        "review": review,
        "version": int(attrs.get("version") or 1),
        "previous_id": attrs.get("previous_id"),
        "evidence": _evidence(connection, rule["id"]),
        "scan_id": attrs.get("scan_id"),
    }


def _area_filter_sql(area: Optional[str]) -> tuple[str, dict[str, Any]]:
    if not area:
        return "", {}
    return " AND attrs->'scope'->'areas' ? :area", {"area": _area_name(area)}


def list_rules(
    connection: Connection,
    *,
    project_id: str,
    status: Optional[str] = None,
    scan_id: Optional[str] = None,
    area: Optional[str] = None,
    limit: int = 200,
    kind: Optional[str] = None,
) -> dict[str, Any]:
    """The Rules review queue (``KgRulesResponse``): the rules of one status (all when None),
    newest first, with counts per status under the same scan/area filter. Business rules and
    data-quality rules alike; ``kind`` narrows to one."""
    if status is not None and status not in STATUSES:
        raise RuleRefused(f"status is one of {', '.join(sorted(STATUSES))}.")
    if kind is not None and kind not in RULE_KINDS:
        raise RuleRefused(f"kind is one of {', '.join(RULE_KINDS)}.")
    _STORE._org_id(connection, project_id, None)
    where = "project_id = :p AND kind = ANY(:kinds)"
    params: dict[str, Any] = {"p": project_id, "kinds": [kind] if kind else list(RULE_KINDS)}
    narrow = ""
    if scan_id:
        narrow += " AND attrs->>'scan_id' = :scan"
        params["scan"] = scan_id
    area_sql, area_params = _area_filter_sql(area)
    narrow += area_sql
    params.update(area_params)
    where += narrow
    counts = rule_counts(connection, project_id, kinds=params["kinds"], where=narrow, params=params)
    listed = where + (f" AND {rule_status_sql()} = :status" if status else "")
    ids = connection.execute(text(
        f"SELECT id FROM kg_nodes WHERE {listed} ORDER BY first_seen DESC, id LIMIT :n"),
        {**params, "status": status, "n": max(1, min(int(limit), 500))}).scalars().all()
    return {"items": [rule_payload(connection, project_id, rid) for rid in ids],
            "counts": counts, "scan_id": scan_id, "scans": scan_labels(connection, project_id)}


def scan_labels(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Every scan that proposed rules here, by what it read and when — never its raw id (tester
    round 2: the Scan filter listed `rscan_caa44184979a_scan`). Newest first."""
    from .learning_writeback import run_title

    rows = connection.execute(text(
        "SELECT attrs->>'scan_id' AS scan, max(attrs->'provenance'->>'workflow_run_id') AS run, "
        "min(first_seen) AS at, count(*) AS n FROM kg_nodes WHERE project_id = :p AND kind = ANY(:kinds) "
        "AND attrs->>'scan_id' IS NOT NULL GROUP BY 1 ORDER BY min(first_seen) DESC LIMIT 50"),
        {"p": project_id, "kinds": list(RULE_KINDS)}).mappings().all()
    out = []
    for r in rows:
        label = run_title(connection, r["run"]) if r["run"] else (
            f"A scan · {r['at']:%b %d, %H:%M}" if r["at"] else "A scan")
        out.append({"id": r["scan"], "label": label, "rules": int(r["n"])})
    return out


def rules_for(
    connection: Connection,
    *,
    project_id: str,
    symbols: Sequence[str] = (),
    packages: Sequence[str] = (),
    areas: Sequence[str] = (),
    query: str = "",
    status: str = "approved",
    limit: int = 20,
) -> list[str]:
    """Rule ids for an agent: the rules (approved unless ``status='proposed'``) that govern the
    given symbols — directly, through the symbol's class or package, or its area — or the given
    packages and areas, or whose text matches ``query``. With nothing named, the newest."""
    if status not in ("approved", "proposed"):
        raise RuleRefused("status is approved or proposed.")
    tier = "approved" if status == "approved" else "inferred"
    targets: set[str] = set()          # code node ids a governs edge may reach
    area_names: set[str] = {_area_name(a) for a in areas}
    package_keys: set[str] = set()
    for ref in symbols:
        node = resolve_code(connection, project_id, ref)
        targets.add(node["id"])
        owner = _owner_class_key(node)
        if owner:
            found = _STORE.find_by_key(connection, project_id=project_id, kind="symbol", node_key=owner)
            if found:
                targets.add(found["id"])
        module = (node.get("attrs") or {}).get("module")
        if module and node.get("repo"):
            package_keys.add(f"module:{node['repo']}:{module}")
        area = _area_of(node.get("path"))
        if area:
            area_names.add(area)
    for ref in packages:
        package_keys.add(_package_node(connection, project_id, ref)["node_key"])
    if package_keys:
        targets.update(connection.execute(text(
            "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'module' AND node_key = ANY(:k)"),
            {"p": project_id, "k": sorted(package_keys)}).scalars().all())
    def base(n: str = "") -> str:
        a = f"{n}." if n else ""
        return (f"{a}project_id = :p AND {a}kind IN ('rule', '{DQ_RULE_KIND}') AND {a}trust_tier = :tier "
                f"AND ({a}valid_to IS NULL OR {a}valid_to > now()) "
                f"AND coalesce({a}attrs->>'status', 'proposed') = :status")

    params: dict[str, Any] = {"p": project_id, "tier": tier, "status": status,
                              "n": max(1, min(int(limit), 50))}
    matches: list[str] = []
    named = bool(symbols or packages or areas)
    if named:
        params.update({"targets": sorted(targets), "areas": sorted(area_names)})
        # A proposed rule has no governs edges yet: its evidence and scope say where it applies.
        matches = list(connection.execute(text(
            f"""
            SELECT n.id FROM kg_nodes n
            WHERE {base("n")}
              AND (EXISTS (SELECT 1 FROM kg_edges e WHERE e.src_id = n.id
                             AND e.kind IN ('governs', 'evidenced_by') AND e.dst_id = ANY(:targets)
                             AND (e.valid_to IS NULL OR e.valid_to > now()))
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(
                                coalesce(n.attrs->'scope'->'areas', '[]'::jsonb)) a
                              WHERE a = ANY(:areas)))
            ORDER BY n.first_seen DESC LIMIT :n
            """), params).scalars().all())
    if query.strip():
        # The one written-item search (find.find_written, as the console and kg_search): an id,
        # the phrase, or what a matching run produced, first; then every word in search_tsv; then
        # any of the words in a rule's text and attributes.
        from .find import find_written

        written = find_written(connection, project_id, query, kinds=list(RULE_KINDS), limit=200)
        allowed = set(connection.execute(text(f"SELECT id FROM kg_nodes WHERE {base()} AND id = ANY(:ids)"),
                                         {**params, "ids": [w["id"] for w in written]}).scalars().all())
        written = [w for w in written if w["id"] in allowed]
        matches.extend(w["id"] for w in written if w["match"] != "words" and w["id"] not in matches)
        found = connection.execute(text(
            f"SELECT id FROM kg_nodes WHERE {base()} AND search_tsv @@ plainto_tsquery('english', :q) "
            "ORDER BY ts_rank(search_tsv, plainto_tsquery('english', :q)) DESC LIMIT :n"),
            {**params, "q": query}).scalars().all()
        matches.extend(i for i in found if i not in matches)
        matches.extend(w["id"] for w in written if w["id"] not in matches)
    if not named and not query.strip():
        matches = list(connection.execute(text(
            f"SELECT id FROM kg_nodes WHERE {base()} ORDER BY first_seen DESC LIMIT :n"), params).scalars().all())
    return matches[: params["n"]]


# --- writing -------------------------------------------------------------------------------------
def _anchor(connection: Connection, project_id: str, kind: str, key: str, name: str,
            attrs: Optional[dict] = None) -> str:
    """An observed anchor node (run, agent, person) a rule's edges hang on; an existing one is
    kept as it is (ingest writes a run's full record later under the same key)."""
    found = _STORE.find_by_key(connection, project_id=project_id, kind=kind, node_key=key)
    if found is not None:
        return found["id"]
    return _STORE.upsert_node(connection, project_id=project_id, kind=kind, node_key=key, name=name,
                              body="", trust_tier="observed", actor_kind="system", source="rules",
                              attrs=attrs or {})


def _agent_name(connection: Connection, project_id: str, role_key: str) -> Optional[str]:
    row = connection.execute(text(
        "SELECT role_key, display_name, spec FROM roles WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": role_key}).mappings().first()
    if row is None:
        return None
    try:
        from ..agent_identity import display_name_of

        return display_name_of(dict(row))
    except Exception:  # noqa: BLE001
        return row["display_name"]


def _agent_id(connection: Connection, project_id: str, role_key: str) -> Optional[str]:
    """The agent's Passport id (roles.agent_id)."""
    found = connection.execute(text(
        "SELECT agent_id FROM roles WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": role_key}).scalar()
    return str(found) if found else None


def _provenance(connection: Connection, project_id: str, actor_kind: str,
                actor_ref: Optional[str]) -> dict[str, Any]:
    from ..llm_telemetry import GENERAL_NAME, GENERAL_ROLE

    ctx = _context()
    role = str(actor_ref or ctx.get("speaker_role") or ctx.get("role_key") or "").strip() or None
    stage = ctx.get("step") or ctx.get("stage")
    run_id = ctx.get("run_id")
    name = agent_id = None
    if actor_kind == "agent":
        # The context's name is the speaker's only when the rule is the speaker's own.
        mine = role in (ctx.get("speaker_role"), ctx.get("role_key"))
        name = (ctx.get("agent_name") if mine else None) or (_agent_name(connection, project_id, role) if role else None)
        agent_id = (ctx.get("agent_id") if mine else None) or (_agent_id(connection, project_id, role) if role else None)
        if role == GENERAL_ROLE or not (role or name):
            # NEVER BLANK: a turn nobody was addressed in is BOSS itself, said so.
            role, name, agent_id = GENERAL_ROLE, GENERAL_NAME, None
    return {
        "actor_kind": actor_kind,
        "agent_role": role if actor_kind == "agent" else None,
        "agent_name": name,
        "agent_id": agent_id,
        "run_id": run_id,
        "workflow_run_id": ctx.get("workflow_run_id"),
        "code_task_run_id": ctx.get("code_task_run_id") or (run_id if str(run_id or "").startswith("ctr") else None),
        "step": stage,
        "model_id": ctx.get("model_id") or ctx.get("model"),
        "proposed_at": _now_iso(),
    }


def _scope(connection: Connection, project_id: str, scope: Mapping[str, Any],
           evidence_nodes: list[dict]) -> tuple[dict[str, Any], list[dict], list[dict]]:
    """The scope, every entry resolved: (scope, symbol nodes, package nodes)."""
    if scope is not None and not isinstance(scope, Mapping):
        raise RuleRefused("scope must be {areas[], packages[], symbols[]}.")
    scope = scope or {}
    unknown = set(scope) - {"repo", "areas", "packages", "symbols"}
    if unknown:
        raise RuleRefused(f"scope has unknown keys {sorted(unknown)}; it is {{areas, packages, symbols}}.")
    symbol_nodes = [resolve_code(connection, project_id, s) for s in _as_list(scope.get("symbols"), "scope.symbols")]
    package_nodes = [_package_node(connection, project_id, p) for p in _as_list(scope.get("packages"), "scope.packages")]
    areas = list(dict.fromkeys(_area_name(a) for a in _as_list(scope.get("areas"), "scope.areas")))
    for area in areas:
        if not _area_exists(connection, project_id, area):
            raise RuleRefused(f"'{area}' is not an area of this project's code.")
    if not (symbol_nodes or package_nodes or areas):
        symbol_nodes = list({n["id"]: n for n in evidence_nodes}.values())     # what it cites
    repos = {n.get("repo") for n in symbol_nodes + evidence_nodes if n.get("repo")}
    repo = str(scope.get("repo") or "").strip() or (sorted(repos)[0] if len(repos) == 1 else None)
    resolved = {
        "repo": repo,
        "areas": areas,
        "packages": list(dict.fromkeys(_package_scope_key(p) for p in package_nodes)),
        "symbols": list(dict.fromkeys(n["node_key"] for n in symbol_nodes)),
    }
    return resolved, symbol_nodes, package_nodes


def _package_scope_key(node: Mapping[str, Any]) -> str:
    """A package as a rule's scope stores it: its name (``org.apache.fineract.portfolio...``)."""
    return node["name"]


_QUOTE_SPACES = re.compile(r"\s+")


def _squeeze(value: str) -> str:
    """Text compared for a verbatim quote: line breaks, runs of spaces and non-breaking spaces
    are one space (a PDF line break inside a sentence is not a different sentence)."""
    return _QUOTE_SPACES.sub(" ", str(value or "").replace("\u00a0", " ")).strip()


def clause_evidence(connection: Connection, project_id: str, item: Mapping[str, Any]) -> tuple[dict, dict]:
    """``{clause, quote}`` → (the clause node, the evidence edge's attrs). The clause must be a
    live clause of this project, and the quote a verbatim passage of its text — else refused."""
    ref = str(item.get("clause") or "").strip()
    row = connection.execute(text(
        "SELECT id, kind, node_key, name, body, attrs, NULL AS path, NULL AS repo FROM kg_nodes "
        "WHERE project_id = :p AND kind = 'clause' AND (id = :r OR node_key = :r) "
        "AND (valid_to IS NULL OR valid_to > now()) LIMIT 1"), {"p": project_id, "r": ref}).mappings().first()
    if row is None:
        raise RuleRefused(f"'{ref}' is not a clause of a document in this project. Cite the clause "
                          "the rule was read from (its id).")
    quote = _clean(item.get("quote"), field="evidence quote", limit=MAX_EXCERPT)
    if _squeeze(quote) not in _squeeze(row["body"]):
        raise RuleRefused(f"The quote is not in clause {row['name']}: evidence from a document must "
                          "be a passage copied from the clause, word for word.")
    attrs = row["attrs"] or {}
    return dict(row), {"quote": quote, "citation": attrs.get("citation"), "page": attrs.get("page"),
                       "section": attrs.get("section") or row["name"]}


def _evidence_nodes(connection: Connection, project_id: str,
                    evidence: Sequence[Mapping[str, Any]]) -> list[tuple[dict, dict]]:
    if not isinstance(evidence, (list, tuple)) or not evidence:
        raise RuleRefused("A rule needs evidence: at least one {symbol, lines, excerpt} from the code, "
                          "or {clause, quote} from a document's clause.")
    if len(evidence) > MAX_EVIDENCE:
        raise RuleRefused(f"{len(evidence)} pieces of evidence; at most {MAX_EVIDENCE}.")
    out = []
    for item in evidence:
        if not isinstance(item, Mapping):
            raise RuleRefused("Each piece of evidence is {symbol, lines, excerpt} or {clause, quote}.")
        if item.get("clause") and not item.get("symbol"):
            out.append(clause_evidence(connection, project_id, item))
            continue
        node = resolve_code(connection, project_id, str(item.get("symbol") or ""))
        lines = _lines(item.get("lines"), node)
        excerpt = _clean(item.get("excerpt"), field="evidence excerpt", limit=MAX_EXCERPT, required=False)
        out.append((node, {"lines": lines, "excerpt": excerpt or None}))
    return out


def _edge(connection: Connection, project_id: str, src: str, dst: str, kind: str, actor_kind: str,
          actor_ref: Optional[str], attrs: Optional[dict] = None) -> str:
    return _STORE.upsert_edge(connection, project_id=project_id, src_id=src, dst_id=dst, kind=kind,
                              actor_kind=actor_kind, actor_ref=actor_ref, attrs=attrs)


def _existing(connection: Connection, project_id: str, content_key: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT id, attrs, valid_to FROM kg_nodes WHERE project_id = :p AND kind = 'rule' "
        "AND (node_key = :k OR node_key LIKE :versions) "
        "ORDER BY (valid_to IS NULL) DESC, first_seen DESC LIMIT 1"),
        {"p": project_id, "k": f"rule:{content_key}", "versions": f"rule:{content_key}.v%"}).mappings().first()
    return dict(row) if row else None


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
    scan_id: str | None = None,
    actor_kind: str = "agent",
    actor_ref: str | None = None,
) -> dict[str, Any]:
    """Write one proposed rule and its edges; return the rule as the view payload (``KgRule``).

    Args are the ``kg_propose_rule`` tool's (§4.5):

    - ``scope``: ``{areas[], packages[], symbols[]}`` — what it governs. Empty: the evidence
      symbols.
    - ``evidence``: at least one ``{symbol, lines, excerpt}``. Every ``symbol`` must resolve in
      the project (a node id, key or exact name of a live code node), and ``lines`` must lie inside
      it; otherwise the call refuses in words.
    - ``corrects``: the id of the approved rule this one corrects (a ``corrects`` edge; a person
      approves the correction, which then supersedes it).
    - ``scan_id``: the scan that proposed it (else the LLM context's ``knowledge_scan_id``).
    - ``actor_kind``/``actor_ref``: who proposes (an agent and its role key, from the LLM context
      when not given). Whatever the actor, a proposal is ``inferred``.

    It writes an ``inferred`` rule (``attrs.status='proposed'``, ``attrs.provenance`` from the LLM
    context — never from the arguments) plus ``evidenced_by`` (attrs.lines, attrs.excerpt),
    ``proposed_by`` and ``produced`` edges, and ``corrects`` when given. It deduplicates by
    node_key: proposing the same scope + statement again returns the existing rule (a rejected one
    is refused, with the reviewer's note).
    """
    _entitled(project_id)
    if actor_kind not in ("agent", "person", "system"):
        raise RuleRefused(f"unknown actor kind {actor_kind!r}")
    statement = _clean(statement, field="statement", limit=MAX_STATEMENT)
    title = _clean(title, field="title", limit=MAX_TITLE)
    rationale = _clean(rationale, field="rationale", limit=MAX_RATIONALE, required=False)
    category = str(category or "").strip().lower()
    if category not in CATEGORIES:
        raise RuleRefused(f"category is one of {', '.join(sorted(CATEGORIES))}.")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError) as error:
        raise RuleRefused("confidence is a number from 0 to 1.") from error
    if not 0.0 <= confidence <= 1.0:
        raise RuleRefused("confidence is a number from 0 to 1.")
    cited = _evidence_nodes(connection, project_id, evidence)
    # A clause is evidence, never scope: only code a rule cites is what it governs by default.
    resolved, symbol_nodes, package_nodes = _scope(connection, project_id, scope,
                                                   [n for n, _ in cited if n.get("kind") != "clause"])

    version, previous_id = 1, None
    corrected = None
    if corrects:
        corrected = _rule_row(connection, project_id, str(corrects))
        status = (corrected["attrs"] or {}).get("status")
        if status != "approved" or corrected["valid_to"] is not None:
            raise RuleRefused(f"Rule {corrects} is {status or 'not approved'}; only an approved rule is "
                              "corrected. Propose a new rule instead.")
        version = int((corrected["attrs"] or {}).get("version") or 1) + 1
        previous_id = corrected["id"]

    content_key = _content_key(resolved, statement)
    existing = _existing(connection, project_id, content_key)
    if existing is not None:
        status = (existing["attrs"] or {}).get("status")
        if status == "rejected":
            review = (existing["attrs"] or {}).get("review") or {}
            note = f": {review['note']}" if review.get("note") else ""
            raise RuleRefused(f"This rule was rejected by {review.get('by') or 'a reviewer'}{note}. "
                              "Propose it again only with new evidence or a changed statement.")
        return rule_payload(connection, project_id, existing["id"])

    ctx = _context()
    provenance = _provenance(connection, project_id, actor_kind, actor_ref)
    who = provenance["agent_role"] if actor_kind == "agent" else (actor_ref or actor_kind)
    attrs = {
        "statement": statement, "rationale": rationale or None, "category": category,
        "scope": resolved, "status": "proposed", "confidence": confidence,
        "provenance": provenance, "review": None, "version": version, "previous_id": previous_id,
        "content_key": content_key,
        "scan_id": scan_id or ctx.get("knowledge_scan_id"),
        "corrects": previous_id,
    }
    rule_id = _STORE.upsert_node(
        connection, project_id=project_id, kind="rule", node_key=_node_key(content_key, version),
        name=title, body=statement, trust_tier="inferred",
        actor_kind="agent" if actor_kind == "agent" else ("person" if actor_kind == "person" else "system"),
        actor_ref=who, attrs=attrs, source="rules")
    edge_actor = "agent" if actor_kind == "agent" else ("person" if actor_kind == "person" else "system")
    for node, cite in cited:
        _edge(connection, project_id, rule_id, node["id"], "evidenced_by", edge_actor, who, cite)
    step_attrs = {"run_id": provenance["run_id"], "step": provenance["step"]}
    if actor_kind == "agent" and provenance["agent_role"]:
        agent_id = _anchor(connection, project_id, "agent", f"agent:{provenance['agent_role']}",
                           provenance["agent_name"] or provenance["agent_role"],
                           {"role_key": provenance["agent_role"]})
        _edge(connection, project_id, rule_id, agent_id, "proposed_by", edge_actor, who, step_attrs)
    run_ref = provenance["run_id"] or provenance["workflow_run_id"]
    if run_ref:
        from .learning_writeback import run_anchor

        run_id = run_anchor(connection, project_id, run_ref)
        _edge(connection, project_id, run_id, rule_id, "produced", edge_actor, who,
              {"step": provenance["step"], "at": provenance["proposed_at"]})
    if corrected is not None:
        _edge(connection, project_id, rule_id, corrected["id"], "corrects", edge_actor, who, step_attrs)
    _STORE.audit(connection, project_id=project_id, action="knowledge.rule.propose", entity_id=rule_id,
                 actor_kind=edge_actor, actor_ref=who,
                 detail={"category": category, "scan_id": attrs["scan_id"], "corrects": previous_id})
    from .age import after_write

    after_write(connection, project_id)
    return rule_payload(connection, project_id, rule_id)


def _require_person(actor_kind: str) -> None:
    if actor_kind != "person":
        raise PermissionError("Only a person reviews a rule: an agent proposes it, and a person "
                              "approves, edits or rejects it in Rules review.")


def _require_reviewer(actor_ref: Optional[str]) -> str:
    ref = str(actor_ref or "").strip()
    if not ref:
        raise RuleRefused("The reviewer is required (actor).")
    return ref


def _open_rule(connection: Connection, project_id: str, rule_id: str, allowed: tuple[str, ...]) -> dict:
    rule = _rule_row(connection, project_id, rule_id)
    status = (rule["attrs"] or {}).get("status") or "proposed"
    if rule["valid_to"] is not None or status not in allowed:
        raise RuleRefused(f"Rule {rule_id} is {status}; it can be reviewed when it is "
                          f"{' or '.join(allowed)}.")
    return rule


def _review(actor_ref: str, note: Optional[str], decision_id: Optional[str],
            actor_name: Optional[str] = None) -> dict[str, Any]:
    """The review record. `by` is the reviewer's stable principal id; `by_name` the person's
    display name at the time (provenance names a person, never an id)."""
    return {"by": actor_ref, "by_name": _name(actor_name), "at": _now_iso(),
            "decision_id": decision_id,
            "note": _clean(note, field="note", limit=MAX_RATIONALE, required=False) or None}


def _name(value: Optional[str]) -> Optional[str]:
    named = str(value or "").strip()
    return named[:200] or None


def _person(connection: Connection, project_id: str, actor_ref: str,
            actor_name: Optional[str] = None) -> str:
    named = _name(actor_name)
    return _anchor(connection, project_id, "person", f"person:{actor_ref}", named or actor_ref,
                   {"ref": actor_ref, **({"name": named} if named else {})})


def _governs(connection: Connection, project_id: str, rule_id: str, scope: Mapping[str, Any],
             actor_ref: str) -> int:
    """``governs`` edges from an approved rule to every symbol and package its scope names (areas
    have no node; they stay in ``attrs.scope`` and are matched there)."""
    written = 0
    for key in scope.get("symbols") or []:
        node = connection.execute(text(
            "SELECT id FROM kg_nodes WHERE project_id = :p AND node_key = :k AND knowledge_layer = 'code' "
            "AND (valid_to IS NULL OR valid_to > now()) LIMIT 1"), {"p": project_id, "k": key}).scalar()
        if node:
            _edge(connection, project_id, rule_id, node, "governs", "person", actor_ref)
            written += 1
    repo = scope.get("repo")
    for package in scope.get("packages") or []:
        nodes = connection.execute(text(
            "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'module' "
            "AND (valid_to IS NULL OR valid_to > now()) "
            "AND (node_key = :k OR (CAST(:repo AS TEXT) IS NULL AND name = :k) "
            "     OR node_key = 'module:' || CAST(:repo AS TEXT) || ':' || :k)"),
            {"p": project_id, "k": package, "repo": repo}).scalars().all()
        for node in nodes:
            _edge(connection, project_id, rule_id, node, "governs", "person", actor_ref)
            written += 1
    return written


def _merge_attrs(connection: Connection, rule_id: str, patch: Mapping[str, Any]) -> None:
    connection.execute(text(
        "UPDATE kg_nodes SET attrs = attrs || CAST(:patch AS JSONB), last_seen = now() WHERE id = :id"),
        {"patch": json.dumps(dict(patch)), "id": rule_id})


def _supersede(connection: Connection, project_id: str, old_id: str, new_id: str, actor_ref: str) -> None:
    """Close ``old_id``, superseded by ``new_id`` (a ``supersedes`` edge, status superseded)."""
    _merge_attrs(connection, old_id, {"status": "superseded", "superseded_by": new_id})
    _STORE.close_validity(connection, project_id=project_id, valid_to=datetime.now(timezone.utc),
                          actor_kind="person", node_id=old_id, superseded_by=new_id)
    _STORE.audit(connection, project_id=project_id, action="knowledge.rule.supersede", entity_id=old_id,
                 actor_kind="person", actor_ref=actor_ref, detail={"superseded_by": new_id})


def _approve_in_place(connection: Connection, project_id: str, rule: Mapping[str, Any], actor_ref: str,
                      review: dict[str, Any]) -> None:
    attrs = rule["attrs"] or {}
    _STORE.set_trust(connection, project_id=project_id, node_id=rule["id"], trust_tier="approved",
                     actor_kind="person")
    _merge_attrs(connection, rule["id"], {"status": "approved", "review": review})
    _edge(connection, project_id, rule["id"],
          _person(connection, project_id, actor_ref, review.get("by_name")), "approved_by",
          "person", actor_ref, {"at": review["at"], "note": review["note"]})
    _governs(connection, project_id, rule["id"], attrs.get("scope") or {}, actor_ref)
    if rule.get("kind") == DQ_RULE_KIND:
        _thresholds_follow(connection, project_id, rule, "approved", review)
    corrected = attrs.get("corrects")
    if corrected:
        old = _rule_row(connection, project_id, corrected)
        if old["valid_to"] is None:
            _supersede(connection, project_id, corrected, rule["id"], actor_ref)


def approve_rule(
    connection: Connection,
    *,
    project_id: str,
    rule_id: str,
    actor_ref: str,
    actor_kind: str = "person",
    note: Optional[str] = None,
    decision_id: Optional[str] = None,
    actor_name: Optional[str] = None,
) -> dict[str, Any]:
    """A person approves a proposed rule (``KgRuleActionResponse``). A correction, once approved,
    supersedes the rule it corrects. `actor_name`: the person's display name, kept beside the
    id so provenance names them."""
    _entitled(project_id)
    _require_person(actor_kind)
    actor_ref = _require_reviewer(actor_ref)
    rule = _open_rule(connection, project_id, rule_id, ("proposed",))
    review = _review(actor_ref, note, decision_id, actor_name)
    _approve_in_place(connection, project_id, rule, actor_ref, review)
    _STORE.audit(connection, project_id=project_id, action="knowledge.rule.approve", entity_id=rule_id,
                 actor_kind="person", actor_ref=actor_ref,
                 detail={"note": review["note"], "decision_id": decision_id,
                         "by_name": review["by_name"]})
    from .age import after_write

    after_write(connection, project_id)
    return {"rule": rule_payload(connection, project_id, rule_id), "previous": None}


def edit_rule(
    connection: Connection,
    *,
    project_id: str,
    rule_id: str,
    actor_ref: str,
    actor_kind: str = "person",
    statement: Optional[str] = None,
    title: Optional[str] = None,
    category: Optional[str] = None,
    scope: Optional[Mapping[str, Any]] = None,
    note: Optional[str] = None,
    decision_id: Optional[str] = None,
    actor_name: Optional[str] = None,
) -> dict[str, Any]:
    """A person edits a proposed or approved rule: version n+1, approved in the same act,
    ``supersedes`` the old version (closed). Its evidence and lineage carry over."""
    _entitled(project_id)
    _require_person(actor_kind)
    actor_ref = _require_reviewer(actor_ref)
    old = _open_rule(connection, project_id, rule_id, ("proposed", "approved"))
    attrs = dict(old["attrs"] or {})
    new_statement = _clean(statement, field="statement", limit=MAX_STATEMENT) if statement is not None \
        else attrs.get("statement") or old["body"]
    old_title = attrs.get("title") or old["name"]          # a DQ rule's name carries its id
    new_title = _clean(title, field="title", limit=MAX_TITLE) if title is not None else old_title
    new_category = str(category).strip().lower() if category is not None else attrs.get("category")
    if new_category not in CATEGORIES:
        raise RuleRefused(f"category is one of {', '.join(sorted(CATEGORIES))}.")
    old_scope = attrs.get("scope") or {}
    if scope is not None:
        if not isinstance(scope, Mapping):
            raise RuleRefused("scope must be {areas[], packages[], symbols[]}.")
        merged = {k: scope[k] if k in scope else old_scope.get(k)
                  for k in ("areas", "packages", "symbols")}
        evidence_nodes = [dict(r) for r in connection.execute(text(
            "SELECT n.id, n.node_key, n.repo FROM kg_edges e JOIN kg_nodes n ON n.id = e.dst_id "
            "WHERE e.src_id = :id AND e.kind = 'evidenced_by' AND n.kind <> 'clause'"), {"id": rule_id}).mappings()]
        new_scope, _, _ = _scope(connection, project_id, merged, evidence_nodes)
        if scope.get("repo"):
            new_scope["repo"] = str(scope["repo"])
    else:
        new_scope = old_scope
    if (new_statement, new_title, new_category, json.dumps(new_scope, sort_keys=True)) == (
            attrs.get("statement") or old["body"], old_title, attrs.get("category"),
            json.dumps(old_scope, sort_keys=True)):
        raise RuleRefused("Nothing changed. Approve the rule as it is, or change its statement, title, "
                          "category or scope.")
    version = int(attrs.get("version") or 1) + 1
    kind = str(old.get("kind") or "rule")
    content_key = _content_key(new_scope, new_statement)
    if kind == DQ_RULE_KIND:
        # A rulebook rule keeps its id: version n+1 of NPB-DQ-LN-003 is NPB-DQ-LN-003.v<n+1>.
        base = str((attrs.get("dq") or {}).get("rule_id") or old["node_key"]).split(".v")[0]
        key = f"{base}.v{version}"
    else:
        key = _node_key(content_key, version)
    clash = connection.execute(text(
        "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = :kind AND node_key = :k"),
        {"p": project_id, "k": key, "kind": kind}).scalar()
    if clash:
        raise RuleRefused(f"That version already exists as rule {clash}.")
    review = _review(actor_ref, note, decision_id, actor_name)
    new_attrs = {
        **attrs, "statement": new_statement, "category": new_category, "scope": new_scope,
        "status": "approved", "review": review, "version": version, "previous_id": rule_id,
        "content_key": content_key,
        "edited": {"by": actor_ref, "by_name": review["by_name"], "at": review["at"]},
    }
    name = new_title
    if kind == DQ_RULE_KIND:
        rid = str((attrs.get("dq") or {}).get("rule_id") or "")
        new_attrs["title"] = new_title
        name = f"{rid} — {new_title}" if rid and not new_title.startswith(rid) else new_title
    new_id = _STORE.upsert_node(connection, project_id=project_id, kind=kind, node_key=key,
                                name=name, body=new_statement, trust_tier="approved",
                                actor_kind="person", actor_ref=actor_ref, attrs=new_attrs, source="rules")
    # Its evidence and lineage carry over; a data-quality rule's links (the CDEs it checks, the
    # columns and report lines it reaches) and its thresholds too.
    carried = ("evidenced_by", "proposed_by", "corrects", *_DQ_LINK_EDGES) if kind == DQ_RULE_KIND else \
        ("evidenced_by", "proposed_by", "corrects")
    for row in connection.execute(text(
            "SELECT dst_id, kind, attrs FROM kg_edges WHERE src_id = :id AND kind = ANY(:kinds) "
            "AND valid_to IS NULL"), {"id": rule_id, "kinds": list(carried)}).mappings().all():
        _edge(connection, project_id, new_id, row["dst_id"], row["kind"], "person", actor_ref, row["attrs"] or {})
    if kind == DQ_RULE_KIND:
        for row in connection.execute(text(
                "SELECT src_id, kind, attrs FROM kg_edges WHERE dst_id = :id AND kind = ANY(:kinds) "
                "AND valid_to IS NULL"), {"id": rule_id, "kinds": ["of", "OF"]}).mappings().all():
            _edge(connection, project_id, row["src_id"], new_id, row["kind"], "person", actor_ref,
                  row["attrs"] or {})
        _thresholds_follow(connection, project_id, {**old, "id": new_id, "attrs": new_attrs}, "approved", review)
    _edge(connection, project_id, new_id,
          _person(connection, project_id, actor_ref, review.get("by_name")), "approved_by",
          "person", actor_ref, {"at": review["at"], "note": review["note"]})
    _governs(connection, project_id, new_id, new_scope, actor_ref)
    _supersede(connection, project_id, rule_id, new_id, actor_ref)
    corrected = attrs.get("corrects")
    if corrected and corrected != rule_id:
        target = _rule_row(connection, project_id, corrected)
        if target["valid_to"] is None:
            _supersede(connection, project_id, corrected, new_id, actor_ref)
    _STORE.audit(connection, project_id=project_id, action="knowledge.rule.edit", entity_id=new_id,
                 actor_kind="person", actor_ref=actor_ref,
                 detail={"previous_id": rule_id, "version": version, "note": review["note"],
                         "by_name": review["by_name"]})
    from .age import after_write

    after_write(connection, project_id)
    return {"rule": rule_payload(connection, project_id, new_id),
            "previous": rule_payload(connection, project_id, rule_id)}


def reject_rule(
    connection: Connection,
    *,
    project_id: str,
    rule_id: str,
    actor_ref: str,
    actor_kind: str = "person",
    note: Optional[str] = None,
    decision_id: Optional[str] = None,
    actor_name: Optional[str] = None,
) -> dict[str, Any]:
    """A person rejects a proposed rule, or retires an approved one: closed, with the note."""
    _entitled(project_id)
    _require_person(actor_kind)
    actor_ref = _require_reviewer(actor_ref)
    _open_rule(connection, project_id, rule_id, ("proposed", "approved"))
    review = _review(actor_ref, note, decision_id, actor_name)
    _merge_attrs(connection, rule_id, {"status": "rejected", "review": review})
    rule = _rule_row(connection, project_id, rule_id)
    if rule.get("kind") == DQ_RULE_KIND:
        _thresholds_follow(connection, project_id, rule, "rejected", review)
    _STORE.close_validity(connection, project_id=project_id, valid_to=datetime.now(timezone.utc),
                          actor_kind="person", node_id=rule_id)
    _STORE.audit(connection, project_id=project_id, action="knowledge.rule.reject", entity_id=rule_id,
                 actor_kind="person", actor_ref=actor_ref,
                 detail={"note": review["note"], "by_name": review["by_name"]})
    from .age import after_write

    after_write(connection, project_id)
    return {"rule": rule_payload(connection, project_id, rule_id), "previous": None}


# --- data-quality rules read from a document (rulebook source G4/G5) ----------------------------
#: The links a data-quality rule draws to the project's catalogue, by the relationship names of
#: tools/dq_sample/README.md (an ontology pack may spell them in capitals or lower case).
_DQ_LINK_EDGES = ("checks", "CHECKS", "applies_to", "APPLIES_TO", "reaches", "REACHES")
MAX_DQ_LIST = 40


def dq_kinds_missing(connection: Connection, project_id: str) -> list[str]:
    """The data-quality kinds this project's ontology does not grant (empty: it can hold DQ rules)."""
    from .ontology_packs import effective_node_kinds

    kinds = effective_node_kinds(connection, project_id=project_id)
    return [k for k in (DQ_RULE_KIND, DQ_THRESHOLD_KIND) if k not in kinds]


def require_dq_kinds(connection: Connection, project_id: str) -> None:
    missing = dq_kinds_missing(connection, project_id)
    if missing:
        raise RuleRefused(
            "This project's ontology cannot hold data-quality rules: it lacks "
            + " and ".join(f"'{k}'" for k in missing)
            + ". Assign the banking-dq ontology pack (or a pack that includes it) in Knowledge Graph › "
            "Documents, then extract the rules again.")


def _edge_kind(connection: Connection, project_id: str, name: str) -> Optional[str]:
    """The project's spelling of a relationship (``CHECKS`` or ``checks``), or None when its
    ontology grants neither."""
    from .ontology_packs import effective_edge_kinds
    from .store import EDGE_KINDS

    granted = set(EDGE_KINDS) | set(effective_edge_kinds(connection, project_id=project_id))
    for spelling in (name.lower(), name.upper()):
        if spelling in granted:
            return spelling
    return None


def _catalogue_node(connection: Connection, project_id: str, kinds: Sequence[str], ref: str,
                    *, attr: Optional[str] = None) -> Optional[str]:
    """An existing node of one of ``kinds`` named ``ref`` (its key, its name, or ``attrs[attr]``)."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    params = {"p": project_id, "kinds": list(kinds), "r": ref}
    extra = ""
    if attr:
        extra = " OR attrs->>:attr = :r"
        params["attr"] = attr
    return connection.execute(text(
        "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = ANY(:kinds) "
        "AND (valid_to IS NULL OR valid_to > now()) "
        f"AND (node_key = :r OR name = :r{extra}) ORDER BY (node_key = :r) DESC, id LIMIT 1"),
        params).scalar()


def _dq_list(value: Any, field: str) -> list[str]:
    items = _as_list(value, field)
    return [_clean(v, field=field, limit=300) for v in items][:MAX_DQ_LIST]


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None          # NaN is not a threshold


def propose_dq_rule(
    connection: Connection,
    *,
    project_id: str,
    rule_id: str,
    title: str,
    statement: str,
    clause: str,
    quote: str,
    version: Optional[int] = None,
    dimension: str = "",
    severity: str = "",
    check: str = "",
    owner: str = "",
    system: str = "",
    feed: str = "",
    cdes: Sequence[str] = (),
    columns: Sequence[str] = (),
    tables: Sequence[str] = (),
    report_lines: Sequence[str] = (),
    mdrm: Sequence[str] = (),
    related: Sequence[str] = (),
    legal_reference: str = "",
    thresholds: Sequence[Mapping[str, Any]] = (),
    confidence: float = 0.8,
    scan_id: Optional[str] = None,
    actor_kind: str = "agent",
    actor_ref: Optional[str] = None,
) -> dict[str, Any]:
    """Propose one data-quality rule read from a rulebook clause, with its Warning/Error
    ``dq_threshold`` nodes. Everything is ``inferred`` until a person approves it in Rules review.

    Refused, in words, when the project's ontology lacks the banking-dq kinds, when the clause is
    not a clause of this project, when the quote is not verbatim in it, or when the rule id is not
    printed in the clause (a rule is never invented). Proposing the same rule id again returns the
    existing proposal (a rejected one is refused). Links to the project's CDE, column, table and
    report-line nodes are drawn by name when they exist; what did not match is recorded on the rule
    (``attrs.links.unmatched``), never created.
    """
    _entitled(project_id)
    require_dq_kinds(connection, project_id)
    if actor_kind not in ("agent", "person", "system"):
        raise RuleRefused(f"unknown actor kind {actor_kind!r}")
    rule_id = _clean(rule_id, field="rule id", limit=80)
    title = _clean(title, field="title", limit=MAX_TITLE)
    statement = _clean(statement, field="statement", limit=MAX_STATEMENT)
    clause_node, cite = clause_evidence(connection, project_id, {"clause": clause, "quote": quote})
    if rule_id not in _squeeze(clause_node["body"]):
        raise RuleRefused(f"Rule {rule_id} is not printed in clause {clause_node['name']}: a rule is "
                          "proposed only from the clause that states it.")
    try:
        confidence = min(1.0, max(0.0, float(confidence)))
    except (TypeError, ValueError) as error:
        raise RuleRefused("confidence is a number from 0 to 1.") from error
    levels = []
    for item in thresholds or []:
        if not isinstance(item, Mapping):
            continue
        level = str(item.get("level") or "").strip().lower()
        if level not in DQ_LEVELS or any(t["level"] == level for t in levels):
            continue
        words = _clean(item.get("text"), field=f"{level} threshold", limit=600, required=False)
        levels.append({"level": level, "text": words or None,
                       "metric": _clean(item.get("metric"), field="metric", limit=40, required=False) or None,
                       "value": _number(item.get("value"))})
    dq = {
        "rule_id": rule_id, "version": int(version) if isinstance(version, int) or str(version or "").isdigit() else None,
        "title": title,
        "dimension": _clean(dimension, field="dimension", limit=40, required=False).lower() or None,
        "severity": _clean(severity, field="severity", limit=40, required=False) or None,
        "check": _clean(check, field="check", limit=MAX_STATEMENT, required=False) or None,
        "owner": _clean(owner, field="owner", limit=200, required=False) or None,
        "system": _clean(system, field="system", limit=80, required=False) or None,
        "feed": _clean(feed, field="feed", limit=80, required=False) or None,
        "cdes": _dq_list(cdes, "cdes"), "columns": _dq_list(columns, "columns"),
        "tables": _dq_list(tables, "tables"), "report_lines": _dq_list(report_lines, "report_lines"),
        "mdrm": _dq_list(mdrm, "mdrm"), "related": _dq_list(related, "related"),
        "legal_reference": _clean(legal_reference, field="legal reference", limit=300, required=False) or None,
        "source": "document", "document_id": (clause_node.get("attrs") or {}).get("document_id"),
        "clause_section": cite.get("section"), "page": cite.get("page"),
    }
    existing = connection.execute(text(
        "SELECT id, attrs FROM kg_nodes WHERE project_id = :p AND kind = :k AND node_key = :key"),
        {"p": project_id, "k": DQ_RULE_KIND, "key": rule_id}).mappings().first()
    if existing is not None:
        status = (existing["attrs"] or {}).get("status")
        if status == "rejected":
            review = (existing["attrs"] or {}).get("review") or {}
            note = f": {review['note']}" if review.get("note") else ""
            raise RuleRefused(f"Rule {rule_id} was rejected by {review.get('by') or 'a reviewer'}{note}.")
        return rule_payload(connection, project_id, existing["id"])

    ctx = _context()
    provenance = _provenance(connection, project_id, actor_kind, actor_ref)
    who = provenance["agent_role"] if actor_kind == "agent" else (actor_ref or actor_kind)
    edge_actor = "agent" if actor_kind == "agent" else ("person" if actor_kind == "person" else "system")
    attrs = {
        "title": title, "statement": statement, "rationale": None, "category": "validation",
        "scope": {"repo": None, "areas": [], "packages": [], "symbols": []},
        "status": "proposed", "confidence": confidence, "provenance": provenance, "review": None,
        "version": 1, "previous_id": None, "content_key": rule_id,
        "scan_id": scan_id or ctx.get("knowledge_scan_id"), "corrects": None,
        "dq": dq, "thresholds": levels,
    }
    body = statement + (f" Check: {dq['check']}" if dq["check"] else "")
    node_id = _STORE.upsert_node(
        connection, project_id=project_id, kind=DQ_RULE_KIND, node_key=rule_id,
        name=f"{rule_id} — {title}", body=body, trust_tier="inferred", actor_kind=edge_actor,
        actor_ref=who, attrs=attrs, source="rules")
    _edge(connection, project_id, node_id, clause_node["id"], "evidenced_by", edge_actor, who, cite)
    of = _edge_kind(connection, project_id, "of")
    for level in levels:
        words = level["text"] or level["level"]
        threshold_id = _STORE.upsert_node(
            connection, project_id=project_id, kind=DQ_THRESHOLD_KIND, node_key=f"{rule_id}:{level['level']}",
            name=f"{rule_id} {level['level'].capitalize()} when {words}", body=words,
            trust_tier="inferred", actor_kind=edge_actor, actor_ref=who, source="rules",
            attrs={"rule_id": rule_id, "level": level["level"], "metric": level["metric"],
                   "value": level["value"], "text": level["text"], "status": "proposed"})
        if of:
            _edge(connection, project_id, threshold_id, node_id, of, edge_actor, who)
        _edge(connection, project_id, threshold_id, clause_node["id"], "evidenced_by", edge_actor, who, cite)
    links = _link_dq_rule(connection, project_id, node_id, dq, edge_actor, who)
    _merge_attrs(connection, node_id, {"links": links})
    if actor_kind == "agent" and provenance["agent_role"]:
        agent_id = _anchor(connection, project_id, "agent", f"agent:{provenance['agent_role']}",
                           provenance["agent_name"] or provenance["agent_role"],
                           {"role_key": provenance["agent_role"]})
        _edge(connection, project_id, node_id, agent_id, "proposed_by", edge_actor, who,
              {"run_id": provenance["run_id"], "step": provenance["step"]})
    run_ref = provenance["run_id"] or provenance["workflow_run_id"]
    if run_ref:
        from .learning_writeback import run_anchor

        run_node = run_anchor(connection, project_id, run_ref)
        _edge(connection, project_id, run_node, node_id, "produced", edge_actor, who,
              {"step": provenance["step"], "at": provenance["proposed_at"]})
    _STORE.audit(connection, project_id=project_id, action="knowledge.rule.propose", entity_id=node_id,
                 actor_kind=edge_actor, actor_ref=who,
                 detail={"category": "validation", "scan_id": attrs["scan_id"], "rule_id": rule_id,
                         "source": "document"})
    from .age import after_write

    after_write(connection, project_id)
    return rule_payload(connection, project_id, node_id)


def _link_dq_rule(connection: Connection, project_id: str, node_id: str, dq: Mapping[str, Any],
                  actor_kind: str, actor_ref: Optional[str]) -> dict[str, Any]:
    """CHECKS the CDEs, APPLIES_TO the columns and tables, REACHES the report lines — each only
    when the project already holds that node, by name. Returns what was linked and what was not."""
    plan = (
        ("checks", "cde", ("cde",), list(dq.get("cdes") or []), None),
        ("applies_to", "column", ("database_column",), list(dq.get("columns") or []), None),
        ("applies_to", "table", ("database_table", "table"), list(dq.get("tables") or []), None),
        # A report line by its id or caption, or by the MDRM code it carries.
        ("reaches", "report_line", ("report_line",), list(dq.get("report_lines") or []) + list(dq.get("mdrm") or []), "mdrm"),
    )
    linked: dict[str, list[str]] = {}
    unmatched: dict[str, list[str]] = {}
    missing_edges: list[str] = []
    for relation, what, kinds, refs, attr in plan:
        if not refs:
            continue
        edge = _edge_kind(connection, project_id, relation)
        for ref in refs:
            target = _catalogue_node(connection, project_id, kinds, ref, attr=attr)
            if target is None:
                # A report line is matched by its MDRM code; a printed caption that names no
                # node is not a miss when its code matched.
                if what != "report_line" or ref in (dq.get("mdrm") or []):
                    unmatched.setdefault(what, []).append(ref)
                continue
            if edge is None:
                if relation.upper() not in missing_edges:
                    missing_edges.append(relation.upper())
                unmatched.setdefault(what, []).append(ref)
                continue
            _edge(connection, project_id, node_id, target, edge, actor_kind, actor_ref)
            if ref not in linked.setdefault(what, []):
                linked[what].append(ref)
    return {"linked": linked, "unmatched": unmatched, "missing_relationships": missing_edges}


def _thresholds_follow(connection: Connection, project_id: str, rule: Mapping[str, Any], status: str,
                       review: Mapping[str, Any]) -> None:
    """A data-quality rule's thresholds share its review: approved with it, closed with it."""
    rid = str(((rule.get("attrs") or {}).get("dq") or {}).get("rule_id") or "")
    if not rid:
        return
    ids = connection.execute(text(
        "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = :k AND attrs->>'rule_id' = :r "
        "AND (valid_to IS NULL OR valid_to > now())"),
        {"p": project_id, "k": DQ_THRESHOLD_KIND, "r": rid}).scalars().all()
    for threshold_id in ids:
        if status == "approved":
            _STORE.set_trust(connection, project_id=project_id, node_id=threshold_id,
                             trust_tier="approved", actor_kind="person")
            _merge_attrs(connection, threshold_id, {"status": "approved", "review": dict(review)})
        else:
            _merge_attrs(connection, threshold_id, {"status": status, "review": dict(review)})
            _STORE.close_validity(connection, project_id=project_id,
                                  valid_to=datetime.now(timezone.utc), actor_kind="person",
                                  node_id=threshold_id)
