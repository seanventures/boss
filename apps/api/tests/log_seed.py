"""Rows for every stream of the activity log, written into the real tables.

The log is SQL now (activity_sql), so its tests run against Postgres rather than an in-memory
double that only pretended to filter. One helper per stream, each creating the parent rows its
foreign keys need, so a test states only what it is about. `bulk()` writes tens of thousands of
rows in a few statements for the performance budget.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import text

PROJECT = "boss-dogfood"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def _j(value: Any) -> str:
    return json.dumps(value)


def event(c, *, event_type: str, at: datetime, project: str = PROJECT, actor: str = "ceo",
          entity_type: str = "job", entity_id: str = "job_1", data: Optional[dict] = None,
          id: Optional[str] = None) -> str:
    row_id = id or _id("evt")
    c.execute(text(
        "INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, data, "
        "created_at) VALUES (:i, :p, :t, :a, :et, :e, CAST(:d AS JSONB), :at)"),
        {"i": row_id, "p": project, "t": event_type, "a": actor, "et": entity_type,
         "e": entity_id, "d": _j(data or {}), "at": at})
    return row_id


def workflow(c, *, project: str = PROJECT, name: str = "Lead triage", run_id: Optional[str] = None,
             subject: Optional[str] = None, steps: Optional[list[dict]] = None) -> str:
    """A definition, a published version and a run of it."""
    suffix = uuid4().hex[:10]
    spec = {"steps": steps or [{"id": "plan", "label": "Plan it"},
                               {"id": "build", "label": "Build it"}]}
    c.execute(text(
        "INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by) "
        "VALUES (:d, :p, :k, :n, 'ceo')"),
        {"d": f"wfd_{suffix}", "p": project, "k": f"log_{suffix}", "n": name})
    c.execute(text(
        "INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, status, "
        "spec, content_hash, created_by) VALUES (:v, :p, :d, 1, 'published', CAST(:s AS JSONB), "
        ":h, 'ceo')"),
        {"v": f"wfv_{suffix}", "p": project, "d": f"wfd_{suffix}", "s": _j(spec),
         "h": hashlib.sha256(_j(spec).encode()).hexdigest()})
    run = run_id or f"wfr_{suffix}"
    c.execute(text(
        "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, workflow_version_id, "
        "actor, status, input) VALUES (:r, :p, :d, :v, 'ceo', 'running', CAST(:i AS JSONB))"),
        {"r": run, "p": project, "d": f"wfd_{suffix}", "v": f"wfv_{suffix}",
         "i": _j({"subject": subject} if subject else {})})
    return run


def step(c, *, workflow_run_id: str, step_id: str = "build", step_kind: str = "agent",
         status: str = "succeeded", attempt: int = 1, last_error: Optional[str] = None,
         created_at: datetime, started_at: Optional[datetime] = None,
         completed_at: Optional[datetime] = None, output: Optional[dict] = None,
         iteration: int = 0, project: str = PROJECT, id: Optional[str] = None) -> str:
    row_id = id or _id("wsr")
    c.execute(text(
        "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, step_kind, "
        "iteration, status, output, attempt, last_error, started_at, completed_at, created_at) "
        "VALUES (:i, :p, :w, :s, :k, :it, :st, CAST(:o AS JSONB), :a, :e, :sa, :ca, :c)"),
        {"i": row_id, "p": project, "w": workflow_run_id, "s": step_id, "k": step_kind,
         "it": iteration, "st": status, "o": _j(output or {}), "a": attempt, "e": last_error,
         "sa": started_at, "ca": completed_at, "c": created_at})
    return row_id


def build(c, *, title: str = "Ship it #182", project: str = PROJECT,
          id: Optional[str] = None) -> str:
    run = id or _id("crun")
    c.execute(text("INSERT INTO code_task_runs (id, project_id, title) VALUES (:i, :p, :t)"),
              {"i": run, "p": project, "t": title})
    return run


def llm_call(c, *, at: datetime, role: str = "software_engineer", stage: str = "implement",
             ok: bool = True, retries: int = 0, run_id: Optional[str] = None,
             model: str = "claude-sonnet", cost_usd_micros: int = 1000, error: str = "",
             included: bool = False, act_id: Optional[str] = None, project: str = PROJECT,
             id: Optional[str] = None) -> str:
    row_id = id or _id("llm")
    c.execute(text(
        "INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, model, "
        "total_tokens, cost_usd_micros, latency_ms, retries, turns, ok, error, created_at, role, "
        "included_in_subscription, act_id) VALUES (:i, :p, :r, :s, 'claude_code', :m, 1200, "
        ":cost, 900, :re, 3, :ok, :e, :at, :role, :inc, :act)"),
        {"i": row_id, "p": project, "r": run_id, "s": stage, "m": model,
         "cost": cost_usd_micros, "re": retries, "ok": ok, "e": error, "at": at, "role": role,
         "inc": included, "act": act_id})
    return row_id


def turn(c, *, at: datetime, role: str = "software_engineer", content: str = "ran the tests",
         level: str = "verbose", direction: str = "completion", tool: Optional[str] = None,
         run_id: Optional[str] = None, ok: bool = True, act_id: Optional[str] = None,
         turn_index: int = 0, truncated: bool = False, content_bytes: int = 0,
         project: str = PROJECT, id: Optional[str] = None) -> str:
    row_id = id or _id("turn")
    c.execute(text(
        "INSERT INTO agent_turns (id, project_id, code_task_run_id, stage, role_key, engine, "
        "model, turn_index, direction, tool_name, content, content_bytes, truncated, level, ok, "
        "created_at, act_id) VALUES (:i, :p, :r, 'implement', :role, 'claude_code', "
        "'claude-sonnet', :ti, :d, :tool, :c, :cb, :tr, :lv, :ok, :at, :act)"),
        {"i": row_id, "p": project, "r": run_id, "role": role, "ti": turn_index, "d": direction,
         "tool": tool, "c": content, "cb": content_bytes or len(content), "tr": truncated,
         "lv": level, "ok": ok, "at": at, "act": act_id})
    return row_id


def app_log(c, *, at: datetime, message: str = "listening on :3000", level: str = "info",
            service: str = "worker", logger: str = "boss.worker.loop",
            run_id: Optional[str] = None, project: Optional[str] = PROJECT,
            id: Optional[str] = None) -> str:
    row_id = id or _id("alog")
    c.execute(text(
        "INSERT INTO app_logs (id, project_id, run_id, service, level, logger, message, "
        "created_at) VALUES (:i, :p, :r, :s, :l, :lg, :m, :at)"),
        {"i": row_id, "p": project, "r": run_id, "s": service, "l": level, "lg": logger,
         "m": message, "at": at})
    return row_id


def job(c, *, at: datetime, kind: str = "docs.update", status: str = "succeeded",
        project: str = PROJECT, id: Optional[str] = None) -> str:
    row_id = id or _id("job")
    c.execute(text(
        "INSERT INTO jobs (id, project_id, kind, status, created_at) "
        "VALUES (:i, :p, :k, :s, :at)"),
        {"i": row_id, "p": project, "k": kind, "s": status, "at": at})
    return row_id


def deadletter(c, *, at: datetime, source_kind: str = "job", project: str = PROJECT,
               id: Optional[str] = None) -> str:
    row_id = id or _id("dl")
    c.execute(text(
        "INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, created_at) "
        "VALUES (:i, :p, :k, 'job_x', 'gave up after 3 attempts', :at)"),
        {"i": row_id, "p": project, "k": source_kind, "at": at})
    return row_id


def receipt(c, *, at: datetime, effect_kind: str = "github.pull_request.create",
            status: str = "succeeded", project: str = PROJECT, id: Optional[str] = None) -> str:
    row_id = id or _id("rcpt")
    c.execute(text(
        "INSERT INTO receipts (id, org_id, workspace_id, project_id, dedupe_key, effect_kind, "
        "payload_hash, status, created_at) SELECT :i, w.org_id, p.workspace_id, p.id, :i, :k, "
        "'h', :s, :at FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p"),
        {"i": row_id, "p": project, "k": effect_kind, "s": status, "at": at})
    return row_id


def process_step(c, *, at: datetime, action: str = "handoff", status: str = "succeeded",
                 role: str = "software_engineer", project: str = PROJECT,
                 id: Optional[str] = None) -> str:
    suffix = uuid4().hex[:10]
    c.execute(text(
        "INSERT INTO agent_process_templates (id, project_id, process_key, name, spec, created_by) "
        "VALUES (:t, :p, :k, 'Log test', '{}'::jsonb, 'ceo')"),
        {"t": f"apt_{suffix}", "p": project, "k": f"log_{suffix}"})
    c.execute(text(
        "INSERT INTO agent_process_runs (id, project_id, template_id, title, actor) "
        "VALUES (:r, :p, :t, 'Log test', 'ceo')"),
        {"r": f"apr_{suffix}", "p": project, "t": f"apt_{suffix}"})
    row_id = id or _id("aps")
    c.execute(text(
        "INSERT INTO agent_process_steps (id, run_id, project_id, step_key, role_key, action, "
        "status, created_at) VALUES (:i, :r, :p, 'review', :role, :a, :s, :at)"),
        {"i": row_id, "r": f"apr_{suffix}", "p": project, "role": role, "a": action,
         "s": status, "at": at})
    return row_id


def deployment(c, *, at: datetime, app: str = "checkout", environment: str = "production",
               status: str = "succeeded", failure_reason: Optional[str] = None,
               started_at: Optional[datetime] = None, finished_at: Optional[datetime] = None,
               project: str = PROJECT, target_id: Optional[str] = None,
               id: Optional[str] = None) -> str:
    target = target_id
    if target is None:
        c.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
            "target_kind, target_ref, created_by) VALUES (:t, :p, :a, 'acme/app', :e, 'docker', "
            "'reference host', 'ceo') ON CONFLICT (project_id, app_name, environment) DO NOTHING"),
            {"t": _id("dpt"), "p": project, "a": app, "e": environment})
        target = c.execute(text(
            "SELECT id FROM deployment_targets WHERE project_id = :p AND app_name = :a "
            "AND environment = :e"), {"p": project, "a": app, "e": environment}).scalar_one()
    row_id = id or _id("dpr")
    c.execute(text(
        "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, environment, "
        "repo, target_kind, target_ref, status, created_by, created_at, started_at, finished_at, "
        "failure_reason) VALUES (:i, :p, :t, :a, :e, 'acme/app', 'docker', 'reference host', :s, "
        "'deployer', :at, :sa, :fa, :f)"),
        {"i": row_id, "p": project, "t": target, "a": app, "e": environment, "s": status,
         "at": at, "sa": started_at, "fa": finished_at, "f": failure_reason})
    return row_id


def wipe(c, project: str = PROJECT) -> None:
    """Every log row of the project (and project-less app output), so a test owns its log."""
    for table in ("events", "llm_calls", "agent_turns", "jobs", "deadletters", "receipts",
                  "agent_process_steps", "workflow_step_runs", "deployment_runs",
                  "stack_deployments"):
        c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": project})
    c.execute(text("DELETE FROM app_logs WHERE project_id = :p OR project_id IS NULL"),
              {"p": project})


def bulk(c, *, rows: int, end: datetime, span: timedelta, project: str = PROJECT) -> dict:
    """`rows` log lines spread over `span` ending at `end`, across the high-volume streams in a
    realistic mix (events most, then turns, model calls, app output, steps), in a few
    statements. Returns what it wrote, per stream."""
    seconds = max(int(span.total_seconds()), 1)
    mix = {"events": 0.55, "agent_turns": 0.2, "llm_calls": 0.1, "app_logs": 0.1,
           "jobs": 0.05}
    counts = {name: int(rows * share) for name, share in mix.items()}
    counts["events"] += rows - sum(counts.values())
    params = {"p": project, "end": end, "secs": seconds}
    c.execute(text(f"""
        INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, data,
                            created_at)
        SELECT 'evtb_' || g || '_' || md5(random()::text), :p,
               (ARRAY['workflow.step_ready','code_task.stage_completed','budget.reservation_created',
                      'job.failed','workflow.step_succeeded','agent.handoff'])[1 + g % 6],
               'boss', CASE WHEN g % 5 = 0 THEN 'workflow_run' ELSE 'code_task_run' END,
               CASE WHEN g % 5 = 0 THEN 'wfr_bulk_' || (g % 40) ELSE 'crun_bulk_' || (g % 60) END,
               jsonb_build_object('step_id', 'step_' || (g % 7)),
               :end - make_interval(secs => (g::double precision * :secs / {counts['events']}))
        FROM generate_series(1, {counts['events']}) g"""), params)
    c.execute(text(f"""
        INSERT INTO agent_turns (id, project_id, code_task_run_id, stage, role_key, engine, model,
                                 turn_index, direction, content, level, ok, created_at)
        SELECT 'turnb_' || g || '_' || md5(random()::text), :p, 'crun_bulk_' || (g % 60),
               'implement', (ARRAY['software_engineer','adversarial_reviewer','qa_engineer',
                                   'security_reviewer'])[1 + g % 4], 'claude_code', 'sonnet',
               g % 50, 'completion', 'read the file and ran the tests ' || g,
               (ARRAY['verbose','verbose','verbose','info','warn','error'])[1 + g % 6],
               g % 9 <> 0,
               :end - make_interval(secs => (g::double precision * :secs / {counts['agent_turns']}))
        FROM generate_series(1, {counts['agent_turns']}) g"""), params)
    c.execute(text(f"""
        INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, model,
                               total_tokens, cost_usd_micros, latency_ms, retries, turns, ok,
                               created_at, role)
        SELECT 'llmb_' || g || '_' || md5(random()::text), :p, 'crun_bulk_' || (g % 60),
               'implement', 'claude_code', 'sonnet', 1000 + g % 900, 500 + g % 3000, 800,
               CASE WHEN g % 11 = 0 THEN 1 ELSE 0 END, 4, g % 23 <> 0,
               :end - make_interval(secs => (g::double precision * :secs / {counts['llm_calls']})),
               (ARRAY['software_engineer','adversarial_reviewer','qa_engineer'])[1 + g % 3]
        FROM generate_series(1, {counts['llm_calls']}) g"""), params)
    c.execute(text(f"""
        INSERT INTO app_logs (id, project_id, run_id, service, level, logger, message, created_at)
        SELECT 'alogb_' || g || '_' || md5(random()::text), :p, NULL,
               CASE WHEN g % 2 = 0 THEN 'api' ELSE 'worker' END,
               (ARRAY['info','info','warning','error'])[1 + g % 4], 'boss.worker.loop',
               'tick ' || g,
               :end - make_interval(secs => (g::double precision * :secs / {counts['app_logs']}))
        FROM generate_series(1, {counts['app_logs']}) g"""), params)
    c.execute(text(f"""
        INSERT INTO jobs (id, project_id, kind, status, created_at)
        SELECT 'jobb_' || g || '_' || md5(random()::text), :p, 'docs.update',
               (ARRAY['succeeded','failed','queued'])[1 + g % 3],
               :end - make_interval(secs => (g::double precision * :secs / {counts['jobs']}))
        FROM generate_series(1, {counts['jobs']}) g"""), params)
    c.execute(text("ANALYZE events; ANALYZE agent_turns; ANALYZE llm_calls; ANALYZE app_logs; "
                   "ANALYZE jobs"))
    return counts


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)
