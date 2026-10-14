"""An agent session never holds the real model key (services/model_proxy.py).

Verified live on the operator's laptop: the claude_agent_sdk engine hid the Anthropic API key
with CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1, which forces the Claude CLI's bubblewrap sandbox, and
bwrap cannot create a namespace inside BOSS's containers — every Bash call failed. The
claude_code engine never scrubbed: an agent's `env` printed the key.

Now the session gets a per-session run token and the loopback proxy's base URL; the proxy holds
the key. These pin, with the upstream stubbed (no provider is ever called):

  * neither Claude engine's session environment — nor any registered engine's child env —
    holds a provider API key; a session's token is revoked when it ends;
  * the proxy injects the real credential header and strips the client's;
  * it refuses non-model paths, foreign/unknown/expired/revoked tokens, before going upstream;
  * tokens are redacted; usage is tallied per run and never written to llm_calls a second time;
  * end to end: a fake `claude` that calls its ANTHROPIC_BASE_URL with its token reaches the
    stub upstream carrying the key it never saw, for claude_code and claude_agent_sdk.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import sys
import textwrap
import time
from typing import Any, Optional

import pytest

from boss_engine.config import Settings
from boss_engine.services import code_agent as ca
from boss_engine.services import model_proxy as mp

KEY = "sk-ant-api03-" + "k" * 48            # shaped like an API key; not a real one
OPENAI_KEY = "sk-proj-" + "o" * 48           # shaped like one; not a real one
ROUTER_KEY = "sk-or-v1-" + "r" * 48
XAI_KEY = "xai-" + "x" * 48
TOKEN = "sk-ant-oat01-" + "t" * 48          # shaped like a login; not a real one


class StubUpstream:
    """The provider, replaced: records what reached it and answers from a script."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.status = 200
        self.headers: dict[str, str] = {"content-type": "application/json"}
        self.chunks: list[bytes] = [json.dumps({
            "id": "msg_1", "type": "message", "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 12, "output_tokens": 3}}).encode()]

    @contextlib.contextmanager
    def __call__(self, method: str, url: str, headers, body: bytes):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": body})
        yield self.status, dict(self.headers), iter(list(self.chunks))


@pytest.fixture()
def upstream():
    stub = StubUpstream()
    proxy = mp.ModelProxy(upstream=stub, poll_interval=0.02)
    mp.set_proxy(proxy)
    yield stub, proxy
    mp.set_proxy(None)
    proxy.stop()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                 "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENROUTER_API_KEY", "XAI_API_KEY",
                 "BOSS_ANTHROPIC_CREDENTIAL_ORDER", "BOSS_CODE_AGENT_ENGINE"):
        monkeypatch.delenv(name, raising=False)


def _store(monkeypatch, **held):
    def lookup(settings, name, *, strict=False, include_env=True):
        return held.get(name)
    monkeypatch.setattr("boss_engine.services.cli_harness.stored_credential", lookup)


def _settings(**over) -> Settings:
    return Settings().model_copy(update={"claude_code_oauth_token": "",
                                         "anthropic_api_key": None, **over})


def _request(proxy: mp.ModelProxy, method: str, path: str, *, headers: Optional[dict] = None,
             body: bytes = b"") -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection(proxy.host, proxy.port, timeout=10)
    try:
        conn.request(method, path, body=body or None, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def _issue(proxy, provider="anthropic", credential=KEY, **kw) -> mp.Issued:
    kw.setdefault("ttl", 600)
    return proxy.issue(provider, credential, **kw)


# ── the proxy ────────────────────────────────────────────────────────────────────────────────

def test_the_real_key_is_injected_and_the_clients_credentials_are_stripped(upstream):
    stub, proxy = upstream
    issued = _issue(proxy, run_id="crun_1", project_id="p")
    body = json.dumps({"model": "claude-sonnet-5", "max_tokens": 5,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    status, headers, got = _request(
        proxy, "POST", "/anthropic/v1/messages?beta=true", body=body,
        headers={"x-api-key": issued.token, "authorization": "Bearer something-else",
                 "cookie": "a=b", "anthropic-version": "2023-06-01",
                 "anthropic-beta": "prompt-caching", "content-type": "application/json",
                 "accept-encoding": "gzip"})
    assert status == 200 and json.loads(got)["id"] == "msg_1"
    (call,) = stub.calls
    assert call["url"] == "https://api.anthropic.com/v1/messages?beta=true"
    assert call["method"] == "POST" and call["body"] == body
    sent = {k.lower(): v for k, v in call["headers"].items()}
    assert sent["x-api-key"] == KEY                         # the real one, injected
    assert "authorization" not in sent and "cookie" not in sent
    assert sent["anthropic-version"] == "2023-06-01" and sent["anthropic-beta"] == "prompt-caching"
    assert sent["accept-encoding"] == "identity"
    assert issued.token not in json.dumps(sent)
    assert issued.base_url == f"http://127.0.0.1:{proxy.port}/anthropic"
    assert dict(issued.env) == {"ANTHROPIC_API_KEY": issued.token,
                                "ANTHROPIC_BASE_URL": issued.base_url,
                                "ANTHROPIC_API_BASE": issued.base_url}
    assert KEY not in repr(issued) and KEY not in repr(proxy.grant_for(issued.token))


def test_a_bearer_token_works_too_and_an_openai_compatible_key_goes_as_a_bearer(upstream):
    stub, proxy = upstream
    issued = _issue(proxy)
    status, _, _ = _request(proxy, "POST", "/anthropic/v1/messages/count_tokens",
                            headers={"authorization": f"Bearer {issued.token}"}, body=b"{}")
    assert status == 200
    stub.chunks = [json.dumps({"usage": {"prompt_tokens": 9, "completion_tokens": 4}}).encode()]
    oa = _issue(proxy, "openai", OPENAI_KEY)
    assert oa.env["OPENAI_BASE_URL"].endswith("/openai/v1")
    status, _, _ = _request(proxy, "POST", "/openai/v1/chat/completions", body=b"{}",
                            headers={"authorization": f"Bearer {oa.token}"})
    assert status == 200
    assert stub.calls[-1]["url"] == "https://api.openai.com/v1/chat/completions"
    assert stub.calls[-1]["headers"]["authorization"] == f"Bearer {OPENAI_KEY}"
    usage = proxy.grant_for(oa.token).usage
    assert (usage["input_tokens"], usage["output_tokens"]) == (9, 4)
    router = _issue(proxy, "openrouter", ROUTER_KEY)
    _request(proxy, "POST", "/openrouter/v1/chat/completions", body=b"{}",
             headers={"authorization": f"Bearer {router.token}"})
    assert stub.calls[-1]["url"] == "https://openrouter.ai/api/v1/chat/completions"


def test_a_streamed_answer_passes_through_untouched_and_its_usage_is_tallied(upstream):
    stub, proxy = upstream
    events = [
        b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
        b'{"input_tokens":120,"cache_read_input_tokens":900,"output_tokens":1}}}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":'
        b'{"type":"text_delta","text":"he',
        b'llo"}}\n\n',
        b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":42}}\n\n',
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]
    stub.headers = {"content-type": "text/event-stream", "content-length": "999"}
    stub.chunks = events
    issued = _issue(proxy, run_id="crun_sse")
    status, headers, got = _request(proxy, "POST", "/anthropic/v1/messages", body=b"{}",
                                    headers={"x-api-key": issued.token})
    assert status == 200 and got == b"".join(events)
    assert headers["content-type"] == "text/event-stream"
    usage = proxy.run_usage("crun_sse")
    assert usage["requests"] == 1 and usage["input_tokens"] == 120
    assert usage["output_tokens"] == 42 and usage["cache_read_input_tokens"] == 900


@pytest.mark.parametrize("method, path", [
    ("GET", "/anthropic/v1/organizations/me"),
    ("POST", "/anthropic/v1/files"),
    ("POST", "/anthropic/v1/messages/batches"),
    ("GET", "/anthropic/v1/messages"),
    ("POST", "/anthropic/v1/messages/../../admin"),
    ("POST", "/anthropic/v1/%6Dessages"),
    ("POST", "/anthropic/api/oauth/token"),
    ("POST", "/anthropic/"),
    ("DELETE", "/anthropic/v1/messages"),
])
def test_only_the_model_api_paths_go_upstream(upstream, method, path):
    stub, proxy = upstream
    issued = _issue(proxy)
    status, _, body = _request(proxy, method, path, headers={"x-api-key": issued.token})
    assert status in (403, 405)
    assert stub.calls == []
    assert issued.token not in body.decode() and KEY not in body.decode()


def test_foreign_unknown_and_missing_tokens_are_refused(upstream):
    stub, proxy = upstream
    anthropic = _issue(proxy)
    for headers in ({}, {"x-api-key": KEY},                         # the real key is no token
                    {"x-api-key": mp.TOKEN_PREFIX + "0" * 64},       # never issued
                    {"authorization": "Basic " + anthropic.token}):
        status, _, _ = _request(proxy, "POST", "/anthropic/v1/messages", headers=headers,
                                body=b"{}")
        assert status == 401, headers
    # An Anthropic token is not an OpenAI one.
    status, _, _ = _request(proxy, "POST", "/openai/v1/chat/completions", body=b"{}",
                            headers={"authorization": f"Bearer {anthropic.token}"})
    assert status == 401
    status, _, _ = _request(proxy, "POST", "/nowhere/v1/messages", body=b"{}",
                            headers={"x-api-key": anthropic.token})
    assert status == 404
    assert stub.calls == []


def test_a_token_expires_and_is_revoked_with_its_session_and_its_run(upstream, monkeypatch):
    stub, proxy = upstream
    short = _issue(proxy, ttl=30)
    clock = time.time() + 31
    monkeypatch.setattr(mp.time, "time", lambda: clock)
    status, _, body = _request(proxy, "POST", "/anthropic/v1/messages", body=b"{}",
                               headers={"x-api-key": short.token})
    assert status == 401 and b"token_expired" in body
    monkeypatch.undo()

    with pytest.raises(RuntimeError, match="boom"):
        with mp.session_scope():
            inside = _issue(proxy)
            raise RuntimeError("boom")
    grant = proxy.grant_for(inside.token)
    assert grant.revoked and grant.credential == ""           # the key is forgotten too
    status, _, body = _request(proxy, "POST", "/anthropic/v1/messages", body=b"{}",
                               headers={"x-api-key": inside.token})
    assert status == 401 and b"token_revoked" in body

    a, b = _issue(proxy, run_id="crun_end"), _issue(proxy, run_id="crun_other")
    assert proxy.revoke_run("crun_end") == 1
    assert proxy.grant_for(a.token).revoked and not proxy.grant_for(b.token).revoked
    assert stub.calls == []


def test_the_scoped_session_reports_what_crossed_the_proxy(upstream):
    stub, proxy = upstream

    @mp.scoped(attach_usage=True)
    def session():
        issued = _issue(proxy)
        for _ in range(2):
            _request(proxy, "POST", "/anthropic/v1/messages", body=b"{}",
                     headers={"x-api-key": issued.token})
        return {"result": "ok"}

    out = session()
    assert out["_proxy_usage"]["requests"] == 2
    assert out["_proxy_usage"]["input_tokens"] == 24 and out["_proxy_usage"]["output_tokens"] == 6


def test_tokens_are_redacted_by_value_and_by_shape(upstream):
    from boss_engine.identity.redaction import redact_text

    _, proxy = upstream
    issued = _issue(proxy)
    assert issued.token not in redact_text(f"curl -H 'x-api-key: {issued.token}' and {issued.token}")
    assert KEY not in redact_text(f"leaked {KEY}")
    stray = mp.TOKEN_PREFIX + "ab" * 32
    assert stray not in redact_text(f"a token we never issued here: {stray}")


def test_a_proxy_that_cannot_listen_never_falls_back_to_the_key(monkeypatch):
    _store(monkeypatch, ANTHROPIC_API_KEY=KEY)
    broken = mp.ModelProxy(host="203.0.113.7")               # TEST-NET-3: not this machine
    mp.set_proxy(broken)
    try:
        with pytest.raises(mp.ProxyUnavailable):
            ca.ClaudeCodeAgent(_settings())._child_env()
    finally:
        mp.set_proxy(None)


# ── no engine's child environment holds a provider key ───────────────────────────────────────

def _child_env_of(agent: Any) -> Optional[dict]:
    inner = getattr(agent, "_inner", agent)
    for name in ("_child_env", "_env"):
        if hasattr(inner, name):
            return getattr(inner, name)()
    return None


def test_no_engine_child_env_holds_a_stored_provider_credential(upstream, monkeypatch):
    """THE GUARD: every registered engine, built the way a build builds it, with keys both
    stored in the console's store and set in the install's environment. If any engine's child
    environment carries one of those values, this fails naming the engine and the variable."""
    from boss_engine.services.agent_env import agent_environment
    from boss_engine.services.harness_registry import available_harnesses

    _store(monkeypatch, ANTHROPIC_API_KEY=KEY, OPENAI_API_KEY=OPENAI_KEY,
           OPENROUTER_API_KEY=ROUTER_KEY)
    for name, value in (("ANTHROPIC_API_KEY", KEY), ("OPENAI_API_KEY", OPENAI_KEY),
                        ("OPENROUTER_API_KEY", ROUTER_KEY), ("XAI_API_KEY", XAI_KEY)):
        monkeypatch.setenv(name, value)
    secrets = {KEY, OPENAI_KEY, ROUTER_KEY, XAI_KEY}
    checked = []
    for entry in available_harnesses():
        try:
            agent = ca._build_code_agent(_settings(), entry["name"])
        except Exception:  # noqa: BLE001 — an engine this install cannot build starts nothing
            continue
        env = _child_env_of(agent)
        if env is None:
            # An engine-owned loop (native/openai/azure/provider lanes) calls the model from
            # BOSS's own process; the commands it runs get the agent allow-list.
            env = agent_environment()
        leaked = sorted(k for k, v in env.items() if str(v) in secrets)
        assert not leaked, f"{entry['name']} child env holds a provider key in {leaked}"
        assert mp.holds_provider_key(env) == [], entry["name"]
        checked.append(entry["name"])
    assert {"claude_code", "claude_agent_sdk", "aider"} <= set(checked)


def test_a_subscription_login_is_not_proxied(upstream, monkeypatch):
    """Not covered, on purpose and in writing: the Claude subscription's login is the CLI's own
    OAuth session, not an API key (MODEL_CONNECTIONS.md)."""
    _store(monkeypatch, CLAUDE_CODE_OAUTH_TOKEN=TOKEN)
    env = ca.ClaudeCodeAgent(_settings())._child_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_BASE_URL" not in env


# ── end to end: a fake `claude` calls the proxy with what it was given ───────────────────────

FAKE_CLI = textwrap.dedent('''\
    #!PYTHON
    import http.client, json, os, sys, urllib.parse
    def out(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    def call_the_model():
        base = urllib.parse.urlparse(os.environ["ANTHROPIC_BASE_URL"])
        conn = http.client.HTTPConnection(base.hostname, base.port, timeout=10)
        conn.request("POST", base.path + "/v1/messages?beta=true", body=b"{}",
                     headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                              "content-type": "application/json"})
        resp = conn.getresponse()
        body = resp.read()
        # What the agent's own commands would see: its whole environment.
        with open("seen.json", "w") as f:
            json.dump({"env": dict(os.environ), "status": resp.status,
                       "body": body.decode()}, f)
    result = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
              "session_id": "s_fake", "duration_ms": 5, "duration_api_ms": 4, "result": "OK",
              "total_cost_usd": 0.001, "usage": {"input_tokens": 12, "output_tokens": 3},
              "modelUsage": {"claude-sonnet-5": {}}}
    if "--input-format" in sys.argv:                 # the Agent SDK's streaming protocol
        for line in sys.stdin:
            msg = json.loads(line)
            if msg.get("type") == "control_request":
                out({"type": "control_response", "response": {
                    "subtype": "success", "request_id": msg["request_id"], "response": {}}})
            elif msg.get("type") == "user":
                call_the_model()
                out(result)
    else:                                            # `claude -p`
        call_the_model()
        out({"type": "system", "subtype": "init", "session_id": "s_fake"})
        out(result)
''')


def _fake_cli(tmp_path):
    cli = tmp_path / "bin" / "claude"
    cli.parent.mkdir()
    cli.write_text(FAKE_CLI.replace("PYTHON", sys.executable))
    cli.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    return cli, work


def _sdk_ready() -> bool:
    from boss_engine.services import agent_sdk_engine

    return agent_sdk_engine.sdk_available()


@pytest.mark.parametrize("engine", ["claude_code", "claude_agent_sdk"])
def test_a_session_reaches_the_model_with_a_key_it_never_held(engine, upstream, monkeypatch,
                                                              tmp_path):
    if engine == "claude_agent_sdk" and not _sdk_ready():
        pytest.skip("claude-agent-sdk not installed")
    stub, proxy = upstream
    calls: list = []
    monkeypatch.setattr(ca, "record_llm_call", lambda **kw: calls.append(kw))
    monkeypatch.setattr(ca, "record_agent_turns", lambda rows, **kw: len(rows))
    _store(monkeypatch, ANTHROPIC_API_KEY=KEY)
    cli, work = _fake_cli(tmp_path)
    settings = _settings(claude_cli_path=str(cli), agent_sdk_cli_path=str(cli),
                         anthropic_credential_order="api_key_first")
    agent = ca._build_code_agent(settings, engine)
    out = agent.run_role("reply OK", work, read_only=True)
    assert out["summary"] == "OK"
    seen = json.loads((work / "seen.json").read_text())
    env = seen["env"]
    assert seen["status"] == 200 and json.loads(seen["body"])["id"] == "msg_1"
    assert KEY not in json.dumps(env)                        # never in the session
    assert env["ANTHROPIC_API_KEY"].startswith(mp.TOKEN_PREFIX)
    assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB" not in env      # no bubblewrap sandbox forced
    (call,) = stub.calls
    assert call["headers"]["x-api-key"] == KEY               # the proxy added it upstream
    # The session is over: its token is dead.
    status, _, _ = _request(proxy, "POST", "/anthropic/v1/messages", body=b"{}",
                            headers={"x-api-key": env["ANTHROPIC_API_KEY"]})
    assert status == 401
    # Metered once, from the CLI's own result — the proxy's tally is a check, not a charge.
    assert len(calls) == 1 and calls[0]["engine_name"] == engine
    grant = proxy.grant_for(env["ANTHROPIC_API_KEY"])
    assert grant.usage["requests"] == 1 and grant.usage["input_tokens"] == 12


def test_the_proxy_never_writes_llm_calls(upstream, monkeypatch):
    """Usage is recorded by the engine from the session's result (llm_calls, one row); the
    proxy only tallies. A proxied request writes nothing to the ledger."""
    from boss_engine.services import llm_telemetry

    written: list = []
    monkeypatch.setattr(llm_telemetry, "record_llm_call", lambda **kw: written.append(kw))
    monkeypatch.setattr(ca, "record_llm_call", lambda **kw: written.append(kw))
    _, proxy = upstream
    issued = _issue(proxy, run_id="crun_meter")
    for _ in range(3):
        _request(proxy, "POST", "/anthropic/v1/messages", body=b"{}",
                 headers={"x-api-key": issued.token})
    assert written == []
    assert proxy.run_usage("crun_meter")["requests"] == 3
