"""BOSSAPP-808: GET /v1/setup lists GitHub accounts (one row per connection) and the repositories
BOSS can work on, each with the account it goes through and the projects that use it. An install
that had ONE connection before shows it as the first row with its repos — nothing to migrate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import setup_accounts, setup_view

from ._multirepo_support import make_engine, make_world


def _bind(conn, pid, cid, full, role="source", checks=None):
    conn.execute(text(
        "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, full_name, role, "
        "status, checks, checked_at, created_by) VALUES (:i, :p, :c, :o, :n, :f, :r, 'active', "
        "CAST(:k AS jsonb), now(), 'test')"),
        {"i": f"ghb_{pid}_{full.replace('/', '_')}", "p": pid, "c": cid, "o": full.split("/")[0],
         "n": full.split("/")[1], "f": full, "r": role,
         "k": __import__("json").dumps(checks or {"read": {"ok": True, "state": "ok"},
                                                  "write": {"ok": True, "state": "ok"},
                                                  "pull_requests": {"ok": True, "state": "ok"}})})


def _view(engine, pid):
    with engine.begin() as conn:
        now = datetime.now(timezone.utc)
        repository = setup_view._repository(conn, get_settings(), pid, now)
        return repository, setup_accounts.build(conn, get_settings(), pid, repository, now)


def test_the_single_existing_connection_is_the_first_row_with_its_repos():
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    cid = world["connections"][pid]
    with engine.begin() as conn:
        conn.execute(text("UPDATE github_connections SET account_login = 'paramg' WHERE id = :c"), {"c": cid})
        _bind(conn, pid, cid, "paramg/hello", role="primary")
    repository, out = _view(engine, pid)
    first = out["accounts"][0]
    assert first["connection_id"] == cid and first["name"] == "paramg"
    assert first["how"] == "Personal · access token" and first["repo_count"] == 1
    assert first["pill"]["tone"] in ("green", "grey")
    [repo] = [r for r in out["repositories"] if r["in_project"]]
    assert repo["full_name"] == "paramg/hello" and repo["via"] == "paramg"
    assert repo["projects"] == [{"project_id": pid, "name": "Acme Shop"}]
    # the old field is untouched
    assert repository["connection"]["id"] == cid


def test_a_second_org_account_is_its_own_row_and_its_repos_go_through_it():
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    first = world["connections"][pid]
    second = f"ghc_{world['tag']}_org"
    soon = datetime.now(timezone.utc) + timedelta(days=9, hours=2)
    with engine.begin() as conn:
        conn.execute(text("UPDATE github_connections SET account_login = 'paramg' WHERE id = :c"), {"c": first})
        conn.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, credential_ref, "
            "status, account_login, expires_at, created_by, created_at) VALUES (:c, :p, 'seanventures', "
            "'personal_access_token', 'secret:none', 'active', 'sean-bot', :e, 'test', now() + interval '1 second')"),
            {"c": second, "p": pid, "e": soon})
        _bind(conn, pid, first, "paramg/hello", role="primary")
        _bind(conn, pid, second, "seanventures/boss")
    _, out = _view(engine, pid)
    rows = {a["connection_id"]: a for a in out["accounts"] if a["connection_id"]}
    assert list(rows) == [first, second]
    org = rows[second]
    assert org["how"] == "Organisation · access token" and org["name"] == "sean-bot (org)"
    assert org["pill"]["tone"] == "amber" and org["pill"]["label"] == "Token expires in 9 days"
    via = {r["full_name"]: r["account_id"] for r in out["repositories"]}
    assert via == {"paramg/hello": first, "seanventures/boss": second}


def test_a_failed_check_makes_its_account_broken_with_a_fix():
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    cid = world["connections"][pid]
    with engine.begin() as conn:
        _bind(conn, pid, cid, "acme/api", role="primary",
              checks={"read": {"ok": True, "state": "ok"}, "write": {"ok": False, "state": "cant_write"},
                      "pull_requests": {"ok": None, "state": "not_run"}})
    _, out = _view(engine, pid)
    account = next(a for a in out["accounts"] if a["connection_id"] == cid)
    repo = next(r for r in out["repositories"] if r["full_name"] == "acme/api")
    assert repo["checks"]["write"]["ok"] is False
    assert repo["pill"]["tone"] == "red"
    assert account["pill"]["label"] == "Broken" and account["pill"]["action"] == "replace"


def test_the_whole_view_carries_the_new_fields_and_keeps_the_old_ones():
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    with engine.begin() as conn:
        _bind(conn, pid, world["connections"][pid], "acme/web", role="primary")
        view = setup_view.build(conn, get_settings(), pid)
    assert {"github_accounts", "repositories", "repository", "models", "jobs"} <= set(view)
    assert view["repository"]["connections"] and view["github_accounts"]


def test_an_expired_login_reads_expired_in_routing_and_who_does_what_never_plenty():
    expired = {"state": "login_expired", "tone": "red", "label": "Login expired",
               "tip": "The provider refused this login. Sign in again.", "action": "sign_in"}
    models = [{"provider": "codex", "kind": "subscription", "pill": expired},
              {"provider": "claude_code", "kind": "subscription",
               "pill": {"state": "working", "tone": "green", "label": "Working", "tip": ""}}]
    jobs = [{"job": "review", "resolved": {"engine": "codex", "model": None, "label": "Codex", "why": "auto"}},
            {"job": "build", "resolved": {"engine": "claude_code", "model": None, "label": "Claude Code", "why": "auto"}}]
    routing = {"headroom": [{"engine": "codex", "state": "plenty", "label": "Plenty", "left": 0.9, "describe": "x"},
                            {"engine": "claude_code", "state": "plenty", "label": "Plenty", "left": 0.8, "describe": "y"}],
               "sources": [{"engine": "codex", "working": True}]}
    setup_view._say_signed_out(models, jobs, routing)
    codex = routing["headroom"][0]
    assert codex["state"] == "exhausted" and codex["label"] == "Login expired" and codex["left"] == 0
    assert routing["headroom"][1]["state"] == "plenty"
    assert routing["sources"][0]["working"] is False
    assert jobs[0]["resolved"]["pill"]["label"] == "Login expired" and "pill" not in jobs[1]["resolved"]
