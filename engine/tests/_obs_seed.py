"""Seeding for the Observability view tests (obs_view): a project, deployed targets, rows.

Shared by the engine data-truth tests so every one of them seeds the same shapes the views
read — a target is DEPLOYED only with a succeeded deployment_run (deployment_health's rule).
"""
from __future__ import annotations

import os
import re
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")

# Ids of deployment_targets present the first time this process opened the shared test
# database. None until then, so a session that never seeded observability leaves the
# database alone. Rows inserted afterwards are removed at session end (BOSSAPP-973).
_targets_at_open: set[str] | None = None
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def pg_engine():
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    _remember_targets(eng)
    return eng


def _remember_targets(eng) -> None:
    global _targets_at_open
    if _targets_at_open is not None:
        return
    with eng.connect() as c:
        _targets_at_open = {str(i) for i in c.execute(text(
            "SELECT id FROM deployment_targets")).scalars()}


def _sql_name(name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise RuntimeError(f"refusing to interpolate {name!r}")
    return name


def _fk_children(c, table: str) -> list[tuple[str, list[str]]]:
    rows = c.execute(text(
        "SELECT child.relname, ARRAY(SELECT a.attname::text "
        " FROM unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord) "
        " JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = u.attnum "
        " ORDER BY u.ord) "
        "FROM pg_constraint con "
        "JOIN pg_class child ON child.oid = con.conrelid "
        "JOIN pg_namespace ns ON ns.oid = child.relnamespace "
        "WHERE con.contype = 'f' AND con.confrelid = to_regclass(:t) "
        " AND ns.nspname = current_schema() AND con.conrelid <> con.confrelid"),
        {"t": table}).all()
    return [(str(child), [str(col) for col in cols]) for child, cols in rows]


def _primary_key(c, table: str) -> str | None:
    cols = [str(name) for name in c.execute(text(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey) "
        "WHERE i.indrelid = to_regclass(:t) AND i.indisprimary ORDER BY a.attnum"),
        {"t": table}).scalars()]
    return cols[0] if len(cols) == 1 else None


def _purge_referencing(c, table: str, ids: list[str], seen: set[tuple[str, tuple[str, ...]]]) -> None:
    """Delete rows that reference `ids` before the rows themselves, including through a
    restrict foreign key onto a child. Cascade would not fire until the parent goes, and a
    grandchild with no ON DELETE CASCADE blocks that."""
    key = (table, tuple(sorted(ids)))
    if not ids or key in seen:
        return
    seen.add(key)
    parent = _sql_name(table)
    pk = _primary_key(c, parent)
    for child, cols in _fk_children(c, parent):
        child_name = _sql_name(child)
        columns = [_sql_name(col) for col in cols]
        where = " OR ".join(f"{col} = ANY(:ids)" for col in columns)
        if pk is not None:
            child_pk = _primary_key(c, child_name)
            if child_pk is not None:
                child_ids = [str(i) for i in c.execute(text(
                    f"SELECT {_sql_name(child_pk)} FROM {child_name} WHERE {where}"),
                    {"ids": ids}).scalars() if i is not None]
                if child_ids:
                    _purge_referencing(c, child_name, child_ids, seen)
        for col in columns:
            c.execute(text(f"DELETE FROM {child_name} WHERE {col} = ANY(:ids)"), {"ids": ids})


def drop_targets_added_after_snapshot() -> None:
    """Remove deployment_targets this process inserted on the shared test database."""
    if _targets_at_open is None or not PG_URL.startswith("postgres"):
        return
    eng = create_engine(PG_URL, future=True)
    try:
        with eng.begin() as c:
            if _targets_at_open:
                added = [str(i) for i in c.execute(text(
                    "SELECT id FROM deployment_targets WHERE NOT (id = ANY(:ids))"),
                    {"ids": list(_targets_at_open)}).scalars()]
            else:
                added = [str(i) for i in c.execute(text(
                    "SELECT id FROM deployment_targets")).scalars()]
            if not added:
                return
            _purge_referencing(c, "deployment_targets", added, set())
            c.execute(text("DELETE FROM deployment_targets WHERE id = ANY(:ids)"), {"ids": added})
    finally:
        eng.dispose()


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
