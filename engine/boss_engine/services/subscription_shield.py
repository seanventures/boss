"""A SUBSCRIPTION LOGIN NEVER ENTERS AN AGENT'S SESSION (BOSSAPP-1480).

API keys already reach models through the model proxy (model_proxy): the session holds a run
token, the proxy holds the key. Subscription logins did not: the Claude token rode in the CLI's
environment, the Claude CLI login and the Codex and Grok login files were copied into the run's
HOME, and an agent — or any code it ran — could read them with `env` or `cat`.

Now every engine's session is built the same way, whatever the engine:

  * the proxy holds the login (a fixed value, or a SOURCE it reads on each request, so a login
    the control plane refreshes — claude_login, codex_login — is followed live);
  * the session gets a run token (short-lived, bound to the run, revoked when the session ends)
    and the proxy's base URL, through the route each CLI itself honours:

      claude_code  CLAUDE_CODE_OAUTH_TOKEN=<run token>, ANTHROPIC_BASE_URL=<proxy>
      codex        a session CODEX_HOME whose auth.json is a ChatGPT-mode login carrying the run
                   token (no refresh token, an unsigned id_token with only the plan and account
                   id), and a model provider with `requires_openai_auth = true` at the proxy —
                   Codex ignores OPENAI_BASE_URL in ChatGPT mode (measured, codex-cli 0.154)
      grok         a session GROK_HOME with no login in it, GROK_CLI_CHAT_PROXY_BASE_URL=<proxy>
                   and GROK_AUTH_PROVIDER_COMMAND printing the run token (Grok Build's
                   documented external-auth contract)

  * a session home holds links to the CLI's state (sessions, history — so `codex exec resume`
    still finds its thread) and never a login file; it is removed when the session ends.

What a sandboxed agent can read is therefore a token that works only on the model API paths of
one provider, through BOSS's loopback proxy, for this session. The real login stays in the
control plane, 0700 to the boss uid.
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import shlex
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import model_proxy

log = logging.getLogger(__name__)

CLAUDE_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
#: A route already chosen that bills money: a login beside it is dropped, never proxied.
_METERED_ANTHROPIC = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
#: What a run's Codex login carries in place of the refresh token (sandbox_identity's value).
RUN_REFRESH_TOKEN = "rt_boss_run_copy_cannot_refresh"
#: The model provider a shielded Codex session uses: the ChatGPT backend, through the proxy.
CODEX_PROVIDER = "boss_chatgpt"
#: The id_token claims a run's Codex login keeps: which plan, which account. Never an email.
_CODEX_CLAIMS = ("chatgpt_account_id", "chatgpt_plan_type")
_OPENAI_AUTH = "https://api.openai.com/auth"
#: Files in a CLI home that hold a credential: never linked or copied into a session home.
_LOGIN_FILES = ("auth.json", "credentials.json", ".credentials.json", "mcp_credentials.json")


# ── shared ────────────────────────────────────────────────────────────────────────────────────

def _sandbox() -> Any:
    try:
        from . import sandbox_identity

        return sandbox_identity.current()
    except Exception:  # noqa: BLE001 — no sandbox bound: the session runs as the control plane
        return None


def _issue(provider: str, credential: str = "", *, settings: Any = None,
           source: Optional[Callable[[], str]] = None,
           proxy: Optional[model_proxy.ModelProxy] = None) -> model_proxy.Issued:
    proxy = proxy or model_proxy.get()
    run_id, project_id = model_proxy._run_binding()
    return proxy.issue(provider, credential, ttl=model_proxy.session_ttl(settings),
                       run_id=run_id, project_id=project_id, source=source)


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _is_login_file(name: str) -> bool:
    return any(name == f or name.startswith(f + ".") for f in _LOGIN_FILES)


def _session_home(prefix: str, state_home: str, sandbox: Any, *,
                  keep_dirs: tuple[str, ...] = (), skip: tuple[str, ...] = ()) -> str:
    """A fresh home for one session: links to every entry of `state_home` (the CLI's own state)
    except login files and `skip`, and nothing else. In a sandbox it lives in the run's HOME,
    writable by the run's uid; otherwise in the control plane's temp dir, 0700. Removed when the
    session ends (model_proxy.on_session_end); a sandbox's is also removed with its run."""
    if sandbox is not None:
        os.makedirs(state_home, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(state_home, 0o2777)
        home = tempfile.mkdtemp(prefix=prefix, dir=str(sandbox.home))
        os.chmod(home, 0o2777)
    else:
        home = tempfile.mkdtemp(prefix=prefix)
        os.chmod(home, 0o700)
    for name in keep_dirs:
        # State a CLI creates on first use (Codex's `sessions`) is made in the persistent home
        # first, so a session's thread outlives its session home and can be resumed.
        path = os.path.join(state_home, name)
        with contextlib.suppress(OSError):
            os.makedirs(path, exist_ok=True)
            if sandbox is not None:
                os.chmod(path, 0o2777)
    if os.path.isdir(state_home):
        for entry in os.listdir(state_home):
            if _is_login_file(entry) or entry in skip:
                continue
            with contextlib.suppress(OSError):
                os.symlink(os.path.join(state_home, entry), os.path.join(home, entry))
    if not model_proxy.on_session_end(lambda: shutil.rmtree(home, ignore_errors=True)):
        log.debug("subscription shield: no session scope; %s is removed with its run", home)
    return home


def _write(path: str, text: str, *, sandbox: Any) -> None:
    """A run-token file: 0644 in a sandbox (the run's uid reads what BOSS wrote; it holds no
    secret), 0600 otherwise. Never follows a link planted in its place."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    mode = 0o644 if sandbox is not None else 0o600
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)


# ── Claude Code ───────────────────────────────────────────────────────────────────────────────

def claude_cli_login_source() -> Callable[[], str]:
    """The Claude CLI login's CURRENT access token, read from the control plane's own
    .credentials.json on every request (claude_login refreshes it there, under its lock)."""
    def read() -> str:
        from . import claude_login

        oauth = claude_login.read_oauth() or {}
        return str(oauth.get("accessToken") or "")
    return read


def claude(env: dict, *, settings: Any = None, cli_login: bool = False,
           proxy: Optional[model_proxy.ModelProxy] = None) -> dict:
    """`env` with the Claude subscription replaced by a run token at the proxy.

    `CLAUDE_CODE_OAUTH_TOKEN` (the long-lived token, a pool member's) becomes a run token; with
    `cli_login` (the CLI's own login, which used to be copied into the run as a file) the proxy
    reads the control plane's current access token instead. Either way the CLI gets
    ANTHROPIC_BASE_URL. A metered route already in `env` (an API key or a gateway's run token)
    wins and the login is dropped: exactly one credential reaches the CLI."""
    token = (env.get(CLAUDE_TOKEN_ENV) or "").strip()
    if token.startswith(model_proxy.TOKEN_PREFIX):
        return env
    if any((env.get(n) or "").strip() for n in _METERED_ANTHROPIC):
        env.pop(CLAUDE_TOKEN_ENV, None)
        return env
    if token:
        issued = _issue("claude_subscription", token, settings=settings, proxy=proxy)
    elif cli_login:
        issued = _issue("claude_subscription", settings=settings, proxy=proxy,
                        source=claude_cli_login_source())
    else:
        return env
    env[CLAUDE_TOKEN_ENV] = issued.token
    env.update(issued.env)
    return env


# ── Codex ─────────────────────────────────────────────────────────────────────────────────────

def _b64(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt_claims(token: Any) -> dict:
    try:
        part = str(token).split(".")[1]
        doc = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:  # noqa: BLE001 — an unreadable id_token keeps no claims
        return {}
    return doc if isinstance(doc, dict) else {}


def codex_run_login(real: dict, token: str, *, ttl: float, now: Optional[float] = None) -> dict:
    """The Codex login a session gets: ChatGPT mode, so Codex speaks ChatGPT's backend and sends
    its account id, with the run token as its access token, no refresh token, and an UNSIGNED
    id_token carrying only the plan and account id (Codex reads them, never verifies them)."""
    now = time.time() if now is None else now
    tokens = real.get("tokens") if isinstance(real.get("tokens"), dict) else {}
    auth = _jwt_claims(tokens.get("id_token")).get(_OPENAI_AUTH) or {}
    kept = {k: auth[k] for k in _CODEX_CLAIMS if isinstance(auth, dict) and k in auth}
    account = tokens.get("account_id") or kept.get("chatgpt_account_id")
    id_token = ".".join((_b64({"alg": "none", "typ": "JWT"}),
                         _b64({"iss": "boss-model-proxy", "iat": int(now),
                               "exp": int(now + ttl), _OPENAI_AUTH: kept}), "boss"))
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": {"id_token": id_token, "access_token": token,
                       "refresh_token": RUN_REFRESH_TOKEN, "account_id": account,
                       "expires_at": int(now + ttl)},
            "last_refresh": datetime.fromtimestamp(now, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ")}


def _codex_source(login_path: str) -> Callable[[], str]:
    def read() -> str:
        tokens = (_read_json(login_path) or {}).get("tokens")
        return str((tokens or {}).get("access_token") or "") if isinstance(tokens, dict) else ""
    return read


def _load_toml(path: str) -> dict:
    try:
        import tomllib

        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, ValueError):
        return {}


def codex(env: dict, *, settings: Any = None, default_home: str = "~/.codex",
          proxy: Optional[model_proxy.ModelProxy] = None) -> dict:
    """`env` with the Codex login it would use moved behind the proxy: a session CODEX_HOME
    holding a run-token login and a provider at the proxy. An API-key route is left to
    model_proxy.shield_env (a key kept in the login FILE is moved to OPENAI_API_KEY for it), and
    a home with no login is left as it is (the CLI then says it is not signed in)."""
    if any((env.get(n) or "").strip() for n in ("OPENAI_API_KEY", "CODEX_API_KEY")):
        return env
    source_home = os.path.expanduser(env.get("CODEX_HOME") or default_home)
    login_path = os.path.realpath(os.path.join(source_home, "auth.json"))
    real = _read_json(login_path)
    if real is None:
        return env
    sandbox = _sandbox()
    state_home = str(Path(sandbox.home) / ".codex") if sandbox is not None else source_home
    tokens = real.get("tokens") if isinstance(real.get("tokens"), dict) else None
    if not (tokens and tokens.get("access_token")):
        # An API-key login file: the key goes to the environment, where shield_env trades it for
        # a run token; the session home holds no login.
        key = (real.get("OPENAI_API_KEY") or "").strip() if isinstance(
            real.get("OPENAI_API_KEY"), str) else ""
        if not key:
            return env
        env["CODEX_HOME"] = _session_home("codex-session-", state_home, sandbox,
                                          keep_dirs=("sessions",))
        env["OPENAI_API_KEY"] = key
        return env
    config = _load_toml(os.path.join(source_home if sandbox is None else state_home,
                                     "config.toml"))
    chosen = config.get("model_provider")
    if chosen not in (None, "openai", CODEX_PROVIDER):
        log.info("codex: the configured model provider %r is kept; no login is placed", chosen)
        env["CODEX_HOME"] = _session_home("codex-session-", state_home, sandbox,
                                          keep_dirs=("sessions",))
        return env
    if os.path.realpath(_default_codex_login()) == login_path:
        if _codex_login_dead():
            # A login flagged dead (codex_login.mark_revoked) is not handed to a run, not even
            # by proxy: the session starts with no login and Codex says it is not signed in.
            log.warning("codex: the login is marked revoked; the session gets no login")
            env["CODEX_HOME"] = _session_home("codex-session-", state_home, sandbox,
                                              keep_dirs=("sessions",))
            return env
        _codex_before_start()
    issued = _issue("codex_subscription", settings=settings, proxy=proxy,
                    source=_codex_source(login_path))
    home = _session_home("codex-session-", state_home, sandbox, keep_dirs=("sessions",),
                         skip=("config.toml",))
    config["model_provider"] = CODEX_PROVIDER
    providers = dict(config.get("model_providers") or {})
    providers[CODEX_PROVIDER] = {"name": "ChatGPT via BOSS", "base_url": issued.base_url,
                                 "wire_api": "responses", "requires_openai_auth": True}
    config["model_providers"] = providers
    from .harness_delivery import toml_dumps

    _write(os.path.join(home, "config.toml"), toml_dumps(config), sandbox=sandbox)
    _write(os.path.join(home, "auth.json"),
           json.dumps(codex_run_login(real, issued.token,
                                      ttl=model_proxy.session_ttl(settings))), sandbox=sandbox)
    env["CODEX_HOME"] = home
    return env


def _default_codex_login() -> str:
    from . import codex_login

    return codex_login._path(codex_login.AUTH_NAME)


def _codex_login_dead() -> bool:
    from . import codex_login

    return codex_login.failure_stands()


def _codex_before_start() -> None:
    """The control plane's own login is refreshed BEFORE a session needs it (codex_login): the
    session cannot refresh, and the proxy reads the refreshed token on each request."""
    from . import codex_login

    codex_login.before_cli_start()


# ── Grok ──────────────────────────────────────────────────────────────────────────────────────

def grok_access_token(doc: Any) -> str:
    """The session's bearer in a Grok `auth.json`: the first `access_token` (any depth), else a
    `token`. The file's layout is the CLI's own; this reads it without assuming one."""
    queue = [doc]
    fallback = ""
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            for key in ("access_token", "accessToken"):
                if isinstance(node.get(key), str) and node[key].strip():
                    return node[key].strip()
            if not fallback and isinstance(node.get("token"), str):
                fallback = node["token"].strip()
            queue.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            queue.extend(node)
    return fallback


def grok(env: dict, *, settings: Any = None, default_home: str = "~/.grok",
         proxy: Optional[model_proxy.ModelProxy] = None) -> dict:
    """`env` with the Grok session moved behind the proxy: a session GROK_HOME with no login in
    it, the chat proxy at BOSS's, and an auth provider command that prints the run token."""
    source_home = os.path.expanduser(env.get("GROK_HOME") or default_home)
    login_path = os.path.realpath(os.path.join(source_home, "auth.json"))
    if not grok_access_token(_read_json(login_path)):
        return env
    sandbox = _sandbox()
    state_home = str(Path(sandbox.home) / ".grok") if sandbox is not None else source_home
    issued = _issue("grok_subscription", settings=settings, proxy=proxy,
                    source=lambda: grok_access_token(_read_json(login_path)))
    home = _session_home("grok-session-", state_home, sandbox)
    ttl = int(model_proxy.session_ttl(settings))
    env["GROK_HOME"] = home
    env.update(issued.env)
    env["GROK_AUTH_PROVIDER_COMMAND"] = "printf '%s\\n' " + shlex.quote(
        json.dumps({"access_token": issued.token, "expires_in": ttl}))
    env.pop("GROK_AUTH", None)
    return env


# ── one entry for every engine ────────────────────────────────────────────────────────────────

def shield(engine: str, env: dict, *, settings: Any = None, default_home: str = "",
           cli_login: bool = False, proxy: Optional[model_proxy.ModelProxy] = None) -> dict:
    """The one call an engine's session environment goes through (model-agnostic: the same
    guarantee for every engine). Another engine's Claude token is proxied the same way (a CLI
    that reads it reads ANTHROPIC_BASE_URL too); any other raw login variable is dropped."""
    if engine == "codex":
        return codex(env, settings=settings, default_home=default_home or "~/.codex",
                     proxy=proxy)
    if engine == "grok":
        return grok(env, settings=settings, default_home=default_home or "~/.grok", proxy=proxy)
    claude(env, settings=settings, cli_login=cli_login and engine == "claude_code", proxy=proxy)
    for name in model_proxy.holds_subscription_login(env):
        env.pop(name, None)
    return env


__all__ = ["CODEX_PROVIDER", "RUN_REFRESH_TOKEN", "claude", "claude_cli_login_source", "codex",
           "codex_run_login", "grok", "grok_access_token", "shield"]
