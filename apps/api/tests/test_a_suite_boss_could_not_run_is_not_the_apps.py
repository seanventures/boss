"""A SUITE BOSS COULD NOT RUN IS NOT THE APP'S FAILURES (crun_de96bf12, helpdesk-ai-demo).

119 of 190 tests "failed", every one in the suite's setup hook with SQLSTATE 28P01: the test
database BOSS gave the run refused its login (the build-services server had restarted empty).
suite_environment reads the failures' STRUCTURED data (the TAP block's code/failureType, pytest's
exception class, a SQLSTATE class, an errno) and decides the suite could not run; the build says
"BOSS couldn't run the full suite: <reason>", records it where BOSS's own faults go
(boss_recommendations → Self-fixes), and the run's database is made usable again before the
suite runs once more. An app's own setup error — whatever its words — stays the app's.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import build_services, suite_environment
from boss_engine.services.verification_target import checks_headline

FIXTURE = Path(__file__).parent / "fixtures" / "helpdesk_onboarding" / "verify_runs.json"


def _blocked_output() -> str:
    return json.loads(FIXTURE.read_text())[0]["output"]


def _tap(*blocks: tuple[str, str, str, str]) -> str:
    out = []
    for i, (failure_type, error, code, name) in enumerate(blocks, 1):
        out.append(f"not ok {i} - case {i}\n  ---\n  failureType: '{failure_type}'\n"
                   f"  error: '{error}'\n  code: '{code}'\n  name: '{name}'\n  ...\n")
    return "".join(out) + f"1..{len(blocks)}\n# tests {len(blocks) + 3}\n# pass 3\n# fail {len(blocks)}\n"


def test_the_real_case_is_an_environment_failure_owned_by_boss():
    env = suite_environment.classify(_blocked_output())
    assert env is not None
    assert (env["kind"], env["code"], env["owner"]) == ("login", "28P01", "boss")
    assert env["sentence"] == ("BOSS couldn't run the full suite: the test database refused the "
                               "login BOSS gave it.")
    assert env["error"] == "password authentication failed for user"
    assert "boss_test_run" not in env["sentence"], "a generated role name is not news"


@pytest.mark.parametrize("blocks,kind", [
    ([("hookFailed", "connect ECONNREFUSED 127.0.0.1:5432", "ECONNREFUSED", "Error")] * 4,
     "connect"),
    ([("hookFailed", 'database "app_test" does not exist', "3D000", "error")] * 3, "missing"),
    ([("hookFailed", "the database system is shutting down", "57P03", "error")] * 2,
     "restarting"),
])
def test_other_environment_causes_are_read_from_their_codes(blocks, kind):
    env = suite_environment.classify(_tap(*blocks))
    assert env and env["kind"] == kind and env["owner"] == "environment"


@pytest.mark.parametrize("blocks", [
    # The app's own failures, even in a setup hook, even when their words mention a database.
    [("testCodeFailure", "expected 200, got 500 (database row missing)", "ERR_ASSERTION",
      "AssertionError")] * 5,
    [("hookFailed", "Cannot find module './db.mjs'", "ERR_MODULE_NOT_FOUND", "Error")] * 5,
    # A database error the APP caused (a constraint it violated: SQLSTATE class 23).
    [("testCodeFailure", "duplicate key value violates unique constraint", "23505", "error")] * 5,
    # One login failure among many real failures is not "the suite could not run".
    [("hookFailed", "password authentication failed", "28P01", "error")]
    + [("testCodeFailure", "expected true", "ERR_ASSERTION", "AssertionError")] * 4,
])
def test_the_apps_own_failures_stay_the_apps(blocks):
    assert suite_environment.classify(_tap(*blocks)) is None


def test_pytest_reports_are_read_by_exception_class():
    out = ("=== short test summary info ===\n"
           "ERROR tests/test_api.py::test_a - psycopg.OperationalError: connection failed: "
           "FATAL:  password authentication failed for user \"app\"\n"
           "ERROR tests/test_api.py::test_b - psycopg.OperationalError: connection failed: "
           "FATAL:  password authentication failed for user \"app\"\n"
           "FAILED tests/test_api.py::test_c - AssertionError: assert 1 == 2\n"
           "1 failed, 4 passed, 2 errors in 0.5s\n")
    env = suite_environment.classify(out)
    assert env and env["kind"] == "connect" and env["code"] == "OperationalError"
    app = ("FAILED tests/test_x.py::t1 - AssertionError: assert 1 == 2\n"
           "FAILED tests/test_x.py::t2 - KeyError: 'id'\n")
    assert suite_environment.classify(app) is None


def test_the_blocked_build_names_boss_not_the_first_not_ok_line():
    env = suite_environment.classify(_blocked_output())
    check = suite_environment.failed_check(env, "npm test")
    assert checks_headline({"failed_check": check}).endswith(
        "BOSS couldn't run the full suite: the test database refused the login BOSS gave it.")
    assert "not ok" not in checks_headline({"failed_check": check})


def test_after_a_failed_verify_boss_restores_records_and_says_so(engine, monkeypatch):
    """The build's hook: the database is checked (forced) and restored; the run says it; the
    cause is one recommendation for Self-fixes however many builds hit it."""
    seen = []
    monkeypatch.setattr(build_services, "ensure",
                        lambda lease, **kw: seen.append(kw) or "restored")
    pid = get_settings().default_project_id
    sig = suite_environment.signature({"kind": "login", "code": "28P01"})
    with engine.begin() as c:
        c.execute(text("DELETE FROM boss_recommendations WHERE project_id = :p AND signature = :s"),
                  {"p": pid, "s": sig})
    lease = build_services.PostgresLease(run_id="crun_x", name="boss_test_run_00", host="h",
                                         port=5433, password="p" * 48)
    runs = [f"crun_env_{uuid.uuid4().hex[:8]}" for _ in range(2)]
    for run_id in runs:
        env = suite_environment.after_verify(engine, get_settings(), project_id=pid,
                                             run_id=run_id, output=_blocked_output(), lease=lease)
        assert env["restored"] == "restored" and env["owner"] == "boss"
    assert seen == [{"force": True}, {"force": True}]
    with engine.connect() as c:
        events = c.execute(text(
            "SELECT data FROM events WHERE event_type = :e AND entity_id = ANY(:r)"),
            {"e": suite_environment.EVENT, "r": runs}).scalars().all()
        recs = c.execute(text(
            "SELECT title, occurrences, refs FROM boss_recommendations WHERE project_id = :p "
            "AND signature = :s"), {"p": pid, "s": sig}).mappings().all()
    assert len(events) == 2 and all(e["sentence"].startswith("BOSS couldn't run") for e in events)
    assert len(recs) == 1 and recs[0]["occurrences"] == 2
    assert recs[0]["title"] == ("BOSS couldn't run the app's full suite: the test database "
                                "refused the login BOSS gave it")
    assert {r.get("build_id") for r in recs[0]["refs"]} == set(runs)
    # The app's own failures: nothing recorded, nothing restored.
    assert suite_environment.after_verify(
        engine, get_settings(), project_id=pid, run_id="crun_app",
        output=_tap(*[("testCodeFailure", "expected 1", "ERR_ASSERTION", "AssertionError")] * 3),
        lease=lease) is None
    assert len(seen) == 2
