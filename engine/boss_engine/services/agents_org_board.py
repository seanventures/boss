"""Agents › Organization in one read (org-view spec §6.1): who works with whom, where work
waits, what each team spends and lacks.

THE CONTRACT is frozen beside apps/web/lib/agents-org-board.ts. Pure composition plus three
aggregates (work counts, busy time, flow columns); no tables of its own. Every number is a
count or a sum over recorded rows:

  teams and members   agents_live_board.force()     (one resolver: agent_identity.team_of)
  handoffs            agent_acts.observed_edges(with_outcomes, include_people)
  work, open, busy    agent_acts in the window
  spend and models    llm_telemetry.attribute_llm_calls (the Directory reads the same helper)
  gaps                the Kit: kit.holders / kit.recommended / kit.used_in
  missing roles       agents.missing_roles; Named on: agents.steps_naming_all
  plan vs work        org_assignments against 30 days of handoffs

LIVE STATE IS NOT HERE. The web overlays the 4s presence store, so this payload (and its ETag)
changes only when a windowed fact changes. Nothing here is a percent, an ETA, a capacity or a
score: there is no honest source for any of them (§0, §9).
"""
from __future__ import annotations

import hashlib
import json
import statistics
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: The window chips: 24h · 7d · 30d.
WINDOWS = (1, 7, 30)
#: The kinds of act that are work an agent was handed (a run is the person's; an approval is a
#: decision, not work).
WORK_KINDS = ("step", "stage", "seat", "chat", "handoff")
#: The kinds whose open span is time an agent was busy. Handoff acts are instants.
BUSY_KINDS = ("step", "stage", "seat", "chat")
OPEN = ("pending", "active", "awaiting_input")
WAITING_MAX = 5
AGENT_EDGES_MAX = 200
FLOW_RUNS_MAX = 500
#: Plan vs work reads 30 days whatever the window says (§4.5), and calls a pair "works
#: together" from this many handoffs.
DRIFT_HOURS = 720
DRIFT_MIN_HANDOFFS = 5
DRIFT_MAX = 10
#: Server cache per (project, days). A write's refresh asks with fresh=1 and skips it.
CACHE_SECONDS = 15.0

_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


class OrgBoardError(ValueError):
    """A request this board cannot answer (a window it does not offer)."""


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _usd(micros: int) -> float:
    return round(int(micros) / 1_000_000, 6)


def _savepoint(connection: Connection, fn, *args: Any, **kwargs: Any) -> tuple[Any, Optional[str]]:
    """(result, None), or (None, reason) when the read failed: one source failing never takes
    the board down — its rows say `unknown` and the rest stands."""
    try:
        with connection.begin_nested():
            return fn(*args, **kwargs), None
    except Exception as error:  # noqa: BLE001 — reported as the row's reason
        return None, f"{type(error).__name__}: {str(error).splitlines()[0][:200]}"


# ── the aggregates ───────────────────────────────────────────────────────────────────────────


def work_counts(connection: Connection, project_id: str, hours: int) -> dict[str, dict[str, int]]:
    """{role: {done, failed, open}}: acts that ENDED in the window as complete / error, and acts
    still open (any age — an old open item is exactly the stuck signal)."""
    rows = connection.execute(
        text(
            """
            SELECT role_key,
                   count(*) FILTER (WHERE status = 'complete' AND ended_at IS NOT NULL) AS done,
                   count(*) FILTER (WHERE status = 'error' AND ended_at IS NOT NULL) AS failed,
                   count(*) FILTER (WHERE ended_at IS NULL AND status = ANY(:open)) AS open
            FROM agent_acts
            WHERE project_id = :p AND role_key IS NOT NULL AND kind = ANY(:kinds)
              AND (ended_at IS NULL OR ended_at > now() - make_interval(hours => :h))
            GROUP BY role_key
            """
        ),
        {"p": project_id, "h": int(hours), "kinds": list(WORK_KINDS), "open": list(OPEN)},
    ).mappings().all()
    return {str(r["role_key"]): {"done": int(r["done"]), "failed": int(r["failed"]),
                                 "open": int(r["open"])} for r in rows}


def busy_seconds(connection: Connection, project_id: str, hours: int) -> dict[str, int]:
    """{role: seconds}: the UNION of the agent's act spans inside the window — never their sum,
    so a stage nested in its own step is counted once (gaps and islands)."""
    rows = connection.execute(
        text(
            """
            WITH bounds AS (SELECT now() AS t1, now() - make_interval(hours => :h) AS t0),
            spans AS (
              SELECT a.role_key,
                     greatest(a.started_at, b.t0) AS s,
                     least(CASE WHEN a.ended_at IS NOT NULL THEN a.ended_at
                                WHEN a.status = ANY(:open) THEN b.t1
                                ELSE a.started_at END, b.t1) AS e
              FROM agent_acts a, bounds b
              WHERE a.project_id = :p AND a.role_key IS NOT NULL AND a.kind = ANY(:kinds)
                AND a.started_at < b.t1
                AND (a.ended_at IS NULL OR a.ended_at > b.t0)
            ),
            valid AS (SELECT * FROM spans WHERE e > s),
            marked AS (
              SELECT role_key, s, e,
                     max(e) OVER (PARTITION BY role_key ORDER BY s, e
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end
              FROM valid
            ),
            grouped AS (
              SELECT role_key, s, e,
                     sum(CASE WHEN prev_end IS NULL OR s > prev_end THEN 1 ELSE 0 END)
                       OVER (PARTITION BY role_key ORDER BY s, e) AS island
              FROM marked
            ),
            islands AS (
              SELECT role_key, min(s) AS s, max(e) AS e FROM grouped GROUP BY role_key, island
            )
            SELECT role_key, COALESCE(sum(extract(epoch FROM (e - s))), 0) AS secs
            FROM islands GROUP BY role_key
            """
        ),
        {"p": project_id, "h": int(hours), "kinds": list(BUSY_KINDS), "open": list(OPEN)},
    ).mappings().all()
    return {str(r["role_key"]): int(round(float(r["secs"] or 0))) for r in rows}


def open_items(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Every open piece of work, oldest first, each with the agent it waits with: its own act's
    role, or — for a gate, whose act is nobody's — the agent whose work the gate holds
    (agent_acts.owners_of_decisions). A stage open inside its own agent's open step is part of
    that step, not a second item."""
    from . import agent_acts

    rows = [dict(r) for r in connection.execute(
        text(
            """
            SELECT act_id, parent_act_id, role_key, status, label, started_at,
                   workflow_run_id, decision_id
            FROM agent_acts
            WHERE project_id = :p AND ended_at IS NULL AND status = ANY(:open)
              AND kind = ANY(:kinds)
              AND (role_key IS NOT NULL
                   OR (decision_id IS NOT NULL AND status = 'awaiting_input'))
            ORDER BY started_at, act_id
            """
        ),
        {"p": project_id, "open": list(OPEN), "kinds": list(WORK_KINDS)},
    ).mappings().all()]
    owners = agent_acts.owners_of_decisions(
        connection, project_id, [r["decision_id"] for r in rows
                                 if r["role_key"] is None and r["decision_id"]])
    by_id = {r["act_id"]: r for r in rows}
    out = []
    for r in rows:
        role = r["role_key"] or owners.get(str(r["decision_id"]))
        if not role:
            continue  # a wait nobody's work is in: BOSS's or yours, never a guessed face
        parent = by_id.get(r["parent_act_id"]) if r["parent_act_id"] else None
        if parent is not None and parent["role_key"] == r["role_key"]:
            continue
        out.append({
            "role_key": str(role), "act_id": str(r["act_id"]), "status": str(r["status"]),
            "label": str(r["label"] or ""), "since": _iso(r["started_at"]),
            "run_id": r["workflow_run_id"],
            "attention_id": (str(r["decision_id"]) if r["decision_id"]
                             and r["status"] == "awaiting_input" else None),
        })
    return out


def flow_columns(connection: Connection, project_id: str, hours: int,
                 team_of_role: Mapping[str, str], team_order: list[str]) -> dict[str, int]:
    """{team: column}: where each team usually joins a run. For the newest runs (≤500) started
    in the window, the acts in order give each team the index at which it first appears; a
    team's column is the dense rank of the median of those indices. Recomputed per window, so
    the map does not move with polling. A team with no runs has no column."""
    rows = connection.execute(
        text(
            """
            WITH roots AS (
              SELECT root_act_id FROM agent_acts
              WHERE project_id = :p AND started_at > now() - make_interval(hours => :h)
              GROUP BY root_act_id ORDER BY max(started_at) DESC LIMIT :n
            )
            SELECT a.root_act_id, a.role_key
            FROM agent_acts a JOIN roots r ON r.root_act_id = a.root_act_id
            WHERE a.project_id = :p AND a.role_key IS NOT NULL
            ORDER BY a.root_act_id, a.started_at, a.act_id
            """
        ),
        {"p": project_id, "h": int(hours), "n": FLOW_RUNS_MAX},
    ).mappings().all()
    joins: dict[str, list[int]] = {}
    current: Optional[str] = None
    order: list[str] = []
    for r in rows:
        if r["root_act_id"] != current:
            current, order = r["root_act_id"], []
        team = team_of_role.get(str(r["role_key"]), "yours")
        if team not in order:
            order.append(team)
            joins.setdefault(team, []).append(len(order) - 1)
    medians = {t: statistics.median(v) for t, v in joins.items()}
    distinct = sorted(set(medians.values()))
    rank = {m: i for i, m in enumerate(distinct)}
    return {t: rank[m] for t, m in medians.items()}


# ── gaps (the Kit) ───────────────────────────────────────────────────────────────────────────


def _active_steps(connection: Connection, project_id: str) -> dict[tuple[str, str], dict]:
    rows = connection.execute(
        text("""SELECT d.workflow_key, v.spec FROM workflow_definitions d
                JOIN workflow_versions v ON v.id = d.active_version_id
                WHERE d.project_id = :p"""), {"p": project_id}).mappings().all()
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        spec = r["spec"] if isinstance(r["spec"], dict) else json.loads(r["spec"] or "{}")
        for step in spec.get("steps") or []:
            if isinstance(step, dict):
                out[(str(r["workflow_key"]), str(step.get("id") or ""))] = step
    return out


def kit_gaps(connection: Connection, settings: Any, project_id: str) -> dict[str, Any]:
    """{roles: {role: {abilities: [...], step_gaps: [...]}}, labels: {ability: label}}.

    MISSING means one of two recorded things, never a guess from text: an ability the role's
    spec recommends (or grants) that it does not hold, or an ability a workflow step uses
    whose named agent does not hold it."""
    from . import kit
    from .step_agents import named_agent

    defs = kit.definitions(settings, media=kit.media_installed(connection, project_id))
    abilities = defs["abilities"]
    labels = {a["key"]: str(a.get("label") or a["key"]) for a in abilities}
    held = kit.holders(connection, settings, project_id, abilities)
    recs = kit.recommended(kit._roles(connection, project_id))
    uses = kit.used_in(connection, settings, project_id, abilities)
    steps = _active_steps(connection, project_id)
    out: dict[str, dict[str, list]] = {}

    def slot(role: str) -> dict[str, list]:
        return out.setdefault(role, {"abilities": [], "step_gaps": []})

    for a in abilities:
        key = a["key"]
        holders = set(held.get(key) or [])
        for role in recs.get(key) or []:
            if role not in holders:
                slot(role)["abilities"].append({"ability": key, "label": labels[key]})
        seen: set[tuple[str, str]] = set()
        for use in uses.get(key) or []:
            where = (use["workflow_key"], use["step_id"])
            if where in seen:
                continue
            seen.add(where)
            named = named_agent(steps.get(where) or {})
            if named and named not in holders:
                slot(named)["step_gaps"].append({
                    "workflow_key": use["workflow_key"], "workflow_name": use["workflow_name"],
                    "step_id": use["step_id"], "step_title": use["step_title"],
                    "role_key": named, "ability": key, "label": labels[key]})
    return {"roles": out, "labels": labels}


# ── plan vs work ─────────────────────────────────────────────────────────────────────────────


def plan_vs_work(connection: Connection, project_id: str,
                 team_of_role: Mapping[str, str]) -> dict[str, Any]:
    """Planned lines with no work along them, and pairs that work together with no line.

    `Plan it` proposes a manager (operator decision 2026-10-04: the team's manager by default,
    changeable): of the two, the one that is its team's manager in the plan — the planned
    member most of the plan reports to — else the one that sends the other more work."""
    from . import agent_acts

    lines = {str(r["agent_id"]): (str(r["reports_to"]) if r["reports_to"] else None)
             for r in connection.execute(
                 text("SELECT agent_id, reports_to FROM org_assignments WHERE project_id = :p"),
                 {"p": project_id}).mappings().all()}
    edges = agent_acts.observed_edges(connection, project_id, hours=DRIFT_HOURS)
    pair: dict[tuple[str, str], int] = {}
    for e in edges:
        pair[(e["source"], e["target"])] = pair.get((e["source"], e["target"]), 0) + e["times"]

    def between(a: str, b: str) -> int:
        return pair.get((a, b), 0) + pair.get((b, a), 0)

    def planned(a: str, b: str) -> bool:
        return lines.get(a) == b or lines.get(b) == a

    planned_unused = [{"agent": a, "reports_to": p} for a, p in sorted(lines.items())
                      if p and between(a, p) == 0]
    reports: dict[str, int] = {}
    for a, p in lines.items():
        if p:
            reports[p] = reports.get(p, 0) + 1
    managers: dict[str, str] = {}
    for team in set(team_of_role.values()):
        planned_members = [r for r in set(lines) | set(reports) if team_of_role.get(r) == team]
        best = sorted(planned_members, key=lambda r: (-reports.get(r, 0),
                                                      lines.get(r) is not None, r))
        if best and reports.get(best[0], 0) > 0:
            managers[team] = best[0]
    unplanned = []
    done: set[frozenset[str]] = set()
    for (a, b) in pair:
        key = frozenset((a, b))
        if key in done or a == b:
            continue
        done.add(key)
        total = between(a, b)
        if total < DRIFT_MIN_HANDOFFS or planned(a, b):
            continue
        x, y = sorted((a, b))
        team_managers = {managers.get(team_of_role.get(x, "yours")),
                         managers.get(team_of_role.get(y, "yours"))}
        if x in team_managers and y not in team_managers:
            parent, why = x, "manager"
        elif y in team_managers and x not in team_managers:
            parent, why = y, "manager"
        else:
            parent, why = (x if pair.get((x, y), 0) >= pair.get((y, x), 0) else y), "sender"
        unplanned.append({"a": x, "b": y, "handoffs": total, "a_to_b": pair.get((x, y), 0),
                          "b_to_a": pair.get((y, x), 0), "parent": parent,
                          "child": y if parent == x else x, "parent_is": why})
    unplanned.sort(key=lambda u: (-u["handoffs"], u["a"], u["b"]))
    return {"planned_unused": planned_unused, "unplanned_pairs": unplanned[:DRIFT_MAX],
            "lines": len([p for p in lines.values() if p]), "managers": managers}


# ── the board ────────────────────────────────────────────────────────────────────────────────


def _models(counter: Mapping[str, int]) -> list[dict[str, Any]]:
    return [{"model": m, "calls": n} for m, n in
            sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))]


def _configured_model(spec: Any) -> Optional[str]:
    policy = spec.get("model_policy") if isinstance(spec, dict) else None
    if not isinstance(policy, dict):
        return None
    for key in ("model", "preferred_model", "default_model"):
        if policy.get(key):
            return str(policy[key])
    return None


def etag_of(board: Mapping[str, Any]) -> str:
    material = {k: v for k, v in board.items() if k not in ("generated_at", "etag")}
    return '"ob-' + hashlib.sha256(json.dumps(material, sort_keys=True, default=str)
                                   .encode()).hexdigest()[:24] + '"'


def build(connection: Connection, settings: Any, project_id: str, *, days: int = 7,
          fresh: bool = False) -> dict[str, Any]:
    if int(days) not in WINDOWS:
        raise OrgBoardError(f"days must be one of {', '.join(str(w) for w in WINDOWS)}.")
    days = int(days)
    key = (project_id, days)
    if not fresh:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    board = _compose(connection, settings, project_id, days)
    with _cache_lock:
        _cache[key] = (time.monotonic(), board)
    return board


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _compose(connection: Connection, settings: Any, project_id: str, days: int) -> dict[str, Any]:
    from . import agent_acts, agents, agents_live_board
    from .agent_identity import TEAMS, team_of, teams_by_role
    from .llm_telemetry import attribute_llm_calls

    hours = days * 24
    teams_raw, agents_raw = agents_live_board.force(connection, project_id, [])
    team_order = [k for k, _, _ in TEAMS]
    roles_rows = connection.execute(
        text("SELECT role_key, spec FROM roles WHERE project_id = :p AND role_key <> 'human'"),
        {"p": project_id}).mappings().all()
    registered = {str(r["role_key"]) for r in roles_rows}
    specs = {str(r["role_key"]): r["spec"] for r in roles_rows}
    team_of_role: dict[str, str] = {k: v["key"] for k, v in teams_by_role(connection,
                                                                          project_id).items()}
    for a in agents_raw:
        team_of_role.setdefault(a["role_key"], a["team"])
    teams: dict[str, dict[str, Any]] = {}
    for t in teams_raw:
        teams[t["key"]] = {"key": t["key"], "label": t["label"], "tip": t.get("tip"),
                           "members": list(t["members"])}
    members = {m for t in teams.values() for m in t["members"]}

    counts = work_counts(connection, project_id, hours)
    busy = busy_seconds(connection, project_id, hours)
    waits = open_items(connection, project_id)
    edges = agent_acts.observed_edges(connection, project_id, hours=hours, with_outcomes=True,
                                      include_people=True)
    spend_rows, spend_reason = _savepoint(connection, attribute_llm_calls, connection,
                                          project_id, hours)

    # AN AGENT DOING WORK WITH NO REGISTERED ROLE sits in Yours, tagged, never hidden.
    seen_roles = (set(counts) | set(busy) | {w["role_key"] for w in waits}
                  | {e["source"] for e in edges if e["source"] != "person"}
                  | {e["target"] for e in edges}
                  | {r["role"] for r in spend_rows or [] if r["role"]})
    for role in sorted(seen_roles - members - {"human"}):
        team = team_of_role.get(role) or team_of(None)["key"]
        if role in registered and team in teams:
            continue  # retired: its numbers count to its team, it is not on the floor
        team_of_role[role] = team if team in teams else "yours"
        if role in registered:
            continue
        if "yours" not in teams:
            teams["yours"] = {"key": "yours", "label": "Yours", "tip": None, "members": []}
        teams["yours"]["members"].append(role)
        members.add(role)

    def team_for(role: str) -> str:
        t = team_of_role.get(role, "yours")
        return t if t in teams else "yours"

    # spend
    by_role_spend: dict[str, dict[str, Any]] = {}
    totals = {"metered_usd": 0.0, "plan_calls": 0, "untied_usd": 0.0, "untied_calls": 0,
              "spend_ok": spend_reason is None, "spend_reason": spend_reason}
    metered_total = untied_micros = 0
    for r in spend_rows or []:
        metered_total += r["metered_micros"]
        if r["included"]:
            totals["plan_calls"] += r["calls"]
        if not r["role"]:
            untied_micros += r["metered_micros"]
            totals["untied_calls"] += r["calls"]
            continue
        s = by_role_spend.setdefault(r["role"], {"micros": 0, "plan_calls": 0, "models": {}})
        s["micros"] += r["metered_micros"]
        if r["included"]:
            s["plan_calls"] += r["calls"]
        if r["model"]:
            s["models"][r["model"]] = s["models"].get(r["model"], 0) + r["calls"]
    totals["metered_usd"] = _usd(metered_total)
    totals["untied_usd"] = _usd(untied_micros)

    # gaps
    kit, kit_reason = _savepoint(connection, kit_gaps, connection, settings, project_id)
    kit_roles = (kit or {}).get("roles", {})
    missing, missing_reason = _savepoint(connection, agents.missing_roles, connection, project_id)
    named_on, _ = _savepoint(connection, agents.steps_naming_all, connection, project_id)

    # edges
    agent_edges = sorted(
        ({"from": e["source"], "to": e["target"], "handoffs": e["times"],
          "failed": e["failed"], "waiting": e["waiting"], "stale": e["stale"],
          "how": e["how"], "label": e["last_action"], "last_at": _iso(e["last_at"])}
         for e in edges), key=lambda e: (-e["handoffs"], e["from"], e["to"]))
    team_edges: dict[tuple[str, str], dict[str, Any]] = {}
    self_handoffs: dict[str, int] = {}
    for e in agent_edges:
        a = "person" if e["from"] == "person" else team_for(e["from"])
        b = team_for(e["to"])
        if a == b:
            self_handoffs[a] = self_handoffs.get(a, 0) + e["handoffs"]
            continue
        t = team_edges.setdefault((a, b), {"from": a, "to": b, "handoffs": 0, "failed": 0,
                                           "waiting": 0, "stale": 0, "last_at": None})
        for k in ("handoffs", "failed", "waiting", "stale"):
            t[k] += e[k]
        if e["last_at"] and (t["last_at"] is None or e["last_at"] > t["last_at"]):
            t["last_at"] = e["last_at"]
    cols = flow_columns(connection, project_id, hours, {r: team_for(r) for r in team_of_role},
                        team_order)

    # agents
    by_role_open: dict[str, list[dict[str, Any]]] = {}
    for w in waits:
        by_role_open.setdefault(w["role_key"], []).append(w)
    agent_rows = []
    for t in teams.values():
        for role in t["members"]:
            s = by_role_spend.get(role, {})
            g = kit_roles.get(role, {})
            agent_rows.append({
                "role_key": role, "team": t["key"], "registered": role in registered,
                "work": counts.get(role, {"done": 0, "failed": 0, "open": 0}),
                "busy_seconds": busy.get(role, 0),
                "spend": {"metered_usd": _usd(s.get("micros", 0)),
                          "plan_calls": s.get("plan_calls", 0)},
                "models": _models(s.get("models", {})),
                "configured_model": _configured_model(specs.get(role)),
                "open": by_role_open.get(role, [])[:WAITING_MAX],
                "open_total": len(by_role_open.get(role, [])),
                "named_on": [{k: n[k] for k in ("workflow_key", "workflow_name", "step_id",
                                                "step_title")}
                             for n in (named_on or {}).get(role, [])],
                "gaps": {"abilities": g.get("abilities", []), "step_gaps": g.get("step_gaps", [])},
            })

    # teams
    team_rows = []
    for t in teams.values():
        ms = t["members"]
        # every role whose numbers count to this team: its members, plus retired agents of it
        counted = [r for r in set(team_of_role) | set(by_role_spend) | set(counts) | set(busy)
                   if team_for(r) == t["key"]]
        work = {k: sum(counts.get(r, {}).get(k, 0) for r in counted)
                for k in ("done", "failed", "open")}
        waiting = sorted((w for w in waits if team_for(w["role_key"]) == t["key"]),
                         key=lambda w: (w["since"] or "", w["act_id"]))
        micros = sum(by_role_spend.get(r, {}).get("micros", 0) for r in counted)
        plan_calls = sum(by_role_spend.get(r, {}).get("plan_calls", 0) for r in counted)
        models: dict[str, int] = {}
        for r in counted:
            for m, n in by_role_spend.get(r, {}).get("models", {}).items():
                models[m] = models.get(m, 0) + n
        abilities: dict[str, dict[str, Any]] = {}
        step_gaps = []
        for r in ms:
            g = kit_roles.get(r, {})
            for a in g.get("abilities", []):
                abilities.setdefault(a["ability"], {**a, "roles": []})["roles"].append(r)
            step_gaps.extend(g.get("step_gaps", []))
        configured = sorted({c for c in (_configured_model(specs.get(r)) for r in ms) if c})
        team_rows.append({
            "key": t["key"], "label": t["label"], "tip": t.get("tip"), "members": ms,
            "flow_col": cols.get(t["key"]), "self_handoffs": self_handoffs.get(t["key"], 0),
            "work": work,
            "waiting": waiting[:WAITING_MAX], "waiting_total": len(waiting),
            "members_load": sorted(({"role_key": r, "busy_seconds": busy.get(r, 0)} for r in ms),
                                   key=lambda x: (-x["busy_seconds"], x["role_key"])),
            "spend": {"metered_usd": _usd(micros), "plan_calls": plan_calls},
            "models": _models(models), "configured_models": configured,
            "gaps": {"abilities": sorted(abilities.values(), key=lambda a: a["label"]),
                     "step_gaps": step_gaps,
                     "missing_roles": sum(1 for m in missing or [] if m["team"] == t["key"]),
                     "kit_ok": kit_reason is None, "kit_reason": kit_reason,
                     "roles_ok": missing_reason is None},
        })

    drift = plan_vs_work(connection, project_id, {r: team_for(r) for r in team_of_role})
    board: dict[str, Any] = {
        "project_id": project_id, "days": days,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "totals": totals, "teams": team_rows, "agents": agent_rows,
        "team_edges": sorted(team_edges.values(),
                             key=lambda e: (-e["handoffs"], e["from"], e["to"])),
        "agent_edges": agent_edges[:AGENT_EDGES_MAX],
        "truncated": len(agent_edges) > AGENT_EDGES_MAX,
        "missing_roles": [{**m, "team": m["team"] if m["team"] in teams else "yours"}
                          for m in missing or []],
        "declared_vs_observed": drift,
    }
    board["etag"] = etag_of(board)
    return board


def handoffs(connection: Connection, project_id: str, *, days: int,
             sources: Iterable[str], targets: Iterable[str], limit: int = 10) -> dict[str, Any]:
    """The newest handoffs along one edge (the Edge panel): acts by a `targets` role whose
    parent act is a `sources` role's — or, for `person`, acts a person handed directly. The
    same definition as observed_edges, so the rows are the edge's own evidence."""
    if int(days) not in WINDOWS:
        raise OrgBoardError(f"days must be one of {', '.join(str(w) for w in WINDOWS)}.")
    srcs = sorted({str(s) for s in sources if s})
    dsts = sorted({str(t) for t in targets if t})
    if not srcs or not dsts:
        return {"items": []}
    agents_from = [s for s in srcs if s != "person"]
    rows = connection.execute(
        text(
            """
            SELECT c.act_id, c.role_key AS to_role,
                   CASE WHEN p.role_key IS NULL THEN 'person' ELSE p.role_key END AS from_role,
                   c.status, c.label, c.started_at, c.ended_at, c.workflow_run_id, c.how
            FROM agent_acts c LEFT JOIN agent_acts p ON p.act_id = c.parent_act_id
            WHERE c.project_id = :p AND c.role_key = ANY(:dst)
              AND c.started_at > now() - make_interval(hours => :h)
              AND ((p.role_key = ANY(:src) AND p.role_key <> c.role_key)
                   OR (:people AND c.handed_by_kind = 'person' AND p.role_key IS NULL))
            ORDER BY c.started_at DESC, c.act_id DESC
            LIMIT :n
            """
        ),
        {"p": project_id, "dst": dsts, "src": agents_from or [""], "h": int(days) * 24,
         "people": "person" in srcs, "n": max(1, min(int(limit), 50))},
    ).mappings().all()
    from .agent_acts import HOWS, edge_label

    return {"items": [{"act_id": r["act_id"], "from": r["from_role"], "to": r["to_role"],
                       "status": r["status"], "label": r["label"],
                       "how": edge_label(r["how"]) if r["how"] in HOWS else None,
                       "started_at": _iso(r["started_at"]), "ended_at": _iso(r["ended_at"]),
                       "run_id": r["workflow_run_id"]} for r in rows]}
