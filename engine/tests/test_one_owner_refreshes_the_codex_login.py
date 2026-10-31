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


# P1 (criterion 2, UNVERIFIED and defensive; see codex_login's docstring): codex may refresh its
# own ChatGPT login on last_refresh age alone (around 8 days old), not only on the access token's
# expiry. refresh_if_due must cover that trigger too, with a safety margin,
# so a login the control plane just seeded never still looks "due" to the run's own codex.
def test_p1_refresh_if_due_also_covers_last_refresh_age_like_codex_does(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch, expires_in=5 * 3600,
                 last_refresh=_iso(codex_login.REFRESH_AGE_INTERVAL_SECONDS -
                                    codex_login.REFRESH_AGE_MARGIN_SECONDS + 60))
    endpoint = _Endpoint()
    out = codex_login.refresh_if_due(post=endpoint)

    assert out["result"] == "refreshed"
    assert len(endpoint.calls) == 1
    new = _tokens(src)
    assert new["access_token"] == "ACCESS_2"


def test_p1_a_freshly_seeded_copy_never_looks_due_on_age_to_a_codex_that_refreshes_on_it(
        tmp_path, monkeypatch):
    """A fake codex that refreshes whenever last_refresh is older than the real interval: after
    the control plane's own background owner refreshes an age-due login (refresh_if_due, the
    same tick `start_all`'s refresher runs) and then seeds (reseed_codex_login), the run's copy
    must never cross that line. Starting from an already-fresh login would pass even if seeding
    never refreshed anything; starting age-due exercises the actual refresh path."""
    from boss_engine.services import sandbox_identity

    src = _login(tmp_path, monkeypatch, expires_in=5 * 3600,
                 last_refresh=_iso(codex_login.REFRESH_AGE_INTERVAL_SECONDS -
                                    codex_login.REFRESH_AGE_MARGIN_SECONDS + 60))
    endpoint = _Endpoint()
    out = codex_login.refresh_if_due(post=endpoint)
    assert out["result"] == "refreshed"
    assert len(endpoint.calls) == 1
    assert _tokens(src)["access_token"] == "ACCESS_2"

    run_home = tmp_path / "run-home"
    run_home.mkdir()
    ident = _Ident(run_home)
    monkeypatch.setattr(sandbox_identity, "current", lambda: ident)
    sandbox_identity.reseed_codex_login(ident)

    copy = run_home / ".codex" / "auth.json"
    doc = json.loads(copy.read_text())
    assert doc["tokens"]["access_token"] == "ACCESS_2"

    def _fake_codex_refreshes() -> bool:
        age = time.time() - __import__("datetime").datetime.fromisoformat(
            doc["last_refresh"].replace("Z", "+00:00")).timestamp()
        return age > codex_login.REFRESH_AGE_INTERVAL_SECONDS

    assert _fake_codex_refreshes() is False


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
def test_p1_the_seeded_run_copy_keeps_the_chatgpt_login_shape(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch, expires_in=5 * 3600,
                 last_refresh=_iso(codex_login.REFRESH_AGE_INTERVAL_SECONDS -
                                    codex_login.REFRESH_AGE_MARGIN_SECONDS + 60))
    endpoint = _Endpoint()
    monkeypatch.setattr(codex_login, "_http_post", endpoint)
    run_home = tmp_path / "run-home"
    run_home.mkdir()

    sandbox_identity._seed_codex_login(_Ident(run_home))

    assert len(endpoint.calls) == 1
    copied = json.loads((run_home / ".codex" / "auth.json").read_text())
    assert copied["auth_mode"] == "chatgpt"
    assert copied["tokens"]["access_token"] == "ACCESS_2"
    # the run never holds the real refresh token (only the control plane does)
    assert copied["tokens"]["refresh_token"] == sandbox_identity.RUN_REFRESH_TOKEN
    assert _tokens(src)["access_token"] == "ACCESS_2"
    assert _tokens(src)["refresh_token"] == "REFRESH_2"


def test_a_run_can_never_rotate_the_operators_codex_login(tmp_path, monkeypatch):
    """When a run's codex refreshed (it does on a 401), OpenAI rotated the token, the control
    plane kept the old one, and its next use revoked the whole login (boss-1, 10-29 and 10-30).
    A run's copy, seeded or reseeded, carries a refresh token that cannot rotate anything; every
    other field codex needs (access and id tokens, account, auth mode) is the real one."""
    src = _login(tmp_path, monkeypatch, expires_in=5 * 3600, refresh="REAL_REFRESH")
    real = json.loads(src.read_text())
    run_home = tmp_path / "run-home"
    run_home.mkdir()
    ident = _Ident(run_home)

    for seed in (lambda: sandbox_identity._seed_codex_login(ident),
                 lambda: sandbox_identity.copy_codex_tokens(ident)):
        seed()
        copied = json.loads((run_home / ".codex" / "auth.json").read_text())
        assert "REAL_REFRESH" not in json.dumps(copied)
        assert copied["tokens"]["refresh_token"] == sandbox_identity.RUN_REFRESH_TOKEN
        for key in ("access_token", "id_token", "account_id"):
            assert copied["tokens"].get(key) == real["tokens"].get(key)
        assert copied["auth_mode"] == real["auth_mode"]
    assert json.loads(src.read_text())["tokens"]["refresh_token"] == "REAL_REFRESH"


def test_the_seeded_copy_is_readable_by_the_runs_own_uid(tmp_path, monkeypatch):
    """The run's codex runs as the run's uid with no groups; BOSS writes the copy as itself. A
    0600 copy left codex unable to read its login, and it fell back to API-key mode: "401
    wss://api.openai.com/v1/responses" in every build (boss-1, 2026-10-30). Seeding and every
    reseed must leave it world-readable, like the other seeded logins."""
    import stat

    _login(tmp_path, monkeypatch, expires_in=5 * 3600)
    run_home = tmp_path / "run-home"
    run_home.mkdir()
    ident = _Ident(run_home)

    sandbox_identity._seed_codex_login(ident)
    seeded = run_home / ".codex" / "auth.json"
    assert stat.S_IMODE(seeded.stat().st_mode) & stat.S_IROTH

    os.chmod(seeded, 0o600)                       # a reseed restores it
    assert sandbox_identity.copy_codex_tokens(ident)
    assert stat.S_IMODE(seeded.stat().st_mode) & stat.S_IROTH


# P1 (criterion 4): a run's copy coming back rotated is a containment failure, never a
# legitimate rotation — the control plane's own file must not move.
def test_p1_a_rotated_run_copy_is_never_adopted_by_the_control_plane(tmp_path, monkeypatch):
    held = _login(tmp_path, monkeypatch, expires_in=5 * 3600)
    run_home = tmp_path / "run-home"
    run_home.mkdir()
    (run_home / ".codex").mkdir()
    (run_home / ".codex" / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "last_refresh": _iso(60),
        "tokens": {"access_token": "A2", "refresh_token": "R2", "expires_at":
                   int(time.time() + 3600)}}))

    before = held.read_text()
    sandbox_identity._return_rotated_codex_login(_Ident(run_home, key="run-x"))

    assert held.read_text() == before


# P1 (criterion 5): codex's own words for a dead credential flip the login state to the
# red-pill word immediately, without waiting on a scheduled proof.
def test_p1_a_revoked_token_error_flips_state_without_waiting_for_a_tick(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch, expires_in=5 * 3600)
    assert codex_login.status()["state"] != codex_login.REFRESH_FAILED

    codex_login.mark_revoked("invalidated oauth token")

    st = codex_login.status()
    assert st["state"] == codex_login.REFRESH_FAILED
    assert "ACCESS" not in json.dumps(st) and "REFRESH" not in json.dumps(st)


# P1 (criterion 6): a long, multi-stage build follows the control plane's own refresh — each
# codex start re-seeds from the current tokens rather than relying on the run's stale copy.
def test_p1_a_long_build_follows_the_control_planes_refresh(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch, expires_in=5 * 3600, access="ACCESS_1", refresh="REFRESH_1")
    run_home = tmp_path / "run-home"
    run_home.mkdir()
    ident = _Ident(run_home)
    sandbox_identity._seed_codex_login(ident)
    copy = run_home / ".codex" / "auth.json"
    assert json.loads(copy.read_text())["tokens"]["access_token"] == "ACCESS_1"

    _login(tmp_path, monkeypatch, expires_in=8 * 3600, access="ACCESS_9", refresh="REFRESH_9")
    monkeypatch.setattr(sandbox_identity, "current", lambda: ident)
    sandbox_identity.reseed_codex_login(ident)

    assert json.loads(copy.read_text())["tokens"]["access_token"] == "ACCESS_9"


# P1: a credential already flagged dead (mark_revoked) must not be handed to a NEW sandbox just
# because its access token has not yet expired — sharing a known-revoked login is the one thing
# mark_revoked exists to prevent, not just a label on the control plane's own file.
def test_p1_a_revoked_login_is_not_seeded_into_a_new_sandbox(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch, expires_in=5 * 3600)
    codex_login.mark_revoked("invalidated oauth token")
    run_home = tmp_path / "run-home"
    run_home.mkdir()

    sandbox_identity._seed_codex_login(_Ident(run_home))

    assert not (run_home / ".codex" / "auth.json").is_file()


# P1: the same credential, already seeded before it was revoked, must not be re-copied into a
# run's home on a later reseed (ahead of a later `codex` start in a long, multi-stage build).
def test_p1_a_revoked_login_is_not_reseeded_into_a_running_sandbox(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch, expires_in=5 * 3600)
    run_home = tmp_path / "run-home"
    run_home.mkdir()
    ident = _Ident(run_home)
    sandbox_identity._seed_codex_login(ident)
    copy = run_home / ".codex" / "auth.json"
    before = copy.read_text()

    codex_login.mark_revoked("invalidated oauth token")
    monkeypatch.setattr(sandbox_identity, "current", lambda: ident)
    sandbox_identity.reseed_codex_login(ident)

    assert copy.read_text() == before


# The one owner is started where builds run: start_all (system_threads) launches the Codex
# login refresher alongside Claude's, so a running install has exactly one refresher holder.
def test_start_all_starts_the_one_codex_login_refresher(monkeypatch):
    from boss_engine.services import codex_login, system_threads

    calls: list[str] = []
    monkeypatch.setattr(codex_login, "start", lambda *a, **k: calls.append("codex") or None)

    started = system_threads.start_all(engine=object(), settings=object())

    assert calls == ["codex"]
    assert started.get("codex_login") is True
