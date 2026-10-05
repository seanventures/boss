"""Rows for the Command Center view's tests, written straight into the tables the view reads.

The view is a read: what it must get right is counting, bucketing and attributing rows that
exist. Driving a workflow through the worker to make each row would test the worker; these put
the rows down directly, in the shapes the writers leave them (see the column comments in
db/schema.sql), and `busy_day` seeds the spec §6.4 scale for the performance test.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

PROJECT = "boss-dogfood"


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def workflow(c, *, name: str = "Ship it", steps: int = 3, namespace: str = "product",
             labels: Optional[list[str]] = None) -> tuple[str, str]:
    """A published product workflow with `steps` agent steps: (definition id, version id)."""
    did, vid = uid("wfd"), uid("wfv")
    key = uid("wf").replace("_", "-")
    spec = {"entry_step": "s1", "steps": [{"id": f"s{i + 1}", "kind": "agent", "label": (labels or [])[i] if i < len(labels or []) else f"Step {i + 1}"}
                                          for i in range(steps)]}
    c.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by, namespace) "
                   "VALUES (:i, :p, :k, :n, 'test', :ns)"),
              {"i": did, "p": PROJECT, "k": key, "n": name, "ns": namespace})
    c.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, spec, "
                   "content_hash, created_by) VALUES (:i, :p, :d, 1, CAST(:s AS jsonb), :h, 'test')"),
              {"i": vid, "p": PROJECT, "d": did, "s": json.dumps(spec), "h": uid("h")})
    return did, vid


def run(c, wf: tuple[str, str], *, status: str, created: datetime,
        completed: Optional[datetime] = None, title: str = "", error: Optional[str] = None,
        parent: Optional[str] = None) -> str:
    rid = uid("wfr")
    c.execute(text(
        "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, workflow_version_id, actor, "
        "status, input, error, created_at, updated_at, completed_at, parent_run_id) VALUES "
        "(:i, :p, :d, :v, 'ceo', :s, CAST(:inp AS jsonb), :e, :c, :u, :done, :parent)"),
        {"i": rid, "p": PROJECT, "d": wf[0], "v": wf[1], "s": status,
         "inp": json.dumps({"title": title} if title else {}), "e": error, "c": created,
         "u": completed or created, "done": completed, "parent": parent})
    return rid


def step(c, run_id: str, step_id: str, *, status: str, started: Optional[datetime],
         completed: Optional[datetime] = None, kind: str = "agent",
         decision_id: Optional[str] = None) -> str:
    sid = uid("wsr")
    if decision_id:
        c.execute(text("INSERT INTO decisions (id, project_id, question, status, risk_tier, default_action) "
                       "VALUES (:i, :p, 'Go on?', :s, 1, 'hold') ON CONFLICT (id) DO NOTHING"),
                  {"i": decision_id, "p": PROJECT, "s": "approved" if completed else "pending"})
    c.execute(text(
        "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, step_kind, status, "
        "started_at, completed_at, decision_id, created_at, updated_at) VALUES "
        "(:i, :p, :r, :s, :k, :st, :a, :b, :d, COALESCE(:a, now()), COALESCE(:b, :a, now()))"),
        {"i": sid, "p": PROJECT, "r": run_id, "s": step_id, "k": kind, "st": status, "a": started,
         "b": completed, "d": decision_id})
    return sid


def act(c, *, role_key: Optional[str], started: datetime, ended: Optional[datetime] = None,
        kind: str = "step", status: str = "complete", handed_by_kind: str = "boss",
        handed_by_id: Optional[str] = None, run_id: Optional[str] = None) -> str:
    aid = uid("act")
    c.execute(text(
        "INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, root_act_id, handed_by_kind, "
        "handed_by_id, how, accountable, workflow_run_id, label, started_at, ended_at) VALUES "
        "(:i, :p, :r, :k, :s, :i, :hk, :hi, 'started', 'ceo', :run, 'Step · test', :a, :b)"),
        {"i": aid, "p": PROJECT, "r": role_key, "k": kind, "s": status, "hk": handed_by_kind,
         "hi": handed_by_id, "run": run_id, "a": started, "b": ended})
    return aid


def receipt(c, *, at: datetime, micros: int, included: bool = False,
            equivalent: Optional[int] = None) -> str:
    rid = uid("rcpt")
    c.execute(text(
        "INSERT INTO cost_receipts (id, project_id, category, skill_or_model_id, estimated_usd_micros, "
        "actual_usd_micros, included_in_subscription, equivalent_usd_micros, created_at) VALUES "
        "(:i, :p, 'llm', 'test-model', :m, :m, :inc, :eq, :at)"),
        {"i": rid, "p": PROJECT, "m": micros, "inc": included, "eq": equivalent, "at": at})
    return rid


def code_task(c, *, status: str, created: datetime) -> str:
    cid = uid("ctr")
    c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, created_at) "
                   "VALUES (:i, :p, 'A build', :s, :c)"),
              {"i": cid, "p": PROJECT, "s": status, "c": created})
    return cid


def roster(c) -> list[str]:
    return [str(r[0]) for r in c.execute(
        text("SELECT role_key FROM roles WHERE project_id = :p AND role_key <> 'human' "
             "AND COALESCE(lifecycle, 'active') <> 'retired' ORDER BY role_key"), {"p": PROJECT})]


def busy_day(c, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """The spec §6.4 seeded scale: 60 live + 14-day runs, 165 decisions (127 expired), 474
    code-task runs, 6,000 acts in 48h, 20,000 receipts in 30d, 30,000 model calls in 30d, 2,000
    jobs, 50 previews. Bulk-inserted with generate_series so seeding takes seconds."""
    now = now or datetime.now(timezone.utc)
    wf = workflow(c, name="Ship it", steps=7)
    keys = roster(c) or ["software_engineer"]
    runs = []
    for i in range(60):
        live = i < 12
        status = "running" if live else ("failed" if i % 9 == 0 else "completed")
        created = now - timedelta(hours=2 + i * 5)
        rid = run(c, wf, status=status, created=created, title=f"Ship it #{100 + i}",
                  completed=None if live else created + timedelta(minutes=40))
        runs.append(rid)
        for k in range(1, 5):
            done = not live or k < 3
            step(c, rid, f"s{k}", status="succeeded" if done else "running",
                 started=created + timedelta(minutes=5 * k),
                 completed=created + timedelta(minutes=5 * k + 4) if done else None)
    p = {"p": PROJECT, "now": now, "keys": keys, "nk": len(keys), "run": runs[0]}
    c.execute(text(
        "INSERT INTO decisions (id, project_id, question, status, risk_tier, default_action, created_at) "
        "SELECT 'dec_seed_' || g, :p, 'Approve?', CASE WHEN g <= 127 THEN 'expired' ELSE 'pending' END, "
        "1, 'hold', :now - (g || ' hours')::interval FROM generate_series(1, 165) g"), p)
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, title, status, created_at) "
        "SELECT 'ctr_seed_' || g, :p, 'A build', CASE WHEN g % 5 = 0 THEN 'failed' ELSE 'completed' END, "
        ":now - (g || ' hours')::interval FROM generate_series(1, 474) g"), p)
    c.execute(text(
        "INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, root_act_id, handed_by_kind, "
        "handed_by_id, how, accountable, workflow_run_id, label, started_at, ended_at) "
        "SELECT 'act_seed_' || g, :p, (CAST(:keys AS text[]))[1 + g % :nk], 'step', 'complete', 'act_seed_' || g, "
        "CASE WHEN g % 3 = 0 THEN 'agent' WHEN g % 3 = 1 THEN 'boss' ELSE 'person' END, "
        "CASE WHEN g % 3 = 0 THEN (CAST(:keys AS text[]))[1 + (g + 1) % :nk] WHEN g % 3 = 2 THEN 'ceo' END, "
        "'started', 'ceo', :run, 'Step · seed', :now - (g * 28.8 || ' seconds')::interval, "
        ":now - (g * 28.8 - 20 || ' seconds')::interval FROM generate_series(1, 6000) g"), p)
    c.execute(text(
        "INSERT INTO cost_receipts (id, project_id, category, skill_or_model_id, estimated_usd_micros, "
        "actual_usd_micros, included_in_subscription, equivalent_usd_micros, created_at) "
        "SELECT 'rcpt_seed_' || g, :p, 'llm', 'm', 1000, 1000, g % 4 = 0, 1500, "
        ":now - (g * 129.6 || ' seconds')::interval FROM generate_series(1, 20000) g"), p)
    c.execute(text(
        "INSERT INTO llm_calls (id, project_id, ok, latency_ms, total_tokens, cost_usd_micros, created_at) "
        "SELECT 'llm_seed_' || g, :p, g % 50 <> 0, 1200, 900, 1000, "
        ":now - (g * 86.4 || ' seconds')::interval FROM generate_series(1, 30000) g"), p)
    c.execute(text(
        "INSERT INTO jobs (id, project_id, kind, status, created_at) "
        "SELECT 'job_seed_' || g, :p, 'test', CASE WHEN g <= 3 THEN 'queued' ELSE 'succeeded' END, "
        ":now - (g || ' minutes')::interval FROM generate_series(1, 2000) g"), p)
    c.execute(text(
        "INSERT INTO app_previews (id, project_id, status, created_at) "
        "SELECT 'prv_seed_' || g, :p, CASE WHEN g = 1 THEN 'running' ELSE 'stopped' END, "
        ":now - (g || ' hours')::interval FROM generate_series(1, 50) g"), p)
    return {"runs": runs, "workflow": wf}
