"""Operator steering follow-up on top of test_steer_follows_work_and_merged_pr_skips_followup.py:

1. `carry_pending_guidance` also copies the parent's ALREADY-APPLIED guidance
   (`finalize_ctx.applied_guidance`) onto the child, each item gaining `carried_from`, so a
   reviewer looking at the child (via `_operator_review_block`, which reads
   `applied_guidance`) can see what a steer changed on its parent.

2. `should_create_followup_for_pr` also refuses when the PR's changed files already read
   the same on its base branch — covering an operator who squash-merged the PR's branch
   into another branch LOCALLY, leaving the PR itself open on GitHub (PR #24).
"""
from __future__ import annotations

import json
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
    if not URL:
        pytest.skip("BOSS_TEST_DATABASE_URL is not set")
    assert "test" in URL.rsplit("/", 1)[-1]
    schema = f"t_steer2_{uuid.uuid4().hex[:8]}"
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


def test_applied_guidance_is_carried_onto_the_child_too(db):
    parent = "crun_parent2"
    child = "crun_child2"
    with db.begin() as c:
        _insert_run(c, parent, finalize_ctx=json.dumps(
            {"applied_guidance": [{"text": "use grid not flexbox", "actor": "op",
                                    "applied_stage": "implement"}]}))
        _insert_run(c, child, finalize_ctx=json.dumps({"parent_run_id": parent}))
        g.carry_pending_guidance(c, parent_run_id=parent, child_run_id=child)

    with db.begin() as c:
        row = c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id=:i"),
                         {"i": child}).mappings().first()
    applied = row["finalize_ctx"].get("applied_guidance")
    assert len(applied) == 1
    assert applied[0]["text"] == "use grid not flexbox"
    assert applied[0]["carried_from"] == parent

    # The parent keeps its own record of what it applied — it is history, not a queue.
    with db.begin() as c:
        prow = c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id=:i"),
                          {"i": parent}).mappings().first()
    assert len(prow["finalize_ctx"].get("applied_guidance")) == 1


class _FakeGitHubOpenButContentAlreadyMatchesBase:
    """An operator squash-merged this PR's branch into `rel` by hand; GitHub still shows it
    open, but every file the PR touches already reads the same on its base."""

    def pull_request_snapshot(self, pull_number):
        return {"number": pull_number, "state": "open", "merged_at": None,
                 "base_ref": "rel", "head_ref": "work/pr-24"}

    def pull_request_files(self, pull_number):
        return [{"path": "apps/web/app/x.ts", "status": "modified"}]

    def read_text_file(self, branch_name, path):
        return "same content"


class _FakeGitHubOpenWithRealDiff:
    def pull_request_snapshot(self, pull_number):
        return {"number": pull_number, "state": "open", "merged_at": None,
                 "base_ref": "rel", "head_ref": "work/pr-25"}

    def pull_request_files(self, pull_number):
        return [{"path": "apps/web/app/x.ts", "status": "modified"}]

    def read_text_file(self, branch_name, path):
        return "base version" if branch_name == "rel" else "head version"


def test_no_followup_when_the_base_already_reads_like_the_pr_even_though_github_shows_it_open():
    ok, reason = g.should_create_followup_for_pr(
        _FakeGitHubOpenButContentAlreadyMatchesBase(), pull_request_number=24)
    assert ok is False
    assert "24" in reason

    ok2, reason2 = g.should_create_followup_for_pr(
        _FakeGitHubOpenWithRealDiff(), pull_request_number=25)
    assert ok2 is True
