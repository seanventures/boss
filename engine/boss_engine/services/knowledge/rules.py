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
ENTITLEMENT = "knowledge.rules"

MAX_TITLE = 200
MAX_STATEMENT = 2000
MAX_RATIONALE = 4000
MAX_EXCERPT = 1200
MAX_EVIDENCE = 20
MAX_SCOPE_ITEMS = 50

_STORE = PostgresKnowledgeStore()
_SPACES = re.compile(r"\s+")


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
        "recorded_at, first_seen FROM kg_nodes WHERE id = :id AND kind = 'rule'"),
        {"id": rule_id}).mappings().first()
    if row is None:
        raise LookupError(f"rule not found: {rule_id}")
    if row["project_id"] != project_id:
        raise PermissionError(f"rule {rule_id} is not in project {project_id}")
    return dict(row)


def _sub(status: str, category: str) -> str:
    return f"{status} · {category.replace('_', ' ')}"


def _evidence(connection: Connection, rule_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT e.attrs AS edge_attrs, n.id, n.node_key, n.name, n.path FROM kg_edges e "
        "JOIN kg_nodes n ON n.id = e.dst_id "
        "WHERE e.src_id = :id AND e.kind = 'evidenced_by' ORDER BY e.first_seen, n.node_key"),
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


def rule_payload(connection: Connection, project_id: str, rule_id: str) -> dict[str, Any]:
    """A rule as the view payload (``KgRule``)."""
    rule = _rule_row(connection, project_id, rule_id)
    attrs = rule["attrs"] or {}
    status = str(attrs.get("status") or "proposed")
    category = str(attrs.get("category") or "other")
    provenance = dict(attrs.get("provenance") or {})
    review = attrs.get("review")
    scope = attrs.get("scope") or {}
    return {
        "node": {"id": rule["id"], "key": rule["node_key"], "kind": "rule", "layer": "rules",
                 "label": rule["name"], "sub": _sub(status, category),
                 "trust_tier": rule["trust_tier"], "status": status},
        "title": rule["name"],
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
) -> dict[str, Any]:
    """The Rules review queue (``KgRulesResponse``): the rules of one status (all when None),
    newest first, with counts per status under the same scan/area filter."""
    if status is not None and status not in STATUSES:
        raise RuleRefused(f"status is one of {', '.join(sorted(STATUSES))}.")
    _STORE._org_id(connection, project_id, None)
    where = "project_id = :p AND kind = 'rule'"
    params: dict[str, Any] = {"p": project_id}
    if scan_id:
        where += " AND attrs->>'scan_id' = :scan"
        params["scan"] = scan_id
    area_sql, area_params = _area_filter_sql(area)
    where += area_sql
    params.update(area_params)
    counts = {s: 0 for s in sorted(STATUSES)}
    for value, n in connection.execute(text(
            f"SELECT coalesce(attrs->>'status', 'proposed'), count(*) FROM kg_nodes WHERE {where} GROUP BY 1"),
            params).all():
        if value in counts:
            counts[value] = int(n)
    listed = where + (" AND coalesce(attrs->>'status', 'proposed') = :status" if status else "")
    ids = connection.execute(text(
        f"SELECT id FROM kg_nodes WHERE {listed} ORDER BY first_seen DESC, id LIMIT :n"),
        {**params, "status": status, "n": max(1, min(int(limit), 500))}).scalars().all()
    return {"items": [rule_payload(connection, project_id, rid) for rid in ids],
            "counts": counts, "scan_id": scan_id}


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
        return (f"{a}project_id = :p AND {a}kind = 'rule' AND {a}trust_tier = :tier "
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
        found = connection.execute(text(
            f"SELECT id FROM kg_nodes WHERE {base()} AND search_tsv @@ plainto_tsquery('english', :q) "
            "ORDER BY ts_rank(search_tsv, plainto_tsquery('english', :q)) DESC LIMIT :n"),
            {**params, "q": query}).scalars().all()
        matches.extend(i for i in found if i not in matches)
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


def _evidence_nodes(connection: Connection, project_id: str,
                    evidence: Sequence[Mapping[str, Any]]) -> list[tuple[dict, dict]]:
    if not isinstance(evidence, (list, tuple)) or not evidence:
        raise RuleRefused("A rule needs evidence: at least one {symbol, lines, excerpt} from the code.")
    if len(evidence) > MAX_EVIDENCE:
        raise RuleRefused(f"{len(evidence)} pieces of evidence; at most {MAX_EVIDENCE}.")
    out = []
    for item in evidence:
        if not isinstance(item, Mapping):
            raise RuleRefused("Each piece of evidence is {symbol, lines, excerpt}.")
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
    resolved, symbol_nodes, package_nodes = _scope(connection, project_id, scope, [n for n, _ in cited])

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
        run_id = _anchor(connection, project_id, "run", f"run:{run_ref}", f"Run {run_ref}",
                         {"source_id": run_ref})
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
    new_title = _clean(title, field="title", limit=MAX_TITLE) if title is not None else old["name"]
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
            "WHERE e.src_id = :id AND e.kind = 'evidenced_by'"), {"id": rule_id}).mappings()]
        new_scope, _, _ = _scope(connection, project_id, merged, evidence_nodes)
        if scope.get("repo"):
            new_scope["repo"] = str(scope["repo"])
    else:
        new_scope = old_scope
    if (new_statement, new_title, new_category, json.dumps(new_scope, sort_keys=True)) == (
            attrs.get("statement") or old["body"], old["name"], attrs.get("category"),
            json.dumps(old_scope, sort_keys=True)):
        raise RuleRefused("Nothing changed. Approve the rule as it is, or change its statement, title, "
                          "category or scope.")
    version = int(attrs.get("version") or 1) + 1
    content_key = _content_key(new_scope, new_statement)
    key = _node_key(content_key, version)
    clash = connection.execute(text(
        "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'rule' AND node_key = :k"),
        {"p": project_id, "k": key}).scalar()
    if clash:
        raise RuleRefused(f"That version already exists as rule {clash}.")
    review = _review(actor_ref, note, decision_id, actor_name)
    new_attrs = {
        **attrs, "statement": new_statement, "category": new_category, "scope": new_scope,
        "status": "approved", "review": review, "version": version, "previous_id": rule_id,
        "content_key": content_key,
        "edited": {"by": actor_ref, "by_name": review["by_name"], "at": review["at"]},
    }
    new_id = _STORE.upsert_node(connection, project_id=project_id, kind="rule", node_key=key,
                                name=new_title, body=new_statement, trust_tier="approved",
                                actor_kind="person", actor_ref=actor_ref, attrs=new_attrs, source="rules")
    for row in connection.execute(text(
            "SELECT dst_id, kind, attrs FROM kg_edges WHERE src_id = :id AND kind IN ('evidenced_by', 'proposed_by', 'corrects')"),
            {"id": rule_id}).mappings().all():
        _edge(connection, project_id, new_id, row["dst_id"], row["kind"], "person", actor_ref, row["attrs"] or {})
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
    _STORE.close_validity(connection, project_id=project_id, valid_to=datetime.now(timezone.utc),
                          actor_kind="person", node_id=rule_id)
    _STORE.audit(connection, project_id=project_id, action="knowledge.rule.reject", entity_id=rule_id,
                 actor_kind="person", actor_ref=actor_ref,
                 detail={"note": review["note"], "by_name": review["by_name"]})
    from .age import after_write

    after_write(connection, project_id)
    return {"rule": rule_payload(connection, project_id, rule_id), "previous": None}
