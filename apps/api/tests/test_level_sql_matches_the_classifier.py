"""The log's SQL level and status equal the Python classifier's, for every stream.

The filters, the facet counts and the histogram read `activity_sql`'s CASE expressions; every
row the log returns is still built by the Python classifier. If the two disagreed, `level=error`
would return rows that then read "Info", and a chip's count would not match the rows it shows.
So both run over the same fixtures — every combination of the names and statuses that carry a
classifier marker, upper case, padding and empties — and must agree on every one.
"""

from __future__ import annotations

import itertools
import json

import pytest
from sqlalchemy import text

from boss_engine.services import activity_sql
from boss_engine.services.activity_log import _build

WORDS = ["", "job.failed", "x.error", "deadletter.y", "rejected", "blocked", "step_stuck",
         "stalled", "timeout", "expired", "waiting", "queued", "lease.renew", "succeeded",
         "completed", "certified", "accepted", "cancelled", "closed", "resolved", "done",
         "Handoff Succeeded", "budget.reserved", "ERROR", "Timeout.Closed", " padded ", "ok",
         "info", "deploy.STUCK", "Waiting.Done"]
STATUSES = ["", "failed", "succeeded", "running", "queued", "waiting", "stuck", "timeout",
            "expired", "cancelled", "done", "resolved", "closed", " ok ", "open", "Completed",
            "stuck but resolved", "leased"]

COLUMNS = {
    "event": "event_type text, actor text, entity_type text, entity_id text, data jsonb",
    "process_step": "action text, status text, step_key text, role_key text, handoff_from text",
    "job": "kind text, status text, lease_owner text",
    "deadletter": "source_kind text, status text",
    "receipt": "effect_kind text, status text",
    "llm_call": ("stage text, role text, ok boolean, retries int, model text, error text, "
                 "code_task_run_id text"),
    "agent_turn": ("direction text, tool_name text, level text, ok boolean, role_key text, "
                   "stage text, content text"),
    "app_log": "level text, logger text, service text, run_id text, message text",
    "workflow_step": ("step_id text, step_kind text, status text, attempt int, last_error text, "
                      "workflow_run_id text"),
    "deployment": ("app_name text, environment text, status text, failure_reason text, "
                   "created_by text, line text, label text"),
}


def _fixtures(source: str) -> list[dict]:
    if source == "event":
        return [{"event_type": w, "actor": "ceo", "entity_type": "job", "entity_id": "j",
                 "data": {}} for w in WORDS]
    if source == "process_step":
        return [{"action": a, "status": s, "step_key": k, "role_key": "r", "handoff_from": None}
                for a, s, k in itertools.product(WORDS, STATUSES, ["", "lease_wait"])]
    if source == "job":
        return [{"kind": k, "status": s, "lease_owner": None}
                for k, s in itertools.product(WORDS, STATUSES)]
    if source == "deadletter":
        return [{"source_kind": k, "status": s} for k, s in itertools.product(WORDS, STATUSES)]
    if source == "receipt":
        return [{"effect_kind": k, "status": s} for k, s in itertools.product(WORDS, STATUSES)]
    if source == "llm_call":
        return [{"stage": st, "role": "r", "ok": ok, "retries": re, "model": "m", "error": "",
                 "code_task_run_id": "crun_x"}
                for st, ok, re in itertools.product(WORDS, [True, False], [0, 1, 3])]
    if source == "agent_turn":
        return [{"direction": d, "tool_name": t, "level": lv, "ok": ok, "role_key": "r",
                 "stage": "s", "content": "c"}
                for d, t, lv, ok in itertools.product(
                    ["completion", "tool_call", "fail", ""], ["", "bash", "error_tool"],
                    ["", "verbose", "info", "warn", "error", "debug", " info "], [True, False])]
    if source == "app_log":
        return [{"level": lv, "logger": lg, "service": "worker", "run_id": None, "message": "m"}
                for lv, lg in itertools.product(
                    ["", "info", "warning", "WARNING", "critical", "fatal", "error", "debug",
                     "verbose", " warn ", "Error"],
                    ["", "boss.x.failure", "boss", "a.b.stuck", "timeout", "a.waiting."])]
    if source == "workflow_step":
        return [{"step_id": sid, "step_kind": kind, "status": st, "attempt": at,
                 "last_error": le, "workflow_run_id": "wfr_x"}
                for sid, kind, st, at, le in itertools.product(
                    ["", "build", "fix_error"], ["", "agent", "lease"],
                    ["pending", "ready", "leased", "running", "waiting", "succeeded", "skipped",
                     "failed", "deadlettered", "cancelled"],
                    [0, 1, 2], ["", "boom", "  "])]
    if source == "deployment":
        runs = [{"app_name": app, "environment": env, "status": st, "failure_reason": fr,
                 "created_by": "d", "line": "run", "label": ""}
                for app, env, st, fr in itertools.product(
                    ["checkout", "error-app"], ["", "production", "stuck-env"],
                    ["created", "running", "succeeded", "failed", "rolled_back", "timeout"],
                    [None, "", "health check never passed"])]
        # A deploy's steps are lines of the same stream (activity_sql._DEPLOY_LINES).
        steps = [{"app_name": "checkout", "environment": "staging", "status": st,
                  "failure_reason": fr, "created_by": "d", "line": "step", "label": label}
                 for st, fr, label in itertools.product(
                     ["", "pending", "running", "ok", "OK", "failed", "error", "skipped",
                      "succeeded", " Failed ", "timeout"],
                     [None, "", "boom"],
                     ["Preflight checks", "fix error page", "", "health", "waiting room"])]
        return runs + steps
    raise AssertionError(source)


@pytest.mark.parametrize("source", list(activity_sql.SOURCES))
def test_level_sql_matches_the_classifier(engine, source):
    spec = activity_sql.SOURCES[source]
    rows = _fixtures(source)
    assert rows, source
    with engine.begin() as c:
        sql_rows = c.execute(text(
            f"SELECT ord, {spec.level} AS level, {spec.status} AS status "
            f"FROM jsonb_to_recordset(CAST(:rows AS jsonb)) "
            f"AS t({COLUMNS[source]}, ord int) ORDER BY ord"),
            {"rows": json.dumps([{**row, "ord": i} for i, row in enumerate(rows)])},
        ).mappings().all()
    assert len(sql_rows) == len(rows)
    mismatches = []
    for row, got in zip(rows, sql_rows):
        item = _build({"src": source, "id": "x", "ts": None, "raw": row})
        if (item["level"], item["status"]) != (got["level"], got["status"]):
            mismatches.append((row, (item["level"], item["status"]),
                               (got["level"], got["status"])))
    assert not mismatches, f"{len(mismatches)} of {len(rows)} differ, e.g. {mismatches[:3]}"
