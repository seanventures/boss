"""Seeding for the Observability view tests (obs_view): a project, deployed targets, rows.

Shared by the engine data-truth tests so every one of them seeds the same shapes the views
read — a target is DEPLOYED only with a succeeded deployment_run (deployment_health's rule).
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")


def pg_engine():
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    return eng


def project(c, pid: str | None = None) -> str:
    pid = pid or f"p_obs_{uuid4().hex[:8]}"
    c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_obs', 'obs') ON CONFLICT DO NOTHING"))
    c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_obs', 'org_obs', 'obs') "
                   "ON CONFLICT DO NOTHING"))
    c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_obs', :p, :p) "
                   "ON CONFLICT DO NOTHING"), {"p": pid})
    return pid


def target(c, pid: str, tid: str, app: str, env: str = "prod", *, deployed: bool = True,
           commit: str = "a1b2c3d4e5f6") -> str:
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        " target_ref, status, created_by, domains) "
        "VALUES (:t, :p, :a, 'acme/app', :e, 'ssh', 'deploy@10.0.0.9:22', 'active', 'test', "
        " CAST(:d AS jsonb))"),
        {"t": tid, "p": pid, "a": app, "e": env, "d": f'["https://{app}.example.com"]'})
    if deployed:
        c.execute(text(
            "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
            " environment, repo, target_kind, target_ref, commit_ref, status, created_by, "
            " finished_at) VALUES (:id, :p, :t, :a, :e, 'acme/app', 'ssh', 'x', :c, 'succeeded', "
            " 'test', now() - interval '40 days')"),
            {"id": f"dr_{uuid4().hex[:10]}", "p": pid, "t": tid, "a": app, "e": env, "c": commit})
    return tid


def sample(c, pid: str, tid: str, at, ok: bool, latency: int | None, error: str | None = None):
    c.execute(text(
        "INSERT INTO deployment_health_samples (id, project_id, deployment_target_id, checked_at, "
        " ok, status_code, latency_ms, error) VALUES (:id, :p, :t, :at, :ok, :sc, :ms, :err)"),
        {"id": f"dhs_{uuid4().hex}", "p": pid, "t": tid, "at": at, "ok": ok,
         "sc": 200 if ok else 503, "ms": latency, "err": None if ok else (error or "HTTP 503")})


def llm_call(c, pid: str, at, *, model="m-a", stage="implement", role="software_engineer",
             engine="claude_code", tin=100, tout=20, cost=1000, est=False, latency=500,
             ok=True, included=False, retries=0, error=None, run_id=None):
    c.execute(text(
        "INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, model, role, "
        " prompt_tokens, completion_tokens, total_tokens, cost_usd_micros, price_estimated, "
        " latency_ms, retries, turns, ok, error, included_in_subscription, created_at) "
        "VALUES (:id, :p, :run, :stage, :eng, :model, :role, :tin, :tout, :tt, :cost, :est, :lat, "
        " :ret, 1, :ok, :err, :inc, :at)"),
        {"id": f"llmc_{uuid4().hex}", "p": pid, "run": run_id, "stage": stage, "eng": engine,
         "model": model, "role": role, "tin": tin, "tout": tout, "tt": tin + tout, "cost": cost,
         "est": est, "lat": latency, "ret": retries, "ok": ok, "err": error, "inc": included,
         "at": at})
