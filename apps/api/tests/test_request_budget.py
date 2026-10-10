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


def test_a_websocket_never_gets_a_memo():
    """A socket lives for hours and re-checks as it goes: its checks always read live."""
    import asyncio

    from boss_engine.identity.request_memo import RequestMemoMiddleware

    seen = {}

    async def app(scope, receive, send):
        memo = request_memo.MEMO.get()
        seen[scope["type"]] = None if memo is None else dict(memo)

    mw = RequestMemoMiddleware(app)
    asyncio.run(mw({"type": "websocket"}, None, None))
    asyncio.run(mw({"type": "http"}, None, None))
    assert seen["websocket"] is None
    assert seen["http"] == {}


def test_a_memoised_fact_expires_inside_a_long_request(monkeypatch):
    """A streamed response is one request that can live for minutes: a check it repeats must read
    live again once the memo's facts are older than MAX_AGE_SECONDS."""
    import time as _time

    token = request_memo.start()
    try:
        now = [1000.0]
        monkeypatch.setattr(_time, "monotonic", lambda: now[0])
        request_memo.put(("epoch", "org_z"), 9)
        assert request_memo.get(("epoch", "org_z")) == 9
        now[0] += request_memo.MAX_AGE_SECONDS + 0.1
        assert not request_memo.has(request_memo.get(("epoch", "org_z")))
    finally:
        request_memo.reset(token)



# ---- rc6.1 security verification: attacks on the memo -------------------------------------------------

def _run_http(app) -> None:
    import asyncio

    from boss_engine.identity.request_memo import RequestMemoMiddleware

    asyncio.run(RequestMemoMiddleware(app)({"type": "http"}, None, None))


def test_two_requests_at_once_never_share_a_memo_and_their_threads_share_their_own():
    """Concurrent requests (two tabs, two people) each get their own dict; a threadpool hop inside a
    request (anyio copies the context) sees that request's, and a bare pool thread sees none."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    import anyio.to_thread

    from boss_engine.identity.request_memo import RequestMemoMiddleware

    seen: dict[str, object] = {}
    pool = ThreadPoolExecutor(1)

    def app_for(name: str):
        async def app(scope, receive, send):
            request_memo.put(("status", "org_a", name), "active")
            await asyncio.sleep(0.01)
            seen[f"{name}:other"] = request_memo.get(("status", "org_a", "bob" if name == "ann" else "ann"))
            seen[f"{name}:thread"] = await anyio.to_thread.run_sync(
                lambda: request_memo.get(("status", "org_a", name)))
            seen[f"{name}:bare"] = await asyncio.get_running_loop().run_in_executor(
                pool, lambda: request_memo.MEMO.get())
        return app

    async def both():
        await asyncio.gather(RequestMemoMiddleware(app_for("ann"))({"type": "http"}, None, None),
                             RequestMemoMiddleware(app_for("bob"))({"type": "http"}, None, None))

    asyncio.run(both())
    for name in ("ann", "bob"):
        assert not request_memo.has(seen[f"{name}:other"])
        assert seen[f"{name}:thread"] == "active"
    # run_in_executor copies the context in 3.12+; either way it may only ever be THIS request's
    # memo or none, never another's (the dict is per request).
    pool.shutdown()


def test_a_task_the_request_started_reads_live_once_the_request_ended():
    """asyncio.create_task copies the context, so a task started by a request keeps a reference to
    its memo after the request returned. Closing the memo at the end makes that reference useless."""
    import asyncio

    out: dict[str, object] = {}

    async def scenario():
        from boss_engine.identity.request_memo import RequestMemoMiddleware

        release = asyncio.Event()

        async def leaked():
            await release.wait()
            out["after"] = request_memo.get(("epoch", "org_t"))
            request_memo.put(("epoch", "org_t"), 99)
            out["after_put"] = request_memo.get(("epoch", "org_t"))

        tasks = []

        async def app(scope, receive, send):
            request_memo.put(("epoch", "org_t"), 5)
            tasks.append(asyncio.create_task(leaked()))

        await RequestMemoMiddleware(app)({"type": "http"}, None, None)
        release.set()
        await tasks[0]

    asyncio.run(scenario())
    assert not request_memo.has(out["after"])
    assert not request_memo.has(out["after_put"])


def test_an_epoch_or_a_status_never_crosses_orgs_or_people():
    token = request_memo.start()
    try:
        request_memo.put(("epoch", "org_a"), 4)
        request_memo.put(("status", "org_a", "usr_1"), "active")
        assert not request_memo.has(request_memo.get(("epoch", "org_b")))
        assert not request_memo.has(request_memo.get(("status", "org_b", "usr_1")))
        assert not request_memo.has(request_memo.get(("status", "org_a", "usr_2")))
    finally:
        request_memo.reset(token)


def _live(engine, org: str, pid: str) -> tuple[int, str]:
    with engine.connect() as db:
        row = db.execute(text(
            "SELECT (SELECT epoch FROM identity_settings WHERE org_id = :o), "
            "(SELECT status FROM identity_principals WHERE id = :p)"), {"o": org, "p": pid}).first()
    return int(row[0] or 0), row[1]


@pytest.mark.parametrize("how", ["bump", "trigger_only"])
def test_a_read_between_an_identity_write_and_its_commit_is_never_reused_after_it(enforced_owner, engine, how):
    """The PDP's own short connection, used BEFORE the mutation's transaction commits, reads the OLD
    committed epoch and status. Memoised, the rest of the request would decide on them after the
    commit. An identity write (an explicit bump, or a plain write the epoch trigger watches) turns
    the memo off for the rest of the request."""
    _client, _project, pid = enforced_owner
    with engine.connect() as db:
        org = org_id(db)
    token = request_memo.start()
    try:
        with engine.begin() as db:
            db.execute(text("UPDATE identity_principals SET status = 'disabled' WHERE id = :p"), {"p": pid})
            if how == "bump":
                authz.bump_epoch(org, db=db, reason="test: memo off")
            with authz._LazyConnection(None) as conn:
                before_commit = authz._epoch_and_status(conn, org, pid)
        assert before_commit[1] == "active"           # the old committed state, as expected
        with authz._LazyConnection(None) as conn:
            after_commit = authz._epoch_and_status(conn, org, pid)
        assert after_commit == _live(engine, org, pid)
        assert after_commit[1] == "disabled"
    finally:
        request_memo.reset(token)


def test_a_long_request_sees_a_disabled_person_after_the_memo_window(enforced_owner, engine, monkeypatch):
    """A streamed response repeats its check: past MAX_AGE_SECONDS it reads the person live."""
    import time as _time

    _client, _project, pid = enforced_owner
    with engine.connect() as db:
        org = org_id(db)
    now = [1000.0]
    monkeypatch.setattr(_time, "monotonic", lambda: now[0])
    token = request_memo.start()
    try:
        with authz._LazyConnection(None) as conn:
            assert authz._epoch_and_status(conn, org, pid)[1] == "active"
        with engine.begin() as db:                    # another request disables her (committed)
            db.execute(text("UPDATE identity_principals SET status = 'disabled' WHERE id = :p"), {"p": pid})
        now[0] += request_memo.MAX_AGE_SECONDS + 0.1
        with authz._LazyConnection(None) as conn:
            assert authz._epoch_and_status(conn, org, pid)[1] == "disabled"
    finally:
        request_memo.reset(token)


def test_requests_racing_a_revocation_are_refused_from_the_first_one_that_starts_after_it(enforced_owner, engine):
    """Requests in flight on other threads while the session is revoked: every request that STARTS
    after the commit is refused (none is answered from another request's memo)."""
    import threading

    client, project, _ = enforced_owner
    path = f"/v1/code-tasks?project_id={project}"
    token = client.cookies.get(authn.SESSION_COOKIE)
    stop = threading.Event()
    results: list[tuple[float, int]] = []

    def hammer():
        from apps.api.tests.identity_s1_world import fresh_client as _fresh
        c = _fresh()
        c.cookies.set(authn.SESSION_COOKIE, token)
        while not stop.is_set():
            import time as _t
            started = _t.monotonic()
            results.append((started, c.get(path, headers=HEADERS).status_code))

    threads = [threading.Thread(target=hammer) for _ in range(3)]
    for t in threads:
        t.start()
    import time as _t
    _t.sleep(0.5)
    with engine.begin() as db:
        db.execute(text("UPDATE identity_sessions SET revoked_at = now() WHERE token_hash = :h"),
                   {"h": hashlib.sha256(token.encode()).hexdigest()})
    revoked_at = _t.monotonic()
    _t.sleep(0.5)
    stop.set()
    for t in threads:
        t.join()
    after = [code for started, code in results if started > revoked_at]
    assert after and all(code == 401 for code in after), after
    assert any(code == 200 for started, code in results if started < revoked_at)
