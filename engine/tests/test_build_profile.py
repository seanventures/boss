"""Every build says where its time and tokens went (services/build_profile.py).

A 120-minute HelpDesk build had to be explained by hand with SQL: ~85% of implement was the
model producing tokens, mostly hidden thinking, not tools running. These pin that arithmetic on
a synthetic transcript, the event a terminal run emits, and the tool time the stream parser now
records on each tool_result row.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services.agent_turns import StreamParser, parse_stream
from boss_engine.services.build_profile import (
    EVENT_TYPE,
    emit_build_profile,
    format_profile,
    profile_from_rows,
)

T0 = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def turn(sec, idx, direction, tool=None, size=0, ok=True, content="", stage="implement",
         act="act_impl"):
    return {"stage": stage, "act_id": act, "turn_index": idx, "direction": direction,
            "tool_name": tool, "content_bytes": size, "ok": ok, "content": content,
            "latency_ms": 0, "created_at": at(sec)}


def read(path):
    return json.dumps({"file_path": path})


# Session A: 4 model steps, 4 tools (one failing), the same file read twice.
SESSION_A = [
    turn(0, -1, "prompt", size=5000),
    turn(10, 0, "tool_call", "Read", 30, content=read("/w/a.py")),        # model 10s
    turn(12, 1, "tool_result", "Read", 900),                              # tool 2s
    turn(20, 2, "tool_call", "Write", 380, content=read("/w/b.py")),      # model 8s
    turn(21, 3, "tool_result", "Write", 40),                              # tool 1s
    turn(30, 4, "completion", size=76, content="Now I check it."),        # model 9s
    turn(31, 5, "tool_call", "Read", 30, content=read("/w/a.py")),        # same step; re-read
    turn(31.5, 6, "tool_result", "Read", 900),                            # tool 0.5s
    turn(40, 7, "tool_call", "Bash", 190, content=json.dumps({"command": "npm test"})),  # 8.5s
    turn(50, 8, "tool_result", "Bash", 30, ok=False,
         content="<tool_use_error>Exit code 1\nboom</tool_use_error>"),   # tool 10s
]
# Session B, interleaved with A in the same act (a parallel part): 2 steps, one 60s tool.
SESSION_B = [
    turn(1, -1, "prompt", size=5000),
    turn(5, 0, "tool_call", "Bash", 190,
         content=json.dumps({"command": "npx playwright install chromium"})),  # model 4s
    turn(65, 1, "tool_result", "Bash", 500),                              # tool 60s
    turn(70, 2, "completion", size=38, content="Installed."),             # model 5s
]
PULSE = turn(15, 0, "completion", size=30, content="Thinking… 30s so far")
# Session A written a second time, as one batch (one transaction, one created_at).
DUPLICATE = [{**r, "created_at": at(100)} for r in SESSION_A]
REVIEW = [turn(200, -1, "prompt", stage="review", act="act_rev", size=100),
          turn(230, 0, "completion", stage="review", act="act_rev", size=380, content="LGTM")]
CALLS = [
    {"stage": "implement", "completion_tokens": 1000, "turns": 5, "created_at": at(70)},
    {"stage": "review", "completion_tokens": 200, "turns": 1, "created_at": at(230)},
]
RUN = {"id": "crun_x", "status": "failed", "phase": "implement",
       "created_at": at(-30), "updated_at": at(240)}


def _profile():
    rows = SESSION_A + SESSION_B + [PULSE] + DUPLICATE + REVIEW
    return profile_from_rows(sorted(rows, key=lambda r: r["created_at"]), CALLS, RUN)


def test_the_arithmetic_of_a_stage():
    impl = _profile()["stages"]["implement"]

    assert impl["sessions"] == 2                      # the batch duplicate is not a third
    assert impl["duplicate_rows"] == len(SESSION_A)
    assert impl["model_min"] == round((10 + 8 + 9 + 8.5 + 4 + 5) / 60, 2)
    assert impl["tool_min"] == round((2 + 1 + 0.5 + 10 + 60) / 60, 2)
    assert impl["steps"] == 4 + 2                     # the pulse is not a step
    assert impl["llm_turns"] == 5
    assert impl["output_tokens"] == 1000
    assert impl["written_bytes"] == 380 and impl["written_tokens"] == 100
    assert impl["bash_tokens"] == 100                 # 380 bytes of commands
    assert impl["narration_tokens"] == 30             # 114 bytes; the pulse excluded
    assert impl["think_tokens"] == 770 and impl["think_share"] == 0.77
    assert impl["failed_tool_calls"] == 1
    assert impl["top_errors"] == [{"error": "Bash: Exit code 1: boom", "count": 1}]
    assert impl["slowest_tools"][0] == {"tool": "Bash: npx playwright install chromium",
                                        "seconds": 60.0}
    assert [t["seconds"] for t in impl["slowest_tools"]] == [60.0, 10.0, 2.0, 1.0, 0.5]
    assert impl["reads"] == 2 and impl["rereads"] == 1 and impl["files_reread"] == 1
    assert impl["wall_min"] == round(70 / 60, 2)   # the duplicate's late rows ignored


def test_run_totals_and_serial_gaps():
    p = _profile()
    assert p["stage_order"] == ["implement", "review"]
    assert p["stages"]["review"]["model_min"] == 0.5
    t = p["totals"]
    assert t["output_tokens"] == 1200
    assert t["model_min"] == round(round(44.5 / 60, 2) + 0.5, 2)
    assert t["steps"] == 7
    assert t["wall_min"] == 4.5                       # the run's own 270 s
    assert p["serial_gaps"] == [
        {"after": "run start", "before": "implement", "minutes": 0.5},
        {"after": "implement", "before": "review", "minutes": round(130 / 60, 2)},
        {"after": "review", "before": "run end", "minutes": round(10 / 60, 2)},
    ]
    assert "implement" in format_profile(p) and "Serial gaps" in format_profile(p)


def test_a_recorded_tool_latency_wins_over_timestamps():
    rows = [turn(0, -1, "prompt"), turn(1, 0, "tool_call", "Bash", 10),
            {**turn(1.1, 1, "tool_result", "Bash"), "latency_ms": 42_000}]
    assert profile_from_rows(rows, [])["stages"]["implement"]["tool_min"] == 0.7


# ── The stream parser records how long each tool ran ─────────────────────────────────────────

def _lines():
    use = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "ls"}}]}}
    res = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "x"}]}}
    return json.dumps(use), json.dumps(res)


def test_a_live_tool_result_records_how_long_the_tool_ran():
    now = iter([100.0, 103.25])
    parser = StreamParser(clock=lambda: next(now))
    use, res = _lines()
    parser.feed(use)
    result = parser.feed(res)[0]
    assert result["direction"] == "tool_result" and result["latency_ms"] == 3250


def test_a_batch_parse_after_the_fact_records_no_latency():
    _, turns = parse_stream("\n".join(_lines()))
    assert turns[-1]["latency_ms"] == 0


# ── A terminal run emits its profile ─────────────────────────────────────────────────────────

def _seed(eng, status: str) -> str:
    from ._obs_seed import project

    run_id = f"crun_{uuid4().hex[:20]}"
    with eng.begin() as c:
        pid = project(c, "p_build_profile")
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status) "
                       "VALUES (:r, :p, 'profile me', :s)"), {"r": run_id, "p": pid, "s": status})
        for r in SESSION_A + SESSION_B:
            c.execute(text(
                "INSERT INTO agent_turns (id, project_id, code_task_run_id, stage, turn_index, "
                "direction, tool_name, content, content_bytes, ok, created_at) VALUES "
                "(:id, :p, :run, :stage, :turn_index, :direction, :tool_name, :content, "
                ":content_bytes, :ok, :created_at)"),
                {**r, "id": f"turn_{uuid4().hex[:20]}", "p": pid, "run": run_id})
        c.execute(text("INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, "
                       "completion_tokens, turns) VALUES (:id, :p, :r, 'implement', 1000, 5)"),
                  {"id": f"llmc_{uuid4().hex}", "p": pid, "r": run_id})
    return run_id


def _profiles(eng, run_id):
    with eng.connect() as c:
        return [d if isinstance(d, dict) else json.loads(d) for d in c.execute(text(
            "SELECT data FROM events WHERE entity_id = :r AND event_type = :k"),
            {"r": run_id, "k": EVENT_TYPE}).scalars().all()]


def test_a_terminal_run_emits_its_build_profile_once():
    from ._obs_seed import pg_engine

    from boss_engine.services.agent_acts import settle_code_task

    eng = pg_engine()
    run_id = _seed(eng, "failed")
    settle_code_task(eng, run_id)            # the build runner's `finally`
    settle_code_task(eng, run_id)            # finalisation can run twice; one event
    events = _profiles(eng, run_id)
    assert len(events) == 1
    impl = events[0]["stages"]["implement"]
    assert events[0]["status"] == "failed"
    assert impl["output_tokens"] == 1000 and impl["steps"] == 6
    assert impl["tool_min"] == round(73.5 / 60, 2)


def test_a_running_run_emits_nothing_and_a_broken_profile_never_raises(monkeypatch):
    from ._obs_seed import pg_engine

    from boss_engine.services import build_profile

    eng = pg_engine()
    running = _seed(eng, "running")
    assert emit_build_profile(eng, running) is None
    assert _profiles(eng, running) == []

    done = _seed(eng, "completed")

    def boom(*_a, **_k):
        raise RuntimeError("profile exploded")

    monkeypatch.setattr(build_profile, "profile_from_rows", boom)
    assert emit_build_profile(eng, done) is None     # logged, not raised
    assert _profiles(eng, done) == []
