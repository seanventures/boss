"""Seeding for the Observability view API tests: projects, deployed targets, samples, calls."""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.app.db import begin

PROJECT_ID = "boss-dogfood"


def second_project(engine, pid: str | None = None) -> str:
    pid = pid or f"p_obs_{uuid4().hex[:8]}"
    with begin(engine) as c:
        ws = c.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                       {"p": PROJECT_ID}).scalar()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:p, :w, :p, :p) ON CONFLICT DO NOTHING"), {"p": pid, "w": ws})
    return pid


def deployed_target(c, pid: str, tid: str, app: str, env: str = "prod") -> str:
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        " target_ref, status, created_by, domains) VALUES (:t, :p, :a, 'acme/app', :e, 'ssh', "
        " 'deploy@10.0.0.9:22', 'active', 'test', CAST(:d AS jsonb))"),
        {"t": tid, "p": pid, "a": app, "e": env, "d": json.dumps([f"https://{app}.example.com"])})
    c.execute(text(
        "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, environment, "
        " repo, target_kind, target_ref, commit_ref, status, created_by, finished_at) "
        "VALUES (:id, :p, :t, :a, :e, 'acme/app', 'ssh', 'x', 'a1b2c3d4e5', 'succeeded', 'test', "
        " now() - interval '40 days')"),
        {"id": f"dr_{uuid4().hex[:10]}", "p": pid, "t": tid, "a": app, "e": env})
    return tid


def sample(c, pid: str, tid: str, ok: bool = True, latency: int = 120, ago_s: int = 60):
    c.execute(text(
        "INSERT INTO deployment_health_samples (id, project_id, deployment_target_id, checked_at, "
        " ok, status_code, latency_ms) VALUES (:id, :p, :t, now() - make_interval(secs => :ago), "
        " :ok, 200, :ms)"),
        {"id": f"dhs_{uuid4().hex}", "p": pid, "t": tid, "ago": ago_s, "ok": ok, "ms": latency})


def llm_call(c, pid: str, *, model: str = "m-a", ok: bool = True, ago_s: int = 120,
             cost: int = 1000):
    c.execute(text(
        "INSERT INTO llm_calls (id, project_id, stage, engine, model, role, prompt_tokens, "
        " completion_tokens, total_tokens, cost_usd_micros, latency_ms, ok, created_at) "
        "VALUES (:id, :p, 'implement', 'claude_code', :m, 'software_engineer', 100, 20, 120, :c, "
        " 900, :ok, now() - make_interval(secs => :ago))"),
        {"id": f"llmc_{uuid4().hex}", "p": pid, "m": model, "c": cost, "ok": ok, "ago": ago_s})
