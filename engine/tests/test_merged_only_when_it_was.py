"""A pull request is merged only when it was merged (pr_merge).

THE CASE (HelpDesk AI recording run, 2026-10-16): the Factory showed "#229 merged" while GitHub
still had PR #229 open. The merge question was approved, and the approval recorded
`blocked_manual_merge_required` — BOSS does not merge in that setup. Every reader took the
approval for the merge, and the deploy step after it shipped the PR's head saying nothing.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import ci_gate, pr_merge, stack_deploy

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL",
                         "postgresql+psycopg://boss:boss@localhost:5432/boss_test")


def _run(**kw):
    return {"id": "crun_x", "pull_request_number": 229,
            "pull_request_url": "https://github.com/acme/helpdesk/pull/229", **kw}


@pytest.mark.parametrize("status", ["approved", "executed", "merged"])
def test_an_approval_is_not_a_merge(status):
    assert pr_merge.state(_run(), {"status": status}) == pr_merge.MERGE_PENDING


def test_merged_comes_from_a_merge_sha_only():
    assert pr_merge.state(_run(), {"status": "approved", "merged_sha": "abc"}) == pr_merge.MERGED
    assert pr_merge.state(_run(finalize_ctx={"merge": {"sha": "abc"}}),
                          {"status": "pending"}) == pr_merge.MERGED
    assert pr_merge.state(_run(), {"status": "rejected"}) == pr_merge.CLOSED
    assert pr_merge.state(_run(), {"status": "pending"}) == pr_merge.OPEN
    assert pr_merge.state({"id": "x"}, {"status": "approved"}) is None


def test_the_deploy_step_says_a_manual_merge_is_pending():
    said = pr_merge.sentence({"state": pr_merge.MERGE_PENDING, "number": 229, "manual": True})
    assert "PR #229 is NOT merged" in said and "MANUAL MERGE IS PENDING" in said
    assert "head" in said
    assert pr_merge.sentence({"state": pr_merge.MERGED, "number": 229}) == ""


# ── against the database ───────────────────────────────────────────────────────────────────

@pytest.fixture
def db():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM code_task_runs LIMIT 1"))
            if c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar() is None:
                pytest.skip("boss_test has no workspace")
    except Exception as error:  # pragma: no cover
        pytest.skip(f"boss_test unavailable: {error}")
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with engine.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:i, :w, 'merge test', :i)"), {"i": pid, "w": wid})
    yield engine, pid
    with engine.begin() as c:
        for table in ("events", "artifacts", "code_task_runs", "decisions"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})  # noqa: S608
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


def _insert(c, pid: str, *, decision_status: str = "approved") -> tuple[str, str]:
    from .test_a_failed_step_stops_the_run import _insert_awaited

    run_id = f"crun_{uuid.uuid4().hex[:10]}"
    _insert_awaited(c, pid, "code_task_runs", run_id, "completed")
    decision_id = f"dec_{uuid.uuid4().hex[:10]}"
    _insert_awaited(c, pid, "decisions", decision_id, decision_status)
    c.execute(text("UPDATE decisions SET status = :s WHERE id = :d"),
              {"s": decision_status, "d": decision_id})
    c.execute(text("UPDATE code_task_runs SET pull_request_number = 229, pull_request_url = :u, "
                   "decision_id = :d, spec = CAST(:s AS JSONB) WHERE id = :i"),
              {"u": "https://github.com/acme/helpdesk/pull/229", "d": decision_id, "i": run_id,
               "s": json.dumps({"repo": "acme/helpdesk"})})
    return run_id, decision_id


class _GitHub:
    def __init__(self, pr: dict):
        self.pr = pr

    def pull_request(self, number):
        return self.pr


def test_an_approved_manual_merge_is_pending_until_github_says_merged(db, monkeypatch):
    engine, pid = db
    with engine.begin() as c:
        run_id, decision_id = _insert(c, pid)
        # What decisions._create_merge_gate_block records when BOSS does not merge here.
        c.execute(text("INSERT INTO artifacts (id, project_id, artifact_type, title, content) "
                       "VALUES (:i, :p, 'merge_gate', 'gate', CAST(:c AS JSONB))"),
                  {"i": f"art_{uuid.uuid4().hex[:8]}", "p": pid,
                   "c": json.dumps({"status": pr_merge.MANUAL_GATE_STATUS,
                                    "approved_review_decision_id": decision_id})})
        seen = pr_merge.observe(c, None, pid, run_id,
                                reader=_GitHub({"state": "open", "merged": False}))
        assert seen["state"] == pr_merge.MERGE_PENDING and seen["manual"] is True
        assert "MANUAL MERGE IS PENDING" in pr_merge.sentence(seen)

        from boss_engine.services import factory_view

        facts = factory_view.load_facts(c, pid, [{"id": run_id, "decision_id": decision_id}])
        row = c.execute(text("SELECT * FROM code_task_runs WHERE id = :i"),
                        {"i": run_id}).mappings().first()
        assert factory_view._pr_state(dict(row), facts) == "open"

        # GitHub merged it (by hand): recorded on the run, and now — only now — merged.
        seen = pr_merge.observe(c, None, pid, run_id, reader=_GitHub(
            {"state": "closed", "merged": True, "merge_commit_sha": "f" * 40}))
        assert seen["state"] == pr_merge.MERGED and seen["sha"] == "f" * 40
        row = c.execute(text("SELECT * FROM code_task_runs WHERE id = :i"),
                        {"i": run_id}).mappings().first()
        assert dict(row["finalize_ctx"])["merge"]["source"] == "github"
        assert factory_view._pr_state(dict(row), facts) == "merged"


def test_a_merge_boss_performed_counts_by_its_sha(db):
    engine, pid = db
    with engine.begin() as c:
        run_id, decision_id = _insert(c, pid, decision_status="executed")
        assert pr_merge.merged_shas(c, pid, [decision_id]) == {}
        c.execute(text("INSERT INTO artifacts (id, project_id, artifact_type, title, content) "
                       "VALUES (:i, :p, 'merge_result', 'merged', CAST(:c AS JSONB))"),
                  {"i": f"art_{uuid.uuid4().hex[:8]}", "p": pid,
                   "c": json.dumps({"decision_id": decision_id, "merged_sha": "a" * 40,
                                    "merged": True})})
        assert pr_merge.merged_shas(c, pid, [decision_id]) == {decision_id: "a" * 40}
        pr_merge.record_for_pull_request(c, pid, "https://github.com/acme/helpdesk/pull/229",
                                         "a" * 40, source="boss")
        row = c.execute(text("SELECT * FROM code_task_runs WHERE id = :i"),
                        {"i": run_id}).mappings().first()
        assert pr_merge.recorded_sha(dict(row)) == "a" * 40


def test_the_deploy_step_never_ships_an_unmerged_head_silently(db, monkeypatch):
    engine, pid = db
    with engine.begin() as c:
        run_id, decision_id = _insert(c, pid)
        c.execute(text("INSERT INTO artifacts (id, project_id, artifact_type, title, content) "
                       "VALUES (:i, :p, 'merge_gate', 'gate', CAST(:c AS JSONB))"),
                  {"i": f"art_{uuid.uuid4().hex[:8]}", "p": pid,
                   "c": json.dumps({"status": pr_merge.MANUAL_GATE_STATUS,
                                    "approved_review_decision_id": decision_id})})
    monkeypatch.setattr(ci_gate, "reader_for_project",
                        lambda *a, **k: _GitHub({"state": "open", "merged": False}))
    monkeypatch.setattr(stack_deploy, "plan_stack_deploy", lambda engine, settings, **kw: {
        "id": "sdep_1", "commit": "b" * 40, "repository": "acme/helpdesk",
        "decision_id": "dec_deploy"})
    out = stack_deploy.plan_for_workflow_step(
        engine, None, project_id=pid, actor="ceo",
        target={"id": "deptgt_1", "repo": "acme/helpdesk", "app_name": "HelpDesk AI",
                "target_kind": "ssh"},
        inputs={"run_id": run_id}, workflow_run_id="wfr_x", step_id="deploy")
    assert out["status"] == "awaiting"
    assert out["manual_merge_pending"] is True
    assert out["merge"]["state"] == pr_merge.MERGE_PENDING
    assert out["note"].startswith("PR #229 is NOT merged")
    with engine.begin() as c:
        said = c.execute(text("SELECT data FROM events WHERE project_id = :p AND "
                              "event_type = 'workflow.deploy_unmerged'"), {"p": pid}).scalar()
    assert said and said["manual_merge_pending"] is True


def test_a_merged_build_deploys_its_merge_commit_not_its_pinned_head(db, monkeypatch):
    """helpdesk-ai 2026-11-06: the deploy waited for PR #3's merge, then planned 2d0ea0a — the PR as
    first opened (pass 1's pinned head) — while main held its repair and follow-up at 256bb1d."""
    engine, pid = db
    with engine.begin() as c:
        run_id, _decision = _insert(c, pid, decision_status="executed")
    monkeypatch.setattr(ci_gate, "reader_for_project", lambda *a, **k: _GitHub(
        {"state": "closed", "merged": True, "merged_at": "now", "merge_commit_sha": "c" * 40}))
    planned: dict = {}

    def plan(engine, settings, **kw):
        planned.update(kw)
        return {"id": "sdep_1", "commit": kw.get("ref"), "repository": "acme/helpdesk",
                "decision_id": "dec_deploy"}
    monkeypatch.setattr(stack_deploy, "plan_stack_deploy", plan)
    stack_deploy.plan_for_workflow_step(
        engine, None, project_id=pid, actor="ceo",
        target={"id": "deptgt_1", "repo": "acme/helpdesk", "app_name": "HelpDesk AI",
                "target_kind": "ssh"},
        inputs={"run_id": run_id}, workflow_run_id="wfr_x", step_id="deploy",
        commit="d" * 40)
    assert planned.get("ref") == "c" * 40, planned
