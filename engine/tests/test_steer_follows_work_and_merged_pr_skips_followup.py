"""Fast fix: a steer reaches the run doing the work; no follow-up on a merged PR.

Contract this test suite pins down for the implementer (none of this exists yet):

1. ``governed_code_task_service.carry_pending_guidance(connection, *, parent_run_id,
   child_run_id)`` — called in the same transaction that creates a repair child / retry /
   follow-up. Moves the PARENT's unread ``finalize_ctx.pending_guidance`` onto the CHILD's
   ``finalize_ctx.pending_guidance`` (each item gaining ``carried_from: <parent_run_id>``),
   and clears the parent's. ``take_pending_guidance`` is untouched and still pops once.

2. ``governed_code_task_service.late_steer_response(connection, *, run_id, project_id,
   guidance, actor)`` — the decision ``steer_code_task`` defers to once a run is past the
   point where any future stage would read ``pending_guidance`` (marked on the run's
   ``finalize_ctx`` as ``past_last_stage_boundary: true``). If a follow-up/retry already
   exists for that run (a row with ``finalize_ctx.parent_run_id`` pointing at it), the steer
   is written onto THAT run's ``pending_guidance`` and the answer says so. If none exists
   yet, nothing is written and the answer says in words that this run finished its last
   stage and to retry it with the guidance as the fix note — never a silent no-op queue.

3. ``governed_code_task_service.should_create_followup_for_pr(gh, *, pull_request_number)``
   — returns ``(True, "")`` ordinarily, and ``(False, <reason mentioning merged or closed>)``
   when the PR's ``pull_request_snapshot`` reports it merged or closed. The automatic
   "checks failing" / "deferred findings" follow-up spawn is gated on this before it creates
   the task.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import governed_code_task_service as g

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")

TABLE = """
CREATE TABLE code_task_runs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT,
  status TEXT NOT NULL DEFAULT 'running', phase TEXT NOT NULL DEFAULT 'queued',
  title TEXT NOT NULL DEFAULT '', spec JSONB NOT NULL DEFAULT '{}'::jsonb,
  state JSONB NOT NULL DEFAULT '{}'::jsonb, usage JSONB NOT NULL DEFAULT '{}'::jsonb,
  finalize_ctx JSONB NOT NULL DEFAULT '{}'::jsonb, reservation_id TEXT,
  pull_request_url TEXT, pull_request_number INTEGER, decision_id TEXT, error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now())
"""


@pytest.fixture()
def db():
    """A private schema with just the run table, so this suite never races another."""
    if not URL:
        pytest.skip("BOSS_TEST_DATABASE_URL is not set")
    assert "test" in URL.rsplit("/", 1)[-1]
    schema = f"t_steer_{uuid.uuid4().hex[:8]}"
    driver_url = URL.replace("postgresql://", "postgresql+psycopg://").replace(
        "postgres://", "postgresql+psycopg://")
    base = create_engine(driver_url)
    with base.begin() as c:
        c.execute(text(f"CREATE SCHEMA {schema}"))
    eng = create_engine(driver_url, connect_args={"options": f"-csearch_path={schema}"})
    with eng.begin() as c:
        c.execute(text(TABLE))
    try:
        yield eng
    finally:
        eng.dispose()
        with base.begin() as c:
            c.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        base.dispose()


def _insert_run(c, rid, *, project="p1", finalize_ctx="{}"):
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, title, finalize_ctx) "
        "VALUES (:i, :p, 'running', 'a build', CAST(:f AS jsonb))"),
        {"i": rid, "p": project, "f": finalize_ctx})


def test_p1_guidance_carried_to_child_is_read_once_and_cleared_on_parent(db):
    import json

    parent = "crun_parent1"
    child = "crun_child1"
    with db.begin() as c:
        _insert_run(c, parent, finalize_ctx=json.dumps(
            {"pending_guidance": [{"text": "use grid not flexbox", "actor": "op"}]}))
        _insert_run(c, child, finalize_ctx=json.dumps({"parent_run_id": parent}))
        g.carry_pending_guidance(c, parent_run_id=parent, child_run_id=child)

    # Carried onto the child, tagged with where it came from.
    first = g.take_pending_guidance(db, child, stage="design")
    assert len(first) == 1
    assert first[0]["text"] == "use grid not flexbox"
    assert first[0].get("carried_from") == parent

    # Consumed exactly once — a second read of the same run finds nothing left.
    assert g.take_pending_guidance(db, child, stage="implement") == []
    # And the parent no longer holds a copy, so it can never double-apply.
    assert g.take_pending_guidance(db, parent) == []


def test_p1_late_steer_lands_on_the_followup_or_is_refused_in_words(db):
    import json

    run_with_followup = "crun_late_a"
    its_followup = "crun_late_a_followup"
    run_with_no_followup = "crun_late_b"

    with db.begin() as c:
        _insert_run(c, run_with_followup,
                    finalize_ctx=json.dumps({"past_last_stage_boundary": True}))
        _insert_run(c, its_followup,
                    finalize_ctx=json.dumps({"parent_run_id": run_with_followup}))
        _insert_run(c, run_with_no_followup,
                    finalize_ctx=json.dumps({"past_last_stage_boundary": True}))

    with db.begin() as c:
        landed = g.late_steer_response(
            c, run_id=run_with_followup, project_id="p1",
            guidance="use grid not flexbox", actor="op")
    assert landed["ok"] is True
    assert its_followup in str(landed)
    carried = g.take_pending_guidance(db, its_followup, stage="design")
    assert any(i["text"] == "use grid not flexbox" for i in carried)

    with db.begin() as c:
        refused = g.late_steer_response(
            c, run_id=run_with_no_followup, project_id="p1",
            guidance="use grid not flexbox", actor="op")
    assert refused["ok"] is False
    assert "last stage" in refused["error"].lower()
    assert "retry" in refused["error"].lower()
    # Never a silent queue nobody will ever read.
    assert g.take_pending_guidance(db, run_with_no_followup) == []


class _FakeGitHubMergedPR:
    def pull_request_snapshot(self, pull_number):
        return {"number": pull_number, "state": "closed", "merged_at": "2026-10-30T00:00:00Z"}


class _FakeGitHubOpenPR:
    def pull_request_snapshot(self, pull_number):
        return {"number": pull_number, "state": "open", "merged_at": None}


def test_p1_no_followup_is_created_for_an_already_merged_pr():
    ok, reason = g.should_create_followup_for_pr(_FakeGitHubMergedPR(), pull_request_number=24)
    assert ok is False
    assert "merged" in reason.lower() or "closed" in reason.lower()

    ok2, reason2 = g.should_create_followup_for_pr(_FakeGitHubOpenPR(), pull_request_number=24)
    assert ok2 is True
