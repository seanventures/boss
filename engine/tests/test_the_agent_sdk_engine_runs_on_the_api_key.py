"""The claude_agent_sdk engine: Claude Agent SDK sessions, billed to the Anthropic API key.

The operator's promotional credits cover Agent SDK / API usage, not Claude Code. This engine
runs the same agent loop as claude_code through the official SDK on the console's Anthropic API
key, behind the same engine interface. These pin, with the SDK stubbed (no API call anywhere):

  * what a session is started with — cwd, tools, read-only permission mode for review seats,
    max turns, model (default claude-sonnet-5), resume and its fallback;
  * the message stream -> agent_turns rows, and the result -> llm_calls usage/cost, metered;
  * the credential: the API key only (never the login) — and never the key itself in the
    session: a model-proxy run token and the proxy's base URL, no CLI sandbox forced (no
    CLAUDE_CODE_SUBPROCESS_ENV_SCRUB), redacted, never logged;
  * selection (install-wide and per job) and readiness;
  * and, as plumbing, one real session through the SDK and BOSS's owned transport against a
    fake `claude` binary that speaks the stream-json protocol.
"""
from __future__ import annotations

import json
import logging
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from boss_engine.config import Settings
from boss_engine.services import agent_sdk_engine as sdk
from boss_engine.services import code_agent as ca

TOKEN = "sk-ant-oat01-" + "t" * 48          # shaped like a login; not a real one
KEY = "sk-ant-api03-" + "k" * 48            # shaped like an API key; not a real one

needs_sdk = pytest.mark.skipif(not sdk.sdk_available(), reason="claude-agent-sdk not installed")


# ── SDK message doubles (the converter reads them by type name, as it reads the SDK's) ───────

@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: Optional[bool] = None


@dataclass
class AssistantMessage:
    content: list
    model: str = "claude-sonnet-5"
    usage: Optional[dict] = None
    session_id: Optional[str] = None


@dataclass
class UserMessage:
    content: Any


@dataclass
class SystemMessage:
    subtype: str = "init"
    data: dict = field(default_factory=dict)


@dataclass
class ResultMessage:
    subtype: str = "success"
    duration_ms: int = 10
    duration_api_ms: int = 9
    is_error: bool = False
    num_turns: int = 3
    session_id: str = "sess_1"
    total_cost_usd: Optional[float] = 0.0123
    usage: Optional[dict] = None
    result: Optional[str] = "Changed app.py to fix the bug."
    model_usage: Optional[dict] = None
    api_error_status: Optional[int] = None
    errors: Optional[list] = None
    stop_reason: Optional[str] = None


def _session(result: Optional[ResultMessage] = None) -> list:
    return [
        SystemMessage(),
        AssistantMessage([TextBlock("Reading the file."),
                          ToolUseBlock("tu_1", "Read", {"file_path": "app.py"})]),
        UserMessage([ToolResultBlock("tu_1", "print('hi')")]),
        AssistantMessage([ToolUseBlock("tu_2", "Edit", {"file_path": "app.py"}),
                          ToolUseBlock("tu_3", "web_search", {"query": "x"})]),
        UserMessage([ToolResultBlock("tu_2", "ok"), ToolResultBlock("tu_3", "boom", True)]),
        result or ResultMessage(
            usage={"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 1000,
                   "cache_creation_input_tokens": 200},
            model_usage={"claude-sonnet-5": {"inputTokens": 100, "outputTokens": 50,
                                             "costUSD": 0.0123}}),
    ]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                 "BOSS_ANTHROPIC_CREDENTIAL_ORDER", "BOSS_CODE_AGENT_ENGINE"):
        monkeypatch.delenv(name, raising=False)


def _store(monkeypatch, **held):
    def lookup(settings, name, *, strict=False, include_env=True):
        return held.get(name)
    monkeypatch.setattr("boss_engine.services.cli_harness.stored_credential", lookup)


def _settings(**over) -> Settings:
    return Settings().model_copy(update={"claude_code_oauth_token": "",
                                         "anthropic_api_key": None, **over})


@pytest.fixture()
def recorded(monkeypatch):
    """The two recorders the engine reports through, captured instead of written."""
    got: dict[str, list] = {"turns": [], "calls": [], "prompts": []}

    def turns(rows, *, engine_name, model=None, stage=None, prompt=None):
        if prompt is not None:
            got["prompts"].append((engine_name, prompt))
        got["turns"].extend((engine_name, r) for r in rows)
        return len(rows)

    def call(**kw):
        got["calls"].append(kw)

    monkeypatch.setattr(ca, "record_agent_turns", turns)
    monkeypatch.setattr(ca, "record_llm_call", call)
    return got


@pytest.fixture()
def fake_sdk(monkeypatch):
    """`agent_sdk_engine._query` replaced: records each session's options, replays messages."""
    sessions: list[dict] = []
    script: dict[str, Any] = {"messages": _session(), "raise": []}

    def query(*, prompt, options, transport=None):
        sessions.append({"prompt": prompt, "options": options, "transport": transport})

        async def gen():
            if script["raise"]:
                raise script["raise"].pop(0)
            for m in script["messages"]:
                yield m
        return gen()

    monkeypatch.setattr(sdk, "_query", query)
    return sessions, script


def _agent(settings=None):
    return sdk.agent_class()(settings or _settings())


# ── what a session is started with ──────────────────────────────────────────────────────────

@needs_sdk
def test_a_review_seat_is_read_only_on_the_default_model(monkeypatch, tmp_path, fake_sdk,
                                                           recorded):
    _store(monkeypatch, ANTHROPIC_API_KEY=KEY)
    sessions, _ = fake_sdk
    out = _agent().run_role("Review this change.", tmp_path, read_only=True)
    opts = sessions[0]["options"]
    assert opts.cwd == str(tmp_path)
    assert opts.permission_mode == "default"                 # no auto-accepted edits
    assert opts.allowed_tools == ["Read", "Grep", "Glob", "Bash"]
    assert not {"Edit", "Write"} & set(opts.allowed_tools)
    s = _settings()
    assert opts.max_turns == min(int(s.code_agent_max_turns), int(s.code_agent_review_max_turns))
    assert opts.model == "claude-sonnet-5"
    assert opts.resume is None
    assert opts.system_prompt["preset"] == "claude_code"
    assert "Review this change." in sessions[0]["prompt"]
    assert out["session_id"] == "sess_1" and out["turns"] == 3
    assert out["summary"] == "Changed app.py to fix the bug."


@needs_sdk
def test_a_writing_session_accepts_edits_on_the_chosen_model_and_resumes(monkeypatch, tmp_path,
                                                                          fake_sdk, recorded):
    _store(monkeypatch, ANTHROPIC_API_KEY=KEY)
    sessions, _ = fake_sdk
    settings = _settings(agent_sdk_model="claude-opus-5")
    _agent(settings).run_role("Implement it.", tmp_path, max_turns=7)
    _agent(settings).run_role("Fix the failing test.", tmp_path, model="claude-haiku-5",
                              resume_session_id="sess_1")
    first, second = sessions[0]["options"], sessions[1]["options"]
    assert first.permission_mode == "acceptEdits" and first.max_turns == 7
    assert first.model == "claude-opus-5"
    assert "Edit" in first.allowed_tools and "Write" in first.allowed_tools
    assert second.resume == "sess_1" and second.model == "claude-haiku-5"
    # Every session goes through BOSS's owned transport, never the SDK's own spawn.
    assert all(s["transport"] is not None for s in sessions)


@needs_sdk
def test_a_session_that_cannot_be_resumed_starts_fresh(monkeypatch, tmp_path, fake_sdk,
                                                        recorded):
    _store(monkeypatch, ANTHROPIC_API_KEY=KEY)
    sessions, script = fake_sdk
    script["raise"] = [RuntimeError("No conversation found with session ID: sess_gone")]
    out = _agent().run_role("Fix it.", tmp_path, resume_session_id="sess_gone")
    assert [s["options"].resume for s in sessions] == ["sess_gone", None]
    assert out["session_id"] == "sess_1"


# ── the stream -> turns, the result -> llm_calls ─────────────────────────────────────────────

@needs_sdk
def test_the_message_stream_becomes_the_common_turn_schema(monkeypatch, tmp_path, fake_sdk,
                                                           recorded):
    _store(monkeypatch, ANTHROPIC_API_KEY=KEY)
    _agent().run_role("Implement it.", tmp_path)
    rows = [r for engine, r in recorded["turns"]]
    assert {engine for engine, _ in recorded["turns"]} == {"claude_agent_sdk"}
    shape = [(r["direction"], r.get("tool_name", "")) for r in rows]
    assert shape == [("completion", ""), ("tool_call", "Read"), ("tool_result", "Read"),
                     ("tool_call", "Edit"), ("tool_call", "WebSearch"),
                     ("tool_result", "Edit"), ("tool_result", "WebSearch")]
    assert [r["ok"] for r in rows if r["direction"] == "tool_result"] == [True, True, False]
    assert recorded["prompts"] and recorded["prompts"][0][1] == "Implement it."


@needs_sdk
def test_usage_and_cost_are_recorded_as_metered_api_spend(monkeypatch, tmp_path, fake_sdk,
                                                          recorded):
    _store(monkeypatch, CLAUDE_CODE_OAUTH_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY)
    out = _agent().run_role("Implement it.", tmp_path)
    (call,) = recorded["calls"]
    usage = call["usage"]
    assert call["engine_name"] == "claude_agent_sdk"
    assert call["cost_usd_micros"] == 12300 and call["turns"] == 3
    assert usage["model"] == "claude-sonnet-5"
    assert (usage["prompt_tokens"], usage["completion_tokens"], usage["cache_read_tokens"],
            usage["cache_write_tokens"]) == (100, 50, 1000, 200)
    assert usage["billing"] == "metered" and usage["source_id"] == "anthropic_api"
    from boss_engine.services.llm_telemetry import engine_call_is_included

    assert engine_call_is_included("claude_agent_sdk", usage) is False
    assert engine_call_is_included("claude_agent_sdk", {}) is False      # not UNMETERED
    assert out["usage"]["billing"] == "metered"


@needs_sdk
def test_out_of_turns_is_a_partial_attempt_and_a_refusal_is_an_error(monkeypatch, tmp_path,
                                                                     fake_sdk, recorded):
    _store(monkeypatch, ANTHROPIC_API_KEY=KEY)
    _, script = fake_sdk
    script["messages"] = [ResultMessage(subtype="error_max_turns", is_error=True, result=None)]
    out = _agent().run_role("Implement it.", tmp_path)
    assert "ran out of turns" in out["summary"] and out["session_id"] == "sess_1"
    script["messages"] = [ResultMessage(
        is_error=True, api_error_status=400,
        result="Your credit balance is too low to access the Anthropic API.")]
    with pytest.raises(RuntimeError, match="HTTP 400.*credit balance"):
        _agent().run_role("Implement it.", tmp_path)
    assert recorded["calls"][-1]["ok"] is False


def test_the_converter_maps_every_message_kind():
    assert sdk.message_to_event(SystemMessage()) is None
    ev = sdk.message_to_event(UserMessage("plain"))
    assert ev["message"]["content"] == [{"type": "text", "text": "plain"}]
    res = sdk.message_to_event(ResultMessage(model_usage={"m": {}}))
    assert res["type"] == "result" and res["modelUsage"] == {"m": {}}
    assert sdk.normalise_tool_name("MultiEdit") == "Edit"
    assert sdk.normalise_tool_name("Bash") == "Bash"


@needs_sdk
def test_the_real_sdk_types_map_the_same_way():
    from claude_agent_sdk import AssistantMessage as A, TextBlock as T, ToolUseBlock as U

    ev = sdk.message_to_event(A(content=[T("hi"), U("1", "Write", {"file_path": "a"})],
                                model="claude-sonnet-5"))
    assert [b["type"] for b in ev["message"]["content"]] == ["text", "tool_use"]
    assert ev["message"]["content"][1]["name"] == "Write"


# ── the credential ───────────────────────────────────────────────────────────────────────────

@needs_sdk
def test_only_the_api_key_reaches_the_child_never_the_login(monkeypatch, tmp_path, fake_sdk,
                                                            recorded, caplog):
    caplog.set_level(logging.DEBUG)
    _store(monkeypatch, CLAUDE_CODE_OAUTH_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)            # inherited, too
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod")
    sessions, _ = fake_sdk
    _agent().run_role("Implement it.", tmp_path)
    env = sessions[0]["options"].env
    assert env["ANTHROPIC_API_KEY"].startswith("boss_mdl_")         # a run token, never the key
    assert KEY not in env.values()
    assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "DATABASE_URL" not in env                                # the agent allow-list
    assert sdk.SCRUB_ENV not in env                                 # no bubblewrap sandbox
    # The session is over: its token no longer works.
    from boss_engine.services import model_proxy

    grant = model_proxy.get().grant_for(env["ANTHROPIC_API_KEY"])
    assert grant is not None and grant.revoked and grant.credential == ""
    assert KEY not in caplog.text and KEY not in repr(sessions[0]["options"].__dict__.get(
        "model"))
    from boss_engine.identity.redaction import redact_text

    assert KEY not in redact_text(f"leaked {KEY}")


def test_no_stored_key_refuses_even_with_a_working_login(monkeypatch, tmp_path):
    _store(monkeypatch, CLAUDE_CODE_OAUTH_TOKEN=TOKEN)
    with pytest.raises(sdk.NoAnthropicKey, match="Anthropic API key"):
        _agent()._child_env()
    from boss_engine.services.claude_credential import resolve_anthropic_api_key

    assert resolve_anthropic_api_key(_settings()).kind == "none"
    # A settings object bound to the LOGIN is not a key either.
    bound = _settings(claude_credential_kind="subscription", claude_code_oauth_token=TOKEN)
    assert resolve_anthropic_api_key(bound).kind == "none"


def test_the_api_key_resolution_ignores_the_subscription_first_order(monkeypatch):
    from boss_engine.services.claude_credential import resolve_anthropic_api_key

    _store(monkeypatch, CLAUDE_CODE_OAUTH_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY)
    got = resolve_anthropic_api_key(_settings())
    assert (got.kind, got.billing) == ("stored_key", "metered")
    assert KEY not in repr(got)
    _store(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    assert resolve_anthropic_api_key(_settings()).kind == "env_key"


# ── selection and readiness ──────────────────────────────────────────────────────────────────

def test_selectable_install_wide_and_by_name(monkeypatch):
    from boss_engine.services.harness_registry import available_harnesses

    entry = next(h for h in available_harnesses() if h["name"] == "claude_agent_sdk")
    assert entry["verified"] is False                     # Auto never picks it unasked
    monkeypatch.setenv("BOSS_CODE_AGENT_ENGINE", "claude_agent_sdk")
    settings = Settings()
    assert settings.code_agent_engine == "claude_agent_sdk"
    agent = ca._build_code_agent(settings)
    assert agent.engine == "claude_agent_sdk"
    assert ca._build_code_agent(Settings(), "claude_agent_sdk").engine == "claude_agent_sdk"
    from boss_engine.services.model_rate_limiter import provider_of

    assert provider_of("claude_agent_sdk") == "anthropic"


def _key_row(conn, *, source_id="anthropic_api", ref="sr_1"):
    from sqlalchemy import text

    conn.execute(text(
        "INSERT INTO entitlement_sources (project_id, source_id, provider, connection_status, "
        "credential_secret_ref_id, updated_at, cost_mode, observed_capacity, "
        "connection_metadata, supported_models, created_at) VALUES ('p', :s, 'anthropic', "
        "'connected', :r, :t, 'metered', '{}', '{}', '[]', :t)"),
        {"s": source_id, "r": ref, "t": datetime.now(timezone.utc)})
    conn.execute(text(
        "INSERT INTO secret_refs (id, project_id, provider, status, updated_at, ref_key) "
        "VALUES (:r, 'p', 'anthropic', 'active', :t, 'k')"),
        {"r": ref, "t": datetime.now(timezone.utc)})


def test_ready_and_offered_per_job_when_a_key_is_stored(monkeypatch, entitlement_tables):
    from sqlalchemy import create_engine

    from boss_engine.services import setup_service

    monkeypatch.setattr(sdk, "sdk_available", lambda: True)
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        entitlement_tables(conn)
        assert sdk.readiness(conn, _settings(), "p")["ready"] is False
        assert "claude_agent_sdk" not in setup_service.connected_engines(conn, _settings(), "p")
        _key_row(conn)
        state = sdk.readiness(conn, _settings(), "p")
        assert state == {"engine": "claude_agent_sdk", "ready": True,
                         "why": "Anthropic API key stored"}
        found = setup_service.connected_engines(conn, _settings(), "p")["claude_agent_sdk"]
        assert (found["kind"], found["source_id"]) == ("metered", "anthropic_api")
        # A Build/Review/Chat pin on it is honoured (model_routing.resolve_job).
        from boss_engine.services import model_routing

        monkeypatch.setattr(model_routing, "_chosen",
                            lambda c, p, key: "claude_agent_sdk" if key in (
                                "code_agent_engine", "code_review_engine", "chat_llm_engine")
                            else None)
        for job in ("build", "review", "chat"):
            picked = model_routing.resolve_job(conn, _settings(), "p", job)
            assert (picked.engine, picked.metered) == ("claude_agent_sdk", True), job
    monkeypatch.setattr(sdk, "sdk_available", lambda: False)
    assert sdk.readiness(None, _settings(), "p")["ready"] is False


def test_the_jobs_picker_lists_it_beside_the_anthropic_key(monkeypatch):
    from boss_engine.services import setup_view

    monkeypatch.setattr(sdk, "sdk_available", lambda: True)
    key = {"provider": "anthropic", "kind": "metered", "label": "Anthropic",
           "source_id": "anthropic_api", "model": None,
           "pill": {"state": "working"}}
    monkeypatch.setattr(setup_view, "model_working", lambda e: True)
    by_engine: dict = {"claude_code": key}
    setup_view._offer_agent_sdk(_settings(), [key], by_engine)
    option = setup_view._option("claude_agent_sdk", by_engine["claude_agent_sdk"]["model"],
                                by_engine["claude_agent_sdk"])
    assert option == {"engine": "claude_agent_sdk", "model": "claude-sonnet-5",
                      "label": "Claude Agent SDK · claude-sonnet-5", "metered": True}
    # No key, no option.
    empty: dict = {}
    setup_view._offer_agent_sdk(_settings(), [], empty)
    assert empty == {}


# ── plumbing: one real SDK session through the owned transport, against a fake CLI ──────────

FAKE_CLI = textwrap.dedent('''\
    #!PYTHON
    import json, os, sys
    def out(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    with open("record.json", "w") as f:
        json.dump({"argv": sys.argv[1:],
                   "key": os.environ.get("ANTHROPIC_API_KEY", ""),
                   "base": os.environ.get("ANTHROPIC_BASE_URL", ""),
                   "has_login": "CLAUDE_CODE_OAUTH_TOKEN" in os.environ,
                   "has_db": "DATABASE_URL" in os.environ,
                   "scrub": os.environ.get("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"),
                   "entry": os.environ.get("CLAUDE_CODE_ENTRYPOINT")}, f)
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("type") == "control_request":
            out({"type": "control_response", "response": {
                "subtype": "success", "request_id": msg["request_id"], "response": {}}})
        elif msg.get("type") == "user":
            out({"type": "system", "subtype": "init", "session_id": "s_fake", "data": {}})
            out({"type": "assistant", "session_id": "s_fake", "message": {
                "role": "assistant", "model": "claude-sonnet-5", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash",
                     "input": {"command": "ls"}}]}})
            out({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "a.py"}]}})
            out({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
                 "session_id": "s_fake", "duration_ms": 5, "duration_api_ms": 4,
                 "result": "OK", "total_cost_usd": 0.001,
                 "usage": {"input_tokens": 10, "output_tokens": 2},
                 "modelUsage": {"claude-sonnet-5": {}}})
''')


@needs_sdk
def test_a_session_runs_end_to_end_through_the_owned_transport(monkeypatch, tmp_path, recorded):
    cli = tmp_path / "bin" / "claude"
    cli.parent.mkdir()
    cli.write_text(FAKE_CLI.replace("PYTHON", sys.executable))
    cli.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    _store(monkeypatch, CLAUDE_CODE_OAUTH_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod")
    agent = _agent(_settings(agent_sdk_cli_path=str(cli)))
    out = agent.run_role("reply OK", work, read_only=True)
    assert out["summary"] == "OK" and out["session_id"] == "s_fake"
    seen = json.loads((work / "record.json").read_text())
    assert seen["key"].startswith("boss_mdl_") and seen["key"] != KEY
    assert seen["base"].startswith("http://127.0.0.1:")
    assert not seen["has_login"] and not seen["has_db"]
    assert seen["scrub"] is None and seen["entry"] == "sdk-py"
    argv = seen["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert "--input-format" in argv
    assert [(r["direction"], r.get("tool_name")) for _, r in recorded["turns"]] == [
        ("tool_call", "Bash"), ("tool_result", "Bash")]
    assert recorded["calls"][0]["usage"]["billing"] == "metered"
