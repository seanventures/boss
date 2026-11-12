"""Procedural priors v1 (BOSSAPP-1031): the scorekeeper for first moves.

What must hold, and is enforced here:

  * the fingerprint is a TOTAL, deterministic function — any input names a situation;
  * build_priors replays agent_turns with the documented progress credit, and a second run
    never double-counts (the per-org high-water mark);
  * the advisory block respects its thresholds, stays top-5, and is framed as observations,
    not instructions; empty priors render nothing;
  * with the knob off the native lane's prompts are BYTE-IDENTICAL to today's (golden);
  * ordered mode reorders the tool schemas stably and never drops one, and the reviewer's
    read-only surface is untouched;
  * example_excerpt can never carry secret or credential material (args-SHAPE only, scrubbed).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from boss_engine.services.code_agent import NativeCodeAgent, _PARALLEL_TOOLS_RULE
from boss_engine.services.code_agent_tools import tool_schemas
from boss_engine.services.knowledge import procedural_priors as pp
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


# ── the fingerprint is total and deterministic ─────────────────────────────────────────────

def test_the_fingerprint_is_total_and_deterministic():
    cases = [
        ("implement", "python", None, False),
        (None, None, None, False),                      # everything missing still names a situation
        ("", "", "", True),
        ("solo", "unknown", "no class in this text at all", False),
        ("Check | Tests ", " PY THON ", "E   AssertionError: boom", True),
        (42, object(), b"TypeError: nope", False),      # hostile types, not just hostile strings
    ]
    for stage, lang, err, wrote in cases:
        first = pp.fingerprint(stage, lang, err, wrote=wrote)
        assert first == pp.fingerprint(stage, lang, err, wrote=wrote)
        version, *rest = first.split("|")
        assert version == "v1" and len(rest) == 3
        assert all(part for part in rest), first


def test_missing_stage_language_and_error_fall_back_to_names_not_blanks():
    assert pp.fingerprint(None, None) == "v1|solo|unknown|exploration"
    assert pp.fingerprint(None, None, wrote=True) == "v1|solo|unknown|building"


def test_the_error_class_rides_the_existing_signature_regex():
    out = "$ python3 -m pytest\n(exit 1, 9ms)\nE   AssertionError: boom"
    assert pp.fingerprint("implement", "python", out) == \
        "v1|implement|python|post_test_failure:assertionerror"
    # No recognisable class: still total, still deterministic.
    key = pp.fingerprint("implement", "python", "something went sideways")
    assert key.startswith("v1|implement|python|post_test_failure:")


def test_a_pipe_in_a_part_cannot_forge_another_field():
    key = pp.fingerprint("imp|lement", "py|thon", None)
    assert key.count("|") == 3


def test_the_situation_state_machine_flips_as_designed():
    tracker = pp.SituationTracker(stage="implement", repo_language="python")
    assert tracker.key.endswith("|exploration")
    tracker.observe_result("grep", "src/x.py:1: hit", ok=True)
    assert tracker.key.endswith("|exploration"), "a read is not a write"
    tracker.observe_result("edit_file", "edited", ok=True)
    assert tracker.key.endswith("|building")
    tracker.observe_result("bash", "$ pytest\n(exit 1, 2ms)\nE   AssertionError: no", ok=True)
    assert tracker.key.endswith("|post_test_failure:assertionerror")
    tracker.observe_result("read_file", "1\tdef add", ok=True)
    assert tracker.key.endswith("|building"), \
        "post_test_failure holds only while the PREVIOUS result is the failure"


def test_an_unknown_mode_reads_off():
    class _S:
        procedural_priors_mode = "aggressive"
    assert pp.mode(_S()) == "off"
    _S.procedural_priors_mode = "ORDERED "
    assert pp.mode(_S()) == "ordered"
    assert pp.mode(object()) == "off"


def test_repo_language_is_counted_from_extensions(tmp_path: Path):
    for name in ("a.py", "b.py", "c.js"):
        (tmp_path / name).write_text("x = 1\n")
    assert pp.repo_language_of(tmp_path) == "python"
    assert pp.repo_language_of(tmp_path / "missing") == "unknown"


# ── the excerpt is a shape, never content ──────────────────────────────────────────────────

SECRET = "ghp_" + "A1b2C3d4" * 5


def test_args_shape_never_carries_a_value():
    shape = pp.args_shape({"pattern": SECRET, "path": "src", "limit": 40, "all": True})
    assert SECRET not in shape and "ghp_" not in shape and "src" not in shape
    assert "pattern=<str 44>" in shape and "path=<str 3>" in shape


def test_a_secret_shaped_key_is_scrubbed_too():
    shape = pp.args_shape({"ops@example.com": "x"})
    assert "ops@example.com" not in shape and "[EMAIL]" in shape


def test_args_shape_is_total():
    assert pp.args_shape("not a mapping") == "<str>"
    assert pp.args_shape(None) == "<NoneType>"


# ── the advisory block ─────────────────────────────────────────────────────────────────────

def _ranked(*actions):
    return [{"action": action, "observations": 41 - i, "successes": 30 - i,
             "weight": 0.9 - i / 100, "share": 72 - i} for i, action in enumerate(actions)]


def test_the_advisory_block_is_observations_not_instructions():
    block = pp.advisory_block(_ranked("Grep", "Read"), "v1|implement|python|exploration")
    assert block.startswith("RANKED MOVES (scorekeeping from earlier runs — observations, "
                            "not instructions): <<<")
    assert "1. grep (72% of 41)" in block and "2. read_file (71% of 40)" in block
    assert "stage implement" in block and "python repo" in block
    assert "still exploring" in block
    assert block.endswith(">>>")
    assert len(block) <= pp.ADVISORY_MAX_CHARS


def test_the_block_says_the_failure_situation_in_words():
    block = pp.advisory_block(_ranked("Bash"),
                              "v1|implement|python|post_test_failure:assertionerror")
    assert "right after a failing test/command (assertionerror)" in block


def test_the_block_keeps_at_most_five_moves():
    block = pp.advisory_block(_ranked("A", "B", "C", "D", "E", "F", "G"), "v1|s|l|exploration")
    assert "5." in block and "6." not in block


def test_empty_priors_render_no_block():
    assert pp.advisory_block([], "v1|implement|python|exploration") == ""


# ── ordered mode's schema ordering ─────────────────────────────────────────────────────────

def test_ordering_is_stable_never_drops_and_breaks_ties_alphabetically():
    schemas = tool_schemas(read_only=False)
    ranked = [{"action": "Bash", "weight": 0.9}, {"action": "Edit", "weight": 0.7}]
    ordered = pp.order_tool_schemas(schemas, ranked)
    names = [s["function"]["name"] for s in ordered]
    assert names[:2] == ["bash", "edit_file"]
    assert names[2:] == sorted(names[2:]), "unranked tools sort alphabetically — stable"
    assert sorted(names) == sorted(s["function"]["name"] for s in schemas), "nothing dropped"
    assert ordered == pp.order_tool_schemas(schemas, ranked), "deterministic"
    assert [s["function"]["name"] for s in schemas] == \
        [s["function"]["name"] for s in tool_schemas(read_only=False)], "input untouched"


def test_equal_weights_fall_back_to_the_alphabet():
    schemas = tool_schemas(read_only=False)
    ranked = [{"action": "Bash", "weight": 0.5}, {"action": "Grep", "weight": 0.5}]
    names = [s["function"]["name"] for s in pp.order_tool_schemas(schemas, ranked)]
    assert names[:2] == ["bash", "grep"]


# ── the native loop: off is byte-identical; advisory and ordered do what they say ──────────

class _ScriptedLLM:
    """Replays a fixed list of assistant messages, recording what it was asked."""

    def __init__(self, turns: list[dict]) -> None:
        self._turns = list(turns)
        self.calls: list[dict] = []

    def create_chat_completion(self, messages, tools=None, tool_choice="auto",
                               model_override=None, redact=False):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": list(tools or [])})
        if not self._turns:
            return {"content": "done", "tool_calls": None, "raw": None}, {"model": "test"}
        return self._turns.pop(0), {"model": "test"}


def _tool_turn(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"content": None,
            "tool_calls": [{"id": call_id, "name": name, "arguments": json.dumps(args)}],
            "raw": None}


def _settings(**overrides):
    """By ALIAS: Settings ignores unknown field names silently (see the native-lane tests)."""
    from boss_engine.config import Settings

    return Settings(BOSS_CODE_AGENT_CONTEXT_FILES=0, **overrides)


def _agent(repo: Path, llm, **overrides) -> NativeCodeAgent:
    agent = NativeCodeAgent.__new__(NativeCodeAgent)
    agent.settings = _settings(**overrides)
    agent._llm = llm
    return agent


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("BOSS_CAPTURE_AGENT_TURNS", "0")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    return tmp_path


def _golden_messages(prompt: str) -> list[dict]:
    """What today's loop sends, built from the same constants the loop uses."""
    return [
        {"role": "system", "content": NativeCodeAgent._TOOL_SYS_EDIT + _PARALLEL_TOOLS_RULE},
        {"role": "user", "content": f"{prompt}\n\n"},
    ]


def test_with_the_knob_off_prompts_are_byte_identical(repo: Path, monkeypatch):
    def _never(*args, **kwargs):  # priors must not even be LOOKED UP when off
        raise AssertionError("runtime_ranked must not be called with the knob off")
    monkeypatch.setattr(pp, "runtime_ranked", _never)
    llm = _ScriptedLLM([_tool_turn("grep", {"pattern": "def add"})])
    out = _agent(repo, llm).run_role("Improve add()", repo)
    assert out["stop_reason"] == "done"
    assert llm.calls[0]["messages"] == _golden_messages("Improve add()")
    assert [t["function"]["name"] for t in llm.calls[0]["tools"]] == \
        [t["function"]["name"] for t in tool_schemas(read_only=False)]
    assert all(m["role"] != "user" for m in llm.calls[-1]["messages"][2:]), \
        "no advisory user message is ever injected while the knob is off"


def test_advisory_mode_appends_the_block_to_the_end_of_the_user_message(repo: Path, monkeypatch):
    monkeypatch.setattr(pp, "runtime_ranked", lambda *a, **k: _ranked("Grep", "Read"))
    llm = _ScriptedLLM([])
    _agent(repo, llm, BOSS_PROCEDURAL_PRIORS="advisory").run_role("Improve add()", repo)
    golden = _golden_messages("Improve add()")
    system, user = llm.calls[0]["messages"][:2]
    assert system == golden[0], "the shared prefix (the system prompt) never changes"
    assert user["content"].startswith(golden[1]["content"])
    assert "RANKED MOVES" in user["content"] and user["content"].rstrip().endswith(">>>")
    assert [t["function"]["name"] for t in llm.calls[0]["tools"]] == \
        [t["function"]["name"] for t in tool_schemas(read_only=False)], \
        "advisory mode never reorders the tools"


def test_a_situation_flip_injects_once_and_only_once(repo: Path, monkeypatch):
    asked: list[str] = []

    def _fake(settings, *, project_id, fingerprint_key, db_engine=None):
        asked.append(fingerprint_key)
        return _ranked("Grep", "Edit")
    monkeypatch.setattr(pp, "runtime_ranked", _fake)
    llm = _ScriptedLLM([
        _tool_turn("grep", {"pattern": "def add"}),
        _tool_turn("edit_file", {"path": "src/calc.py", "old_string": "a + b",
                                 "new_string": "a + b  # ok"}, "c2"),
        _tool_turn("grep", {"pattern": "def sub"}, "c3"),
    ])
    _agent(repo, llm, BOSS_PROCEDURAL_PRIORS="advisory").run_role("Improve add()", repo)
    users = [m for m in llm.calls[-1]["messages"] if m["role"] == "user"]
    assert len(users) == 2, "one flip (exploration -> building) injects exactly one message"
    assert "RANKED MOVES" in users[1]["content"]
    assert asked == ["v1|solo|python|exploration", "v1|solo|python|building"]


def test_ordered_mode_reorders_the_schema_and_drops_nothing(repo: Path, monkeypatch):
    monkeypatch.setattr(pp, "runtime_ranked",
                        lambda *a, **k: [{"action": "Bash", "weight": 0.9, "observations": 10,
                                          "successes": 8, "share": 80}])
    llm = _ScriptedLLM([])
    _agent(repo, llm, BOSS_PROCEDURAL_PRIORS="ordered").run_role("Improve add()", repo)
    names = [t["function"]["name"] for t in llm.calls[0]["tools"]]
    assert names[0] == "bash"
    assert sorted(names) == sorted(t["function"]["name"] for t in tool_schemas(read_only=False))


def test_the_reviewers_read_only_surface_is_untouched_even_when_ordered(repo: Path, monkeypatch):
    def _never(*args, **kwargs):
        raise AssertionError("a read-only step must never consult priors")
    monkeypatch.setattr(pp, "runtime_ranked", _never)
    llm = _ScriptedLLM([])
    _agent(repo, llm, BOSS_PROCEDURAL_PRIORS="ordered").run_role(
        "Review add()", repo, read_only=True)
    assert [t["function"]["name"] for t in llm.calls[0]["tools"]] == \
        [t["function"]["name"] for t in tool_schemas(read_only=True)]
    assert all("RANKED MOVES" not in str(m.get("content")) for m in llm.calls[0]["messages"])


# ── build_priors on a seeded database ──────────────────────────────────────────────────────

ORG, WS, PROJECT = "org_pp", "ws_pp", "proj_pp"
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def pg_engine():
    url = os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(marker in name for marker in ("test", "testing", "ci", "scratch", "tmp")):
        pytest.skip("BOSS_KG_TEST_DATABASE_URL must name a disposable database")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"disposable postgres is not reachable: {exc.__class__.__name__}")
    from boss_engine.db import ensure_schema

    ensure_schema(engine)
    return engine


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(f"INSERT INTO orgs (id, name) VALUES ('{ORG}', 'PP')"))
            conn.execute(text(
                f"INSERT INTO workspaces (id, org_id, name) VALUES ('{WS}', '{ORG}', 'PP')"))
            conn.execute(text(
                f"INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                f"('{PROJECT}', '{WS}', 'PP', 'pp')"))
            yield conn
        finally:
            transaction.rollback()


def _run(conn, run_id: str, status: str) -> None:
    conn.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, title) "
        "VALUES (:id, :p, :status, :id)"), {"id": run_id, "p": PROJECT, "status": status})


def _turn(conn, run_id: str, seconds: int, direction: str, tool: str, content, *,
          ok: bool = True, stage: str = "implement") -> None:
    conn.execute(text(
        """
        INSERT INTO agent_turns (id, project_id, code_task_run_id, stage, direction,
                                 tool_name, content, ok, turn_index, created_at)
        VALUES (:id, :p, :r, :stage, :d, :tool, :content, :ok, :i,
                CAST(:epoch AS TIMESTAMPTZ) + make_interval(secs => :sec))
        """),
        {"id": uuid.uuid4().hex, "p": PROJECT, "r": run_id, "stage": stage, "d": direction,
         "tool": tool, "content": content if isinstance(content, str) else json.dumps(content),
         "ok": ok, "i": seconds, "sec": seconds, "epoch": EPOCH.isoformat()})


def _seed_progressive_episode(conn) -> None:
    """(a) grep -> read -> edit -> failing test -> passing test, run completed."""
    _run(conn, "run_a", "completed")
    _turn(conn, "run_a", 1, "tool_call", "grep", {"pattern": "def add", "path": "src"})
    _turn(conn, "run_a", 2, "tool_result", "grep", "src/calc.py:1: def add")
    _turn(conn, "run_a", 3, "tool_call", "read_file", {"path": "src/calc.py"})
    _turn(conn, "run_a", 4, "tool_result", "read_file", "1\tdef add(a, b):")
    _turn(conn, "run_a", 5, "tool_call", "edit_file",
          {"path": "src/calc.py", "old_string": "a + b", "new_string": "a - b"})
    _turn(conn, "run_a", 6, "tool_result", "edit_file", "edited src/calc.py")
    _turn(conn, "run_a", 7, "tool_call", "bash", {"command": "python3 -m pytest"})
    _turn(conn, "run_a", 8, "tool_result", "bash",
          "$ python3 -m pytest\n(exit 1, 90ms)\nE   AssertionError: boom")
    _turn(conn, "run_a", 9, "tool_call", "bash", {"command": "python3 -m pytest"})
    _turn(conn, "run_a", 10, "tool_result", "bash", "$ python3 -m pytest\n(exit 0, 80ms)\n2 passed")


def _priors(conn) -> dict:
    rows = conn.execute(text(
        "SELECT fingerprint_key, action, success_count, failure_count, example_run_id, "
        "example_excerpt FROM kg_action_priors WHERE org_id = :o"), {"o": ORG}).mappings().all()
    return {(r["fingerprint_key"], r["action"]): dict(r) for r in rows}


def test_a_progressive_episode_is_credited_as_designed(connection):
    _seed_progressive_episode(connection)
    result = pp.build_priors(connection, org_id=ORG)
    assert result["observations"] == 5 and result["failures"] == 0
    priors = _priors(connection)
    explore = "v1|implement|python|exploration"
    assert priors[(explore, "Grep")]["success_count"] == 1
    assert priors[(explore, "Read")]["success_count"] == 1
    assert priors[(explore, "Edit")]["success_count"] == 1
    assert priors[("v1|implement|python|building", "Bash")]["success_count"] == 1, \
        "the failing test run still led to progress: the next run passed"
    assert priors[("v1|implement|python|post_test_failure:assertionerror", "Bash")][
        "success_count"] == 1
    assert priors[(explore, "Grep")]["example_run_id"] == "run_a"
    assert priors[(explore, "Grep")]["example_excerpt"] == "path=<str 3>, pattern=<str 7>"


def test_flailing_greps_with_no_progress_score_failures(connection):
    """(b) the llama3.1 shape: greps leading nowhere in a run whose gates failed."""
    _run(connection, "run_b", "failed")
    for i, pattern in enumerate(("absurd1", "absurd2", "absurd3")):
        _turn(connection, "run_b", 2 * i + 1, "tool_call", "grep", {"pattern": pattern})
        _turn(connection, "run_b", 2 * i + 2, "tool_result", "grep", "(no matches)")
    pp.build_priors(connection, org_id=ORG)
    row = _priors(connection)[("v1|implement|unknown|exploration", "Grep")]
    assert row["failure_count"] == 3 and row["success_count"] == 0


def test_a_second_run_does_not_double_count(connection):
    """(c) idempotence: the high-water mark makes the refold a no-op."""
    _seed_progressive_episode(connection)
    first = pp.build_priors(connection, org_id=ORG)
    assert first["observations"] == 5
    before = _priors(connection)
    second = pp.build_priors(connection, org_id=ORG)
    assert second["observations"] == 0
    assert _priors(connection) == before
    # An explicit --since older than the mark is a deliberate re-fold and DOES count again.
    refold = pp.build_priors(connection, org_id=ORG, since=EPOCH - timedelta(days=1))
    assert refold["observations"] == 5


def test_no_secret_can_enter_the_example_excerpt(connection):
    _run(connection, "run_s", "completed")
    _turn(connection, "run_s", 1, "tool_call", "grep", {"pattern": SECRET, "path": "src/x.py"})
    _turn(connection, "run_s", 2, "tool_result", "grep", "(no matches)")
    pp.build_priors(connection, org_id=ORG)
    excerpts = connection.execute(text(
        "SELECT example_excerpt FROM kg_action_priors WHERE org_id = :o"), {"o": ORG}
    ).scalars().all()
    for excerpt in excerpts:
        assert SECRET not in (excerpt or "") and "ghp_" not in (excerpt or "")


def test_ranked_actions_respect_the_thresholds(connection):
    def _seed(action, s, f, seen):
        connection.execute(text(
            """
            INSERT INTO kg_action_priors (id, org_id, fingerprint_key, action,
                                          success_count, failure_count, last_seen_at)
            VALUES (:id, :o, 'v1|implement|python|exploration', :a, :s, :f, :seen)
            """), {"id": uuid.uuid4().hex, "o": ORG, "a": action, "s": s, "f": f, "seen": seen})
    _seed("Grep", 30, 11, NOW)             # strong and fresh: kept
    _seed("Read", 11, 9, NOW)              # kept: usefulness 12/22
    _seed("Glob", 4, 0, NOW)               # 4 observations < 5: dropped
    _seed("Bash", 2, 18, NOW)              # usefulness 3/22 < 0.45: dropped
    _seed("LS", 30, 1, NOW - timedelta(days=540))  # decayed by the 180-day half-life: dropped
    ranked = pp.ranked_actions(connection, org_id=ORG,
                               fingerprint_key="v1|implement|python|exploration", now=NOW)
    assert [item["action"] for item in ranked] == ["Grep", "Read"]
    assert ranked[0]["share"] == 73 and ranked[0]["observations"] == 41


def test_runtime_ranked_never_raises(connection):
    """The loop-facing door returns [] on any failure — a prior never fails the work."""
    assert pp.runtime_ranked(_settings(), project_id="no_such_project",
                             fingerprint_key="v1|solo|unknown|exploration",
                             db_engine=None) == []
