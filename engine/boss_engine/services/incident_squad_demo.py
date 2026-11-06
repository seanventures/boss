"""The Order API incident, re-run with the incident squad (design §12.6 "Demo (Phase 1 exit)").

A REPRODUCIBLE SCENARIO, NOT A REAL HOST
----------------------------------------
D2 (docs/community/RELEASE_NOTES_v1.0-rc10.8.md): BOSS built an Order API next to HelpDesk AI,
a faulty release went out, customers complained, BOSS's alert fired and BOSS investigated,
fixed and verified recovery. This module seeds that incident into ONE project from fixtures —
an incident row with the serving release, the failing route, the app's log signatures, the
release diff and customer reports; the firing alert; a small code graph for the Order API
(POST /orders → create_order → order_total → bulk_discount); and the PAST incident's precedent
learning (the D2 occurrence) — then runs the real squad over it with the scripted engine
(no model) and FixtureEffects (no host is touched). Every row it writes is the shape the real
pipeline writes, so the Agent Operations view draws it exactly as it would a live incident.

The alert reaches the squad the real way: the `app_alerts` signal adapter sweeps it into a
signal, the Chief-of-Staff stand-in (`incident_squad.intake`) opens the item and routes it,
and `incident_squad.route_incident` runs the squad.

    production (default)  the squad recommends a rollback with one dissent → Needs you;
                          approve it in the console (or pass approve_as) and the squad rolls
                          back, starts the fix, verifies recovery and writes the postmortem
    staging               maker-checker: the reviewer agrees, the rollback is automatic (A2)

Run it (never against a production install; it writes demo rows marked `demo:`):

    PYTHONPATH=engine python3 -m boss_engine.services.incident_squad_demo \\
        --project <project id> [--env staging] [--approve-as <person id>]

or POST /v1/ambient/demo/order-api-incident {project_id, environment?, approve_as?} on an API
started with BOSS_DEMO_SCENARIOS=1.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import incident_squad as squad
from .ambient import read_model, signals

ENV_FLAG = "BOSS_DEMO_SCENARIOS"
APP = "order-api"
REPO = "boss-demo/order-api"
SERVING = "6bf2672e41c0a9d35b7f18c2e6d4a0b9c3f1e852"
PREVIOUS = "2c41a9e07d5b3f8e1a6c9d2b4e7f0a3c5d8b1e96"
COUPON = "9d1e7b3a52c84f06e9b1d7a3c5f2e8b0d4a6c193"
ROUTE = "POST /orders"
PAST_INCIDENT = "INC-31 (D2, Oct 18)"


def enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip().lower() in ("1", "true", "yes")


def demo_mind(environment: str = "production") -> squad.ScriptedMind:
    """The scripted engine: what each seat says, in its own words. One reviewer dissents on the
    production rollback — the coupon change shipped in the same release."""
    staging = environment.lower() in squad.NON_PROD_ENVS
    return squad.ScriptedMind({
        ("agent:operations_engineer", "verdict"): {
            "key": "top", "confidence": 0.9,
            "reason": "Release 6bf2672 changed bulk_discount on the POST /orders path and the "
                      "TypeError is raised there. The signature matches INC-31."},
        ("agent:adversarial_reviewer", "critique"): {
            "key": "agree" if staging else "concern",
            "reason": "The rollback also reverts the coupon change 9d1e7b3 from the same "
                      "release; acceptable only because the fix follows."},
        ("agent:operations_engineer", "vote"): {
            "key": "approve", "reason": "Logs, diff and the past incident agree."},
        ("agent:backend_engineer", "vote"): {
            "key": "approve", "reason": "Rollback is clean; I fix bulk_discount forward."},
        ("agent:adversarial_reviewer", "vote"): {
            "key": "reject", "reason": "Customers lose coupon codes; prefer a hotfix."},
    })


class FixtureEffects:
    """What the actions do in the demo: nothing outside the database. The rollback and the fix
    are recorded as the scenario's outcomes; recovery is the monitor's verdict, written as the
    incident's `verification` evidence exactly as incident_response.look_at_recovery writes it."""

    def rollback(self, connection, *, project_id, ctx, actor) -> dict[str, Any]:
        return {"ok": True, "word": f"Rolled back to {PREVIOUS[:7]}"}

    def start_fix(self, connection, *, project_id, ctx, brief, actor) -> dict[str, Any]:
        return {"ok": True, "link": ("pull_request", f"{REPO}#17"),
                "word": "Fix PR opened (demo)"}

    def check_recovery(self, connection, *, project_id, ctx) -> dict[str, Any]:
        from .incident_response import _evidence

        summary = "Recovered: 0.4% of 1,180 requests failed over 5 min (baseline 0.3%)"
        if ctx.incident_id:
            _evidence(connection, ctx.incident_id, "verification", ref="recovered",
                      payload={"state": "recovered", "summary": summary,
                               "independent": "demo fixture (scenario)"})
        return {"status": "recovered", "word": summary,
                "evidence": [{"kind": "incident", "ref": ctx.incident_id}]}


# ── seeding ──────────────────────────────────────────────────────────────────────────────────

def _store():
    from .knowledge.store import PostgresKnowledgeStore

    return PostgresKnowledgeStore()


def _code_graph(connection: Connection, project_id: str) -> dict[str, str]:
    """The Order API's code graph, as code_index would write it."""
    st = _store()
    ids: dict[str, str] = {}

    def symbol(key: str, name: str, path: str) -> str:
        nid = st.upsert_node(connection, project_id=project_id, kind="symbol",
                             node_key=f"symbol:{REPO}:{key}", name=name, body=f"python {name}",
                             trust_tier="observed", actor_kind="system", source="code_index",
                             attrs={"lang": "python", "demo": True})
        connection.execute(text("UPDATE kg_nodes SET repo = :r, path = :f WHERE id = :i"),
                           {"r": REPO, "f": path, "i": nid})
        ids[key] = nid
        return nid

    endpoint = st.upsert_node(connection, project_id=project_id, kind="endpoint",
                              node_key=f"endpoint:{REPO}:{ROUTE}", name=ROUTE, body=ROUTE,
                              trust_tier="observed", actor_kind="system", source="code_index",
                              attrs={"method": "POST", "route": "/orders", "demo": True})
    ids["endpoint"] = endpoint
    create = symbol("app.orders.create_order", "create_order", "app/orders.py")
    total = symbol("app.pricing.order_total", "order_total", "app/pricing.py")
    bulk = symbol("app.pricing.bulk_discount", "bulk_discount", "app/pricing.py")
    coupon = symbol("app.coupons.apply_coupon", "apply_coupon", "app/coupons.py")
    reprice = symbol("app.invoices.reprice", "reprice", "app/invoices.py")
    for src, dst, kind in ((create, endpoint, "exposes"), (create, total, "calls"),
                           (total, bulk, "calls"), (create, coupon, "calls"),
                           (reprice, total, "calls")):
        st.upsert_edge(connection, project_id=project_id, src_id=src, dst_id=dst, kind=kind,
                       actor_kind="system", attrs={"demo": True})
    return ids


def _past_incident(connection: Connection, project_id: str) -> str:
    """The D2 occurrence's postmortem, as the squad's own close-out writes it: an incident node
    and a precedent learning."""
    from .knowledge import learning_writeback as lw

    found = connection.execute(text(
        "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'precedent' "
        "AND attrs->>'incident_id' = :i"), {"p": project_id, "i": PAST_INCIDENT}).scalar()
    if found:
        return found
    item = lw.record(
        connection, project_id=project_id, learning_kind="precedent",
        title="order-api: pricing change broke large orders",
        statement="POST /orders 5xx TypeError in pricing for large orders after a release",
        detail={"symptom": "POST /orders 5xx TypeError in pricing for large orders after a "
                           "release",
                "cause_summary": "A pricing change for bulk orders broke totals for 10+ items",
                "fix_summary": "Roll back to the last healthy release, then fix forward with a "
                               "regression test for 15+ items",
                "applies_to": [ROUTE, APP], "reuse_count": 0},
        about={"symbols": ["app/pricing.py"]},
        evidence=[{"type": "metric", "ref": PAST_INCIDENT, "excerpt": "6.1% 5xx on order-api"},
                  {"type": "test", "ref": "tests/test_pricing.py::test_15_items",
                   "excerpt": "regression test", "passed": True}],
        confidence=0.85, observed={"agent_role": "sre", "step": "postmortem"})
    node_id = item["node"]["id"]
    connection.execute(text("UPDATE kg_nodes SET attrs = attrs || CAST(:a AS JSONB) "
                            "WHERE id = :n"),
                       {"a": json.dumps({"incident_id": PAST_INCIDENT, "demo": True}),
                        "n": node_id})
    old = _store().upsert_node(
        connection, project_id=project_id, kind="incident", node_key="incident:demo-inc-31",
        name="Order API 5xx on large orders (D2)", body="Rolled back, then fixed forward.",
        trust_tier="observed", actor_kind="system", source="incident_squad",
        attrs={"incident_id": PAST_INCIDENT, "demo": True})
    _store().upsert_edge(connection, project_id=project_id, src_id=old, dst_id=node_id,
                         kind="produced", actor_kind="system", attrs={"demo": True})
    return node_id


def seed(connection: Connection, project_id: str, *, environment: str = "production",
         now: Optional[datetime] = None) -> dict[str, Any]:
    """Write the scenario's rows. Returns {incident_id, alert_id, precedent_id, code}."""
    now = now or datetime.now(timezone.utc)
    fired = now - timedelta(seconds=5)
    released = now - timedelta(minutes=7)
    tag = uuid4().hex[:8]
    incident_id = f"inc_demo_{tag}"
    alert_id = f"aal_demo_{tag}"
    routes = [{"route": ROUTE, "requests": 610, "errors": 79, "error_rate": 0.13},
              {"route": "GET /orders/{id}", "requests": 590, "errors": 0, "error_rate": 0.0}]
    alert_ctx = {"fired_at": fired.isoformat(), "error_rate": 0.066, "requests": 1200,
                 "errors": 79, "routes": routes, "alert_id": alert_id,
                 "rule": {"error_rate_percent": 5, "window_minutes": 5, "min_requests": 50}}
    deploy = {"commit_sha": SERVING, "previous_commit_sha": PREVIOUS, "ref": "main",
              "repository": REPO, "deployed_at": released.isoformat(),
              "previous_deployed_at": (released - timedelta(days=1)).isoformat()}
    fctx = {"app": {"target_id": None, "app_name": APP, "environment": environment},
            "deploy": deploy, "alert": alert_ctx, "occurrence": fired.isoformat(),
            "demo": True}
    connection.execute(text(
        "INSERT INTO incidents (id, project_id, dedupe_key, title, service, severity, status, "
        "first_seen, last_seen, finalize_ctx) VALUES (:i, :p, :k, :t, :s, 'critical', 'open', "
        ":f, :n, CAST(:c AS JSONB))"),
        {"i": incident_id, "p": project_id, "k": f"demo:{APP}:{tag}",
         "t": f"Demo: Order API ({environment}) 6.6% of requests failing", "s": APP,
         "f": fired, "n": now, "c": json.dumps(fctx)})
    ev = [("alert", "5", alert_ctx, fired), ("deployment", SERVING, deploy, fired),
          ("customer_reports", "helpdesk", {"count": 4, "sample": "checkout fails when I "
                                            "order 12 cables"}, fired + timedelta(seconds=40)),
          ("logs", APP, {"word": "Read 412 log lines from 2 containers", "signatures": [
              {"exc_type": "TypeError", "location": "app/pricing.py:42", "route": ROUTE,
               "count": 79, "sample": "unsupported operand type(s) for *: 'NoneType' and "
                                     "'float'"}]}, fired + timedelta(seconds=50)),
          ("release_diff", SERVING, {"word": "2 commit(s), 2 file(s) changed", "commits": [
              {"sha": SERVING, "message": "Bulk discount tiers for 10+ items"},
              {"sha": COUPON, "message": "Coupon codes on checkout"}], "files": [
              {"filename": "app/pricing.py", "status": "modified", "additions": 18,
               "deletions": 4},
              {"filename": "app/coupons.py", "status": "added", "additions": 41,
               "deletions": 0}]}, fired + timedelta(seconds=55))]
    for kind, ref, payload, at in ev:
        connection.execute(text(
            "INSERT INTO incident_evidence (id, incident_id, kind, ref, payload, created_at) "
            "VALUES (:id, :i, :k, :r, CAST(:p AS JSONB), :at)"),
            {"id": f"ev_demo_{uuid4().hex[:12]}", "i": incident_id, "k": kind, "r": ref,
             "p": json.dumps(payload), "at": at})
    connection.execute(text(
        "INSERT INTO app_alerts (id, project_id, rule_kind, source_kind, fingerprint, alertname, "
        "labels, annotations, value, threshold, unit, state, active_at, fired_at, last_eval_at, "
        "incident_id, urgency) VALUES (:id, :p, 'error_rate', 'rule', :fp, 'OrderApiErrorRate', "
        "CAST(:l AS JSONB), CAST(:a AS JSONB), 6.6, 5, '%', 'firing', :f, :f, :n, :i, 'high')"),
        {"id": alert_id, "p": project_id, "fp": f"demo-{APP}-5xx-{tag}",
         "l": json.dumps({"severity": "high", "app": APP, "env": environment,
                          "route": "/orders", "method": "POST", "demo": "true"}),
         "a": json.dumps({"summary": "Demo: order-api 5xx at 6.6%",
                          "runbook_url": "boss:runbook/app-serving"}),
         "f": fired, "n": now, "i": incident_id})
    code = _code_graph(connection, project_id)
    precedent = _past_incident(connection, project_id)
    return {"incident_id": incident_id, "alert_id": alert_id, "precedent_id": precedent,
            "code": code}


# ── the scenario clock (demo only) ───────────────────────────────────────────────────────────
#
# A scripted run takes a fraction of a second, so its diagram would read "Recovered 0s". The
# REPLAY re-times its own rows onto the D2 incident's pace, measured from the moment the item
# opened: each decision lands at its milestone and every other row is interpolated between
# them, keeping their order. This is never called on a real run: real incidents keep real time.
# The ledger and the board are append-only, so their rows are re-written the way a retention
# job removes them (boss.ledger_purge) and inserted again, unchanged but for their time.

#: Seconds after the item opened at which each decision lands (question -> one per occurrence).
MILESTONES: dict[str, list[int]] = {
    "Who works this signal?": [5],
    "Who should own this incident?": [5],           # the Chief of Staff's own call
    squad.Q_TRIAGE: [40],
    squad.Q_CAUSE: [150],                  # RCA 1m50s after triage
    squad.Q_LEARN: [158],
    squad.Q_MITIGATE: [165, 185],          # the squad's call; a person's answer
    squad.Q_FIX: [237],                    # after a 50s rollback
    squad.Q_VERIFY: [250],
    squad.Q_CLOSE: [252],                  # recovered 4m12s after the alert opened the item
}


def _interp(points: list[tuple[datetime, datetime]], at: datetime) -> datetime:
    if at <= points[0][0]:
        return points[0][1] - (points[0][0] - at)
    for (r0, t0), (r1, t1) in zip(points, points[1:]):
        if at <= r1:
            span = (r1 - r0).total_seconds()
            share = (at - r0).total_seconds() / span if span > 0 else 1.0
            return t0 + (t1 - t0) * share
    return points[-1][1] + (at - points[-1][0])


def apply_scenario_clock(connection: Connection, project_id: str, item_id: str) -> None:
    """Re-time the replay's item onto MILESTONES (demo only; see above)."""
    item = connection.execute(text(
        "SELECT created_at FROM work_items WHERE id = :w AND project_id = :p AND "
        "subject_ref LIKE 'demo:%'"), {"w": item_id, "p": project_id}).mappings().first()
    if item is None:
        raise ValueError("the scenario clock re-times demo items only")
    base = item["created_at"]
    rows = connection.execute(text(
        "SELECT act_id, question, created_at FROM agent_act_decisions WHERE work_item_id = :w "
        "ORDER BY created_at"), {"w": item_id}).mappings().all()
    seen: dict[str, int] = {}
    points = [(base, base)]
    for r in rows:
        n = seen.get(r["question"], 0)
        seen[r["question"]] = n + 1
        marks = MILESTONES.get(r["question"]) or []
        if n < len(marks):
            target = base + timedelta(seconds=marks[n])
            if target > points[-1][1] and r["created_at"] > points[-1][0]:
                points.append((r["created_at"], target))

    # Pinned at the END: the last milestone keeps its real time and everything before it moves
    # back, so nothing is dated in the future (a person's later answer still comes after).
    shift = points[-1][1] - points[-1][0]

    def at(value: Optional[datetime]) -> Optional[datetime]:
        return None if value is None else _interp(points, value) - shift

    # work_items' own times are now() — the transaction's start — so they are re-timed from the
    # board rows that mark the same moments (clock_timestamp): the resolve, the ask, the answer.
    marks = connection.execute(text(
        "SELECT max(created_at) FILTER (WHERE kind = 'resolve') AS resolved, "
        "max(created_at) AS last FROM work_item_messages WHERE work_item_id = :w"),
        {"w": item_id}).mappings().one()
    asked = connection.execute(text(
        "SELECT max(created_at) FILTER (WHERE decided_by LIKE 'squad:%') AS asked, "
        "min(created_at) FILTER (WHERE decided_by LIKE 'person:%') AS answered "
        "FROM agent_act_decisions WHERE work_item_id = :w"), {"w": item_id}).mappings().one()
    connection.execute(text("SET LOCAL boss.ledger_purge = 'on'"))
    for table, key, cols in (("work_item_messages", "work_item_id", ("created_at",)),
                             ("agent_act_decisions", "work_item_id",
                              ("created_at", "outcome_at"))):
        kept = connection.execute(text(
            f"SELECT to_jsonb(t) AS row FROM {table} t WHERE {key} = :w ORDER BY created_at"),
            {"w": item_id}).scalars().all()
        connection.execute(text(f"DELETE FROM {table} WHERE {key} = :w"), {"w": item_id})
        for row in kept:
            for col in cols:
                if row.get(col):
                    row[col] = at(datetime.fromisoformat(row[col])).isoformat()
            connection.execute(text(
                f"INSERT INTO {table} SELECT (jsonb_populate_record(NULL::{table}, "
                "CAST(:r AS JSONB))).*"), {"r": json.dumps(row)})
    connection.execute(text("SET LOCAL boss.ledger_purge = 'off'"))
    for act in connection.execute(text(
            "SELECT act_id, started_at, ended_at FROM agent_acts WHERE subject_id = :w"),
            {"w": item_id}).mappings().all():
        connection.execute(text("UPDATE agent_acts SET started_at = :s, ended_at = :e "
                                "WHERE act_id = :a"),
                           {"s": at(act["started_at"]), "e": at(act["ended_at"]),
                            "a": act["act_id"]})
    opened = base - shift
    connection.execute(text("UPDATE work_item_links SET created_at = :t WHERE work_item_id = :w"),
                       {"t": opened, "w": item_id})
    connection.execute(text(
        "UPDATE signals SET first_seen_at = :t, last_seen_at = :t, observed_at = :t, "
        "created_at = :t WHERE work_item_id = :w AND source = 'alert'"),
        {"t": opened - timedelta(seconds=3), "w": item_id})
    times = connection.execute(text(
        "SELECT resolved_at, needs_you_at, answered_at, outcome FROM work_items "
        "WHERE id = :w"), {"w": item_id}).mappings().one()
    outcome = times["outcome"]
    resolved = at(marks["resolved"]) if times["resolved_at"] else None
    if resolved is not None and outcome:
        took = int(round((resolved - opened).total_seconds()))
        outcome = outcome.split(" in ")[0] + f" in {took // 60}m{took % 60:02d}s"
    connection.execute(text(
        "UPDATE work_items SET created_at = :c, updated_at = :u, resolved_at = :r, "
        "needs_you_at = :n, answered_at = :a, outcome = :o WHERE id = :w"),
        {"c": opened, "u": at(marks["last"]), "r": resolved,
         "n": at(asked["asked"]) if times["needs_you_at"] else None,
         "a": at(asked["answered"]) if times["answered_at"] else None, "o": outcome,
         "w": item_id})


# ── the run ──────────────────────────────────────────────────────────────────────────────────

def run_scenario(connection: Connection, project_id: str, *, environment: str = "production",
                 approve_as: Optional[str] = None, settings: Any = None) -> dict[str, Any]:
    """prepare() then run the squad on the scripted engine, then the scenario clock; with
    `approve_as`, answer the production Needs-you card as that person. Returns the stages and
    the Agent Operations graph of the item."""
    prepared = prepare(connection, project_id, environment=environment, settings=settings)
    item_id = prepared["item_id"]
    stages = [squad.route_incident(connection, project_id=project_id, item_id=item_id,
                                   mind=demo_mind(environment), effects=FixtureEffects())]
    if approve_as and stages[-1]["stage"] == "needs_you":
        stages.append(squad.resume(connection, project_id=project_id, item_id=item_id,
                                   answer="approve", actor=approve_as,
                                   mind=demo_mind(environment), effects=FixtureEffects(),
                                   note="Approved: roll back, the fix follows"))
    apply_scenario_clock(connection, project_id, item_id)
    graph = read_model.graph(connection, item_id, project_id=project_id)
    return {"project_id": project_id, **prepared, "stages": [s["stage"] for s in stages],
            "needs_you_decision_id": stages[-1].get("decision_id"), "graph": graph}



def run_through_chief_of_staff(connection: Connection, project_id: str, *,
                               environment: str = "production", settings: Any = None,
                               approve_as: Optional[str] = None) -> dict[str, Any]:
    """THE INTEGRATED PATH: the alert becomes a signal (the real app_alerts adapter), the CHIEF OF
    STAFF triages it (rules, route mode: an incident goes straight to the SRE squad), and the
    SRE squad works it through the same handler the owners' tick calls
    (incident_squad.route_incident) on the scripted engine and fixture effects. The item is
    marked `demo:` (subject_ref) so a person's answer resumes it on the same scripted engine."""
    from .ambient import chief_of_staff

    seeded = seed(connection, project_id, environment=environment)
    signals.sweep(connection, project_id=project_id, only=["app_alerts"])
    sig = connection.execute(text(
        "SELECT id FROM signals WHERE project_id = :p AND evidence @> CAST(:e AS JSONB) "
        "ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "e": json.dumps([{"kind": "alert", "ref": seeded["alert_id"]}])}
    ).scalar()
    if sig is None:
        raise RuntimeError("the app_alerts adapter did not record the demo alert")
    triage = chief_of_staff.triage_project(connection, settings, project_id, mode="route",
                                           max_model_calls=0)
    item_id = connection.execute(text("SELECT work_item_id FROM signals WHERE id = :s"),
                                 {"s": sig}).scalar()
    if item_id is None:
        raise RuntimeError(f"the Chief of Staff did not open an item for the alert: {triage}")
    connection.execute(text("UPDATE work_items SET subject_ref = :r WHERE id = :w"),
                       {"r": f"demo:{APP}", "w": item_id})
    stages = [squad.route_incident(connection, project_id=project_id, item_id=item_id,
                                   mind=demo_mind(environment), effects=FixtureEffects())]
    if approve_as and stages[-1]["stage"] == "needs_you":
        stages.append(squad.resume(connection, project_id=project_id, item_id=item_id,
                                   answer="approve", actor=approve_as,
                                   mind=demo_mind(environment), effects=FixtureEffects(),
                                   note="Approved: roll back, the fix follows"))
    apply_scenario_clock(connection, project_id, item_id)
    graph = read_model.graph(connection, item_id, project_id=project_id)
    return {"project_id": project_id, **seeded, "signal_id": sig, "item_id": item_id,
            "triage": triage.get("counts") or {}, "stages": [s["stage"] for s in stages],
            "needs_you_decision_id": stages[-1].get("decision_id"), "graph": graph}


def prepare(connection: Connection, project_id: str, *, environment: str = "production",
            settings: Any = None) -> dict[str, Any]:
    """Seed, sweep the alert into a signal (the real app_alerts adapter) and let the CoS
    stand-in open and route the item. Returns the seeded ids plus signal_id and item_id."""
    seeded = seed(connection, project_id, environment=environment)
    signals.sweep(connection, project_id=project_id, only=["app_alerts"])
    sig_id = connection.execute(text(
        "SELECT id FROM signals WHERE project_id = :p AND evidence @> CAST(:e AS JSONB) "
        "ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "e": json.dumps([{"kind": "alert", "ref": seeded["alert_id"]}])}
    ).scalar()
    if sig_id is None:
        raise RuntimeError("the app_alerts adapter did not record the demo alert")
    opened = squad.intake(connection, project_id=project_id, signal_id=sig_id,
                          settings=settings, subject_ref=f"demo:{APP}")
    return {**seeded, "signal_id": sig_id, "item_id": opened["item"]["id"]}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Re-run the Order API incident with the "
                                                 "incident squad (demo fixtures; no host).")
    parser.add_argument("--project", required=True)
    parser.add_argument("--env", default="production")
    parser.add_argument("--approve-as", default=None)
    args = parser.parse_args(argv)
    from sqlalchemy import create_engine

    from ..config import get_settings
    from ..db import ensure_schema

    settings = get_settings()
    engine = create_engine(settings.database_url, future=True)
    ensure_schema(engine)
    with engine.begin() as c:
        out = run_scenario(c, args.project, environment=args.env, approve_as=args.approve_as)
    g = out["graph"]
    print(json.dumps({"item_id": out["item_id"], "stages": out["stages"],
                      "needs_you_decision_id": out["needs_you_decision_id"],
                      "badges": g["badges"],
                      "flow": [f"{n['stage']} {n['type']}: {n['label']}" for n in g["nodes"]]},
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
