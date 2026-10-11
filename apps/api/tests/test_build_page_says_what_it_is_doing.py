"""A build's page says what the build is doing, and what each tool call did.

The operator's page for attempt 5 of HelpDesk read "Skipped" under Plan and Tests (they were
carried from attempt 4's saved workspace), "Not yet / Not run" down the Outputs, and "Bash · ok"
fifty-two times. The cockpit now carries `summary` (stage against its time limit, where the
attempt was seeded from, the retry note, files so far, the last test result with the failing
tests' names, the gates), a kept station says `Kept from #N`, and
GET /v1/code-tasks/{id}/tool-log lists each call with its command or path, its time, its exit
code and a failed call's error — clipped and with credentials redacted.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from apps.api.tests import _factory_support as fx

VERIFY = """\
============================= test session starts ==============================
collected 11 items

tests/test_auth.py ..FFFF.F.FF                                           [100%]

=================================== FAILURES ===================================
___________________________ test_google_login_redirect _________________________
Traceback (most recent call last):
  File "/work/tests/test_auth.py", line 41, in test_google_login_redirect
    assert resp.status_code == 302
AssertionError: assert 500 == 302
=========================== short test summary info ============================
FAILED tests/test_auth.py::test_google_login_redirect - AssertionError: assert 500 == 302
FAILED tests/test_auth.py::test_github_login_redirect - AssertionError
FAILED tests/test_auth.py::test_boss_login - KeyError: 'BOSS_CLIENT_ID'
========================= 7 failed, 4 passed in 3.21s ==========================
"""


def _cockpit(client, run_id):
    r = client.get(f"/v1/code-tasks/{run_id}/cockpit", params={"project_id": fx.PROJECT})
    assert r.status_code == 200, r.text
    return r.json()


def _turn(engine, run_id, act_id, *, direction, tool, content, ok=True, at=None):
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO agent_turns (id, project_id, code_task_run_id, stage, direction, "
            "tool_name, content, ok, act_id, created_at) VALUES (:i, :p, :r, 'repair', :d, :t, "
            ":c, :ok, :a, :at)"),
            {"i": f"turn_{uuid.uuid4().hex[:12]}", "p": fx.PROJECT, "r": run_id, "d": direction,
             "t": tool, "c": content if isinstance(content, str) else json.dumps(content),
             "ok": ok, "a": act_id, "at": at or datetime.now(timezone.utc)})


def _lineage(engine):
    """Attempt 4 failed its tests and saved its workspace; attempt 5 was seeded from it."""
    four = fx.new_run(
        engine, status="failed", phase="failed", title="HelpDesk: add login · retry 4",
        minutes_ago=30,
        state={"last_verify_output": VERIFY,
               "tests": [{"command": "pytest -q", "exit_code": 1, "ok": False}]},
        fctx={"attempt": 4, "root_run_id": "crun_root", "parent_run_id": "crun_three",
              "saved_workspace": {"files": [{"path": "app/auth.py", "content": "x"}]}})
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO quality_gates (id, project_id, code_task_run_id, status, gates_passed, "
            "gates_failed) VALUES (:i, :p, :r, 'failed', "
            "'[\"code_written\", \"existing_tests_intact\"]'::jsonb, "
            "'[\"written_tests_pass\"]'::jsonb)"),
            {"i": f"qg_{four}", "p": fx.PROJECT, "r": four})
    five = fx.new_run(
        engine, status="running", phase="repair", title="HelpDesk: add login · retry 5",
        minutes_ago=0,
        fctx={"attempt": 5, "root_run_id": "crun_root", "parent_run_id": four,
              "seed_workspace_from": four,
              "feedback_history": [{"attempt": 5, "parent_run_id": four,
                                    "fix_note": "Use the BOSS client id from settings",
                                    "prev_status": "failed"}]})
    with engine.begin() as c:
        for kind, data in (("code_task.workspace_seeded", {"from_run": four, "files": 75}),
                           ("code_task.tests_resumed", {"skipped_stage": "write_tests",
                                                        "seeded_from": four,
                                                        "existing_tests": 11})):
            c.execute(text(
                "INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, "
                "data) VALUES (:i, :p, :t, 'boss', 'code_task_run', :r, CAST(:d AS JSONB))"),
                {"i": f"evt_{uuid.uuid4().hex[:12]}", "p": fx.PROJECT, "t": kind, "r": five,
                 "d": json.dumps(data)})
    return four, five


def test_a_seeded_attempt_says_what_it_is_doing_now(client, engine):
    four, five = _lineage(engine)
    act = fx.stage(engine, five, "repair")["act_id"]
    t0 = datetime.now(timezone.utc) - timedelta(minutes=2)
    _turn(engine, five, act, direction="tool_call", tool="Write",
          content={"file_path": "app/auth.py", "content": "SECRET BODY"}, at=t0)
    _turn(engine, five, act, direction="tool_result", tool="Write", content="ok",
          at=t0 + timedelta(milliseconds=40))
    body = _cockpit(client, five)
    s = body["summary"]
    assert s["attempt"] == 5 and s["phase"] == "repair"
    assert s["stage"]["title"] == "Repair" and s["stage"]["started_at"]
    assert s["time_limit_s"] and s["time_limit_s"] > 0
    assert s["seeded"] == {"from_run": four, "attempt": 4, "files": 75, "tests": 11,
                           "at": s["seeded"]["at"], "missing": False}
    assert s["note"] == "Use the BOSS client id from settings"
    # Files so far: what the agents wrote in THIS attempt, before the run's own list exists.
    assert s["files"] == ["app/auth.py"] and s["files_final"] is False
    # The last test result is attempt 4's, said as attempt 4's, with the runner's own counts.
    v = s["verify"]
    assert (v["run_id"], v["attempt"], v["own"], v["ok"]) == (four, 4, False, False)
    assert (v["failed"], v["passed"], v["total"]) == (7, 4, 11)
    assert v["failing"][:3] == ["tests/test_auth.py::test_google_login_redirect",
                                "tests/test_auth.py::test_github_login_redirect",
                                "tests/test_auth.py::test_boss_login"]
    g = s["gates"]
    assert g["attempt"] == 4 and g["failed"] == ["New tests pass"] and len(g["passed"]) == 2
    assert "SECRET BODY" not in json.dumps(s)


def test_a_station_carried_from_a_saved_workspace_says_kept_not_skipped(client, engine):
    _four, five = _lineage(engine)
    fx.stage(engine, five, "repair")
    stations = {s["key"]: s for s in _cockpit(client, five)["stations"]}
    for key in ("plan", "tests"):
        assert stations[key]["word"] == "Kept from #4", stations[key]
        assert stations[key]["status"] == "done" and stations[key]["kept_from"] == 4
        assert "saved workspace" in stations[key]["tip"]
    # A build that skipped a station by its profile still says Skipped.
    plain = fx.new_run(engine, status="running", phase="implement", state={"risk_tier": 0})
    fx.stage(engine, plain, "implement")
    words = {s["key"]: s["word"] for s in _cockpit(client, plain)["stations"]}
    assert words["plan"] == "Skipped"


def test_the_tool_log_says_what_each_call_did(client, engine):
    _four, five = _lineage(engine)
    act = fx.stage(engine, five, "repair")["act_id"]
    t0 = datetime.now(timezone.utc) - timedelta(minutes=3)
    calls = [
        ("Bash", {"command": "pytest -q tests/test_auth.py", "description": "run"},
         "Exit code 1\nTraceback (most recent call last):\n  File \"app/auth.py\", line 3\n"
         "KeyError: 'BOSS_CLIENT_ID'\ntoken ghp_abcdefghijklmnopqrstuvwxyz0123456789", False),
        ("Read", {"file_path": "app/auth.py"}, "1 import os", True),
        ("Read", {"file_path": "app/settings.py"}, "1 X = 1", True),
        ("Edit", {"file_path": "app/auth.py", "old_string": "a", "new_string": "SECRET EDIT"},
         "The file app/auth.py has been updated.", True),
    ]
    for i, (tool, args, result, ok) in enumerate(calls):
        _turn(engine, five, act, direction="tool_call", tool=tool, content=args,
              at=t0 + timedelta(seconds=10 * i))
        _turn(engine, five, act, direction="tool_result", tool=tool, content=result, ok=ok,
              at=t0 + timedelta(seconds=10 * i, milliseconds=1500))
    r = client.get(f"/v1/code-tasks/{five}/tool-log", params={"project_id": fx.PROJECT})
    assert r.status_code == 200, r.text
    log = r.json()
    items = log["items"]
    assert [(i["tool"], i["arg"]) for i in items] == [
        ("Bash", "pytest -q tests/test_auth.py"), ("Read", "app/auth.py"),
        ("Read", "app/settings.py"), ("Edit", "app/auth.py")]
    bash = items[0]
    assert bash["ok"] is False and bash["exit_code"] == 1 and bash["ms"] == 1500
    assert "KeyError: 'BOSS_CLIENT_ID'" in bash["error"]
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in r.text   # redacted
    assert "SECRET EDIT" not in r.text                                 # never what was written
    assert items[1]["ok"] is True and items[1]["output"] == "1 import os"
    assert all(i["act_id"] == act for i in items)
    # The verify output search reads: the nearest attempt's, said as whose.
    assert log["verify"]["attempt"] == 4 and "Traceback" in log["verify"]["output"]
    other = client.get(f"/v1/code-tasks/{five}/tool-log", params={"project_id": "elsewhere"})
    assert other.status_code in (403, 404)


def test_verify_counts_reads_what_each_runner_printed():
    from boss_engine.services.build_detail import verify_counts

    jest = "FAIL src/a.test.ts\n  ✕ logs in with Google (12 ms)\n  ✓ shows the page\n" \
           "Tests:       1 failed, 3 passed, 4 total\n"
    got = verify_counts(jest)
    assert (got["failed"], got["passed"], got["total"]) == (1, 3, 4)
    assert got["failing"] == ["logs in with Google"]
    vitest = " FAIL  tests/a.test.ts > login > redirects\n Tests  2 failed | 9 passed (11)\n"
    got = verify_counts(vitest)
    assert (got["failed"], got["passed"]) == (2, 9)
    assert got["failing"] == ["tests/a.test.ts > login > redirects"]
    tap = "not ok 3 - rejects a bad token\n# tests 5\n# pass 4\n# fail 1\n"
    got = verify_counts(tap)
    assert (got["failed"], got["passed"], got["total"]) == (1, 4, 5)
    assert got["failing"] == ["rejects a bad token"]
    unittest = "FAIL: test_x (tests.test_a.T)\n----\nRan 6 tests in 0.1s\n\nFAILED (failures=2)\n"
    got = verify_counts(unittest)
    assert (got["failed"], got["passed"], got["total"]) == (2, 4, 6)
    # A log with no summary line claims no numbers.
    got = verify_counts("npm ERR! missing script: test")
    assert got["failed"] is None and got["passed"] is None and got["total"] is None
