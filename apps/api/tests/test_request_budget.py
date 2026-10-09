"""THE API'S PER-REQUEST BUDGET, AND WHAT THE IDENTITY MEMO MAY NEVER DO (console latency, 2026-10-09).

boss-1's Factory took 3.6-4.6 s after rc6. One render asked the API ~40 questions at once and each
paid the identity guard on two pooled connections: the session, the principal, then the PDP read
the org's epoch and the principal's status AGAIN, then the project's org — four statements before
the handler's first. Measured on the seeded stack, identity was ~40% of the API's time per render.

Now authentication reads the session, its principal and the org's epoch in ONE statement, and the
PDP reuses them for the rest of THAT request (boss_engine/identity/request_memo.py). These tests
pin the budget so it cannot creep back, and pin what must not change:

  * a signed-in person's console GET costs at most TWO identity statements (the session, the
    project's org) and stays under its endpoint's total;
  * a revoked session is refused at its very next request (nothing outlives a request);
  * a role taken away is refused at the very next request (the epoch moves);
  * an identity change INSIDE a request clears the memo (bump), so a later check in the same
    request sees it.
"""
from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,
    fresh_client,
    make_user,
    org_id,
    promote_fresh_mfa,
    sign_in,
    world,
)
from boss_engine.config import get_settings
from boss_engine.identity import authn, authz, request_memo

REAL = {
    (authz, "check"): authz.check,
    (authz, "effective_permissions"): authz.effective_permissions,
    (authz, "bump_epoch"): authz.bump_epoch,
}

#: The most statements identity may spend on one signed-in GET (session ⋈ principal ⋈ epoch, and
#: the project's org for a project-scoped permission).
AUTH_STATEMENTS = 2

#: Each console GET's whole budget (identity included) on the test database. Measured after the
#: fix plus headroom; a regression that adds a statement per row or per check fails here.
ENDPOINT_BUDGETS = {
    "/v1/code-tasks?project_id={p}": 8,
    "/v1/factory/apps?project_id={p}": 20,
    "/v1/factory/flow?project_id={p}": 60,
    "/v1/identity/me/permissions": 4,
    "/v1/identity/me/projects": 8,
    "/v1/orgs": 8,
    "/v1/packs?project_id={p}": 6,
}


@pytest.fixture
def enforced_owner(world, engine, monkeypatch):
    for (module, name), fn in REAL.items():
        monkeypatch.setattr(module, name, fn)
    monkeypatch.setattr(get_settings(), "authz_mode", "enforce")
    monkeypatch.setattr(get_settings(), "authz_router_modes", "")
    authz.HELD_CACHE.clear()
    with engine.begin() as db:
        project = db.execute(text(
            "SELECT p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
            "WHERE w.org_id = :o ORDER BY p.created_at LIMIT 1"), {"o": org_id(db)}).scalar()
    assert project, "the test database has no project in the default org"
    pid = make_user(engine, "budget-owner@example.test", role="owner")
    client = fresh_client()
    assert sign_in(client, "budget-owner@example.test").status_code == 200
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    yield client, project, pid
    authz.HELD_CACHE.clear()


def _metrics_of(client, path: str):
    from apps.api.app import request_metrics

    response = client.get(path, headers=HEADERS)
    m = request_metrics.last_metrics()
    assert m is not None
    return response, m


def test_a_signed_in_get_spends_at_most_two_identity_statements_and_its_budget(enforced_owner):
    client, project, _ = enforced_owner
    for template, budget in ENDPOINT_BUDGETS.items():
        path = template.format(p=project)
        _metrics_of(client, path)                     # warm the epoch-keyed permission cache
        response, m = _metrics_of(client, path)
        assert response.status_code == 200, (path, response.status_code, response.text[:200])
        assert m.auth_queries >= 1 and m.queries > m.auth_queries, f"{path}: nothing was counted"
        assert m.auth_queries <= AUTH_STATEMENTS, (
            f"{path}: identity spent {m.auth_queries} statements (budget {AUTH_STATEMENTS})")
        assert m.queries <= budget, f"{path}: {m.queries} statements (budget {budget})"
        assert m.writes == 0 or path.startswith("/v1/factory"), f"{path} wrote on a read"


def test_a_revoked_session_is_refused_at_its_next_request(enforced_owner, engine):
    client, project, _ = enforced_owner
    path = f"/v1/code-tasks?project_id={project}"
    assert client.get(path, headers=HEADERS).status_code == 200
    token = client.cookies.get(authn.SESSION_COOKIE)
    with engine.begin() as db:
        db.execute(text("UPDATE identity_sessions SET revoked_at = now() WHERE token_hash = :h"),
                   {"h": hashlib.sha256(token.encode()).hexdigest()})
    assert client.get(path, headers=HEADERS).status_code == 401


def test_a_disabled_person_is_refused_at_their_next_request(enforced_owner, engine):
    client, project, pid = enforced_owner
    path = f"/v1/code-tasks?project_id={project}"
    assert client.get(path, headers=HEADERS).status_code == 200
    with engine.begin() as db:
        db.execute(text("UPDATE identity_principals SET status = 'disabled' WHERE id = :p"), {"p": pid})
    assert client.get(path, headers=HEADERS).status_code == 401


def test_a_role_taken_away_is_refused_at_the_next_request(enforced_owner, engine):
    client, project, pid = enforced_owner
    path = f"/v1/code-tasks?project_id={project}"
    assert client.get(path, headers=HEADERS).status_code == 200
    with engine.begin() as db:
        db.execute(text("DELETE FROM identity_role_bindings WHERE subject_id = :p"), {"p": pid})
        authz.bump_epoch(org_id(db), db=db, reason="test: role removed")
    assert client.get(path, headers=HEADERS).status_code in (403, 404)


def test_an_identity_change_inside_a_request_clears_the_memo(engine):
    token = request_memo.start()
    try:
        request_memo.put(("epoch", "org_x"), 7)
        request_memo.put(("status", "org_x", "usr_x"), "active")
        with engine.begin() as db:
            authz.bump_epoch(org_id(db), db=db, reason="test: memo cleared")
        assert not request_memo.has(request_memo.get(("epoch", "org_x")))
        assert not request_memo.has(request_memo.get(("status", "org_x", "usr_x")))
    finally:
        request_memo.reset(token)


def test_outside_a_request_nothing_is_memoised():
    request_memo.put(("epoch", "org_y"), 3)
    assert not request_memo.has(request_memo.get(("epoch", "org_y")))
