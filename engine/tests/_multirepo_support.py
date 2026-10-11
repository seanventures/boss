"""Shared fixtures for the multi-repo tests: a real Postgres (BOSS_TEST_DATABASE_URL, never a
default), an org with two projects, a token connection per project, and GitHub stand-ins that
record what was asked of them."""
from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, text

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL", "")

ACME = [
    {"id": 101, "full_name": "acme/contracts", "language": "Protobuf", "description": "OpenAPI + protobuf schemas",
     "default_branch": "main", "private": True},
    {"id": 102, "full_name": "acme/api", "language": "Go", "description": "Orders and carts service",
     "default_branch": "main", "private": True},
    {"id": 103, "full_name": "acme/web", "language": "TypeScript", "description": "Storefront UI",
     "default_branch": "main", "private": True},
    {"id": 104, "full_name": "acme/infra", "language": "HCL", "description": "Terraform and deploy config",
     "default_branch": "main", "private": True},
    {"id": 105, "full_name": "acme/mobile", "language": "Kotlin", "default_branch": "develop", "private": True},
    {"id": 106, "full_name": "acme/legacy-admin", "language": "Ruby", "default_branch": "master", "private": True,
     "archived": True},
]


def make_engine():
    if not TEST_DB:
        pytest.skip("BOSS_TEST_DATABASE_URL is not set")
    from boss_engine.db import ensure_schema

    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable: {error}")
    ensure_schema(engine)
    return engine


def make_world(engine) -> dict[str, Any]:
    """An org, two projects (each with a token connection), nothing registered yet."""
    tag = uuid.uuid4().hex[:8]
    org, ws = f"org_mr_{tag}", f"ws_mr_{tag}"
    projects = [f"p_mr_{tag}_a", f"p_mr_{tag}_b"]
    conns = {}
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'Acme')"), {"o": org})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'Acme')"), {"w": ws, "o": org})
        for i, pid in enumerate(projects):
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :n, :s)"),
                      {"p": pid, "w": ws, "n": ["Acme Shop", "Payments"][i], "s": f"{pid}"})
            cid = f"ghc_{tag}_{i}"
            c.execute(text("INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
                           "credential_ref, status, created_by) VALUES (:c, :p, 'token', 'personal_access_token', "
                           "'secret:none', 'active', 'test')"), {"c": cid, "p": pid})
            conns[pid] = cid
    return {"org": org, "projects": projects, "connections": conns, "tag": tag}


def lister_for(items):
    def lister(db, settings, org_id, project_id):
        return "app", [dict(i) for i in items]
    return lister


def prober_for(states: dict[str, str] | None = None, calls: list | None = None):
    """working unless `states` says read_only / no_access for a repository."""
    states = states or {}

    def prober(db, settings, org_id, project_id, full_name):
        if calls is not None:
            calls.append(full_name)
        state = states.get(full_name, "working")
        ok = {"ok": True, "state": "working", "at": "now"}
        bad = {"ok": False, "state": "cant_write", "at": "now"}
        if state == "read_only":
            return {"state": "cant_write", "checks": {"read": ok, "write": bad, "pull_requests": dict(bad)}}
        if state == "no_access":
            return {"state": "no_access", "checks": {"read": {"ok": False, "state": "no_access", "at": "now"}}}
        return {"state": "working", "default_branch": "main",
                "checks": {"read": ok, "write": dict(ok), "pull_requests": dict(ok)}}
    return prober


def bind(engine, world, pid: str, full_name: str, *, role: str = "source", checks: str = "working") -> str:
    bid = f"ghb_{uuid.uuid4().hex[:12]}"
    owner, name = full_name.split("/")
    ok = {"ok": True, "state": "working"}
    chk = {"read": ok, "write": ok if checks == "working" else {"ok": False, "state": "cant_write"},
           "pull_requests": ok if checks == "working" else {"ok": False, "state": "cant_open_prs"}}
    import json

    with engine.begin() as c:
        c.execute(text("INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, full_name, "
                       "role, default_branch, checks, checked_at, created_by) VALUES (:i, :p, :c, :o, :n, :f, :r, 'main', "
                       "CAST(:chk AS JSONB), now(), 'test')"),
                  {"i": bid, "p": pid, "c": world["connections"][pid], "o": owner, "n": name, "f": full_name,
                   "r": role, "chk": json.dumps(chk)})
    return bid
