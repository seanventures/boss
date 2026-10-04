"""Builds written straight into the tables the pipeline writes, for the Factory's API tests."""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from sqlalchemy import text

PROJECT = "boss-dogfood"
ACTOR = "ceo"


def new_run(engine, *, status="running", phase="implement", title="Invoice export",
            state: Optional[dict] = None, fctx: Optional[dict] = None, spec: Optional[dict] = None,
            minutes_ago: int = 1, pr: Optional[int] = None, decision_id: Optional[str] = None,
            run_id: Optional[str] = None, error: Optional[str] = None) -> str:
    run_id = run_id or f"crun_{uuid.uuid4().hex[:12]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, "
            "finalize_ctx, pull_request_url, pull_request_number, decision_id, error, "
            "created_at, updated_at) VALUES (:i, :p, :st, :ph, :t, CAST(:spec AS JSONB), "
            "CAST(:state AS JSONB), CAST(:f AS JSONB), :u, :n, :d, :e, "
            "now() - make_interval(mins => :m + 5), now() - make_interval(mins => :m))"),
            {"i": run_id, "p": PROJECT, "st": status, "ph": phase, "t": title,
             "spec": json.dumps(spec or {}), "state": json.dumps(state or {}),
             "f": json.dumps(fctx or {}), "u": f"https://github.example/acme/app/pull/{pr}"
             if pr else None, "n": pr, "d": decision_id, "e": error, "m": minutes_ago})
    return run_id


def stage(engine, run_id: str, step: str, *, seat: Optional[int] = None) -> dict[str, Any]:
    from boss_engine.services import agent_acts

    with engine.begin() as c:
        return agent_acts.stage_act(c, project_id=PROJECT, code_task_run_id=run_id, stage=step,
                                    seat=seat)


def finish(engine, run_id: str) -> None:
    from boss_engine.services import agent_acts

    with engine.begin() as c:
        agent_acts.close_code_task(c, run_id)


def call(engine, run_id: str, act_id: Optional[str], *, stage_key: str = "implement",
         micros: int = 0, included: bool = False) -> None:
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, model, "
            "cost_usd_micros, included_in_subscription, act_id, total_tokens, turns) "
            "VALUES (:i, :p, :r, :s, 'echo', 'm', :c, :inc, :a, 100, 2)"),
            {"i": f"llm_{uuid.uuid4().hex[:12]}", "p": PROJECT, "r": run_id, "s": stage_key,
             "c": micros, "inc": included, "a": act_id})


def turn(engine, run_id: str, act_id: str, *, tool: str, content: Any,
         direction: str = "tool_call") -> None:
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO agent_turns (id, project_id, code_task_run_id, stage, direction, "
            "tool_name, content, act_id) VALUES (:i, :p, :r, 'implement', :d, :t, :c, :a)"),
            {"i": f"turn_{uuid.uuid4().hex[:12]}", "p": PROJECT, "r": run_id, "d": direction,
             "t": tool, "c": content if isinstance(content, str) else json.dumps(content),
             "a": act_id})


BANNED_KEYS = {"eta", "percent", "progress", "percentage", "estimate", "estimated"}


def keys(value, path=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, f"{path}.{k}"
            yield from keys(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from keys(v, f"{path}[{i}]")
