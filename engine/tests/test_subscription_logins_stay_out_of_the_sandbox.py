"""No subscription login is readable from inside an agent step, for any engine (BOSSAPP-1480).

API keys already stayed in BOSS (model_proxy). Subscription logins did not: the Claude token rode
in the CLI's environment, and the Claude CLI login and the Codex and Grok login files were copied
into the run's HOME — `env` or `cat` from any command the agent ran printed them.

Now (subscription_shield): the proxy holds every login; a session gets a run token and the proxy's
base URL through the route each CLI honours, and a session home with no login file in it. These
pin, per engine, with the upstream stubbed (no provider is called):

  * a sandboxed step's environment and every file it can reach under its run directory hold no
    part of any real login — checked by running a real shell inside the step's environment;
  * the run token reaches the model through the proxy, which attaches the REAL login upstream
    (and follows a refreshed one), and refuses anything that is not a model path;
  * when the session ends the token is dead and the session home is gone;
  * the sandbox boundary drops a raw login a step inherited, and seeding copies none.
"""
from __future__ import annotations

import base64
import contextlib
import http.client
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from boss_engine.config import Settings
from boss_engine.services import code_agent as ca
from boss_engine.services import model_proxy as mp
from boss_engine.services import sandbox_identity, subscription_shield
from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES, GenericCliCodeAgent

# Shaped like the real things; none is real.
CLAUDE_TOKEN = "sk-ant-oat01-" + "L" * 60
CLAUDE_ACCESS = "sk-ant-oat01-" + "A" * 60
CLAUDE_ACCESS_2 = "sk-ant-oat01-" + "B" * 60
CLAUDE_REFRESH = "sk-ant-ort01-" + "R" * 60
CODEX_ACCESS = "eyJhbGciOiJSUzI1NiJ9." + "c" * 80 + ".sig"
CODEX_REFRESH = "rt_" + "r" * 60
GROK_ACCESS = "grok-session-" + "g" * 60
GROK_REFRESH = "grok-refresh-" + "h" * 60
EMAIL = "someone@example.com"


def _jwt(payload: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(payload)}.realsignature"


CODEX_ID = _jwt({"email": EMAIL, "exp": int(time.time()) + 86400,
                 "https://api.openai.com/auth": {"chatgpt_account_id": "acct-123",
                                                 "chatgpt_plan_type": "pro",
                                                 "chatgpt_user_id": "user-secretish"}})
SECRETS = (CLAUDE_TOKEN, CLAUDE_ACCESS, CLAUDE_ACCESS_2, CLAUDE_REFRESH, CODEX_ACCESS,
           CODEX_REFRESH, CODEX_ID, GROK_ACCESS, GROK_REFRESH, EMAIL)


class StubUpstream:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @contextlib.contextmanager
    def __call__(self, method: str, url: str, headers, body: bytes):
        self.calls.append({"method": method, "url": url,
                           "headers": {k.lower(): v for k, v in headers.items()}})
        yield 200, {"content-type": "application/json"}, iter([b'{"ok": true}'])


@pytest.fixture()
def upstream():
    stub = StubUpstream()
    proxy = mp.ModelProxy(upstream=stub, poll_interval=0.02)
    mp.set_proxy(proxy)
    yield stub, proxy
    mp.set_proxy(None)
    proxy.stop()


@pytest.fixture()
def control_plane(tmp_path, monkeypatch):
    """The control plane's own logins, where BOSS keeps them (0700 to boss in an image)."""
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                 "OPENAI_API_KEY", "OPENAI_BASE_URL", "XAI_API_KEY", "GROK_AUTH",
                 "GROK_CLI_CHAT_PROXY_BASE_URL", "BOSS_ANTHROPIC_CREDENTIAL_ORDER",
                 "BOSS_CODE_AGENT_ENGINE", "BOSS_MODEL_PROXY_VIA_GATEWAY"):
        monkeypatch.delenv(name, raising=False)
    cp = tmp_path / "boss"
    (cp / ".claude").mkdir(parents=True)
    (cp / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": CLAUDE_ACCESS, "refreshToken": CLAUDE_REFRESH,
        "expiresAt": int((time.time() + 86400) * 1000)}}))
    (cp / ".codex").mkdir()
    (cp / ".codex" / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None, "last_refresh": "2026-11-25T00:00:00Z",
        "tokens": {"id_token": CODEX_ID, "access_token": CODEX_ACCESS,
                   "refresh_token": CODEX_REFRESH, "account_id": "acct-123",
                   "expires_at": int(time.time()) + 86400}}))
    (cp / ".codex" / "config.toml").write_text('model = "gpt-6-test"\n')
    (cp / ".grok").mkdir()
    (cp / ".grok" / "auth.json").write_text(json.dumps({"grok.com": {
        "access_token": GROK_ACCESS, "refresh_token": GROK_REFRESH, "email": EMAIL}}))
    (cp / ".grok" / "config.toml").write_text('[cli]\nuse_leader = false\n')
    monkeypatch.setenv("HOME", str(cp))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cp / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(cp / ".codex"))
    monkeypatch.setenv("GROK_HOME", str(cp / ".grok"))
    # The control plane refreshing its own logins is codex_login/claude_login's (tested there).
    monkeypatch.setattr(subscription_shield, "_codex_before_start", lambda: None)
    monkeypatch.setattr(sandbox_identity, "_refresh_logins_ahead", lambda ident: None)
    monkeypatch.setattr("boss_engine.services.claude_login.before_cli_start", lambda **kw: None)
    return cp


@pytest.fixture()
def sandbox(tmp_path, monkeypatch, control_plane):
    """A run's sandbox identity, bound as the current one, seeded the way acquire() seeds it."""
    ident = sandbox_identity.SandboxIdentity(key="run-1480", uid=61480,
                                             run_dir=tmp_path / "run")
    for sub in (ident.run_dir, ident.home, ident.tmp):
        sub.mkdir(exist_ok=True)
    (ident.run_dir / "ws").mkdir()
    monkeypatch.setattr(sandbox_identity, "current", lambda: ident)
    sandbox_identity._seed_cli_logins(ident)
    return ident


def _store(monkeypatch, **held):
    def lookup(settings, name, *, strict=False, include_env=True):
        return held.get(name)
    monkeypatch.setattr("boss_engine.services.cli_harness.stored_credential", lookup)


def _settings(**over) -> Settings:
    return Settings().model_copy(update={"claude_code_oauth_token": "",
                                         "anthropic_api_key": None, **over})


def _request(proxy: mp.ModelProxy, method: str, path: str, token: str,
             headers: Optional[dict] = None) -> int:
    conn = http.client.HTTPConnection(proxy.host, proxy.port, timeout=10)
    try:
        conn.request(method, path, body=b"{}" if method == "POST" else None,
                     headers={"authorization": f"Bearer {token}", **(headers or {})})
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def _what_a_step_can_read(env: dict, cwd: Path) -> str:
    """Run a shell in the step's own environment and print everything it can reach: its whole
    environment, and every file under its HOME and its CLI homes, links followed."""
    script = ('env; for d in "$HOME" "$CODEX_HOME" "$GROK_HOME" "$CLAUDE_CONFIG_DIR" "$TMPDIR"; '
              'do [ -n "$d" ] && [ -d "$d" ] && find -L "$d" -type f -exec cat {} + 2>/dev/null; '
              'done; true')
    out = subprocess.run(["/bin/sh", "-c", script], env={**env, "PATH": "/usr/bin:/bin"},
                         cwd=str(cwd), capture_output=True, text=True, timeout=30)
    return out.stdout + out.stderr


def _assert_nothing_readable(env: dict, ident: Any) -> None:
    seen = _what_a_step_can_read(env, ident.run_dir / "ws")
    leaked = [s[:16] + "…" for s in SECRETS if s in seen]
    assert not leaked, f"a step can read a subscription login: {leaked}"
    assert mp.holds_subscription_login(env) == []
    # And nothing anywhere in the run's directory, linked or not.
    for path in ident.run_dir.rglob("*"):
        if path.is_file():
            text = path.read_text(errors="ignore")
            assert not any(s in text for s in SECRETS), path


# ── Claude Code ───────────────────────────────────────────────────────────────────────────────

def test_claude_long_lived_token_stays_in_the_proxy(upstream, sandbox, monkeypatch):
    stub, proxy = upstream
    _store(monkeypatch, CLAUDE_CODE_OAUTH_TOKEN=CLAUDE_TOKEN)
    session = ca.ClaudeCodeAgent(_settings())._child_env()
    env = sandbox.env(session)                       # what the sandboxed CLI and its tools get
    run_token = env["CLAUDE_CODE_OAUTH_TOKEN"]
    assert run_token.startswith(mp.TOKEN_PREFIX)
    assert env["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{proxy.port}/claude_subscription"
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    _assert_nothing_readable(env, sandbox)

    # Claude Code 2.1 sends exactly this with an OAuth token and a base URL (measured).
    assert _request(proxy, "POST", "/claude_subscription/v1/messages?beta=true", run_token,
                    {"anthropic-beta": "claude-code-20250219,oauth-2025-04-20"}) == 200
    (call,) = stub.calls
    assert call["url"] == "https://api.anthropic.com/v1/messages?beta=true"
    assert call["headers"]["authorization"] == f"Bearer {CLAUDE_TOKEN}"
    assert call["headers"]["anthropic-beta"] == "claude-code-20250219,oauth-2025-04-20"
    assert "x-api-key" not in call["headers"]
    # Only the model paths: not the account's OAuth endpoints, not another provider's prefix.
    assert _request(proxy, "GET", "/claude_subscription/api/oauth/profile", run_token) == 403
    assert _request(proxy, "POST", "/anthropic/v1/messages", run_token) == 401
    assert len(stub.calls) == 1


def test_claude_cli_login_is_read_live_by_the_proxy_never_copied(upstream, sandbox,
                                                                 control_plane, monkeypatch):
    """The CLI's own login used to be copied into the run's .claude as a file. Now the session
    gets a run token, and the proxy reads the control plane's CURRENT access token per request
    — so a refresh by claude_login is followed without the run ever holding a token."""
    from boss_engine.services.claude_credential import KIND_CLI_LOGIN, ClaudeCredential

    stub, proxy = upstream
    monkeypatch.setattr(ca.ClaudeCodeAgent, "_resolve_credential",
                        lambda self: ClaudeCredential(KIND_CLI_LOGIN))
    env = sandbox.env(ca.ClaudeCodeAgent(_settings())._child_env())
    run_token = env["CLAUDE_CODE_OAUTH_TOKEN"]
    assert run_token.startswith(mp.TOKEN_PREFIX)
    assert not (sandbox.home / ".claude" / ".credentials.json").exists()
    _assert_nothing_readable(env, sandbox)

    assert _request(proxy, "POST", "/claude_subscription/v1/messages", run_token) == 200
    assert stub.calls[-1]["headers"]["authorization"] == f"Bearer {CLAUDE_ACCESS}"
    creds = control_plane / ".claude" / ".credentials.json"
    doc = json.loads(creds.read_text())
    doc["claudeAiOauth"]["accessToken"] = CLAUDE_ACCESS_2          # claude_login refreshed it
    creds.write_text(json.dumps(doc))
    assert _request(proxy, "POST", "/claude_subscription/v1/messages", run_token) == 200
    assert stub.calls[-1]["headers"]["authorization"] == f"Bearer {CLAUDE_ACCESS_2}"


def test_a_metered_route_drops_the_login_instead_of_carrying_both(upstream, monkeypatch):
    env = {"CLAUDE_CODE_OAUTH_TOKEN": CLAUDE_TOKEN, "ANTHROPIC_API_KEY": "boss_mdl_" + "0" * 64}
    subscription_shield.claude(env)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


# ── Codex ─────────────────────────────────────────────────────────────────────────────────────

def _codex_env(settings: Settings) -> dict:
    return GenericCliCodeAgent(settings, BUILTIN_CLI_HARNESSES["codex"])._env()


def test_codex_login_stays_in_the_proxy(upstream, sandbox):
    import tomllib

    stub, proxy = upstream
    env = sandbox.env(_codex_env(_settings()))
    home = Path(env["CODEX_HOME"])
    assert sandbox._inside(home) and home != sandbox.home / ".codex"
    assert "OPENAI_API_KEY" not in env
    _assert_nothing_readable(env, sandbox)

    login = json.loads((home / "auth.json").read_text())
    run_token = login["tokens"]["access_token"]
    assert login["auth_mode"] == "chatgpt" and run_token.startswith(mp.TOKEN_PREFIX)
    assert login["tokens"]["refresh_token"] == subscription_shield.RUN_REFRESH_TOKEN
    assert login["tokens"]["account_id"] == "acct-123"
    claims = subscription_shield._jwt_claims(login["tokens"]["id_token"])
    assert claims["https://api.openai.com/auth"] == {"chatgpt_account_id": "acct-123",
                                                     "chatgpt_plan_type": "pro"}
    config = tomllib.loads((home / "config.toml").read_text())
    provider = config["model_providers"][config["model_provider"]]
    assert provider == {"name": "ChatGPT via BOSS", "wire_api": "responses",
                        "requires_openai_auth": True,
                        "base_url": f"http://127.0.0.1:{proxy.port}/codex_subscription"}
    # State links to the run's persistent .codex, so `codex exec resume` finds its thread.
    assert (home / "sessions").resolve() == (sandbox.home / ".codex" / "sessions").resolve()

    # Codex 0.154 with that provider sends these, with its account id (measured).
    for method, path in (("GET", "/models?client_version=0.154.0"), ("POST", "/responses")):
        assert _request(proxy, method, "/codex_subscription" + path, run_token,
                        {"chatgpt-account-id": "acct-123"}) == 200
    assert stub.calls[-1]["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert stub.calls[-1]["headers"]["authorization"] == f"Bearer {CODEX_ACCESS}"
    assert stub.calls[-1]["headers"]["chatgpt-account-id"] == "acct-123"
    assert _request(proxy, "POST", "/codex_subscription/../oauth/token", run_token) in (403, 404)
    assert _request(proxy, "GET", "/codex_subscription/wham/usage", run_token) == 403


def test_codex_session_ends_with_its_token_and_home(upstream, control_plane):
    """Unsandboxed too (a developer's machine): the session's home is a temp dir with no link
    to the real login, removed with the session; its token dies when the session ends."""
    _, proxy = upstream
    with mp.session_scope():
        env = _codex_env(_settings())
        home = Path(env["CODEX_HOME"])
        assert home != control_plane / ".codex"
        assert not (home / "auth.json").is_symlink()
        token = json.loads((home / "auth.json").read_text())["tokens"]["access_token"]
        assert CODEX_ACCESS not in (home / "auth.json").read_text()
        assert _request(proxy, "POST", "/codex_subscription/responses", token) == 200
    assert not home.exists()
    assert _request(proxy, "POST", "/codex_subscription/responses", token) == 401


# ── Grok ──────────────────────────────────────────────────────────────────────────────────────

def test_grok_session_stays_in_the_proxy(upstream, sandbox):
    stub, proxy = upstream
    env = sandbox.env(GenericCliCodeAgent(_settings(), BUILTIN_CLI_HARNESSES["grok"])._env())
    home = Path(env["GROK_HOME"])
    assert sandbox._inside(home) and not (home / "auth.json").exists()
    assert (home / "config.toml").read_text() == "[cli]\nuse_leader = false\n"
    assert env["GROK_CLI_CHAT_PROXY_BASE_URL"] == (
        f"http://127.0.0.1:{proxy.port}/grok_subscription/v1")
    _assert_nothing_readable(env, sandbox)

    # Grok runs its auth provider command through `sh -c` and takes `access_token`.
    printed = subprocess.run(["/bin/sh", "-c", env["GROK_AUTH_PROVIDER_COMMAND"]],
                             capture_output=True, text=True, check=True).stdout
    run_token = json.loads(printed)["access_token"]
    assert run_token.startswith(mp.TOKEN_PREFIX)
    assert _request(proxy, "POST", "/grok_subscription/v1/chat/completions", run_token) == 200
    assert stub.calls[-1]["url"] == "https://cli-chat-proxy.grok.com/v1/chat/completions"
    assert stub.calls[-1]["headers"]["authorization"] == f"Bearer {GROK_ACCESS}"
    assert _request(proxy, "GET", "/grok_subscription/v1/settings", run_token) == 403


# ── the boundary itself ───────────────────────────────────────────────────────────────────────

def test_seeding_a_run_copies_no_login(sandbox):
    for path in sandbox.run_dir.rglob("*"):
        if path.is_file():
            assert not any(s in path.read_text(errors="ignore") for s in SECRETS), path
    assert not (sandbox.home / ".codex" / "auth.json").exists()
    assert not (sandbox.home / ".grok" / "auth.json").exists()
    assert not (sandbox.home / ".claude" / ".credentials.json").exists()


def test_the_sandbox_drops_a_raw_login_a_step_inherited(sandbox, monkeypatch):
    """A step that passes no environment gets the agent allow-list, which names
    CLAUDE_CODE_OAUTH_TOKEN; a raw login there never crosses into the run."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", CLAUDE_TOKEN)
    monkeypatch.setenv("GROK_AUTH", GROK_ACCESS)
    env = sandbox.env(None)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "GROK_AUTH" not in env
    _assert_nothing_readable(env, sandbox)
    token = "boss_mdl_" + "1" * 64
    assert sandbox.env({"CLAUDE_CODE_OAUTH_TOKEN": token})["CLAUDE_CODE_OAUTH_TOKEN"] == token


def test_every_engine_is_shielded_the_same_way(upstream, control_plane):
    """Model-agnostic: an engine BOSS has no adapter for still never carries a raw login — a
    Claude token goes through the proxy, any other login variable is dropped."""
    env = {"CLAUDE_CODE_OAUTH_TOKEN": CLAUDE_TOKEN, "CODEX_ACCESS_TOKEN": CODEX_ACCESS}
    subscription_shield.shield("some_acp_agent", env)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"].startswith(mp.TOKEN_PREFIX)
    assert "CODEX_ACCESS_TOKEN" not in env
    assert mp.holds_subscription_login(env) == []


# ── what a run plants in its own HOME between sessions (security review of #338) ──────────────

def _secret_dir(tmp_path: Path) -> Path:
    """Something only the control plane may touch: 0700, a TOML with a secret in it."""
    secret = tmp_path / "control-plane-private"
    secret.mkdir(mode=0o700)
    (secret / "config.toml").write_text('token = "PLANTED_SECRET_VALUE"\n')
    os.chmod(secret, 0o700)
    return secret


def _start_within(seconds: float, fn):
    import threading

    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("v", fn()), daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "a session start hung on something the run planted"
    return out["v"]


@pytest.mark.parametrize("engine", ["codex", "grok"])
def test_a_state_home_swapped_for_a_link_is_never_followed(engine, upstream, sandbox, tmp_path):
    import stat as _stat

    secret = _secret_dir(tmp_path)
    state = sandbox.home / f".{engine}"
    import shutil as _shutil

    _shutil.rmtree(state)
    state.symlink_to(secret)                       # the agent, between two sessions
    env = _start_within(10, lambda: sandbox.env(
        GenericCliCodeAgent(_settings(), BUILTIN_CLI_HARNESSES[engine])._env()))
    assert _stat.S_IMODE(secret.stat().st_mode) == 0o700          # never chmod-ed
    home = Path(env[f"{engine.upper()}_HOME"])
    for entry in home.iterdir():                                   # nothing linked to it
        assert not (entry.is_symlink() and str(secret) in os.path.realpath(entry))
        if entry.is_file() and not entry.is_symlink():
            assert "PLANTED_SECRET_VALUE" not in entry.read_text()


def test_a_planted_fifo_or_link_inside_the_state_home_is_not_read_or_chmod_ed(upstream, sandbox,
                                                                            tmp_path):
    import stat as _stat

    secret = _secret_dir(tmp_path)
    state = sandbox.home / ".codex"
    os.mkfifo(state / "config.toml")              # would block an open()
    (state / "sessions").rmdir()
    (state / "sessions").symlink_to(secret)       # would be chmod-ed 2777 by a re-create
    (state / "auth.json").symlink_to(secret / "config.toml")
    env = _start_within(10, lambda: sandbox.env(_codex_env(_settings())))
    assert _stat.S_IMODE(secret.stat().st_mode) == 0o700
    home = Path(env["CODEX_HOME"])
    config = home / "config.toml"
    assert not config.is_symlink() and "PLANTED_SECRET_VALUE" not in config.read_text()
    assert "boss_chatgpt" in config.read_text()
    login = home / "auth.json"
    assert not login.is_symlink()
    assert json.loads(login.read_text())["tokens"]["access_token"].startswith(mp.TOKEN_PREFIX)


def test_a_refused_login_answers_in_boss_words(upstream, control_plane):
    stub, proxy = upstream

    @contextlib.contextmanager
    def refused(method, url, headers, body):
        stub.calls.append({"url": url})
        yield 401, {"content-type": "application/json"}, iter([b'{"error":"expired"}'])

    proxy.upstream = refused
    issued = proxy.issue("codex_subscription", CODEX_ACCESS, ttl=60)
    conn = http.client.HTTPConnection(proxy.host, proxy.port, timeout=10)
    conn.request("POST", "/codex_subscription/responses", body=b"{}",
                 headers={"authorization": f"Bearer {issued.token}"})
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 401 and body["error"]["code"] == "subscription_login_refused"
    assert "Codex" in body["error"]["message"] and "Setup" in body["error"]["message"]


def test_stale_session_homes_are_swept(monkeypatch, tmp_path):
    monkeypatch.setattr(subscription_shield.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(subscription_shield, "_SWEPT", False)
    old, fresh = tmp_path / "codex-session-old", tmp_path / "grok-session-new"
    other = tmp_path / "unrelated-old"
    for p in (old, fresh, other):
        p.mkdir()
    long_ago = time.time() - subscription_shield._STALE_SECONDS - 60
    os.utime(old, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    subscription_shield._sweep_stale_session_homes()
    assert not old.exists() and fresh.exists() and other.exists()
