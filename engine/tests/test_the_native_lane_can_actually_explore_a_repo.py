"""The engine-native coding agent must be an agent, not one completion.

Before BOSS-407 the native lane was a single chat completion that returned whole-file
JSON rewrites. On the VAG bake-off suite that looks fine, because every task there is
"create one new file" — the one shape where having no tools costs nothing. On a real
repository it is the difference between finding the right function and rewriting a file
you have seen 40% of.

Two properties matter more than the rest and are tested first:

- `test_a_review_step_is_never_handed_a_way_to_write` — "the reviewer cannot edit" is a
  governance invariant of the governed pipeline. With a tool loop it has to be a property
  of the tool SCHEMA, not a sentence in a prompt a model may ignore.
- `test_a_path_outside_the_repo_is_refused` — the agent's whole world is the sandbox. A
  path that escapes it is the same class of failure as the 2026-09-02 data loss.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from boss_engine.services.code_agent import (
    NativeCodeAgent,
    _looks_like_tools_unsupported,
    load_repo_guide,
)
from boss_engine.services.code_agent_tools import SandboxToolset, ToolError, tool_schemas


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
    )
    (tmp_path / "src" / "util.py").write_text("MARKER = 'findme'\n")
    (tmp_path / "README.md").write_text("# demo\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.py").write_text("MARKER = 'should not be walked'\n")
    return tmp_path


# --- the two invariants -------------------------------------------------------

def test_a_review_step_is_never_handed_a_way_to_write(repo: Path) -> None:
    names = {t["function"]["name"] for t in tool_schemas(read_only=True)}
    assert names == {"read_file", "list_dir", "glob", "grep"}
    assert not names & {"write_file", "edit_file", "bash"}

    ts = SandboxToolset(repo, read_only=True)
    for tool, args in (
        ("write_file", {"path": "x.py", "content": "x"}),
        ("edit_file", {"path": "src/calc.py", "old_string": "a + b", "new_string": "a * b"}),
        ("bash", {"command": "python3 -c pass"}),
    ):
        out = ts.call(tool, args)
        assert out.is_error, f"{tool} must be refused in a read-only step"
        assert "read-only" in out.text
    assert (repo / "src" / "calc.py").read_text().count("a + b") == 1


@pytest.mark.parametrize(
    "bad",
    ["/etc/passwd", "../outside.txt", "src/../../outside.txt", "~/secrets"],
)
def test_a_path_outside_the_repo_is_refused(repo: Path, bad: str) -> None:
    ts = SandboxToolset(repo)
    out = ts.call("read_file", {"path": bad})
    assert out.is_error
    assert "absolute" in out.text or "outside the repo" in out.text


def test_a_symlink_is_not_a_way_out(repo: Path, tmp_path: Path) -> None:
    secret = tmp_path.parent / "escape-target.txt"
    secret.write_text("secret")
    link = repo / "link.txt"
    try:
        link.symlink_to(secret)
    except OSError:  # pragma: no cover - platforms without symlink permission
        pytest.skip("symlinks unavailable")
    out = SandboxToolset(repo).call("read_file", {"path": "link.txt"})
    assert out.is_error and "outside the repository root" in out.text


# --- the tools do what they say ----------------------------------------------

def test_grep_finds_code_and_skips_vendored_trees(repo: Path) -> None:
    out = SandboxToolset(repo).call("grep", {"pattern": "MARKER"})
    assert not out.is_error
    assert "src/util.py:1:" in out.text
    assert "node_modules" not in out.text


def test_glob_matches_by_path_and_by_name(repo: Path) -> None:
    ts = SandboxToolset(repo)
    assert "src/calc.py" in ts.call("glob", {"pattern": "src/*.py"}).text
    assert "src/calc.py" in ts.call("glob", {"pattern": "*.py"}).text


def test_read_file_is_line_numbered_and_windowed(repo: Path) -> None:
    out = SandboxToolset(repo).call("read_file", {"path": "src/calc.py", "offset": 4, "limit": 2})
    assert not out.is_error
    assert "showing 4-5" in out.text
    assert "1\tdef add" not in out.text


def test_edit_file_refuses_an_ambiguous_match_instead_of_guessing(repo: Path) -> None:
    (repo / "dup.py").write_text("x = 1\nx = 1\n")
    ts = SandboxToolset(repo)
    out = ts.call("edit_file", {"path": "dup.py", "old_string": "x = 1", "new_string": "x = 2"})
    assert out.is_error and "appears 2 times" in out.text
    assert (repo / "dup.py").read_text() == "x = 1\nx = 1\n"

    ok = ts.call("edit_file", {"path": "dup.py", "old_string": "x = 1",
                               "new_string": "x = 2", "replace_all": True})
    assert not ok.is_error
    assert (repo / "dup.py").read_text() == "x = 2\nx = 2\n"


def test_edit_file_says_what_to_do_when_the_text_is_not_there(repo: Path) -> None:
    out = SandboxToolset(repo).call(
        "edit_file", {"path": "src/calc.py", "old_string": "nope", "new_string": "x"})
    assert out.is_error and "Read the file again" in out.text


def test_written_paths_are_recorded_for_the_gate(repo: Path) -> None:
    ts = SandboxToolset(repo)
    ts.call("write_file", {"path": "new.py", "content": "y = 1\n"})
    ts.call("edit_file", {"path": "src/calc.py", "old_string": "a + b", "new_string": "a + b  # noted"})
    assert sorted(set(ts.written_paths)) == ["new.py", "src/calc.py"]


# --- bash shares ONE allowlist with the rest of BOSS --------------------------

def test_bash_refuses_a_command_outside_the_sandbox_allowlist(repo: Path) -> None:
    ts = SandboxToolset(repo, settings=_settings(BOSS_SANDBOX_VALIDATION_ALLOWED_COMMANDS="python3"))
    assert ts.call("bash", {"command": "curl https://example.com"}).is_error
    assert not ts.call("bash", {"command": "python3 --version"}).is_error


def test_bash_refuses_shell_syntax_rather_than_running_half_of_it(repo: Path) -> None:
    ts = SandboxToolset(repo, settings=_settings(BOSS_SANDBOX_VALIDATION_ALLOWED_COMMANDS="python3"))
    out = ts.call("bash", {"command": "python3 --version && rm -rf /"})
    assert out.is_error and "WITHOUT a shell" in out.text


def test_bash_uses_the_injected_runner_when_one_is_given(repo: Path) -> None:
    seen: list[str] = []

    class _Result:
        stdout, stderr, exit_code = "ok", "", 0

    def runner(cmd, timeout=None):
        seen.append(cmd)
        return _Result()

    ts = SandboxToolset(repo, command_runner=runner)
    out = ts.call("bash", {"command": "npm test"})
    assert seen == ["npm test"]
    assert "exit 0" in out.text


# --- AGENTS.md ingestion (BOSS-408) ------------------------------------------

def test_agents_md_is_read_from_the_target_repo(repo: Path) -> None:
    (repo / "AGENTS.md").write_text("Always use tabs.")
    guide = load_repo_guide(repo)
    assert guide is not None and "Always use tabs." in guide


def test_claude_md_is_read_when_there_is_no_agents_md(repo: Path) -> None:
    (repo / "CLAUDE.md").write_text("House style: no tabs.")
    assert "House style: no tabs." in (load_repo_guide(repo) or "")


def test_agents_md_wins_over_claude_md_and_only_one_is_sent(repo: Path) -> None:
    (repo / "AGENTS.md").write_text("PRIMARY")
    (repo / "CLAUDE.md").write_text("SECONDARY")
    guide = load_repo_guide(repo) or ""
    assert "PRIMARY" in guide and "SECONDARY" not in guide


def test_a_repo_with_no_guide_is_not_an_error(repo: Path) -> None:
    assert load_repo_guide(repo) is None


def test_an_enormous_guide_is_capped(repo: Path) -> None:
    (repo / "AGENTS.md").write_text("x" * 50_000)
    guide = load_repo_guide(repo, max_chars=1_000) or ""
    assert len(guide) < 1_200 and "truncated" in guide


# --- the loop ----------------------------------------------------------------

class _ScriptedLLM:
    """Replays a fixed list of assistant messages, recording what it was asked."""

    def __init__(self, turns: list[dict]) -> None:
        self._turns = list(turns)
        self.calls: list[dict] = []

    def create_chat_completion(self, messages, tools=None, tool_choice="auto",
                               model_override=None, redact=False):
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._turns:
            return {"content": "done", "tool_calls": None, "raw": None}, {"model": "test"}
        return self._turns.pop(0), {"model": "test"}


def _tool_turn(name: str, args: dict, call_id: str = "c1") -> dict:
    return {
        "content": None,
        "tool_calls": [{"id": call_id, "name": name, "arguments": json.dumps(args)}],
        "raw": None,
    }


def _settings(**overrides):
    """Build Settings by ALIAS.

    `Settings` sets `extra="ignore"` and does not set `populate_by_name`, so
    `Settings(native_agent_max_turns=3)` is silently dropped and you get the default —
    a trap worth naming here, because a test that thinks it configured something and
    did not is a test that proves nothing.
    """
    from boss_engine.config import Settings

    return Settings(BOSS_CODE_AGENT_CONTEXT_FILES=0, **overrides)


def _agent(repo: Path, llm, **overrides) -> NativeCodeAgent:
    agent = NativeCodeAgent.__new__(NativeCodeAgent)
    agent.settings = _settings(**overrides)
    agent._llm = llm
    return agent


def test_one_run_role_call_is_a_session_not_a_single_completion(repo: Path) -> None:
    llm = _ScriptedLLM([
        _tool_turn("grep", {"pattern": "def add"}),
        _tool_turn("edit_file", {"path": "src/calc.py", "old_string": "a + b",
                                 "new_string": "a + b  # checked"}, "c2"),
        {"content": "Adjusted add().", "tool_calls": None, "raw": None},
    ])
    out = _agent(repo, llm).run_role("Improve add()", repo)

    assert out["turns"] == 3, "the loop must keep going while the model calls tools"
    assert out["tool_calls"] == 2
    assert out["summary"] == "Adjusted add()."
    assert out["files_touched"] == ["src/calc.py"]
    assert "# checked" in (repo / "src" / "calc.py").read_text()


def test_a_failed_tool_call_is_the_next_turn_not_a_dead_end(repo: Path) -> None:
    llm = _ScriptedLLM([
        _tool_turn("read_file", {"path": "../escape"}),
        {"content": "Could not escape; reported instead.", "tool_calls": None, "raw": None},
    ])
    out = _agent(repo, llm).run_role("Try to read outside", repo)
    assert out["turns"] == 2
    tool_msgs = [m for m in llm.calls[-1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "error:" in tool_msgs[0]["content"]


def test_the_turn_cap_stops_a_looping_model_and_says_so(repo: Path) -> None:
    llm = _ScriptedLLM([_tool_turn("list_dir", {"path": "."}) for _ in range(50)])
    out = _agent(repo, llm, BOSS_NATIVE_AGENT_MAX_TURNS=3).run_role("spin", repo)
    assert out["turns"] == 3
    assert "3-turn cap" in out["summary"]


def test_a_review_run_is_offered_only_read_tools_by_the_loop(repo: Path) -> None:
    llm = _ScriptedLLM([{"content": "SURVIVED", "tool_calls": None, "raw": None}])
    _agent(repo, llm).run_role("Review this", repo, read_only=True)
    offered = {t["function"]["name"] for t in llm.calls[0]["tools"]}
    assert offered == {"read_file", "list_dir", "glob", "grep"}


def test_the_repo_guide_reaches_the_system_prompt(repo: Path) -> None:
    (repo / "AGENTS.md").write_text("NEVER use print().")
    llm = _ScriptedLLM([{"content": "ok", "tool_calls": None, "raw": None}])
    _agent(repo, llm).run_role("do a thing", repo)
    system = llm.calls[0]["messages"][0]["content"]
    assert "NEVER use print()." in system


# --- degrading to the legacy path, visibly ------------------------------------

def test_an_endpoint_without_function_calling_falls_back_to_the_single_shot_path(
    repo: Path, caplog
) -> None:
    class _NoTools:
        def __init__(self) -> None:
            self.calls = 0

        def create_chat_completion(self, messages, tools=None, **kw):
            self.calls += 1
            if tools:
                raise RuntimeError("400 invalid request: 'tools' is unsupported")
            return ({"content": json.dumps({"summary": "did it", "files": []}),
                     "tool_calls": None, "raw": None}, {"model": "test"})

    llm = _NoTools()
    out = _agent(repo, llm).run_role("do a thing", repo)
    assert out["summary"] == "did it"
    assert out["turns"] == 1
    assert llm.calls == 2, "one failed tool attempt, then the legacy call"
    assert any("falling back" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "message",
    ["429 rate limit exceeded", "503 service unavailable", "connection reset",
     "request timed out", "502 bad gateway"],
)
def test_a_transient_failure_is_never_mistaken_for_a_missing_capability(message: str) -> None:
    assert not _looks_like_tools_unsupported(RuntimeError(message))


@pytest.mark.parametrize(
    "message",
    ["400 invalid request: 'tools' is unsupported",
     "unrecognized request argument supplied: tools",
     "this model does not support function calling"],
)
def test_a_real_capability_gap_is_recognised(message: str) -> None:
    assert _looks_like_tools_unsupported(RuntimeError(message))


def test_tools_can_be_switched_off_entirely(repo: Path) -> None:
    llm = _ScriptedLLM([{"content": json.dumps({"summary": "legacy", "files": []}),
                         "tool_calls": None, "raw": None}])
    out = _agent(repo, llm, BOSS_NATIVE_AGENT_TOOLS_ENABLED=False).run_role("do a thing", repo)
    assert out["summary"] == "legacy"
    assert llm.calls[0]["tools"] is None
