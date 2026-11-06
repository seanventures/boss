"""A realistic Recommended roadmap for a DEMO project (BOSSAPP-911) — never on a real one.

`seed(connection, project_id)` writes the kind of history a busy project has — PRs that keep
changing the same checkout files, incidents whose RCA points at them, review findings that
recur, a flaky webhook test, a design that drifted from its PRD, a workflow whose cost jumped,
the design and repair-review stages running long, people steering builds at the same stage —
into the SAME tables and shapes BOSS's own writers use, then runs the real detectors over it.
So the roadmap the console shows is the detectors' honest output, not hand-written cards.

The `put_*` builders are also what the detector tests use: one place knows each writer's shape.

Refused unless the project is a demo (its id, slug or name contains "demo") or
BOSS_DEMO_DATA=1 — the rows are fiction and must never mix with a real project's history.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ._common import now

REPO = "northpeak/saleor"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def is_demo(connection: Connection, project_id: str) -> bool:
    if os.environ.get("BOSS_DEMO_DATA", "").strip() in ("1", "true", "yes"):
        return True
    row = connection.execute(text("SELECT id, slug, name FROM projects WHERE id = :p"),
                             {"p": project_id}).mappings().first()
    return row is not None and any("demo" in str(v or "").lower() for v in row.values())


# ── builders: one per writer's shape ────────────────────────────────────────────────────────

def put_file(connection: Connection, project_id: str, path: str, *, lines: int = 400,
             symbols: Iterable[str] = ("run",), repo: str = REPO) -> dict[str, Any]:
    """A file node and its symbols, as code_index writes them (file:<repo>:<path>,
    symbol:<repo>:<path>:<qualname>; attrs.lines on the file)."""
    file_id = _id("kgn")
    connection.execute(text(
        "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, attrs, source, repo, path) "
        "VALUES (:id, :p, 'file', :k, :n, CAST(:a AS JSONB), 'code_index', :r, :path) "
        "ON CONFLICT (project_id, kind, node_key) DO NOTHING"),
        {"id": file_id, "p": project_id, "k": f"file:{repo}:{path}", "n": path.rsplit("/", 1)[-1],
         "a": json.dumps({"lines": lines, "lang": "python"}), "r": repo, "path": path})
    syms = {}
    for qual in symbols:
        sid = _id("kgn")
        connection.execute(text(
            "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, attrs, source, repo, "
            "path) VALUES (:id, :p, 'symbol', :k, :n, '{}'::jsonb, 'code_index', :r, :path) "
            "ON CONFLICT (project_id, kind, node_key) DO NOTHING"),
            {"id": sid, "p": project_id, "k": f"symbol:{repo}:{path}:{qual}", "n": qual,
             "r": repo, "path": path})
        syms[qual] = sid
    return {"id": file_id, "symbols": syms, "path": path}


def put_callers(connection: Connection, project_id: str, target_symbol_id: str,
                n: int) -> None:
    """Fan-in: `n` caller symbols with a `calls` edge into the target."""
    for i in range(n):
        caller = _id("kgn")
        connection.execute(text(
            "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, source) VALUES "
            "(:id, :p, 'symbol', :k, :n, 'code_index')"),
            {"id": caller, "p": project_id, "k": f"symbol:caller_{caller}", "n": f"caller{i}"})
        connection.execute(text(
            "INSERT INTO kg_edges (id, project_id, src_id, dst_id, kind, source) VALUES "
            "(:id, :p, :s, :d, 'calls', 'code_index')"),
            {"id": _id("kge"), "p": project_id, "s": caller, "d": target_symbol_id})


def put_pr(connection: Connection, project_id: str, number: int, paths: Iterable[str], *,
           at: Optional[datetime] = None, repo: str = REPO) -> str:
    """A merged PR as capture_lifecycle records it: a pull_request node and change links from
    `pr-<repo>#<n>` to every symbol it touched (keys `symbol:<path>:<qual>`)."""
    source_id = f"pr-{repo}#{number}"
    connection.execute(text(
        "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, source) VALUES "
        "(:id, :p, 'pull_request', :k, :n, 'capture') ON CONFLICT DO NOTHING"),
        {"id": _id("kgn"), "p": project_id, "k": f"pull_request:{source_id}",
         "n": f"{repo}#{number}"})
    for path in paths:
        connection.execute(text(
            "INSERT INTO kg_change_links (project_id, source_kind, source_id, symbol_key, "
            "recorded_at) VALUES (:p, 'pull_request', :s, :k, :at) ON CONFLICT DO NOTHING"),
            {"p": project_id, "s": source_id, "k": f"symbol:{path}:run", "at": at or now()})
    return source_id


def put_incident(connection: Connection, project_id: str, *, title: str, suspect_file: str = "",
                 cause_key: str = "", exception: str = "", severity: str = "critical",
                 at: Optional[datetime] = None, resolved_after_min: Optional[float] = 42,
                 mitigated_after_min: Optional[float] = 9) -> str:
    """An incident and its RCA hypothesis (incident_response._evidence 'rca_hypothesis')."""
    inc = _id("inc")
    seen = at or now()
    connection.execute(text(
        "INSERT INTO incidents (id, project_id, dedupe_key, title, service, severity, status, "
        "first_seen, last_seen, mitigated_at, resolved_at) VALUES (:id, :p, :k, :t, 'checkout', "
        ":sev, :st, :at, :at, :m, :r)"),
        {"id": inc, "p": project_id, "k": inc, "t": title, "sev": severity,
         "st": "resolved" if resolved_after_min is not None else "open", "at": seen,
         "m": seen + timedelta(minutes=mitigated_after_min) if mitigated_after_min else None,
         "r": seen + timedelta(minutes=resolved_after_min) if resolved_after_min else None})
    connection.execute(text(
        "INSERT INTO incident_evidence (id, incident_id, kind, ref, payload, created_at) VALUES "
        "(:id, :i, 'rca_hypothesis', '', CAST(:pl AS JSONB), :at)"),
        {"id": _id("iev"), "i": inc, "at": seen, "pl": json.dumps({
            "root_cause": title, "basis": "release_changed_failing_frame", "confidence": 0.8,
            "suspect_file": suspect_file, "cause_key": cause_key, "exception": exception})})
    return inc


def put_build(connection: Connection, project_id: str, *, title: str = "build",
              at: Optional[datetime] = None, review_comments: Iterable[dict] = (),
              baseline: Optional[dict] = None, consistency: Optional[str] = None,
              consistency_summary: str = "", decision_id: Optional[str] = None,
              stage_timings: Iterable[dict] = ()) -> str:
    """A code-task run with the finalize_ctx/state shapes the governed pipeline writes."""
    run = _id("crun")
    state: dict[str, Any] = {}
    if baseline is not None:
        state["baseline"] = baseline
    if consistency:
        state["consistency_verdict"] = consistency
        state["consistency_summary"] = consistency_summary
    ctx: dict[str, Any] = {}
    comments = list(review_comments)
    if comments:
        ctx["review_comments"] = comments
    timings = list(stage_timings)
    if timings:
        ctx["stage_timings"] = timings
    connection.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, state, finalize_ctx, "
        "decision_id, created_at, updated_at) VALUES (:id, :p, 'completed', 'done', :t, "
        "CAST(:s AS JSONB), CAST(:f AS JSONB), :d, :at, :at)"),
        {"id": run, "p": project_id, "t": title, "s": json.dumps(state), "f": json.dumps(ctx),
         "d": decision_id, "at": at or now()})
    return run


def review_comment(body: str, *, stage: str = "review_code", severity: str = "major",
                   path: str = "saleor/payment/gateway.py") -> dict[str, Any]:
    return {"id": _id("rc"), "severity": severity, "body": body, "path": path, "line": 41,
            "author": "adversarial_reviewer", "author_kind": "agent", "status": "addressed",
            "stage": stage, "round": 1}


def put_event(connection: Connection, project_id: str, event_type: str, *, actor: str,
              entity_type: str, entity_id: str, data: dict, at: Optional[datetime] = None) -> str:
    eid = _id("evt")
    connection.execute(text(
        "INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, data, "
        "created_at) VALUES (:id, :p, :t, :a, :et, :ei, CAST(:d AS JSONB), :at)"),
        {"id": eid, "p": project_id, "t": event_type, "a": actor, "et": entity_type,
         "ei": entity_id, "d": json.dumps(data), "at": at or now()})
    return eid


def put_profile(connection: Connection, project_id: str, run_id: str, stages: dict[str, float],
                *, at: Optional[datetime] = None) -> str:
    """build_profile's `code_task.build_profile` event: stages.<name>.wall_min."""
    return put_event(connection, project_id, "code_task.build_profile", actor="boss",
                     entity_type="code_task", entity_id=run_id, at=at,
                     data={"run_id": run_id, "stages": {k: {"wall_min": v}
                                                        for k, v in stages.items()}})


def put_scorecard(connection: Connection, project_id: str, unit_id: str, *, cost_usd: float,
                  workflow_key: str = "feature_build", at: Optional[datetime] = None,
                  unit_kind: str = "build", pr_merged: Optional[bool] = None,
                  pr_reverted: Optional[bool] = None) -> None:
    """A run scorecard row (scorecards.record_scorecard's columns this reads)."""
    connection.execute(text(
        "INSERT INTO run_scorecards (unit_kind, unit_id, project_id, workflow_key, "
        "terminal_state, cost_usd, ended_at, pr_merged, pr_reverted, late_outcomes_at) VALUES "
        "(:k, :u, :p, :w, 'completed', :c, :at, :m, :r, :at) ON CONFLICT (unit_kind, unit_id) "
        "DO UPDATE SET cost_usd = EXCLUDED.cost_usd, pr_merged = EXCLUDED.pr_merged, "
        "pr_reverted = EXCLUDED.pr_reverted, late_outcomes_at = EXCLUDED.late_outcomes_at"),
        {"k": unit_kind, "u": unit_id, "p": project_id, "w": workflow_key, "c": cost_usd,
         "at": at or now(), "m": pr_merged, "r": pr_reverted})


# ── the demo story ──────────────────────────────────────────────────────────────────────────

def seed(connection: Connection, project_id: str, *, settings: Any = None,
         actor: str = "ceo", decide: bool = True) -> dict[str, Any]:
    """Write a realistic history, run the detectors, and (optionally) answer two items the way a
    person would, so the scorecard has acceptance and noise to show."""
    if not is_demo(connection, project_id):
        raise PermissionError("demo data goes only into a demo project (its name, slug or id "
                              "contains 'demo'), or with BOSS_DEMO_DATA=1")
    t = now()
    day = timedelta(days=1)
    checkout = put_file(connection, project_id, "saleor/checkout/complete_checkout.py",
                        lines=1840, symbols=("complete_checkout", "_reserve_stock"))
    put_callers(connection, project_id, checkout["symbols"]["complete_checkout"], 14)
    orders = put_file(connection, project_id, "saleor/order/actions.py", lines=1420,
                      symbols=("fulfill_order", "cancel_order"))
    gateway = put_file(connection, project_id, "saleor/payment/gateway.py", lines=920,
                       symbols=("process_payment",))
    put_file(connection, project_id, "saleor/graphql/checkout/mutations.py", lines=2110)
    put_file(connection, project_id, "saleor/order/tests/test_order_actions.py", lines=600)
    put_file(connection, project_id, "saleor/payment/tests/test_payment_webhooks.py", lines=300)
    for n in range(12):
        put_pr(connection, project_id, 4100 + n, [checkout["path"]], at=t - (40 - 3 * n) * day)
    for n in range(7):
        put_pr(connection, project_id, 4200 + n, [orders["path"]], at=t - (30 - 4 * n) * day)
    for n in range(5):
        put_pr(connection, project_id, 4300 + n, [gateway["path"]], at=t - (25 - 4 * n) * day)
    incidents = [put_incident(connection, project_id,
                              title="Checkout 500s: stock reserved twice under load",
                              suspect_file=checkout["path"], cause_key="checkout.stock_race",
                              at=t - d * day) for d in (38, 19, 4)]
    put_incident(connection, project_id, title="Payment webhook timeouts", severity="warning",
                 cause_key="payment.webhook_timeout", at=t - 12 * day, resolved_after_min=95,
                 mitigated_after_min=30)
    put_incident(connection, project_id, title="Payment webhook timeouts again",
                 severity="warning", cause_key="payment.webhook_timeout", at=t - 2 * day,
                 resolved_after_min=60, mitigated_after_min=14)
    builds = []
    for n in range(8):
        comments = [review_comment("Payment retry has no idempotency key; a retried call can "
                                   "charge twice.")]
        if n % 2 == 0:
            comments.append(review_comment("Missing await on the async webhook handler.",
                                           severity="minor"))
        flaky = {"environment": [{"test_id": "saleor/payment/tests/test_payment_webhooks.py::"
                                  "test_retry_signature", "reason": "timeout"}]} if n < 5 else None
        timings = [{"stage": "design", "total_ms": int((9.5 + n % 3) * 60000)},
                   {"stage": "repair_review", "total_ms": int((17 + n % 4) * 60000)},
                   {"stage": "implement", "total_ms": int(14 * 60000)}]
        run = put_build(connection, project_id, title=f"Checkout change {n + 1}",
                        at=t - (20 - 2 * n) * day, review_comments=comments, baseline=flaky,
                        consistency="drift" if n in (2, 5) else "consistent",
                        consistency_summary="Order cancellation no longer releases stock as the "
                        "approved PRD requires.", stage_timings=timings)
        builds.append(run)
        put_profile(connection, project_id, run, {"design": 9.5 + n % 3,
                                                   "repair_review": 17.0 + n % 4,
                                                   "implement": 14.0},
                    at=t - (20 - 2 * n) * day)
        put_scorecard(connection, project_id, run, cost_usd=31.0 + n if n >= 5 else 12.0 + n,
                      workflow_key="checkout_feature",
                      at=t - (2 if n >= 5 else 20 + n) * day)
    for n in range(3):
        put_scorecard(connection, project_id, f"{builds[n]}_older", cost_usd=11.5 + n,
                      workflow_key="checkout_feature", at=t - (30 + n) * day)
    for n in range(7):
        put_event(connection, project_id, "code_task.steered", actor="ceo",
                  entity_type="code_task", entity_id=builds[n % len(builds)],
                  data={"stage": "write_tests", "count": 1}, at=t - (10 - n) * day)
    report = run_now(connection, project_id, settings=settings)
    decided: dict[str, Any] = {}
    if decide and report["opened"]:
        from . import roadmap

        listed = roadmap.list_roadmap(connection, project_id, status="open")["items"]
        if len(listed) >= 2:
            first, last = listed[0], listed[-1]
            decided["accepted"] = roadmap.accept(
                connection, first["id"], project_id=project_id, actor=actor, as_="ticket",
                note="Top of the list; schedule it next sprint.", settings=settings)[
                "recommendation"]["id"]
            decided["rejected"] = roadmap.reject(
                connection, last["id"], project_id=project_id, actor=actor,
                reason="Known and accepted for now.", category="already_planned")[
                "recommendation"]["id"]
    return {"report": report, "incidents": incidents, "builds": builds, "decided": decided}


def run_now(connection: Connection, project_id: str, *, settings: Any = None) -> dict[str, Any]:
    from . import policy, roadmap

    # A demo shows the whole list at once: lift the per-day open cap for this project only.
    limits = policy.settings_for(connection, project_id)
    if int(limits["daily_attempt_cap"]) < 30:
        policy.update_settings(connection, project_id, actor="boss", daily_attempt_cap=30)
    return roadmap.run(connection, project_id, settings=settings, max_new=12)


__all__ = ["seed", "is_demo", "put_file", "put_callers", "put_pr", "put_incident", "put_build",
           "review_comment", "put_event", "put_profile", "put_scorecard"]
