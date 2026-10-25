"""Learning, live and measured (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §4.4, §4.6).

``live`` is GET /v1/knowledge/view/live (``KgLiveResponse``): the learnings, rules and new
``consulted`` / ``reused`` / ``produced`` edges since a cursor, for one run (a build, a workflow
run and the builds it started) or the whole project. The Graph area and every run view's
"Learned so far" strip poll it every ``poll_after_ms`` while it is on screen and a run is writing.
A use recorded in ``kg_usage`` before its edge was derived is reported as that edge already, so
the strip shows it the moment the agent used it.

``run_proof`` / ``compare`` are the §4.6 plumbing: what a run consulted and reused, and what it
cost in steps, model turns, tokens, money, wall time and repair rounds — so a pair of runs on the
same task class (learning on vs. ``keyword``) can be compared, and an improvement is attributed
to learning only when the later run carries the edge to what the earlier one learned.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .learning_writeback import LEARNING_KINDS, payload_of

POLL_AFTER_MS = 5000
#: A cursor re-reads this much before itself: a write committed a moment after a later one is
#: not lost. The strip merges by id, so a repeat is harmless.
OVERLAP = timedelta(seconds=3)
#: With no cursor and no run: what the last day learned.
FIRST_WINDOW = timedelta(hours=24)
MAX_ITEMS = 100
LIVE_EDGE_KINDS = ("consulted", "reused", "produced", "governs", "approved_by", "corrects")
ACTIVE_BUILD = ("queued", "running")
ACTIVE_WORKFLOW = ("queued", "validating", "running", "waiting", "compensating")
_CURSOR = re.compile(r"^(?P<at>\d{4}-\d\d-\d\dT[0-9:.]+(?:Z|[+-]\d\d:?\d\d)?)(?:#\d+)?$")


def _iso(value: Any) -> Optional[str]:
    """A time as the wire carries it: ``2026-10-24T11:18:40Z``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _cursor_time(value: datetime) -> str:
    """The cursor keeps the microseconds: two writes in one second are told apart."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_cursor(cursor: Optional[str]) -> Optional[datetime]:
    """The cursor's time, or None for none. An unreadable cursor is refused (422)."""
    raw = str(cursor or "").strip()
    if not raw:
        return None
    match = _CURSOR.match(raw)
    if not match:
        raise ValueError("since is the cursor a previous read returned")
    at = datetime.fromisoformat(match.group("at").replace("Z", "+00:00"))
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def run_family(connection: Connection, project_id: str, run: Optional[str]) -> list[str]:
    """A run and the runs under it: a workflow run's builds (finalize_ctx.workflow_run_id); a
    build is itself."""
    ref = str(run or "").strip()
    if not ref:
        return []
    out = [ref]
    out += [str(r) for r in connection.execute(text(
        "SELECT id FROM code_task_runs WHERE project_id = :p AND finalize_ctx->>'workflow_run_id' = :r"),
        {"p": project_id, "r": ref}).scalars().all()]
    return list(dict.fromkeys(out))


def _active_runs(connection: Connection, project_id: str, family: list[str]) -> int:
    params: dict[str, Any] = {"p": project_id, "b": list(ACTIVE_BUILD), "w": list(ACTIVE_WORKFLOW)}
    where_b = where_w = ""
    if family:
        params["f"] = family
        where_b = where_w = " AND id = ANY(:f)"
    builds = connection.execute(text(
        f"SELECT count(*) FROM code_task_runs WHERE project_id = :p AND status = ANY(:b){where_b}"), params).scalar()
    flows = connection.execute(text(
        f"SELECT count(*) FROM workflow_runs WHERE project_id = :p AND status = ANY(:w){where_w}"), params).scalar()
    return int(builds or 0) + int(flows or 0)


def live(connection: Connection, project_id: str, *, since: Optional[str] = None, run: Optional[str] = None,
         withheld: Iterable[str] = ()) -> dict[str, Any]:
    """``KgLiveResponse``. ``withheld``: kinds the caller may not read (decisions and precedents
    without decision:read) — never sent, and no edge to one either."""
    from .rules import rule_payload

    withheld = frozenset(withheld)
    at = parse_cursor(since)
    family = run_family(connection, project_id, run)
    floor = (at - OVERLAP) if at else (None if family else datetime.now(timezone.utc) - FIRST_WINDOW)
    anchors = [f"run:{r}" for r in family]
    kinds = sorted(k for k in LEARNING_KINDS if k not in withheld)
    params: dict[str, Any] = {"p": project_id, "kinds": kinds, "floor": floor, "anchors": anchors,
                              "n": MAX_ITEMS, "family": family}
    # LEARNINGS: what the run (or anyone, with no run) learned or saw again since the cursor.
    run_sql = ("AND EXISTS (SELECT 1 FROM kg_edges p JOIN kg_nodes r ON r.id = p.src_id "
               "WHERE p.dst_id = n.id AND p.kind = 'produced' AND r.node_key = ANY(:anchors))") if family else ""
    rows = connection.execute(text(
        f"""
        SELECT n.id, n.project_id, n.kind, n.node_key, n.name, n.body, n.attrs, n.trust_tier, n.created_by,
               n.first_seen, n.recorded_at, n.last_seen, n.valid_to
        FROM kg_nodes n
        WHERE n.project_id = :p AND n.kind = ANY(:kinds) AND n.node_key LIKE 'learning:%'
          AND (CAST(:floor AS TIMESTAMPTZ) IS NULL OR n.last_seen >= :floor) {run_sql}
        ORDER BY n.last_seen DESC LIMIT :n
        """), params).mappings().all()
    learnings = [payload_of(connection, dict(r)) for r in rows]
    stamps = [r["last_seen"] for r in rows if r["last_seen"] is not None]

    # EDGES the run drew (or anyone drew) since the cursor.
    edge_run_sql = "AND s.node_key = ANY(:anchors)" if family else ""
    edges_out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in connection.execute(text(
            f"""
            SELECT e.src_id, e.dst_id, e.kind, e.attrs, e.last_seen, s.node_key AS src_key, d.kind AS dst_kind
            FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id JOIN kg_nodes d ON d.id = e.dst_id
            WHERE e.project_id = :p AND e.kind = ANY(:ek) AND e.valid_to IS NULL
              AND (CAST(:floor AS TIMESTAMPTZ) IS NULL OR e.last_seen >= :floor) {edge_run_sql}
            ORDER BY e.last_seen DESC LIMIT :n
            """), {**params, "ek": list(LIVE_EDGE_KINDS)}).mappings():
        if row["dst_kind"] in withheld:
            continue
        attrs = row["attrs"] or {}
        src_run = str(row["src_key"]).split(":", 1)[-1] if str(row["src_key"]).startswith("run:") else None
        edges_out[(row["dst_id"], row["kind"] if row["kind"] != "produced" else f"produced:{row['src_id']}")] = {
            "src": row["src_id"], "dst": row["dst_id"], "kind": row["kind"],
            "run_id": (run if family and src_run in family else (attrs.get("run_id") or src_run)),
            "how": attrs.get("how") if row["kind"] == "consulted" else None,
            "at": _iso(row["last_seen"]),
        }
        if row["last_seen"] is not None:
            stamps.append(row["last_seen"])
    # USES not drawn as an edge yet (logged by a tool before the run's edges were derived).
    use_run_sql = ("AND (u.run_id = ANY(:family) OR u.code_task_run_id = ANY(:family) "
                   "OR u.workflow_run_id = ANY(:family))") if family else ""
    for row in connection.execute(text(
            f"""
            SELECT u.fact_id, u.use_kind, u.run_id, u.code_task_run_id, u.workflow_run_id, u.created_at, d.kind AS dst_kind,
                   r.id AS anchor_id
            FROM kg_usage u JOIN kg_nodes d ON d.id = u.fact_id
            LEFT JOIN kg_nodes r ON r.project_id = u.project_id AND r.kind = 'run'
                 AND r.node_key = 'run:' || coalesce(u.code_task_run_id,
                       CASE WHEN u.run_id LIKE 'crun%' THEN u.run_id END, u.workflow_run_id, u.run_id)
            WHERE u.project_id = :p AND u.use_kind IN ('consulted', 'cited', 'reused')
              AND (CAST(:floor AS TIMESTAMPTZ) IS NULL OR u.created_at >= :floor) {use_run_sql}
            ORDER BY u.created_at DESC LIMIT :n
            """), params).mappings():
        if row["dst_kind"] in withheld:
            continue
        kind = "reused" if row["use_kind"] == "reused" else "consulted"
        key = (row["fact_id"], kind)
        if key in edges_out:
            continue
        ref = row["code_task_run_id"] or row["workflow_run_id"] or row["run_id"]
        edges_out[key] = {"src": row["anchor_id"] or f"run:{ref}", "dst": row["fact_id"], "kind": kind,
                          "run_id": run if family else ref,
                          "how": None if kind == "reused" else ("tool" if row["use_kind"] == "consulted" else "cited"),
                          "at": _iso(row["created_at"])}
        if row["created_at"] is not None:
            stamps.append(row["created_at"])
    edges = sorted(edges_out.values(), key=lambda e: str(e["at"] or ""), reverse=True)

    # RULES: those the edges point at (so a "consulted" item has its title) and those the run
    # proposed since the cursor.
    rule_ids = {e["dst"] for e in edges if e["kind"] in ("consulted", "governs", "approved_by", "corrects")}
    rule_ids |= {e["dst"] for e in edges if e["kind"] == "produced"}
    rule_rows = connection.execute(text(
        "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'rule' AND id = ANY(:i)"),
        {"p": project_id, "i": sorted(rule_ids)}).scalars().all() if rule_ids else []
    rules = [rule_payload(connection, project_id, rid) for rid in rule_rows]
    learned_ids = {item["node"]["id"] for item in learnings}
    missing = sorted({e["dst"] for e in edges if e["kind"] in ("consulted", "reused")} - learned_ids - set(rule_rows))
    if missing:
        # A consulted learning from an earlier run: sent so the strip can name it.
        for row in connection.execute(text(
                "SELECT id, project_id, kind, node_key, name, body, attrs, trust_tier, created_by, first_seen, "
                "recorded_at, last_seen, valid_to FROM kg_nodes WHERE project_id = :p AND id = ANY(:i) "
                "AND kind = ANY(:kinds)"), {"p": project_id, "i": missing, "kinds": kinds}).mappings():
            learnings.append(payload_of(connection, dict(row)))
    newest = max(stamps) if stamps else (at or datetime.now(timezone.utc))
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return {
        "cursor": f"{_cursor_time(newest)}#{len(learnings) + len(edges)}",
        "learnings": learnings,
        "rules": rules,
        "edges": edges,
        "active_runs": _active_runs(connection, project_id, family),
        "poll_after_ms": POLL_AFTER_MS,
    }


# --- §4.6: what a run used, and what it cost -------------------------------------------------------
def run_proof(connection: Connection, project_id: str, run: str, *,
              withheld: Iterable[str] = ()) -> dict[str, Any]:
    """One run's scorecard for the before/after comparison: steps (model calls), model turns,
    tokens, cost, wall time, repair rounds, and the items it consulted / reused / learned.

    A conversation (``athr_…``: an agent asked in chat, e.g. an incident sent to the SRE) is
    scored the same way from its own record (learning_conversations.scorecard): its tool calls,
    replies, metered tokens, wall time and time to the root cause it recorded."""
    from .learning_conversations import is_conversation, scorecard

    family = run_family(connection, project_id, run)
    if not family:
        raise LookupError("run is required")
    if is_conversation(run):
        consulted, reused, learned = _used(connection, project_id, [f"run:{run}"], withheld)
        return scorecard(connection, project_id, str(run).strip(), consulted=consulted, reused=reused,
                         learned=learned)
    builds = connection.execute(text(
        "SELECT id, status, created_at, updated_at, state FROM code_task_runs WHERE project_id = :p "
        "AND id = ANY(:f)"), {"p": project_id, "f": family}).mappings().all()
    flow = connection.execute(text(
        "SELECT id, status, created_at, updated_at FROM workflow_runs WHERE project_id = :p AND id = :r"),
        {"p": project_id, "r": run}).mappings().first()
    if not builds and flow is None and not connection.execute(text(
            "SELECT 1 FROM kg_nodes WHERE project_id = :p AND kind = 'run' AND node_key = :k"),
            {"p": project_id, "k": f"run:{run}"}).first():
        raise LookupError(f"run not found: {run}")
    calls = connection.execute(text(
        "SELECT count(*) AS steps, coalesce(sum(turns), 0) AS turns, coalesce(sum(total_tokens), 0) AS tokens, "
        "coalesce(sum(cost_usd_micros), 0) AS cost FROM llm_calls WHERE code_task_run_id = ANY(:f)"),
        {"f": family}).mappings().one()
    starts = [b["created_at"] for b in builds] + ([flow["created_at"]] if flow else [])
    ends = [b["updated_at"] for b in builds] + ([flow["updated_at"]] if flow else [])
    wall = (max(ends) - min(starts)).total_seconds() if starts and ends else None
    repairs = 0
    for b in builds:
        state = b["state"] if isinstance(b["state"], dict) else {}
        try:
            repairs += int(state.get("repair_attempts") or 0)
        except (TypeError, ValueError):
            pass
    consulted, reused, learned = _used(connection, project_id, [f"run:{r}" for r in family], withheld)
    from .runtime import knowledge_mode

    from .learning_writeback import run_title

    return {
        "run_id": run,
        # By its name, never its id: the console says what the baseline was.
        "title": run_title(connection, run),
        "status": (flow["status"] if flow else (builds[0]["status"] if builds else None)),
        "knowledge_graph": knowledge_mode(connection, project_id),
        "steps": int(calls["steps"] or 0),
        "turns": int(calls["turns"] or 0),
        "tokens": int(calls["tokens"] or 0),
        "cost_usd": round(int(calls["cost"] or 0) / 1e6, 4),
        "wall_seconds": round(wall, 1) if wall is not None else None,
        "repair_rounds": repairs,
        "learned": learned,
        "consulted": consulted,
        "reused": reused,
    }


def _used(connection: Connection, project_id: str, anchors: list[str],
          withheld: Iterable[str] = ()) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """What the run (or conversation) nodes ``anchors`` consulted and reused, and how many
    learned items they produced."""
    used = connection.execute(text(
        """
        SELECT e.kind, e.attrs->>'how' AS how, d.id, d.kind AS item_kind, d.name, d.attrs->'provenance' AS prov,
               (SELECT r2.node_key FROM kg_edges p2 JOIN kg_nodes r2 ON r2.id = p2.src_id
                 WHERE p2.dst_id = d.id AND p2.kind = 'produced' ORDER BY p2.first_seen LIMIT 1) AS learned_in
        FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id JOIN kg_nodes d ON d.id = e.dst_id
        WHERE e.project_id = :p AND s.node_key = ANY(:a) AND e.kind IN ('consulted', 'reused') AND e.valid_to IS NULL
        ORDER BY e.kind, d.id
        """), {"p": project_id, "a": anchors}).mappings().all()
    learned = connection.execute(text(
        "SELECT count(DISTINCT e.dst_id) FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id "
        "JOIN kg_nodes d ON d.id = e.dst_id WHERE e.project_id = :p AND s.node_key = ANY(:a) "
        "AND e.kind = 'produced' AND d.kind = ANY(:k)"),
        {"p": project_id, "a": anchors, "k": sorted(LEARNING_KINDS)}).scalar()

    def item(row) -> dict[str, Any]:
        prov = row["prov"] or {}
        src = str(row["learned_in"] or "")
        return {"id": row["id"], "kind": row["item_kind"], "title": row["name"], "how": row["how"],
                "learned_in": src.split(":", 1)[-1] if src.startswith("run:") else None,
                "learned_by": prov.get("agent_name") or prov.get("agent_role")}

    hidden = frozenset(withheld)
    return ([item(r) for r in used if r["kind"] == "consulted" and r["item_kind"] not in hidden],
            [item(r) for r in used if r["kind"] == "reused" and r["item_kind"] not in hidden],
            int(learned or 0))


def compare(connection: Connection, project_id: str, *, run: str, baseline: str,
            withheld: Iterable[str] = ()) -> dict[str, Any]:
    """The later run (``run``) against the earlier or control one (``baseline``): each metric's
    change, and whether the improvement may be attributed to learning — only when ``run``
    consulted or reused something ``baseline`` learned (spec §4.6, "Claim rule")."""
    later = run_proof(connection, project_id, run, withheld=withheld)
    earlier = run_proof(connection, project_id, baseline, withheld=withheld)
    family = set(run_family(connection, project_id, baseline))
    carried = [i for i in later["consulted"] + later["reused"] if i.get("learned_in") in family]
    delta = {}
    for key in ("steps", "turns", "tokens", "cost_usd", "wall_seconds", "repair_rounds"):
        a, b = earlier.get(key), later.get(key)
        delta[key] = None if a is None or b is None else round(b - a, 4)
    if "time_to_root_cause_seconds" in later or "time_to_root_cause_seconds" in earlier:
        # A conversation's: from the question to the root cause it recorded.
        a, b = earlier.get("time_to_root_cause_seconds"), later.get("time_to_root_cause_seconds")
        delta["time_to_root_cause_seconds"] = None if a is None or b is None else round(b - a, 1)
    if not (later.get("tokens_measured", True) and earlier.get("tokens_measured", True)):
        # Unmetered model calls are not "no tokens": no difference is claimed either way.
        delta["tokens"] = delta["cost_usd"] = None
    better = [k for k in ("steps", "turns", "tokens", "repair_rounds") if delta[k] is not None and delta[k] < 0]
    worse = [k for k in ("steps", "turns", "tokens", "repair_rounds") if delta[k] is not None and delta[k] > 0]
    return {"run": later, "baseline": earlier, "delta": delta, "carried": carried,
            "attributed": bool(carried) and bool(better) and not worse,
            "verdict": ("better, and it used what the baseline learned" if carried and better and not worse else
                        "better, but it did not use what the baseline learned: not attributed to learning"
                        if better and not worse else
                        "not better" if worse or not better else "no difference")}
