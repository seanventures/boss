"""A build a BOSS bug stopped is restarted once BOSS is updated — never left red (2026-11-18).

THE CASE (laptop). CSV Quality Desk's Easy Mode build crun_3c87d5a3 died at 07:51 on the
placeholder-repository bug ("The control plane crashed running this step (HTTP 500):
GitHubAppConfigurationError: No GitHub credentials for _local/lucky…"); PR #203 fixed it and the
laptop was upgraded, but the build stayed failed and red, waiting for the person to press Retry.

The row below is shaped like crun_3c87d5a3 (no failure class: the control plane crashed before
the pipeline could classify it; spec owner/repo `_local`/`lucky`). The retry itself is a stub: the
identity rules it runs under are tested in test_an_automatic_retry_runs_under_the_persons_grant.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import build_failure, build_restart_recovery as brr
from boss_engine.services import code_task_service as cts
from boss_engine.services import local_projects, projects

from ._ambient_support import eng, project  # noqa: F401

CRASH = ("The control plane crashed running this step (HTTP 500): GitHubAppConfigurationError: "
         "No GitHub credentials for _local/lucky: the App has no installation for it, "
         "BOSS_GITHUB_TOKEN is not set, and this project has no repository connected in Setup.")


@pytest.fixture()
def world(eng, project, monkeypatch):  # noqa: F811
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(cts, "PROCESS_STARTED_AT", now - timedelta(minutes=5))
    starts = [f"bst_t{uuid4().hex[:12]}" for _ in range(2)]
    with eng.begin() as c:
        # The worker ran commit A when the build died; it was updated to commit B since.
        for sid, commit, at in ((starts[0], "aaaa1111", now - timedelta(hours=3)),
                                (starts[1], "bbbb2222", now - timedelta(minutes=5))):
            c.execute(text("INSERT INTO boss_starts (id, service, source_commit, started_at) "
                           "VALUES (:i, 'worker', :c, :t)"), {"i": sid, "c": commit, "t": at})
    bound = {"full_name": f"local_{project}/csv-quality-desk"}
    monkeypatch.setattr(local_projects, "local_binding", lambda c, pid, **k: bound)
    monkeypatch.setattr(projects, "ensure_sample_code", lambda *a, **k: None)
    retried: list[dict] = []
    state = {"refuse": False}

    def retry(engine, settings, jobs, run_id, **kw):
        if state["refuse"]:
            raise cts.RetryNeedsPerson("no person's grant covers another attempt")
        retried.append({"run_id": run_id, **kw})
        return {"status": "queued", "run_id": f"crun_next_{run_id[-6:]}"}

    monkeypatch.setattr(cts, "retry_code_task", retry)
    yield {"now": now, "retried": retried, "state": state, "repo": bound["full_name"]}
    with eng.begin() as c:
        c.execute(text("DELETE FROM boss_starts WHERE id = ANY(:i)"), {"i": starts})


def _stopped(eng, pid, *, failed_at, fctx=None, error=CRASH, owner="_local", repo="lucky"):  # noqa: F811
    rid = f"crun_{uuid4().hex[:20]}"
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, phase, error, spec, "
            "finalize_ctx, created_at, updated_at) VALUES (:i, :p, 'CSV Quality Desk', 'failed', "
            "'queued', :e, CAST(:s AS JSONB), CAST(:f AS JSONB), :t, :t)"),
            {"i": rid, "p": pid, "e": error, "t": failed_at,
             "s": json.dumps({"owner": owner, "repo": repo, "instructions": "build it"}),
             "f": json.dumps(fctx or {})})
    return rid


def _ctx(eng, rid):  # noqa: F811
    with eng.begin() as c:
        return c.execute(text("SELECT spec, finalize_ctx FROM code_task_runs WHERE id = :r"),
                         {"r": rid}).mappings().one()


def test_a_build_a_fixed_boss_bug_stopped_is_restarted_once_on_the_new_code(eng, project, world):  # noqa: F811
    rid = _stopped(eng, project, failed_at=world["now"] - timedelta(hours=2))
    out = brr.sweep(eng, get_settings(), jobs=object(), now=world["now"])
    assert out["restarted"] == [rid]
    [call] = world["retried"]
    # As BOSS, on the code running now, under the person's grant (or not at all).
    assert call["automatic"] and call["after_update"] and call["platform_retry"]
    row = _ctx(eng, rid)
    # The placeholder is replaced by the project's real local repository, as Easy Mode does now.
    owner, _, repo = world["repo"].partition("/")
    assert (row["spec"]["owner"], row["spec"]["repo"], row["spec"]["repo_host"]) == (
        owner, repo, "local")
    rec = row["finalize_ctx"]["restart_recovery"]
    assert rec["state"] == "restarted" and rec["words"] == brr.RESTARTED_WORDS
    # Once: the next tick (or a second worker) leaves it alone.
    assert brr.sweep(eng, get_settings(), jobs=object(), now=world["now"])["restarted"] == []
    assert len(world["retried"]) == 1


def test_when_boss_cannot_resume_it_says_so_in_plain_words_never_red(eng, project, world):  # noqa: F811
    world["state"]["refuse"] = True
    rid = _stopped(eng, project, failed_at=world["now"] - timedelta(hours=2))
    out = brr.sweep(eng, get_settings(), jobs=object(), now=world["now"])
    assert out["cannot_resume"] == [rid]
    with eng.begin() as c:
        failure = build_failure.latest_failure(c, project)
    assert failure["word"] == "Stopped by a BOSS bug" and failure["tone"] == "info"
    assert failure["reason"].startswith("Stopped by a BOSS bug fixed in this update")
    card = build_failure.needs_card(failure)
    assert card["tone"] == "info" and "Build failed" not in card["label"]
    # The person's own Retry still works, now on the real repository.
    assert card["retry"] and card["retry"]["run_id"] == rid


def test_a_restart_on_the_same_code_or_a_failure_only_a_person_can_fix_is_left_alone(
        eng, project, world):  # noqa: F811
    with eng.begin() as c:   # the update came BEFORE this failure: the same code would fail again
        c.execute(text("UPDATE boss_starts SET started_at = :t WHERE source_commit = 'bbbb2222' "
                       "AND started_at > :old"), {"t": world["now"] - timedelta(hours=2, minutes=30),
                                                  "old": world["now"] - timedelta(hours=1)})
    same_code = _stopped(eng, project, failed_at=world["now"] - timedelta(hours=2))
    person = _stopped(eng, project, failed_at=world["now"] - timedelta(hours=2),
                      fctx={"failure_class": "needs_human", "reason_code": "harness_unavailable"},
                      error="claude: not logged in", owner="acme", repo="web")
    out = brr.sweep(eng, get_settings(), jobs=object(), now=world["now"])
    assert out == {"restarted": [], "cannot_resume": []} and world["retried"] == []
    for rid in (same_code, person):
        assert "restart_recovery" not in _ctx(eng, rid)["finalize_ctx"]


def test_the_restart_recovery_rides_on_the_monitor_tick():
    import inspect

    from boss_engine.services import monitor_scheduler

    assert "build_restart_recovery.sweep(" in inspect.getsource(monitor_scheduler.tick)
