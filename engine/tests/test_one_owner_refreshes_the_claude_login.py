"""ONE OWNER REFRESHES THE CLAUDE CLI LOGIN, AHEAD OF TIME, AND A FAILURE NEVER WIPES IT.

2026-10-26 15:08 and 2026-10-27 09:25: the login was gone ~8 h after sign-in. The access token
had expired; a `claude` process refreshed, was refused (invalid_grant), and the CLI cleared the
login on disk. A build started on an expired token failed "401 OAuth access token has expired"
(crun_50cf77b8). Now BOSS (claude_login) is the one refresher: under one lock, well before the
CLI's own 5-minute window, and a refused refresh keeps the file and asks a person to sign in.

No network: the token endpoint is a fake `post`. No real token: markers only.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from boss_engine.services import claude_login, sandbox_identity, setup_view
from boss_engine.services import system_doctor as doc
from boss_engine.services.admission import Capacity


@dataclass
class _Ident:
    home: Path
    key: str = "run-key"


def _login(tmp_path: Path, monkeypatch, *, expires_in: float, refresh: str = "REFRESH_1",
           access: str = "ACCESS_1") -> Path:
    conf = tmp_path / "service-claude"
    conf.mkdir(mode=0o700, exist_ok=True)
    src = conf / ".credentials.json"
    src.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": access, "refreshToken": refresh,
        "expiresAt": int((time.time() + expires_in) * 1000),
        "scopes": ["user:inference", "user:profile"], "subscriptionType": "max"},
        "mcpOAuth": {"keep": "me"}}))
    os.chmod(src, 0o600)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(conf))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("BOSS_CLAUDE_LOGIN_REFRESH", raising=False)
    monkeypatch.setattr(sandbox_identity, "current", lambda: None)
    return src


class _Endpoint:
    """The OAuth token endpoint, faked: rotates the pair once per call, or refuses."""

    def __init__(self, *, refuse: str = "", delay: float = 0.0):
        self.calls: list[dict] = []
        self.refuse = refuse
        self.delay = delay
        self._lock = threading.Lock()

    def __call__(self, url: str, body: dict):
        with self._lock:
            self.calls.append(dict(body))
            n = len(self.calls)
        assert url == claude_login.TOKEN_URL
        if self.delay:
            time.sleep(self.delay)
        if self.refuse:
            return 400, {"error": self.refuse, "error_description": "Refresh token not found"}
        return 200, {"access_token": f"ACCESS_{n + 1}", "refresh_token": f"REFRESH_{n + 1}",
                     "expires_in": 8 * 3600, "token_type": "Bearer"}


def _oauth(src: Path) -> dict:
    return json.loads(src.read_text())["claudeAiOauth"]


# ---- the refresher -------------------------------------------------------------------------

def test_a_token_inside_the_window_is_refreshed_the_way_the_cli_does_it(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch, expires_in=20 * 60)       # inside the 30-minute window
    endpoint = _Endpoint()
    out = claude_login.refresh_if_due(post=endpoint)

    assert out["result"] == "refreshed"
    assert len(endpoint.calls) == 1
    body = endpoint.calls[0]
    assert body["grant_type"] == "refresh_token" and body["refresh_token"] == "REFRESH_1"
    assert body["client_id"] == claude_login.CLIENT_ID
    assert body["scope"] == "user:inference user:profile"
    oauth = _oauth(src)
    assert oauth["accessToken"] == "ACCESS_2" and oauth["refreshToken"] == "REFRESH_2"
    assert oauth["expiresAt"] / 1000 - time.time() > 7 * 3600
    assert oauth["subscriptionType"] == "max"                     # the rest of the login kept
    assert json.loads(src.read_text())["mcpOAuth"] == {"keep": "me"}
    assert stat.S_IMODE(src.stat().st_mode) == 0o600
    assert claude_login.status()["state"] == claude_login.OK
    # nothing left behind: no temp files, the CLI's lock directory released
    assert sorted(p.name for p in src.parent.iterdir()) == sorted(
        [".credentials.json", claude_login.LOCK_NAME, claude_login.STATE_NAME])
    state = (src.parent / claude_login.STATE_NAME).read_text()
    assert "ACCESS" not in state and "REFRESH" not in state         # no secret in the state


def test_a_fresh_token_is_left_alone(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch, expires_in=5 * 3600)
    before = src.read_text()
    endpoint = _Endpoint()
    assert claude_login.refresh_if_due(post=endpoint)["result"] == "fresh"
    assert claude_login.tick()["result"] == "fresh"
    assert endpoint.calls == [] and src.read_text() == before


def test_two_refreshers_at_once_refresh_exactly_once(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch, expires_in=60)
    endpoint = _Endpoint(delay=0.4)
    results: list[str] = []
    start = threading.Barrier(4)

    def _go():
        start.wait()
        results.append(claude_login.refresh_if_due(post=endpoint)["result"])

    threads = [threading.Thread(target=_go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(endpoint.calls) == 1, endpoint.calls               # one refresh token spent once
    assert sorted(results) == ["fresh", "fresh", "fresh", "refreshed"]
    assert _oauth(src)["refreshToken"] == "REFRESH_2"


def test_a_cli_holding_its_own_refresh_lock_is_waited_for_not_raced(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch, expires_in=60)
    cli_lock = src.parent / claude_login.CLI_LOCK_NAME
    cli_lock.mkdir()                                                # the CLI is refreshing
    endpoint = _Endpoint()
    out = claude_login.refresh_if_due(post=endpoint, lock_timeout=0.5)
    assert out["result"] == "busy" and endpoint.calls == []
    cli_lock.rmdir()
    assert claude_login.refresh_if_due(post=endpoint)["result"] == "refreshed"


def test_a_refused_refresh_keeps_the_login_and_asks_a_person(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch, expires_in=10 * 60)
    before = src.read_text()
    out = claude_login.refresh_if_due(post=_Endpoint(refuse="invalid_grant"))

    assert out == {"result": "failed", "error": "invalid_grant"}
    assert src.read_text() == before                               # never wiped
    assert stat.S_IMODE(src.stat().st_mode) == 0o600
    st = claude_login.status()
    assert st["state"] == claude_login.REFRESH_FAILED and st["error"] == "invalid_grant"

    # The doctor raises it to the operator, as a card with a way to sign in.
    sig = doc.Signals(cap=Capacity(total_mb=15991, available_mb=13029, cores=8,
                                   disk_free_gb=40, disk_used_pct=48), claude_login=st)
    card = next(f for f in doc.diagnose(sig) if f["kind"] == "claude_signed_out")
    assert card["severity"] == "critical" and card["needs_choice"]
    assert card["title"] == "Claude sign-in needed"
    assert "invalid_grant" in card["detail"]
    assert {o["key"] for o in card["options"]} <= doc.OFFERED_OPTIONS
    assert "claude_sign_in" in {o["key"] for o in card["options"]}

    # A network failure is not a wipe either.
    def _down(url, body):
        raise ConnectionError("no route")
    assert claude_login.refresh_if_due(post=_down)["result"] == "failed"
    assert src.read_text() == before


def test_a_healthy_login_raises_no_card():
    sig = doc.Signals(cap=Capacity(total_mb=15991, available_mb=13029, cores=8,
                                   disk_free_gb=40, disk_used_pct=48),
                      claude_login={"state": "ok", "seconds_left": 20000})
    assert "claude_signed_out" not in {f["kind"] for f in doc.diagnose(sig)}


# ---- every CLI start -----------------------------------------------------------------------

def test_a_cli_start_refreshes_first_and_refuses_an_expired_unrefreshable_login(
        tmp_path, monkeypatch):
    from boss_engine.services.claude_credential import KIND_CLI_LOGIN, ClaudeCredential

    src = _login(tmp_path, monkeypatch, expires_in=3 * 60)          # inside the CLI's 5 min
    endpoint = _Endpoint()
    monkeypatch.setattr(claude_login, "_http_post", endpoint)
    ClaudeCredential(KIND_CLI_LOGIN).apply({})
    assert len(endpoint.calls) == 1 and _oauth(src)["accessToken"] == "ACCESS_2"

    src = _login(tmp_path, monkeypatch, expires_in=-60)              # expired
    before = src.read_text()
    monkeypatch.setattr(claude_login, "_http_post", _Endpoint(refuse="invalid_grant"))
    with pytest.raises(claude_login.ClaudeSignInNeeded) as raised:
        ClaudeCredential(KIND_CLI_LOGIN).apply({})
    assert "Claude sign-in needed" in str(raised.value)
    assert "ACCESS" not in str(raised.value) and "REFRESH" not in str(raised.value)
    assert src.read_text() == before

    from boss_engine.services.model_failures import is_login_rejected
    assert is_login_rejected(str(raised.value))


# ---- the build sandbox ---------------------------------------------------------------------

def test_the_sandbox_gets_a_token_refreshed_to_outlive_a_stage(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch, expires_in=40 * 60)          # < the 60 min a stage needs
    endpoint = _Endpoint()
    monkeypatch.setattr(claude_login, "_http_post", endpoint)
    home = tmp_path / "run-home"
    home.mkdir()
    sandbox_identity._seed_claude_login(_Ident(home))

    assert len(endpoint.calls) == 1
    copied = json.loads((home / ".claude" / ".credentials.json").read_text())["claudeAiOauth"]
    assert copied["accessToken"] == "ACCESS_2"                     # the fresh one
    assert "refreshToken" not in copied
    assert _oauth(src)["refreshToken"] == "REFRESH_2"


def test_the_sandbox_never_gets_an_expired_token_and_its_claude_start_says_sign_in(
        tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch, expires_in=-5 * 60)               # crun_50cf77b8's case
    monkeypatch.setattr(claude_login, "_http_post", _Endpoint(refuse="invalid_grant"))
    home = tmp_path / "run-home"
    home.mkdir()
    ident = _Ident(home)
    sandbox_identity._seed_claude_login(ident)
    assert not (home / ".claude" / ".credentials.json").exists()

    monkeypatch.setattr(sandbox_identity, "current", lambda: ident)
    with pytest.raises(claude_login.ClaudeSignInNeeded) as raised:
        claude_login.before_cli_start()
    assert "has expired" in str(raised.value)


def test_a_long_build_follows_the_control_planes_refresh(tmp_path, monkeypatch):
    src = _login(tmp_path, monkeypatch, expires_in=5 * 3600)
    home = tmp_path / "run-home"
    home.mkdir()
    ident = _Ident(home)
    sandbox_identity._seed_claude_login(ident)
    copy = home / ".claude" / ".credentials.json"
    assert json.loads(copy.read_text())["claudeAiOauth"]["accessToken"] == "ACCESS_1"

    _login(tmp_path, monkeypatch, expires_in=8 * 3600, access="ACCESS_9", refresh="REFRESH_9")
    monkeypatch.setattr(sandbox_identity, "current", lambda: ident)
    claude_login.before_cli_start()                                 # the next stage's claude
    oauth = json.loads(copy.read_text())["claudeAiOauth"]
    assert oauth["accessToken"] == "ACCESS_9" and "refreshToken" not in oauth
    assert src.exists()


# ---- the pill ------------------------------------------------------------------------------

@pytest.mark.parametrize("live,expected", [
    ({"state": "ok", "seconds_left": 7 * 3600}, "working"),
    ({"state": "expiring", "seconds_left": 20 * 60}, "login_expiring"),
    ({"state": "refresh_failed", "seconds_left": 20 * 60}, "login_refresh_failed"),
    ({"state": "expired", "seconds_left": -10}, "login_expired"),
])
def test_the_models_pill_reads_the_real_expiry(live, expected):
    state, _ = setup_view.claude_login_state("working", provider="claude_code",
                                             kind="subscription", login=live)
    assert state == expected
    from boss_engine.services.setup_pills import pill
    shown = pill("model", state, n=20, age="1 min", provider_fix="Sign in.")
    assert shown["label"] != "Working" or expected == "working"


def test_the_pill_of_another_provider_is_not_touched():
    state, _ = setup_view.claude_login_state("working", provider="codex", kind="subscription",
                                             login={"state": "expired"})
    assert state == "working"
