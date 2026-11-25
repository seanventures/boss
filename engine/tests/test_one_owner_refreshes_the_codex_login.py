"""ONE OWNER REFRESHES THE CODEX CHATGPT LOGIN — runs never rotate the real refresh token
(2026-10-29). Mirrors test_one_owner_refreshes_the_claude_login.py for Codex; see
boss_engine.services.claude_login for the pattern this is modeled on.

No network: the token endpoint is a fake `post`. No real token: markers only.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from boss_engine.services import codex_login, sandbox_identity


@dataclass
class _Ident:
    home: Path
    key: str = "run-key"


def _login(tmp_path: Path, monkeypatch, *, expires_in: float, refresh: str = "REFRESH_1",
           access: str = "ACCESS_1", last_refresh: str | None = None) -> Path:
    # Relative to now: a fixed date crosses REFRESH_AGE_INTERVAL_SECONDS a week later and the
    # test silently enters the refresh path.
    last_refresh = last_refresh or _iso(3600)
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700, exist_ok=True)
    src = home / "auth.json"
    exp = int(time.time() + expires_in)
    src.write_text(json.dumps({
        "auth_mode": "chatgpt", "last_refresh": last_refresh,
        "tokens": {"access_token": access, "refresh_token": refresh,
                   "id_token": access, "account_id": "acct-1", "expires_at": exp}}))
    os.chmod(src, 0o600)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(sandbox_identity, "current", lambda: None)
    return src


class _Endpoint:
    """The OpenAI token endpoint, faked: rotates the pair once per call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, url: str, body: dict):
        with self._lock:
            self.calls.append(dict(body))
            n = len(self.calls)
        return 200, {"access_token": f"ACCESS_{n + 1}", "refresh_token": f"REFRESH_{n + 1}",
                     "expires_in": 8 * 3600, "token_type": "Bearer"}


def _tokens(src: Path) -> dict:
    return json.loads(src.read_text())["tokens"]


def _iso(seconds_ago: float) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _session(monkeypatch, ident=None):
    """What a run's Codex session gets (subscription_shield, BOSSAPP-1480): (env, proxy, the
    session's login doc or None). The control plane's login is read, never copied."""
    from boss_engine.services import model_proxy, subscription_shield

    monkeypatch.setattr(sandbox_identity, "current", lambda: ident)
    proxy = model_proxy.ModelProxy()
    env = subscription_shield.codex({"CODEX_HOME": os.environ["CODEX_HOME"]}, proxy=proxy)
    path = Path(env["CODEX_HOME"]) / "auth.json"
    return env, proxy, (json.loads(path.read_text()) if path.exists() else None)


def _held(proxy, doc) -> str:
    from boss_engine.services import model_proxy

    return model_proxy.held_for(doc["tokens"]["access_token"], proxy)


# P1 (criterion 2, UNVERIFIED and defensive; see codex_login's docstring): codex may refresh its
# own ChatGPT login on last_refresh age alone (around 8 days old), not only on the access token's
# expiry. A session's login is written fresh for each session, so it never looks due on age.
def test_p1_a_session_login_never_looks_due_on_age_to_a_codex_that_refreshes_on_it(
        tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch, expires_in=5 * 3600,
                 last_refresh=_iso(codex_login.REFRESH_AGE_INTERVAL_SECONDS -
                                    codex_login.REFRESH_AGE_MARGIN_SECONDS + 60))
    endpoint = _Endpoint()
    monkeypatch.setattr(codex_login, "_http_post", endpoint)
    out = codex_login.refresh_if_due(post=endpoint)
    assert out["result"] == "refreshed"
    assert len(endpoint.calls) == 1
    assert _tokens(src)["access_token"] == "ACCESS_2"

    run_home = tmp_path / "run-home"
    run_home.mkdir()
    _env, proxy, doc = _session(monkeypatch, _Ident(run_home))
    assert _held(proxy, doc) == "ACCESS_2"

    def _fake_codex_refreshes() -> bool:
        age = time.time() - __import__("datetime").datetime.fromisoformat(
            doc["last_refresh"].replace("Z", "+00:00")).timestamp()
        return age > codex_login.REFRESH_AGE_INTERVAL_SECONDS

    assert _fake_codex_refreshes() is False
    proxy.stop()


# P0: the core refresh contract — the real refresh token only ever leaves via the owner's call,
# under the lock, and the file is rewritten in place (never a new, unrelated endpoint hit).
def test_p0_a_token_inside_the_window_is_refreshed_by_the_one_owner(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch, expires_in=10 * 60)
    endpoint = _Endpoint()
    out = codex_login.refresh_if_due(post=endpoint)

    assert out["result"] == "refreshed"
    assert len(endpoint.calls) == 1
    assert endpoint.calls[0].get("refresh_token") == "REFRESH_1"
    new = _tokens(src)
    assert new["access_token"] == "ACCESS_2" and new["refresh_token"] == "REFRESH_2"
    assert codex_login.seconds_left(new) > 7 * 3600


# P1 (criterion 2): two refreshers racing on the same login refresh exactly once — the second
# never reaches the fake endpoint at all, so the real refresh token is never sent twice.
def test_p1_two_refreshers_at_once_refresh_exactly_once(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch, expires_in=60)
    endpoint = _Endpoint()
    results: list[str] = []
    start = threading.Barrier(4)

    def _go():
        start.wait()
        results.append(codex_login.refresh_if_due(post=endpoint)["result"])

    threads = [threading.Thread(target=_go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert len(endpoint.calls) == 1, endpoint.calls
    assert sorted(results) == ["fresh", "fresh", "fresh", "refreshed"]
    assert _tokens(src)["refresh_token"] == "REFRESH_2"


# P1 (criterion 3): a run's seeded copy keeps the shape codex needs to report a ChatGPT login,
# not API-key fallback — the refresh token field survives the seed.
def test_p1_a_run_starts_on_a_refreshed_login_in_the_chatgpt_shape(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch, expires_in=5 * 3600,
                 last_refresh=_iso(codex_login.REFRESH_AGE_INTERVAL_SECONDS -
                                    codex_login.REFRESH_AGE_MARGIN_SECONDS + 60))
    endpoint = _Endpoint()
    monkeypatch.setattr(codex_login, "_http_post", endpoint)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude"))
    run_home = tmp_path / "run-home"
    run_home.mkdir()

    sandbox_identity._seed_cli_logins(_Ident(run_home))       # the control plane refreshes first
    assert len(endpoint.calls) == 1
    assert not (run_home / ".codex" / "auth.json").exists()   # and copies nothing

    _env, proxy, doc = _session(monkeypatch, _Ident(run_home))
    assert doc["auth_mode"] == "chatgpt"
    assert _held(proxy, doc) == "ACCESS_2"
    # the run never holds the real refresh token (only the control plane does)
    assert doc["tokens"]["refresh_token"] == sandbox_identity.RUN_REFRESH_TOKEN
    assert _tokens(src)["access_token"] == "ACCESS_2"
    assert _tokens(src)["refresh_token"] == "REFRESH_2"
    proxy.stop()


def test_a_run_can_never_rotate_the_operators_codex_login(tmp_path, monkeypatch):
    """When a run's codex refreshed (it does on a 401), OpenAI rotated the token, the control
    plane kept the old one, and its next use revoked the whole login (boss-1, 10-29 and 10-30).
    A session's login holds a run token and a refresh token that cannot rotate anything; the
    account and auth mode codex needs are the real ones; no real token is in it at all."""
    src = _login(tmp_path, monkeypatch, expires_in=5 * 3600, refresh="REAL_REFRESH")
    real = json.loads(src.read_text())
    run_home = tmp_path / "run-home"
    run_home.mkdir()
    _env, proxy, doc = _session(monkeypatch, _Ident(run_home))
    text = json.dumps(doc)
    assert "REAL_REFRESH" not in text and "ACCESS_1" not in text
    assert doc["tokens"]["refresh_token"] == sandbox_identity.RUN_REFRESH_TOKEN
    assert doc["tokens"]["account_id"] == real["tokens"]["account_id"]
    assert doc["auth_mode"] == real["auth_mode"]
    assert json.loads(src.read_text())["tokens"]["refresh_token"] == "REAL_REFRESH"
    proxy.stop()


def test_the_session_login_is_readable_by_the_runs_own_uid(tmp_path, monkeypatch):
    """The run's codex runs as the run's uid with no groups; BOSS writes the login as itself. A
    0600 file left codex unable to read its login (boss-1, 2026-10-30). It holds only a run
    token, so it is world-readable inside the run's own directory."""
    import stat

    _login(tmp_path, monkeypatch, expires_in=5 * 3600)
    run_home = tmp_path / "run-home"
    run_home.mkdir()
    env, proxy, _doc = _session(monkeypatch, _Ident(run_home))
    login = Path(env["CODEX_HOME"]) / "auth.json"
    assert stat.S_IMODE(login.stat().st_mode) & stat.S_IROTH
    assert Path(env["CODEX_HOME"]).parent == run_home
    proxy.stop()


# P1 (criterion 4): whatever a run's codex writes into its session login stays there and is
# removed with the session — the control plane's own file never moves.
def test_p1_a_rewritten_session_login_is_never_adopted_by_the_control_plane(tmp_path,
                                                                           monkeypatch):
    from boss_engine.services import model_proxy

    held = _login(tmp_path, monkeypatch, expires_in=5 * 3600)
    before = held.read_text()
    with model_proxy.session_scope():
        env, proxy, _doc = _session(monkeypatch, None)
        home = Path(env["CODEX_HOME"])
        (home / "auth.json").write_text(json.dumps({
            "auth_mode": "chatgpt", "last_refresh": _iso(60),
            "tokens": {"access_token": "A2", "refresh_token": "R2"}}))
    assert not home.exists()
    assert held.read_text() == before
    proxy.stop()


# P1 (criterion 5): codex's own words for a dead credential flip the login state to the
# red-pill word immediately, without waiting on a scheduled proof.
def test_p1_a_revoked_token_error_flips_state_without_waiting_for_a_tick(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch, expires_in=5 * 3600)
    assert codex_login.status()["state"] != codex_login.REFRESH_FAILED

    codex_login.mark_revoked("invalidated oauth token")

    st = codex_login.status()
    assert st["state"] == codex_login.REFRESH_FAILED
    assert "ACCESS" not in json.dumps(st) and "REFRESH" not in json.dumps(st)


# P1 (criterion 6): a long, multi-stage build follows the control plane's own refresh — the
# proxy reads the current tokens on every request; nothing in the run goes stale.
def test_p1_a_long_build_follows_the_control_planes_refresh(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch, expires_in=5 * 3600, access="ACCESS_1", refresh="REFRESH_1")
    run_home = tmp_path / "run-home"
    run_home.mkdir()
    _env, proxy, doc = _session(monkeypatch, _Ident(run_home))
    assert _held(proxy, doc) == "ACCESS_1"
    _login(tmp_path, monkeypatch, expires_in=8 * 3600, access="ACCESS_9", refresh="REFRESH_9")
    assert _held(proxy, doc) == "ACCESS_9"
    proxy.stop()


# P1: a credential already flagged dead (mark_revoked) is not handed to a new session — not
# even by proxy — just because its access token has not yet expired.
def test_p1_a_revoked_login_is_not_handed_to_a_new_session(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch, expires_in=5 * 3600)
    codex_login.mark_revoked("invalidated oauth token")
    run_home = tmp_path / "run-home"
    run_home.mkdir()
    _env, proxy, doc = _session(monkeypatch, _Ident(run_home))
    assert doc is None
    proxy.stop()


# P1: a login revoked mid-build is not handed to the build's next session either.
def test_p1_a_revoked_login_is_not_handed_to_a_running_builds_next_session(tmp_path,
                                                                          monkeypatch):
    _login(tmp_path, monkeypatch, expires_in=5 * 3600)
    run_home = tmp_path / "run-home"
    run_home.mkdir()
    ident = _Ident(run_home)
    _env, proxy, first = _session(monkeypatch, ident)
    assert first is not None
    codex_login.mark_revoked("invalidated oauth token")
    _env, proxy2, second = _session(monkeypatch, ident)
    assert second is None
    proxy.stop()
    proxy2.stop()


# The one owner is started where builds run: start_all (system_threads) launches the Codex
# login refresher alongside Claude's, so a running install has exactly one refresher holder.
def test_start_all_starts_the_one_codex_login_refresher(monkeypatch):
    from boss_engine.services import codex_login, system_threads

    calls: list[str] = []
    monkeypatch.setattr(codex_login, "start", lambda *a, **k: calls.append("codex") or None)

    started = system_threads.start_all(engine=object(), settings=object())

    assert calls == ["codex"]
    assert started.get("codex_login") is True


# --- the refresh writes the rotation back to the secret store (BOSSAPP-1026) --------------
#
# This module is the ONLY process that rotates the real refresh token, so it is the only place
# the store can be kept live from. On a live install the store froze at its 1 Oct credential for
# six weeks while this refresher kept the container alive — and every restore-from-store then
# signed a working surface out.


class _FakeStore:
    def __init__(self):
        self.values = {}
        self.versions = {}

    def get(self, key):
        if key not in self.values:
            raise KeyError(key)
        return self.values[key]

    def put(self, key, value):
        self.values[key] = value
        self.versions[key] = self.versions.get(key, 0) + 1

    def describe(self, key):
        return {"version": self.versions.get(key), "updated_at": "now"}


def test_a_successful_refresh_writes_the_rotation_back_to_the_store(tmp_path, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr("boss_engine.services.secret_backends.default_secret_backend",
                        lambda settings: "database")
    monkeypatch.setattr("boss_engine.services.secret_backends.backend_for",
                        lambda kind, settings: store)
    monkeypatch.delenv("CODEX_AUTH_JSON", raising=False)
    src = _login(tmp_path, monkeypatch, expires_in=10 * 60)

    out = codex_login.refresh_if_due(post=_Endpoint())

    assert out["result"] == "refreshed"
    assert out["store_synced"] is True
    held = json.loads(store.values["BOSS_SUBSCRIPTION_CODEX_AUTH_JSON"])
    assert held == json.loads(src.read_text()), "the store holds the LIVE generation"
    assert held["tokens"]["access_token"] == "ACCESS_2"
    assert store.versions["BOSS_SUBSCRIPTION_CODEX_AUTH_JSON"] == 1


def test_a_store_failure_never_fails_the_refresh(tmp_path, monkeypatch):
    """The refresh already happened; losing it over a store would revoke the live login."""
    monkeypatch.setattr("boss_engine.services.secret_backends.default_secret_backend",
                        lambda settings: (_ for _ in ()).throw(RuntimeError("store is down")))
    monkeypatch.delenv("CODEX_AUTH_JSON", raising=False)
    src = _login(tmp_path, monkeypatch, expires_in=10 * 60)

    out = codex_login.refresh_if_due(post=_Endpoint())

    assert out["result"] == "refreshed", "the refresh stands"
    assert out["store_synced"] is False, "and the miss is visible, never silent"
    assert _tokens(src)["access_token"] == "ACCESS_2"
