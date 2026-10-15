"""Every engine keeps the same contract (services/engine_contract.py), asserted the same way.

BOSS's behaviour must be identical on every engine; only quality and speed may vary. These tests
hold each adapter — Claude Code, Codex, a generic CLI with no read-only mode (Aider), the native
tool loop an OpenRouter/Groq lane runs, and the scripted conformance engine — to ONE set of
assertions, with the engine's I/O stubbed (a fake CLI binary, a fake chat API). No model is called.

The same assertions, per engine:
  1. a writing session returns every contract key, a session id, and leaves its file;
  2. its transcript is in the common schema: a prompt row, a completion, and BOSS's tool names
     only (never write_file / apply_patch / shell);
  3. a continued session either resumes natively or is a new session seeded with what the first
     one was asked and reported — never a cold prompt with no context;
  4. a reviewer cannot change the build's tree, whether the engine has a read-only mode or not;
  5. the ledger has a row for every session, under the engine's name; the model is never the
     engine's name;
  6. a turn cap reads as a partial attempt to the pipeline, whatever the engine said.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from boss_engine.config import get_settings
from boss_engine.services import engine_contract as ec
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context

FIRST_PROMPT = "TASK: create hello.txt saying hi (first session marker 7f3a)"
FOLLOW_UP = "Now fix what the reviewer found."

# ── The ledger, in memory ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def ledger():
    db = create_engine("sqlite://", poolclass=StaticPool,
                       connect_args={"check_same_thread": False})
    with db.begin() as c:
        c.execute(text(
            "CREATE TABLE agent_turns (id TEXT, project_id TEXT, code_task_run_id TEXT, stage TEXT,"
            " role_key TEXT, engine TEXT, model TEXT, turn_index INTEGER, direction TEXT,"
            " tool_name TEXT, content TEXT, content_bytes INTEGER, truncated BOOLEAN,"
            " tokens INTEGER, latency_ms INTEGER, level TEXT, ok BOOLEAN, error TEXT,"
            " act_id TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"))
        c.execute(text(
            "CREATE TABLE llm_calls (id TEXT, project_id TEXT, code_task_run_id TEXT, stage TEXT,"
            " engine TEXT, model TEXT, role TEXT, prompt_tokens INTEGER,"
            " completion_tokens INTEGER, total_tokens INTEGER, cache_read_tokens INTEGER,"
            " cache_write_tokens INTEGER, cost_usd_micros INTEGER, price_estimated BOOLEAN,"
            " latency_ms INTEGER, retries INTEGER, turns INTEGER, ok BOOLEAN, error TEXT,"
            " included_in_subscription BOOLEAN, act_id TEXT, route_id TEXT,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"))
    set_llm_context(db_engine=db, project_id="p_contract", stage="implement")
    yield db
    clear_llm_context()


def _rows(db, sql: str, **params) -> list[dict]:
    with db.begin() as c:
        return [dict(r) for r in c.execute(text(sql), params).mappings()]


# ── The engines, with stubbed I/O ─────────────────────────────────────────────────────────────

FAKE_CLAUDE = textwrap.dedent('''\
    #!{python}
    import json, os, sys, uuid
    argv = sys.argv[1:]
    stdin = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    prompt = argv[argv.index("--") + 1] if "--" in argv else stdin
    with open({log!r}, "a") as log:
        log.write(json.dumps({{"argv": argv, "prompt": prompt}}) + "\\n")
    allowed = argv[argv.index("--allowedTools") + 1] if "--allowedTools" in argv else ""
    sid = argv[argv.index("--resume") + 1] if "--resume" in argv else str(uuid.uuid4())
    def out(o): print(json.dumps(o), flush=True)
    out({{"type": "system", "subtype": "init", "session_id": sid}})
    if "Write" in allowed or "Edit" in allowed:
        open("hello.txt", "w").write("hi")
        out({{"type": "assistant", "message": {{"model": "claude-x", "content": [
            {{"type": "tool_use", "id": "t1", "name": "Write",
              "input": {{"file_path": "hello.txt", "content": "hi"}}}}]}}}})
        out({{"type": "user", "message": {{"content": [
            {{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}}]}}}})
    out({{"type": "assistant", "message": {{"model": "claude-x", "content": [
        {{"type": "text", "text": "Wrote hello.txt. Reviewed hello.txt."}}]}}}})
    out({{"type": "result", "subtype": "success", "result": "Wrote hello.txt.",
          "session_id": sid, "num_turns": 2, "total_cost_usd": 0.01,
          "usage": {{"input_tokens": 10, "output_tokens": 5}}}})
''')

FAKE_CODEX = textwrap.dedent('''\
    #!{python}
    import json, os, sys, uuid
    argv = sys.argv[1:]
    if argv[:2] == ["login", "status"]:
        print("Logged in using ChatGPT"); sys.exit(0)
    prompt = sys.stdin.read()
    with open({log!r}, "a") as log:
        log.write(json.dumps({{"argv": argv, "prompt": prompt}}) + "\\n")
    resume = argv[1] == "resume" if len(argv) > 1 else False
    tid = argv[-2] if resume else str(uuid.uuid4())
    mode = argv[argv.index("--sandbox") + 1] if "--sandbox" in argv else next(
        (a.split("=", 1)[1].strip('"') for a in argv if a.startswith("sandbox_mode=")), "")
    final = argv[argv.index("--output-last-message") + 1]
    def out(o): print(json.dumps(o), flush=True)
    out({{"type": "thread.started", "thread_id": tid}})
    out({{"type": "turn.started"}})
    out({{"type": "item.completed", "item": {{"id": "i0", "type": "command_execution",
          "command": "ls", "aggregated_output": "README", "exit_code": 0,
          "status": "completed"}}}})
    if mode != "read-only":
        open("hello.txt", "w").write("hi")
        out({{"type": "item.completed", "item": {{"id": "i1", "type": "file_change",
              "changes": [{{"path": "hello.txt", "kind": "add"}}], "status": "completed"}}}})
    out({{"type": "item.completed", "item": {{"id": "i2", "type": "agent_message",
          "text": "Wrote hello.txt. Reviewed hello.txt."}}}})
    out({{"type": "turn.completed", "usage": {{"input_tokens": 100,
          "cached_input_tokens": 40, "output_tokens": 20}}}})
    open(final, "w").write("Wrote hello.txt.")
''')

FAKE_AIDER = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    argv = sys.argv[1:]
    prompt = argv[argv.index("--message") + 1] if "--message" in argv else ""
    with open({log!r}, "a") as log:
        log.write(json.dumps({{"argv": argv, "prompt": prompt}}) + "\\n")
    # Aider has NO read-only mode: it writes whatever it is asked.
    open("hello.txt", "w").write("hi")
    print("Wrote hello.txt. Reviewed hello.txt.")
''')


def _bin(dirpath: Path, name: str, body: str) -> Path:
    """A fake CLI. Its log path is written into it: an agent's environment is deny-by-default
    (agent_env), so nothing the test exports reaches the child."""
    path = dirpath / name
    path.write_text(body.format(python=sys.executable, log=str(dirpath / "fake.log")))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class _FakeChat:
    """An OpenAI-compatible chat API that asks for one write_file, then answers."""

    def __init__(self):
        self.prompts: list[str] = []

    def create_chat_completion(self, messages, tools=None, model_override=None, redact=False):
        if not any(m.get("role") == "tool" for m in messages):
            self.prompts.append(str(messages[-1].get("content") or ""))
            return ({"content": "", "tool_calls": [
                {"id": "c1", "name": "write_file",
                 "arguments": json.dumps({"path": "hello.txt", "content": "hi"})}]},
                    {"model": "fake/model", "prompt_tokens": 10, "completion_tokens": 5})
        return ({"content": "Wrote hello.txt. Reviewed hello.txt.", "tool_calls": None},
                {"model": "fake/model", "prompt_tokens": 12, "completion_tokens": 6})


def _claude(tmp_path, monkeypatch):
    from boss_engine.services.code_agent import ClaudeCodeAgent

    fake = _bin(tmp_path, "claude", FAKE_CLAUDE)
    settings = get_settings().model_copy(update={"claude_cli_path": str(fake),
                                                 "claude_code_oauth_token": "test-only"})
    return ClaudeCodeAgent(settings), (lambda: _log(tmp_path))


def _codex(tmp_path, monkeypatch):
    from boss_engine.services.cli_harness import load_specs
    from boss_engine.services.codex_code_agent import CodexCodeAgent

    _bin(tmp_path, "codex", FAKE_CODEX)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    agent = CodexCodeAgent(get_settings(), load_specs(get_settings())["codex"])
    agent._auth_checked = True            # the credential route has its own tests
    monkeypatch.setattr(agent, "remember_rotated_credential", lambda: False)
    return agent, (lambda: _log(tmp_path))


def _aider(tmp_path, monkeypatch):
    from boss_engine.services.cli_harness import GenericCliCodeAgent, load_specs

    _bin(tmp_path, "aider", FAKE_AIDER)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    agent = GenericCliCodeAgent(get_settings(), load_specs(get_settings())["aider"])
    monkeypatch.setattr(agent, "remember_rotated_credential", lambda: False)
    return agent, (lambda: _log(tmp_path))


def _native(tmp_path, monkeypatch):
    from boss_engine.services.code_agent import NativeCodeAgent

    settings = get_settings().model_copy(update={"openai_api_key": "sk-test-only",
                                                 "code_agent_context_files": 0})
    agent = NativeCodeAgent(settings)
    agent.engine = "openrouter"            # what an OpenRouter/Groq lane builds
    chat = _FakeChat()
    agent._llm = chat
    return agent, (lambda: [{"prompt": p, "argv": []} for p in chat.prompts])


def _scripted(tmp_path, monkeypatch):
    from boss_engine.services import scripted_engine

    scripted_engine.reset()
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "hello.txt").write_text("hi")
    agent = scripted_engine.ScriptedEngine(get_settings(), faults=set(), caps=set(),
                                           fixture=fixture, app_dir="")
    return agent, (lambda: [{"prompt": c["prompt_head"], "argv": []}
                            for c in scripted_engine.run_state(None).calls])


ENGINES = {"claude_code": _claude, "codex": _codex, "aider": _aider, "openrouter": _native,
           "scripted": _scripted}


def _log(tmp_path: Path) -> list[dict]:
    path = tmp_path / "fake.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture(params=sorted(ENGINES))
def engine(request, tmp_path, monkeypatch, ledger):
    monkeypatch.setenv("BOSS_CODE_AGENT_IDLE_TIMEOUT_SECONDS", "0")
    raw, log = ENGINES[request.param](tmp_path, monkeypatch)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "README.md").write_text("# a repo\n")
    return request.param, ec.contracted(raw), log, tree, ledger


# ── The one set of assertions ────────────────────────────────────────────────────────────────

def test_a_writing_session_keeps_the_contract(engine):
    name, agent, _log_, tree, db = engine
    r = agent.run_role(FIRST_PROMPT, tree)
    missing = [k for k in ec.RESULT_KEYS if k not in r]
    assert not missing, (name, missing)
    assert r["session_id"], name
    assert r["summary"].strip(), name
    assert r["stop_reason"] in ec.STOP_REASONS, name
    assert r["model"].lower() != r["engine"].lower(), name
    assert (tree / "hello.txt").read_text() == "hi", name


def test_its_transcript_is_in_the_common_schema(engine):
    name, agent, _log_, tree, db = engine
    agent.run_role(FIRST_PROMPT, tree)
    rows = _rows(db, "SELECT direction, tool_name FROM agent_turns")
    directions = {r["direction"] for r in rows}
    assert {"prompt", "completion"} <= directions, (name, directions)
    names = {r["tool_name"] for r in rows if r["tool_name"]}
    assert all(n in ec.CANONICAL_TOOLS for n in names), (name, names)
    if agent.capabilities.streams_turns:
        assert names & ec.WRITE_TOOLS, (name, names)
    calls = _rows(db, "SELECT engine, ok FROM llm_calls")
    assert calls and all(c["engine"] == agent.engine for c in calls), (name, calls)


def test_a_continued_session_carries_its_context(engine):
    name, agent, log, tree, db = engine
    first = agent.run_role(FIRST_PROMPT, tree)
    second = agent.run_role(FOLLOW_UP, tree, resume_session_id=first["session_id"])
    assert second["session_id"], name
    last = log()[-1]
    if agent.capabilities.resume:
        native = first["session_id"]
        assert native in last["argv"], (name, last["argv"])
    else:
        assert "CONTINUING AN EARLIER SESSION" in last["prompt"], name
        assert "first session marker 7f3a" in last["prompt"], name
        assert FOLLOW_UP in last["prompt"], name
    # A third turn continues the chain, not just the last link.
    agent.run_role("And once more.", tree, resume_session_id=second["session_id"])
    if not agent.capabilities.resume:
        assert "first session marker 7f3a" in log()[-1]["prompt"], name


def test_a_reviewer_cannot_change_the_build(engine):
    name, agent, _log_, tree, db = engine
    before = _tree_hash(tree)
    r = agent.run_role("Review the change and give a verdict.", tree, read_only=True)
    assert r["summary"].strip(), name
    assert _tree_hash(tree) == before, f"{name}: the reviewer changed the build's tree"


def test_a_turn_cap_is_a_partial_attempt_on_every_engine():
    from boss_engine.services.governed_code_task_service import check_is_inconclusive

    for engine_name in ENGINES:
        r = ec.normalise_result({"summary": "did some work", "stop_reason": "turn_cap"},
                                engine=engine_name)
        assert check_is_inconclusive(r["summary"]), engine_name


# ── The boundary itself ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,canonical", [
    ("write_file", "Write"), ("edit_file", "Edit"), ("apply_patch", "Edit"), ("shell", "Bash"),
    ("bash", "Bash"), ("read_file", "Read"), ("list_dir", "LS"), ("grep", "Grep"),
    ("Task", "Agent"), ("Write", "Write"), ("mcp__docs__search", "mcp__docs__search"), ("", "")])
def test_tool_names_are_normalised_at_the_boundary(raw, canonical):
    assert ec.canonical_tool_name(raw) == canonical


def test_the_profile_reads_a_native_session_like_a_claude_one():
    from boss_engine.services.build_profile import profile_from_rows

    native = [ec.normalise_turn(t) for t in (
        {"stage": "implement", "direction": "prompt", "turn_index": -1, "content": "do it"},
        {"stage": "implement", "direction": "tool_call", "tool_name": "write_file",
         "turn_index": 0, "content": json.dumps({"path": "a.js", "content": "x" * 380}),
         "content_bytes": 400},
        {"stage": "implement", "direction": "tool_result", "tool_name": "write_file",
         "turn_index": 1, "content": "ok"},
        {"stage": "implement", "direction": "tool_call", "tool_name": "shell", "turn_index": 2,
         "content": {"cmd": ["npm", "test"]}, "content_bytes": 40},
    )]
    stage = profile_from_rows(native, [])["stages"]["implement"]
    assert stage["tool_calls"] == 2
    assert stage["written_bytes"] == 400            # write_file counted as a Write
    assert stage["bash_tokens"] > 0                 # shell counted as Bash
    assert native[3]["content"]["command"] == "npm test"


def test_the_wrapper_passes_attribute_writes_to_the_engine():
    """The pipeline sets `redact` and `command_runner` on the agent it holds."""
    from boss_engine.services.code_agent import PacedCodeAgent

    class Inner:
        engine = "x"

    inner = Inner()
    held = PacedCodeAgent(ec.contracted(inner), limiter=object())
    held.redact = True
    held.command_runner = "runner"
    assert inner.redact is True and inner.command_runner == "runner"


def test_every_registered_engine_declares_its_capabilities():
    from boss_engine.services.harness_registry import _REGISTRY, _ensure_builtins
    from boss_engine.services.cli_harness import GenericCliCodeAgent
    from boss_engine.services.code_agent import ClaudeCodeAgent, NativeCodeAgent
    from boss_engine.services.codex_code_agent import CodexCodeAgent
    from boss_engine.services.scripted_engine import ScriptedEngine

    _ensure_builtins()
    assert "scripted" in _REGISTRY
    for cls in (ClaudeCodeAgent, NativeCodeAgent, CodexCodeAgent, ScriptedEngine):
        assert "capabilities" in dir(cls) or cls is ScriptedEngine, cls
    assert isinstance(ClaudeCodeAgent.capabilities, ec.EngineCapabilities)
    assert ClaudeCodeAgent.capabilities.resume and CodexCodeAgent.capabilities.resume
    assert not NativeCodeAgent.capabilities.resume
    assert isinstance(GenericCliCodeAgent.capabilities, property)


def test_the_conformance_engine_is_never_an_operators_choice(monkeypatch):
    from boss_engine.services.harness_registry import available_harnesses, build_harness

    monkeypatch.delenv("BOSS_SCRIPTED_ENGINE", raising=False)
    assert "scripted" not in {h["name"] for h in available_harnesses()}
    with pytest.raises(RuntimeError, match="conformance"):
        build_harness("scripted", get_settings())


def test_a_wrapper_built_without_init_does_not_recurse():
    """copy.copy / pickle build the object without __init__: `_inner` is missing, and a
    delegating __getattr__ must say so instead of recursing until the stack overflows."""
    import copy

    from boss_engine.services.code_agent import PacedCodeAgent

    class Inner:
        engine = "x"

    for wrapper in (ec.contracted(Inner()), PacedCodeAgent(Inner(), limiter=object())):
        clone = copy.copy(wrapper)
        assert clone.engine == "x"
        bare = type(wrapper).__new__(type(wrapper))
        with pytest.raises(AttributeError):
            bare.anything


def test_the_agent_sdk_engine_keeps_claude_codes_capabilities():
    """The Agent SDK engine (BOSSAPP-537) resumes, reviews read-only, reports cost and adds the
    working rules itself; held to the contract it must not be treated as the poorest engine
    (its sessions discarded, its reviews run on a copy, its cost called unreported)."""
    from boss_engine.services import agent_sdk_engine as sdk
    from boss_engine.services.code_agent import ClaudeCodeAgent

    cls = sdk.build_agent_class()
    assert issubclass(cls, ClaudeCodeAgent)
    assert cls.capabilities == ClaudeCodeAgent.capabilities
    assert cls.adds_working_rules is True
