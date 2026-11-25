"""BOSSAPP-886: several subscription accounts per provider, used as one pool.

Members' logins live in the secret store under their own names in the subscription namespace ->
a run gets the member with the most headroom and keeps it for its task -> a member at its limit is
skipped; the pool is exhausted only when every member is -> the run's rate windows land on its
member -> Claude runs on the member's token, Codex on the member's own CODEX_HOME, and a rotated
codex login is synced back to the MEMBER's store key through the one sync seam -> no pool, no
change. Since BOSSAPP-1480 the session holds a run token, not the login: the model proxy attaches
the ASSIGNED member's login upstream (checked against a fake upstream), and a codex member's
login is refreshed by BOSS in the member's home, since its session cannot refresh.
"""
from __future__ import annotations

import contextlib
import dataclasses
import http.client
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import subscription_pools as sp
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)
TOKEN_A = "sk-ant-oat01-" + "a" * 48         # shaped like logins; not real ones
TOKEN_B = "sk-ant-oat01-" + "b" * 48
INSTALL_TOKEN = "sk-ant-oat01-" + "i" * 48
DEFAULT_ACCESS = "eyJdefault." + "d" * 40 + ".sig"
SEAT_ACCESS = "eyJseat." + "s" * 40 + ".sig"
SEAT_ACCESS_2 = "eyJseat2." + "t" * 40 + ".sig"


class MemoryStore:
    kind = "memory"

    def __init__(self) -> None:
        self.held: dict[str, str] = {}

    def get(self, key):
        from boss_engine.services.secret_backends import SecretNotFound

        if key not in self.held:
            raise SecretNotFound(key)
        return self.held[key]

    def put(self, key, value):
        self.held[key] = value

    def delete(self, key):
        self.held.pop(key, None)


@pytest.fixture()
def store(monkeypatch):
    """ONE store for every reader and writer: the pool's writes, stored_credential's reads and
    sync_rotated_credential's write-back all go through secret_backends."""
    held = MemoryStore()
    from boss_engine.services import secret_backends

    monkeypatch.setattr(secret_backends, "backend_for", lambda kind, settings=None: held)
    monkeypatch.setattr(secret_backends, "pick_default_backend", lambda settings: ("memory", []))
    monkeypatch.setattr(secret_backends, "default_secret_backend", lambda settings: "memory")
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "CODEX_AUTH_JSON"):
        monkeypatch.delenv(name, raising=False)
    from boss_engine.services.model_router import accounts

    accounts.reset_caches()
    yield held
    accounts.reset_caches()
    clear_llm_context()


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM subscription_pool_members LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'w4o1 886', :s)"), {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        c.execute(text("DELETE FROM subscription_accounts WHERE account_key IN (SELECT "
                       "m.account_key FROM subscription_pool_members m JOIN subscription_pools s "
                       "ON s.id = m.pool_id WHERE s.project_id = :p)"), {"p": pid})
        c.execute(text("DELETE FROM subscription_pools WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


def _windows(c, member, *, used=None, rejected_until=None):
    body = {}
    if used is not None:
        body["seven_day"] = {"used": used, "status": "allowed",
                             "resets_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()}
    if rejected_until is not None:
        body["seven_day"] = {"used": 1.0, "status": "rejected", "resets_at": rejected_until.isoformat()}
    c.execute(text("INSERT INTO subscription_accounts (account_key, engine, windows) VALUES "
                   "(:k, 'claude_code', CAST(:w AS JSONB)) ON CONFLICT (account_key) DO UPDATE "
                   "SET windows = EXCLUDED.windows"), {"k": member["account_key"],
                                                       "w": json.dumps(body)})
    from boss_engine.services.model_router import accounts

    accounts.reset_caches()


def _pool(c, pid, engine="claude_code", logins=(TOKEN_A, TOKEN_B)):
    pool = sp.create_pool(c, pid, engine=engine, name="seats", actor="operator")
    members = [sp.add_member(c, Settings(), pid, pool["id"], label=f"seat {i}", credential=v,
                             actor="operator") for i, v in enumerate(logins)]
    return pool, members


def test_logins_go_to_the_subscription_store_never_a_table(eng, project, store):
    with eng.begin() as c:
        pool, members = _pool(c, project)
        listed = sp.describe(c, project)
        rows = c.execute(text("SELECT * FROM subscription_pool_members WHERE pool_id = :p"),
                         {"p": pool["id"]}).mappings().all()
    keys = sorted(store.held)
    assert all(k.startswith("BOSS_SUBSCRIPTION_CLAUDE_CODE_OAUTH_TOKEN_POOL_") for k in keys)
    assert sorted(store.held.values()) == sorted([TOKEN_A, TOKEN_B])
    assert TOKEN_A not in json.dumps(listed, default=str) and "secret_name" not in json.dumps(listed)
    assert all(TOKEN_A not in json.dumps(dict(r), default=str) for r in rows)
    assert [m["account_key"] for m in members] == [f"claude_code:pool:{m['id']}" for m in members]


def test_most_headroom_wins_the_pin_holds_and_exhausted_is_every_member(eng, project, store):
    with eng.begin() as c:
        _pool_, (a, b) = _pool(c, project)
        _windows(c, a, used=0.9)                         # seat 0 nearly spent
        first = sp.assign(c, project, "claude_code", "wfr_1")
        assert first.member_id == b["id"] and not first.pinned
        _windows(c, b, used=0.95)                         # now seat 1 has less left...
        again = sp.assign(c, project, "claude_code", "wfr_1")
        assert again.member_id == b["id"] and again.pinned, "the task stays on its account"
        other = sp.assign(c, project, "claude_code", "wfr_2")
        assert other.member_id == a["id"], "a new task gets the most headroom"
        soon = datetime.now(timezone.utc) + timedelta(hours=2)
        _windows(c, b, rejected_until=soon)               # the pinned seat hits its limit
        moved = sp.assign(c, project, "claude_code", "wfr_1")
        assert moved.member_id == a["id"], "a pin never holds a run on a spent account"
        _windows(c, a, rejected_until=soon + timedelta(hours=1))
        with pytest.raises(sp.PoolExhausted) as full:
            sp.assign(c, project, "claude_code", "wfr_3")
        assert full.value.retry_at is not None and abs(
            (full.value.retry_at - soon).total_seconds()) < 2


def test_a_run_spends_on_its_member(eng, project, store):
    from boss_engine.services.model_router import accounts

    with eng.begin() as c:
        _pool(c, project)
    set_llm_context(project_id=project, run_id="crun_9", db_engine=eng)
    chosen = sp.assignment_for("claude_code", Settings())
    assert chosen is not None
    assert accounts.account_key(None, Settings(), project, "claude_code") == chosen.account_key
    assert accounts.account_key(None, Settings(), project, "codex") != chosen.account_key


class FakeUpstream:
    """The provider, stubbed: records what the model proxy sent upstream (no provider called)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    @contextlib.contextmanager
    def __call__(self, method, url, headers, body):
        self.calls.append({"url": url, "headers": {k.lower(): v for k, v in headers.items()}})
        yield 200, {"content-type": "application/json"}, iter([b'{"ok": true}'])


@pytest.fixture()
def upstream():
    from boss_engine.services import model_proxy as mp

    stub = FakeUpstream()
    proxy = mp.ModelProxy(upstream=stub, poll_interval=0.02)
    mp.set_proxy(proxy)
    yield stub, proxy
    mp.set_proxy(None)
    proxy.stop()


def _through_proxy(proxy, path: str, token: str, headers: Optional[dict] = None) -> int:
    conn = http.client.HTTPConnection(proxy.host, proxy.port, timeout=10)
    try:
        conn.request("POST", path, body=b"{}",
                     headers={"authorization": f"Bearer {token}", **(headers or {})})
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def test_claude_runs_on_the_members_token(eng, project, store, upstream):
    """The run is assigned seat 1 (most headroom). Its session holds only a run token
    (subscription_shield, BOSSAPP-1480) — and that token is bound to SEAT 1's login: what the
    proxy attaches upstream is seat 1's token, never seat 0's and never the install's own login,
    so the seat that was chosen is the account that is billed and whose limits move."""
    from boss_engine.services import code_agent as ca
    from boss_engine.services import model_proxy as mp

    stub, proxy = upstream
    with eng.begin() as c:
        _pool_, (a, b) = _pool(c, project)
        _windows(c, a, used=0.9)
    set_llm_context(project_id=project, run_id="crun_c", db_engine=eng)
    env = ca.ClaudeCodeAgent(Settings().model_copy(
        update={"claude_code_oauth_token": INSTALL_TOKEN, "anthropic_api_key": None}))._child_env()
    run_token = env["CLAUDE_CODE_OAUTH_TOKEN"]
    assert run_token.startswith(mp.TOKEN_PREFIX)
    assert env["ANTHROPIC_BASE_URL"].endswith("/claude_subscription")
    assert "ANTHROPIC_API_KEY" not in env
    held = json.dumps(env)
    assert not any(t in held for t in (TOKEN_A, TOKEN_B, INSTALL_TOKEN)), \
        "no login of any seat (or the install's) is in the session"

    assert _through_proxy(proxy, "/claude_subscription/v1/messages", run_token) == 200
    (call,) = stub.calls
    assert call["headers"]["authorization"] == f"Bearer {TOKEN_B}", \
        "the proxy attaches the ASSIGNED seat's login upstream"
    assert mp.held_for(run_token, proxy) == TOKEN_B


def _codex_login(access: str, refresh: str, *, expires_in: float, last_refresh: str) -> str:
    return json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                       "last_refresh": last_refresh,
                       "tokens": {"id_token": "x.e30.y", "access_token": access,
                                  "refresh_token": refresh, "account_id": "acct-seat",
                                  "expires_at": int(time.time() + expires_in)}})


def test_codex_seat_login_is_refreshed_by_boss_and_attached_by_the_proxy(
        eng, project, store, upstream, monkeypatch, tmp_path):
    """A codex seat's session cannot refresh (its login carries a run token and no refresh
    token), so BOSS refreshes the SEAT's real login — in the seat's own home, with the seat's
    refresh token — and the proxy attaches that seat's CURRENT token. The refreshed generation
    goes back to the SEAT's store key; the single login is neither used nor refreshed."""
    from boss_engine.services import codex_login
    from boss_engine.services import model_proxy as mp
    from boss_engine.services.cli_harness import GenericCliCodeAgent, load_specs

    stub, proxy = upstream
    base = tmp_path / "codex"
    base.mkdir()
    (base / "auth.json").write_text(_codex_login(
        DEFAULT_ACCESS, "rt_default", expires_in=86400, last_refresh="2026-11-20T00:00:00Z"))
    monkeypatch.setenv("CODEX_HOME", str(base))
    # The seat's token runs out in 5 minutes: due for a refresh before any session starts.
    seat = _codex_login(SEAT_ACCESS, "rt_seat", expires_in=300,
                        last_refresh="2026-11-10T00:00:00Z")
    posted: list[dict] = []

    def token_endpoint(url, body):
        posted.append(body)
        return 200, {"access_token": SEAT_ACCESS_2, "refresh_token": "rt_seat_2",
                     "expires_in": 864000}
    monkeypatch.setattr(codex_login, "_http_post", token_endpoint)
    with eng.begin() as c:
        _pool_, (member,) = _pool(c, project, engine="codex", logins=(seat,))
    set_llm_context(project_id=project, run_id="crun_xr", db_engine=eng)
    settings = Settings()
    agent = GenericCliCodeAgent(settings, load_specs(settings)["codex"])
    agent._preflight(read_only=False)
    with mp.session_scope():
        env = agent._env()
        session_home = env["CODEX_HOME"]
        assert session_home != agent.cli_home() and session_home != str(base)
        login = json.loads(open(os.path.join(session_home, "auth.json")).read())
        run_token = login["tokens"]["access_token"]
        assert run_token.startswith(mp.TOKEN_PREFIX)
        assert login["tokens"]["account_id"] == "acct-seat"
        assert not any(t in json.dumps(env) + json.dumps(login)
                       for t in (SEAT_ACCESS, SEAT_ACCESS_2, DEFAULT_ACCESS, "rt_seat"))
        assert [b["refresh_token"] for b in posted] == ["rt_seat"], \
            "the SEAT's login was refreshed with the seat's own refresh token"

        assert _through_proxy(proxy, "/codex_subscription/responses", run_token,
                              {"chatgpt-account-id": "acct-seat"}) == 200
        assert stub.calls[-1]["headers"]["authorization"] == f"Bearer {SEAT_ACCESS_2}"

    seat_home_login = json.loads(open(os.path.join(agent.cli_home(), "auth.json")).read())
    assert seat_home_login["tokens"]["refresh_token"] == "rt_seat_2"
    member_key = sp._store_key(sp.secret_name_for("codex", member["id"]))
    assert json.loads(store.held[member_key])["tokens"]["access_token"] == SEAT_ACCESS_2, \
        "the refreshed generation goes back to the seat's store key"
    assert "BOSS_SUBSCRIPTION_CODEX_AUTH_JSON" not in store.held
    assert json.loads((base / "auth.json").read_text())["tokens"]["access_token"] == \
        DEFAULT_ACCESS, "the single login is untouched"


def test_codex_runs_on_the_members_home_and_syncs_back_to_the_member(eng, project, store,
                                                                     monkeypatch, tmp_path):
    from boss_engine.services.cli_harness import GenericCliCodeAgent, load_specs

    base = tmp_path / "codex"
    base.mkdir()
    (base / "auth.json").write_text(json.dumps({"tokens": {"x": "main"},
                                                "last_refresh": "2026-11-01T00:00:00Z"}))
    (base / "config.toml").write_text("model = 'x'\n")
    monkeypatch.setenv("CODEX_HOME", str(base))
    seat = json.dumps({"tokens": {"x": "seat"}, "last_refresh": "2026-11-10T00:00:00Z"})
    with eng.begin() as c:
        _pool_, (member,) = _pool(c, project, engine="codex", logins=(seat,))
    set_llm_context(project_id=project, run_id="crun_x", db_engine=eng)
    settings = Settings()
    agent = GenericCliCodeAgent(settings, load_specs(settings)["codex"])
    agent._preflight(read_only=False)
    home = agent.cli_home()
    assert home == f"{base}-pool-{member['id']}"
    assert json.loads(open(os.path.join(home, "auth.json")).read())["tokens"]["x"] == "seat"
    assert json.loads((base / "auth.json").read_text())["tokens"]["x"] == "main", \
        "the single login's home is never written"
    env = agent._env()
    assert env["CODEX_HOME"] == home and "OPENAI_API_KEY" not in env
    # The CLI rotated the member's login during the run: it goes back to the MEMBER's key.
    rotated = json.dumps({"tokens": {"x": "seat-2"}, "last_refresh": "2026-11-12T00:00:00Z"})
    with open(os.path.join(home, "auth.json"), "w") as fh:
        fh.write(rotated)
    assert agent.remember_rotated_credential() is True
    member_key = sp._store_key(sp.secret_name_for("codex", member["id"]))
    assert store.held[member_key] == rotated
    assert "BOSS_SUBSCRIPTION_CODEX_AUTH_JSON" not in store.held, "the single login is untouched"


def test_no_pool_changes_nothing(eng, project, store):
    assert sp.assignment_for("claude_code", Settings()) is None        # no project in context
    set_llm_context(project_id=project, run_id="crun_n", db_engine=eng)
    assert sp.assignment_for("claude_code", Settings()) is None        # a project with no pool
    with eng.begin() as c:
        pool, _members = _pool(c, project)
        sp.set_pool_status(c, project, pool["id"], status="disabled", actor="operator")
    assert sp.assignment_for("claude_code", Settings()) is None        # a pool turned off


def test_a_refused_member_leaves_rotation(eng, project, store):
    with eng.begin() as c:
        _pool_, (a, b) = _pool(c, project)
    sp.mark_needs_reauth(Settings().model_copy(update={"database_url": TEST_DB}), a["id"])
    with eng.begin() as c:
        for task in ("t1", "t2", "t3"):
            assert sp.assign(c, project, "claude_code", task).member_id == b["id"]
        fresh = sp.update_member(c, Settings(), project, a["id"], actor="operator",
                                 credential=TOKEN_A)
        assert fresh["status"] == "active"


def test_an_empty_pool_deletes_one_with_accounts_is_refused(eng, project, store):
    """BOSSAPP-1042: a pool can be deleted, but never by cascading its accounts' rows — that
    would orphan their stored logins. Refused while it has accounts; once each is removed (with
    its login), the empty pool goes and starting a new one for the engine works again."""
    with eng.begin() as c:
        pool, (a, b) = _pool(c, project)
        with pytest.raises(sp.PoolError, match="remove its 2 accounts first"):
            sp.delete_pool(c, project, pool["id"], actor="operator")
        assert sp.get_pool(c, project, "claude_code") is not None
        assert len(store.held) == 2, "a refused delete touches no login"
        for m in (a, b):
            sp.remove_member(c, Settings(), project, m["id"], actor="operator")
        assert store.held == {}
        assert sp.delete_pool(c, project, pool["id"], actor="operator") == {"deleted": pool["id"]}
        assert sp.get_pool(c, project, "claude_code") is None
        with pytest.raises(LookupError):
            sp.delete_pool(c, project, pool["id"], actor="operator")
        sp.create_pool(c, project, engine="claude_code", name="again", actor="operator")
        other = f"p_{uuid.uuid4().hex[:10]}"
        with pytest.raises(LookupError):                        # another project's id: not found
            sp.delete_pool(c, other, sp.get_pool(c, project, "claude_code")["id"], actor="operator")


def _codex_seats(eng, project, monkeypatch, tmp_path, *seats):
    """A codex pool of `seats` (login documents), the single login beside it, and an agent."""
    from boss_engine.services.cli_harness import GenericCliCodeAgent, load_specs

    base = tmp_path / "codex"
    base.mkdir()
    (base / "auth.json").write_text(_codex_login(
        DEFAULT_ACCESS, "rt_default", expires_in=86400, last_refresh="2026-11-20T00:00:00Z"))
    monkeypatch.setenv("CODEX_HOME", str(base))
    with eng.begin() as c:
        _pool_, members = _pool(c, project, engine="codex", logins=seats)
    set_llm_context(project_id=project, run_id=f"crun_{uuid.uuid4().hex[:6]}", db_engine=eng)
    settings = Settings()
    return GenericCliCodeAgent(settings, load_specs(settings)["codex"]), members


def _status(eng, member) -> str:
    with eng.begin() as c:
        return c.execute(text("SELECT status FROM subscription_pool_members WHERE id = :i"),
                         {"i": member["id"]}).scalar()


def test_a_transient_seat_refresh_failure_is_retried_never_a_sign_in(
        eng, project, store, upstream, monkeypatch, tmp_path):
    """A 503 from the token endpoint is a blip: the seat stays in rotation, the next session
    tries the refresh again and gets the new token — no person is asked to sign in."""
    from boss_engine.services import codex_login
    from boss_engine.services import model_proxy as mp

    stub, proxy = upstream
    answers = [(503, {"error": "server_error"}),
               (200, {"access_token": SEAT_ACCESS_2, "refresh_token": "rt_seat_2",
                      "expires_in": 864000})]
    monkeypatch.setattr(codex_login, "_http_post", lambda url, body: answers.pop(0))
    agent, (member,) = _codex_seats(eng, project, monkeypatch, tmp_path, _codex_login(
        SEAT_ACCESS, "rt_seat", expires_in=300, last_refresh="2026-11-10T00:00:00Z"))
    agent._preflight(read_only=False)
    with mp.session_scope():
        agent._env()                                    # the refresh answers 503
    assert _status(eng, member) == "active", "a blip never takes a seat out of rotation"
    assert len(answers) == 1

    agent._preflight(read_only=False)
    with mp.session_scope():
        env = agent._env()                               # tried again: refreshed
        token = json.loads(open(os.path.join(env["CODEX_HOME"], "auth.json")).read())[
            "tokens"]["access_token"]
        assert _through_proxy(proxy, "/codex_subscription/responses", token) == 200
    assert not answers
    assert stub.calls[-1]["headers"]["authorization"] == f"Bearer {SEAT_ACCESS_2}"
    assert _status(eng, member) == "active"


def test_a_refused_seat_login_names_the_seat_and_leaves_rotation(
        eng, project, store, upstream, monkeypatch, tmp_path):
    """invalid_grant on an expired seat: the words name THE SEAT (not the install's login) and
    where to replace it, and the seat goes to needs_reauth so the pool's others carry on."""
    from boss_engine.services import codex_login
    from boss_engine.services import model_proxy as mp

    monkeypatch.setattr(codex_login, "_http_post",
                        lambda url, body: (400, {"error": "invalid_grant"}))
    agent, (member,) = _codex_seats(eng, project, monkeypatch, tmp_path, _codex_login(
        SEAT_ACCESS, "rt_seat", expires_in=-60, last_refresh="2026-11-10T00:00:00Z"))
    agent._preflight(read_only=False)
    with mp.session_scope(), pytest.raises(codex_login.CodexSignInNeeded) as dead:
        agent._env()
    words = str(dead.value)
    assert "seat 0" in words and "Setup › Models" in words and "boss connect" not in words
    assert _status(eng, member) == "needs_reauth"


def test_a_seat_refresh_saves_only_to_the_seat_the_session_was_issued_for(
        eng, project, store, upstream, monkeypatch, tmp_path):
    """The seat is bound when the session is shielded: even if the harness is assigned another
    seat afterwards, a refresh during the session lands on the ORIGINAL seat's store key."""
    from boss_engine.services import codex_login
    from boss_engine.services import model_proxy as mp

    stub, proxy = upstream
    monkeypatch.setattr(codex_login, "_http_post", lambda url, body: (
        200, {"access_token": SEAT_ACCESS_2, "refresh_token": "rt_seat_2",
              "expires_in": 864000}))
    # 20 minutes left: no refresh at start (needs 10), due on a request (window 30).
    first = _codex_login(SEAT_ACCESS, "rt_seat", expires_in=1200,
                         last_refresh=datetime.now(timezone.utc).isoformat())
    other = _codex_login("eyJother." + "o" * 40 + ".sig", "rt_other", expires_in=86400,
                         last_refresh="2026-11-11T00:00:00Z")
    agent, (a, b) = _codex_seats(eng, project, monkeypatch, tmp_path, first, other)
    agent._preflight(read_only=False)
    issued_for = agent._pool.member_id
    with mp.session_scope():
        env = agent._env()
        token = json.loads(open(os.path.join(env["CODEX_HOME"], "auth.json")).read())[
            "tokens"]["access_token"]
        assert not stub.calls and json.loads(store.held[sp._store_key(sp.secret_name_for(
            "codex", issued_for))])["tokens"]["access_token"] == SEAT_ACCESS, \
            "not refreshed yet: the refresh happens on the request below"
        # The harness moves on to the other seat while this session is still running.
        moved_to = b["id"] if issued_for == a["id"] else a["id"]
        agent._pool = dataclasses.replace(agent._pool, member_id=moved_to,
                                          secret_name=sp.secret_name_for("codex", moved_to))
        assert _through_proxy(proxy, "/codex_subscription/responses", token) == 200
    assert stub.calls[-1]["headers"]["authorization"] == f"Bearer {SEAT_ACCESS_2}"
    held = {m["id"]: store.held[sp._store_key(sp.secret_name_for("codex", m["id"]))]
            for m in (a, b)}
    other_id = b["id"] if issued_for == a["id"] else a["id"]
    assert json.loads(held[issued_for])["tokens"]["access_token"] == SEAT_ACCESS_2
    assert json.loads(held[other_id])["tokens"]["refresh_token"] != "rt_seat_2"
