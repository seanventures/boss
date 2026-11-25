"""Setup's write check is a LEASED JOB, never work inside the Connect request (review of #258).

Connect records "checking" on the login and queues `engine.check`; the worker leases it and the
API runs it — fenced by the lease — under a build's sandbox identity (engine_policy.run_check).
DB-backed: a disposable database (BOSS_TEST_DATABASE_URL / DATABASE_URL named like a test one).
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.config import get_settings
from boss_engine.services import engine_policy as ep
from boss_engine.services.jobs import JobLeaseService


@pytest.fixture(scope="module")
def pg_engine():
    url = os.environ.get("BOSS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(m in name for m in ("test", "ci", "scratch", "tmp", "boss_run")):
        pytest.skip("a disposable database is required")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"postgres is not reachable: {exc.__class__.__name__}")
    from boss_engine.db import ensure_schema

    ensure_schema(engine)
    return engine


@pytest.fixture
def login(pg_engine):
    org, ws, proj = (f"{p}_{uuid.uuid4().hex[:8]}" for p in ("org", "ws", "proj"))
    source = f"openai_codex_chatgpt_{uuid.uuid4().hex[:6]}"
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'T')"), {"o": org})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'T')"),
                  {"w": ws, "o": org})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:p, :w, 'T', :p)"), {"p": proj, "w": ws})
        c.execute(text(
            "INSERT INTO entitlement_sources (id, project_id, provider, source_id, source_kind,"
            " entitlement_kind, owner_type, owner_id, auth_method, connection_status)"
            " VALUES (:id, :p, 'openai', :sid, 'official_cli', 'subscription', 'project', :p,"
            " 'official_cli_oauth', 'connected')"),
            {"id": f"ent_{uuid.uuid4().hex[:8]}", "p": proj, "sid": source})
    ep.forget()
    yield proj, source
    with pg_engine.begin() as c:
        c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": proj})


def _policy(pg_engine, source):
    with pg_engine.begin() as c:
        value = c.execute(text("SELECT connection_metadata -> 'engine_policy' FROM "
                               "entitlement_sources WHERE source_id = :s"), {"s": source}).scalar()
    return json.loads(value) if isinstance(value, str) else value


def _claim(pg_engine, job_id):
    for _ in range(50):
        with pg_engine.begin() as c:
            got = JobLeaseService(get_settings()).claim_job(c, "worker-test", [ep.JOB_KIND], 300)
        if got is None or got["id"] == job_id:
            return got
    return None


def test_connect_records_checking_and_the_worker_runs_the_leased_job(pg_engine, login,
                                                                     monkeypatch):
    project, source = login
    with pg_engine.begin() as c:
        started = ep.start_check(c, project, "codex", source, "ceo")
    assert started["state"] == "checking" and started["job_id"]
    assert _policy(pg_engine, source)["state"] == "checking"
    assert ep.summary(_policy(pg_engine, source))["label"] == "Checking…"

    job = _claim(pg_engine, started["job_id"])
    assert job is not None and job["kind"] == ep.JOB_KIND
    ran = []
    monkeypatch.setattr(ep, "run_check", lambda engine, settings, **kw: ran.append(kw) or {
        "status": "done", "state": "ok"})
    refused = ep.execute_job(pg_engine, get_settings(), job_id=job["id"], worker_id="worker-test",
                             lease_token="lease_not_the_one")
    assert refused["status"] == "failed" and not ran          # fenced by the lease
    out = ep.execute_job(pg_engine, get_settings(), job_id=job["id"], worker_id="worker-test",
                         lease_token=job["lease_token"])
    assert out["state"] == "ok"
    assert ran == [{"job_id": job["id"], "project_id": project, "provider": "codex",
                    "source_id": source, "actor": "ceo"}]


def test_the_checks_result_is_recorded_per_login_and_read_back(pg_engine, login):
    project, source = login
    with pg_engine.begin() as c:
        ep.record(c, source, {"engine": "codex", "state": "ok", "managed": True,
                              "writer_mode": "workspace-write", "sandbox_proven": True,
                              "network": True, "proved_in": {"job": True, "isolated": True}})
    held = ep.recorded(source)
    assert held["state"] == "ok" and ep.uses_workspace_write(held)
