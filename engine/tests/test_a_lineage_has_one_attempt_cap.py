"""ONE CAP FOR ONE CAUSE (ENT-REC-52, services/lineage_cap.py).

One broken existing test took four builds: the build, its review-repair child, the checks-
failing follow-up and the follow-up's own repair child. Each path kept its own count, and a
follow-up's `attempt` starts again at 1. The plan caps the same cause at three attempts in all,
then a person.

The rule: every build that stops on its checks records its cause (`stop_cause`); the builds of
a window that stopped on the SAME cause are counted, and at the cap every automatic path stops.
A different cause has its own count (BOSS keeps fixing a merge conflict or a deploy on its own);
an attempt that goes green closes the window; only a verified PERSON's start opens a new one —
an agent's retry, a recovery build or any other caller is BOSS's and never resets it. Platform
retries attempt no code and are skipped. A person's own larger repair budget is their window.
The end-to-end proof is test_recovery_faults_end_to_end.py (REC-01: three builds).

These need the full schema. Without BOSS_IDENTITY_FULL_TEST_DATABASE_URL they build it on the
test database (BOSS_TEST_DATABASE_URL); with neither, they fail on CI instead of skipping
(and the e2e plumbing gate runs them on its own full-schema database: RETRY_SUITES).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import code_task_service as cts
from boss_engine.services import lineage_cap

from .test_a_resumed_build_runs_as_the_person import _Jobs
from .test_identity_agents_workflow_consent import URL_ENV
from .test_identity_agents_workflow_consent import env  # noqa: F401  (full-schema fixture)


@pytest.fixture(autouse=True, scope="module")
def _full_schema_database():
    """Never a silent skip: the full schema on the test database when none is named."""
    if os.environ.get(URL_ENV):
        yield
        return
    url = os.environ.get("BOSS_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail(f"{URL_ENV} or BOSS_TEST_DATABASE_URL must be set: the lineage cap's "
                        "tests need a database and may not skip on CI")
        pytest.skip(f"{URL_ENV} / BOSS_TEST_DATABASE_URL not set")
    from sqlalchemy import create_engine

    from boss_engine.db import ensure_schema

    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://"), future=True)
    try:
        ensure_schema(engine)
    finally:
        engine.dispose()
    os.environ[URL_ENV] = url
    try:
        yield
    finally:
        os.environ.pop(URL_ENV, None)

#: After this process started, so no row reads as "failed before BOSS last started".
_T0 = datetime.now(timezone.utc) + timedelta(minutes=5)


W = "written_tests_pass"


def _run(env, *, root=None, n=0, status="failed", cause=W, spec=None, **fctx) -> str:  # noqa: F811
    """A build of a lineage that stopped on `cause` (None: it stopped on nothing)."""
    rid = f"crun_{uuid.uuid4().hex[:12]}"
    ctx = {**({"root_run_id": root, "parent_run_id": root} if root else {}),
           **({"stop_cause": cause} if cause else {}), **fctx}
    with env.engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, "
            "state, finalize_ctx, error, created_at) VALUES (:id, :p, NULL, :st, 'done', "
            "'Recipe counter', CAST(:spec AS JSONB), '{}'::jsonb, CAST(:fc AS JSONB), "
            "'the quality gate blocked', :at)"),
            {"id": rid, "p": env.project, "fc": json.dumps(ctx), "st": status,
             "at": _T0 + timedelta(minutes=n),
             "spec": json.dumps({"instructions": "add a counter", "acceptance_criteria": [],
                                 "test_commands": [], "owner": "acme", "repo": "web",
                                 "base_branch": "main", "risk_tier": 1, **(spec or {})})})
    return rid


def _attempts(env, rid) -> int:  # noqa: F811
    with env.engine.begin() as c:
        return lineage_cap.attempts(c, rid)


def test_the_build_its_repair_and_its_follow_up_are_three_attempts_of_one_lineage(env):  # noqa: F811
    root = _run(env)
    repair = _run(env, root=root, n=1, attempt=2, started_by="boss")
    followup = _run(env, root=root, n=2, is_followup=True, started_by="boss")
    assert [_attempts(env, r) for r in (root, repair, followup)] == [1, 2, 3]
    with env.engine.begin() as c:
        assert not lineage_cap.spent(c, repair) and lineage_cap.spent(c, followup)


def test_a_follow_up_from_before_the_marker_still_counts_as_boss_s(env):  # noqa: F811
    root = _run(env)
    _run(env, root=root, n=1, attempt=2, started_by="boss")
    old = _run(env, root=root, n=2, is_followup=True, deferred_from_run=root)
    assert _attempts(env, old) == 3


def test_a_platform_retry_attempts_no_code_and_is_not_counted(env):  # noqa: F811
    root = _run(env)
    infra = _run(env, root=root, n=1, attempt=2, started_by="boss", auto_infra_retry=1)
    restart = _run(env, root=root, n=2, attempt=3, started_by="platform")
    repair = _run(env, root=root, n=3, attempt=4, started_by="boss")
    assert _attempts(env, infra) == _attempts(env, restart) == 1
    assert _attempts(env, repair) == 2


def test_a_different_cause_has_its_own_attempts(env):  # noqa: F811
    """After a review repair and a CI repair, a merge conflict or an app that will not start is
    a new problem: BOSS keeps fixing it on its own."""
    root = _run(env)
    _run(env, root=root, n=1, attempt=2, started_by="boss")
    _run(env, root=root, n=2, attempt=3, started_by="boss", cause="security_clean")
    serves = _run(env, root=root, n=3, attempt=4, started_by="boss", cause="app_serves")
    assert _attempts(env, serves) == 1
    with env.engine.begin() as c:
        assert lineage_cap.attempts(c, serves, W) == 2


def test_an_attempt_that_goes_green_closes_the_window(env):  # noqa: F811
    root = _run(env)
    _run(env, root=root, n=1, attempt=2, started_by="boss")
    _run(env, root=root, n=2, attempt=3, started_by="boss", status="completed", cause=None)
    again = _run(env, root=root, n=3, attempt=4, started_by="boss")
    assert _attempts(env, again) == 1


def test_an_agents_retry_on_a_spent_lineage_stays_spent(env):  # noqa: F811
    """An agent's tool call, a recovery build or any caller that is not a verified person is
    BOSS's (started_by boss): it never opens a new window."""
    root = _run(env)
    _run(env, root=root, n=1, attempt=2, started_by="boss")
    _run(env, root=root, n=2, is_followup=True, started_by="boss")
    agent = _run(env, root=root, n=3, attempt=3, started_by="boss")
    assert _attempts(env, agent) == 4
    assert lineage_cap.started_by(automatic=False, by_person=False, platform_retry=False) == "boss"
    assert lineage_cap.started_by(automatic=True, by_person=True, platform_retry=False) == "boss"
    assert lineage_cap.started_by(automatic=False, by_person=True, platform_retry=False) == "person"
    assert lineage_cap.started_by(automatic=True, by_person=False, platform_retry=True) == "platform"


def test_a_persons_repair_budget_is_their_window(env):  # noqa: F811
    assert lineage_cap.cap_for({}) == lineage_cap.CAP
    assert lineage_cap.cap_for({"repair_retries": 5}) == 6
    assert lineage_cap.cap_for({"repair_retries": 1}) == lineage_cap.CAP
    root = _run(env, spec={"repair_retries": 5})
    third = _run(env, root=root, n=2, attempt=3, started_by="boss", spec={"repair_retries": 5})
    _run(env, root=root, n=1, attempt=2, started_by="boss")
    with env.engine.begin() as c:
        assert not lineage_cap.spent(c, third, {"repair_retries": 5})
        assert lineage_cap.spent(c, third, {})


def test_a_persons_retry_starts_a_new_window(env):  # noqa: F811
    root = _run(env)
    _run(env, root=root, n=1, attempt=2, started_by="boss")
    spent = _run(env, root=root, n=2, is_followup=True, started_by="boss")
    person = _run(env, root=root, n=3, attempt=3, started_by="person")
    after = _run(env, root=root, n=4, attempt=4, started_by="boss")
    assert _attempts(env, spent) == 3
    assert _attempts(env, person) == 1 and _attempts(env, after) == 2


def test_at_the_cap_boss_s_own_retry_is_refused_and_says_why(env):  # noqa: F811
    """The backstop for every automatic caller (CI repair, the shepherd, a recovery agent's
    repair build): retry_code_task(automatic=True) refuses, in words, before it writes."""
    root = _run(env)
    _run(env, root=root, n=1, attempt=2, started_by="boss")
    third = _run(env, root=root, n=2, attempt=3, started_by="boss")
    with pytest.raises(cts.RetryNeedsPerson) as refused:
        cts.retry_code_task(env.engine, get_settings(), _Jobs(), third, automatic=True,
                            fix_note="Autonomous review-repair: fix the failing test")
    assert "BOSS already tried that for this same cause (3 attempts" in str(refused.value)
    assert lineage_cap.NEXT_STEP in str(refused.value)
    with env.engine.begin() as c:
        children = c.execute(text(
            "SELECT count(*) FROM code_task_runs WHERE finalize_ctx->>'parent_run_id' = :r"),
            {"r": third}).scalar()
    assert children == 0


def test_every_attempt_says_who_started_it():
    """The marker is written with the attempt's row (retry_code_task, the follow-up spawn), so
    the count never depends on reading an event later."""
    import inspect

    from boss_engine.services import governed_code_task_service as governed

    retry = inspect.getsource(cts.retry_code_task)
    assert "_lineage_cap.started_by(" in retry and "by_person=by_person" in retry
    spawn = inspect.getsource(governed._spawn_deferred_findings_task)
    assert "_lineage_cap.STARTED_BY: _lineage_cap.BOSS" in spawn
    assert "_lineage_cap.attempts(c, parent_run_id)" in spawn


def test_a_continuation_keeps_the_lineage_s_risk_tier():
    """A follow-up's brief is BOSS's own finding text ("none on a P0 path (startup, auth,
    health)"); re-deciding its tier from that text made a tier-1 change a tier-3 one that waits
    on a plan approval. The tier asked for, else the one the parent decided, is kept."""
    assert lineage_cap.risk_tier({"spec": {"risk_tier": 1}}) == 1
    assert lineage_cap.risk_tier({"spec": {}, "state": {"risk_tier": 2}}) == 2
    assert lineage_cap.risk_tier({"spec": {}}, {"risk_tier": 0}) == 0
    assert lineage_cap.risk_tier({"spec": {"risk_tier": True}, "state": {}}) is None
    assert lineage_cap.risk_tier(None) is None
