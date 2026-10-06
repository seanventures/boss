"""Shared seeds for the run-scorecard / Improve BOSS tests (docs/product/RUN_SCORECARDS.md)."""
from __future__ import annotations

import hashlib
import json
import types
import uuid
from typing import Any, Optional

from sqlalchemy import text

from apps.api.app.config import get_settings

STATE_KEYS = ("improve_boss", "improve_heartbeat", "improve_collector", "improve_identity",
              "improve_schedule", "improve_maint", "score_weights", "improve_retired",
              "improve_endpoint", "improve_floor", "improve_mask")


def project() -> str:
    return get_settings().default_project_id


def tm_settings(**over: Any) -> Any:
    base = {"telemetry": "", "do_not_track": "", "install_method": "",
            "telemetry_url": "http://127.0.0.1:9/v1/telemetry/ingest",
            "boss_edition": "community"}
    base.update(over)
    return types.SimpleNamespace(**base)


#: The unit tables these tests seed, child first. conftest's shared wipe does not list them, and a
#: sweep in a later test would otherwise score another test's builds.
UNIT_TABLES = ("quality_gates", "llm_calls", "agent_turns", "code_task_runs", "workflow_step_runs",
               "workflow_runs", "workflow_versions", "workflow_definitions", "meeting_runs",
               "discovery_runs", "run_scorecards", "telemetry_batches", "telemetry_sends")


def reset_state(engine) -> None:
    with engine.begin() as c:
        c.execute(text("DELETE FROM system_state WHERE key = ANY(:k)"), {"k": list(STATE_KEYS)})
        for t in UNIT_TABLES:
            if t.startswith("workflow_"):
                # Only what these tests made: the seeded catalogue stays.
                if t == "workflow_step_runs":
                    c.execute(text("DELETE FROM workflow_step_runs WHERE id LIKE 'wsr_%'"))
                elif t == "workflow_runs":
                    c.execute(text("DELETE FROM workflow_runs WHERE id LIKE 'wfr_%' AND "
                                   "workflow_definition_id LIKE 'wfd_%'"))
                elif t == "workflow_versions":
                    c.execute(text("DELETE FROM workflow_versions WHERE id LIKE 'wfv_%' AND "
                                   "NOT EXISTS (SELECT 1 FROM workflow_runs r WHERE r.workflow_version_id = workflow_versions.id)"))
                else:
                    c.execute(text("DELETE FROM workflow_definitions WHERE id LIKE 'wfd_%' AND "
                                   "NOT EXISTS (SELECT 1 FROM workflow_runs r WHERE r.workflow_definition_id = workflow_definitions.id)"))
                continue
            c.execute(text(f"DELETE FROM {t}"))
    from boss_engine.services import improve_collector

    improve_collector.IP_LIMIT.reset()
    improve_collector.INSTALL_LIMIT.reset()
    improve_collector.NEW_INSTALL_LIMIT.reset()


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def seed_build(c, *, status: str = "completed", project_id: Optional[str] = None,
               title: str = "Add a thing", spec: Optional[dict] = None,
               state: Optional[dict] = None, finalize: Optional[dict] = None,
               pr: Optional[int] = None, decision_status: Optional[str] = None,
               cost_micros: int = 1_200_000, model: str = "claude-sonnet-5",
               gates_failed: Optional[list] = None, run_id: Optional[str] = None) -> str:
    pid = project_id or project()
    rid = run_id or uid("crun")
    decision_id = None
    if decision_status:
        decision_id = uid("dec")
        c.execute(text("""
            INSERT INTO decisions (id, project_id, question, recommendation, risk_tier, status)
            VALUES (:id, :p, 'Merge?', CAST(:r AS JSONB), 2, :s)"""),
            {"id": decision_id, "p": pid, "s": decision_status,
             "r": json.dumps({"action": "review_pull_request", "code_task_run_id": rid})})
    c.execute(text("""
        INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, usage,
                                    finalize_ctx, pull_request_number, decision_id,
                                    created_at, updated_at)
        VALUES (:id, :p, :st, 'done', :t, CAST(:spec AS JSONB), CAST(:state AS JSONB),
                CAST(:usage AS JSONB), CAST(:fc AS JSONB), :pr, :d, now() - interval '30 minutes', now())"""),
        {"id": rid, "p": pid, "st": status, "t": title,
         "spec": json.dumps(spec or {"role_key": "software_engineer"}),
         "state": json.dumps(state if state is not None else {
             "build_engine": "claude_code", "model": model, "turns": 30, "files_changed": 4,
             "tests": [{"command": "pytest", "exit_code": 0, "ok": True}],
             "repair_max": 2}),
         "usage": json.dumps({"cost_usd_micros": cost_micros, "model": model}),
         "fc": json.dumps(finalize or {}), "pr": pr, "d": decision_id})
    c.execute(text("""
        INSERT INTO quality_gates (id, project_id, code_task_run_id, tests_passed,
                                   adversarial_verdict, status, gates_passed, gates_failed)
        VALUES (:id, :p, :r, true, 'survived', :s, CAST(:gp AS JSONB), CAST(:gf AS JSONB))"""),
        {"id": uid("qg"), "p": pid, "r": rid, "s": "fail" if gates_failed else "pass",
         "gp": json.dumps(["tests_pass", "code_review_passed"]),
         "gf": json.dumps(gates_failed or [])})
    c.execute(text("""
        INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, model,
                               prompt_tokens, completion_tokens, total_tokens, cost_usd_micros, turns)
        VALUES (:id, :p, :r, 'implement', 'claude_code', :m, 12000, 3000, 15000, :c, 30)"""),
        {"id": uid("llm"), "p": pid, "r": rid, "m": model, "c": cost_micros})
    return rid


def seed_workflow_run(c, *, status: str = "running", key: str = "standard_product_delivery",
                      catalogue: bool = True, steps: tuple = ("research", "build")) -> tuple[str, list[str]]:
    pid = project()
    did, vid, rid = uid("wfd"), uid("wfv"), uid("wfr")
    spec = {"steps": [{"id": s} for s in steps]}
    c.execute(text("""
        INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by, namespace,
                                          is_catalogue)
        VALUES (:id, :p, :k, 'T', 'ceo', 'product', :cat)"""),
        # workflow_key is unique per project, so each seeded run gets its own definition.
        {"id": did, "p": pid, "k": f"{key}__{did[-6:]}", "cat": catalogue})
    c.execute(text("""
        INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, status, spec,
                                       content_hash, created_by)
        VALUES (:id, :p, :d, 1, 'published', CAST(:s AS JSONB), :h, 'ceo')"""),
        {"id": vid, "p": pid, "d": did, "s": json.dumps(spec),
         "h": hashlib.sha256((did + json.dumps(spec)).encode()).hexdigest()})
    c.execute(text("""
        INSERT INTO workflow_runs (id, project_id, workflow_definition_id, workflow_version_id,
                                   actor, status, created_at)
        VALUES (:id, :p, :d, :v, 'ceo', :s, now() - interval '1 hour')"""),
        {"id": rid, "p": pid, "d": did, "v": vid, "s": status})
    step_ids = []
    for s in steps:
        sid = uid("wsr")
        c.execute(text("""
            INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, step_kind,
                                            status, started_at)
            VALUES (:id, :p, :r, :s, 'capability', 'running', now() - interval '20 minutes')"""),
            {"id": sid, "p": pid, "r": rid, "s": s})
        step_ids.append(sid)
    return rid, step_ids


def seed_meeting(c, *, status: str = "completed", decision_status: Optional[str] = None) -> tuple[str, str]:
    pid = project()
    mid, rid = uid("mtg"), uid("mrun")
    decision_id = None
    if decision_status:
        decision_id = uid("dec")
        c.execute(text("""
            INSERT INTO decisions (id, project_id, question, recommendation, risk_tier, status)
            VALUES (:id, :p, 'PRD?', CAST(:r AS JSONB), 2, :s)"""),
            {"id": decision_id, "p": pid, "s": decision_status,
             "r": json.dumps({"tool": "publish_document", "meeting_run_id": rid})})
    c.execute(text("""
        INSERT INTO meeting_runs (id, project_id, status, phase, directive, state, usage, meeting_id,
                                  decision_id)
        VALUES (:id, :p, :s, 'done', 'Plan the thing', CAST(:st AS JSONB), CAST(:u AS JSONB), :m, :d)"""),
        {"id": rid, "p": pid, "s": status, "m": mid, "d": decision_id,
         "st": json.dumps({"synthesis": "agreed"}),
         "u": json.dumps({"cost_usd_micros": 400000, "model": "claude-opus-4"})})
    return rid, mid


def seed_research(c, *, status: str = "completed") -> str:
    rid = uid("disc")
    c.execute(text("""
        INSERT INTO discovery_runs (id, project_id, status, phase, title, state, usage)
        VALUES (:id, :p, :s, 'done', 'Market scan', CAST(:st AS JSONB), CAST(:u AS JSONB))"""),
        {"id": rid, "p": project(), "s": status,
         "st": json.dumps({"gate": {"verdict": "pursue", "confidence": 0.8}}),
         "u": json.dumps({"cost_usd_micros": 900000, "model": "claude-sonnet-5"})})
    return rid


def cards(c, unit_id: str) -> list[dict]:
    return [dict(r) for r in c.execute(text(
        "SELECT * FROM run_scorecards WHERE unit_id = :id"), {"id": unit_id}).mappings().all()]
