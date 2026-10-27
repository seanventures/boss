"""A retry or follow-up is a duplicate only of a run in ITS OWN lineage doing the same work.

crun_ef2252c5 (build E, 2026-10-27): the Engineering Captain's retry_code_task answered
`{status: retried, run_id: crun_6ddf545b, created: false}` — but crun_6ddf545b is build L's
follow-up, another lineage with another fingerprint. Both titles are "Follow-up: checks failing —
…", so active_same_app's app-name rule read both as the app "Checks Failing", start_code_task
returned L's run as E's, and E's retry was dropped while the tool reported success.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from boss_engine.services import code_task_service as cts
from boss_engine.services import run_liveness

from .test_a_resumed_build_runs_as_the_person import _Jobs, _ports  # noqa: F401
from .test_identity_agents_workflow_consent import env  # noqa: F401  (full-schema fixture)

REPO = {"owner": "acme", "repo": "web", "base_branch": "boss/feature"}
FIX = "Make every failing check pass."


def _title(build: str, attempt: int) -> str:
    return f"Follow-up: checks failing — {build} · retry {attempt}"


def _row(env, *, status: str, title: str, root: str, parent: str) -> str:
    rid = f"crun_{uuid.uuid4().hex[:12]}"
    fp = cts.work_fingerprint(project_id=env.project, title=title, instructions=FIX, **REPO)
    with env.engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, state, "
            "finalize_ctx) VALUES (:id, :p, NULL, :s, 'build', :t, CAST(:spec AS JSONB), '{}'::jsonb, "
            "CAST(:fc AS JSONB))"),
            {"id": rid, "p": env.project, "s": status, "t": title,
             "spec": json.dumps({"instructions": FIX, "acceptance_criteria": [], "test_commands": [],
                                 "work_fingerprint": fp, **REPO}),
             "fc": json.dumps({"is_followup": True, "parent_run_id": parent, "root_run_id": root})})
    return rid


def _retry(env, failed: str, root: str, build: str, attempt: int, outcome: dict) -> str:
    with env.engine.begin() as c:
        return cts.start_code_task(
            c, project_id=env.project, task_id=None, title=_title(build, attempt), instructions=FIX,
            acceptance_criteria=[], test_commands=[], finalize_ctx={
                "parent_run_id": failed, "root_run_id": root, "is_followup": True},
            jobs=_Jobs(), simulated=True, outcome=outcome, **REPO)


def _two_lineages(env):
    root_e, root_l = f"crun_e{uuid.uuid4().hex[:10]}", f"crun_l{uuid.uuid4().hex[:10]}"
    live_l = _row(env, status="running", title=_title("Console deep links (build L)", 2),
                  root=root_l, parent=root_l)
    failed_e = _row(env, status="failed", title=_title("Iteration efficiency (build E)", 1),
                    root=root_e, parent=root_e)
    return root_e, failed_e, live_l


def test_a_retry_of_one_lineage_is_not_deduped_onto_another_lineages_running_follow_up(env):  # noqa: F811
    root_e, failed_e, live_l = _two_lineages(env)
    outcome: dict = {}
    new = _retry(env, failed_e, root_e, "Iteration efficiency (build E)", 2, outcome)
    assert new != live_l and outcome.get("created") is not False, outcome
    with env.engine.begin() as c:
        fc = c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id=:i"), {"i": new}).scalar()
    assert fc["root_run_id"] == root_e
    # And the shepherd agrees: the failed E follow-up is not "superseded" by L's live run.
    with env.engine.begin() as c:
        assert run_liveness.same_work_in_flight(c, failed_e) != live_l


def test_a_true_duplicate_retry_of_the_same_lineage_still_dedupes(env):  # noqa: F811
    root_e, failed_e, _ = _two_lineages(env)
    live_e = _row(env, status="running", title=_title("Iteration efficiency (build E)", 2),
                  root=root_e, parent=failed_e)
    outcome: dict = {}
    again = _retry(env, failed_e, root_e, "Iteration efficiency (build E)", 3, outcome)
    assert again == live_e and outcome == {"created": False, "run_id": live_e}


def test_a_fresh_start_keeps_the_app_name_guard(env):  # noqa: F811
    """Not a continuation (no parent run): the coarser same-app guard still applies."""
    title = "Shorty — self-hosted URL shortener"
    rid = f"crun_{uuid.uuid4().hex[:12]}"
    with env.engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, state, "
            "finalize_ctx) VALUES (:id, :p, NULL, 'running', 'build', :t, CAST(:spec AS JSONB), "
            "'{}'::jsonb, '{}'::jsonb)"),
            {"id": rid, "p": env.project, "t": f"{title} · retry 2",
             "spec": json.dumps({"instructions": "other words", "work_fingerprint": "x", **REPO})})
        assert cts.in_flight_same_work(c, env.project, fingerprint="y", repo="web", title=title) == rid
