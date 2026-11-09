"""WAITING FOR GITHUB IN EVERY VIEW (BOSSAPP-922), AND THE MACHINE CHECKS AS DOTS (BOSSAPP-924).

A build that finished while GitHub was down ends `waiting_for_github` (github_outage.
mark_run_waiting) with a `repo_push_pending` row that knows when BOSS tries the push next. Every
read the console draws it from must say it as WAITING — the operator's words, with the next try —
never failed, never stalled, never a Needs-you card a person must act on:

  * the wait reason every surface's WhyLine draws (stuck_work.wait_reason): code `github`, the
    words, `next_at`;
  * Project › Work (work.view): the row is running, carries `github_wait`, never failed/stalled;
  * the build page (factory_view.build_state): its own state `github_wait`, "Waiting for GitHub";
  * the Overview's Needs you: an FYI (`github_waits`), not a card, not counted.

And the onboarding agent's machine checks for a local project come out as five dots, what it
fixed, and one line (with its fix link) for what it could not fix.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.services import factory_view, github_outage, stuck_work
from boss_engine.services.project_experience import onboarding, overview
from boss_engine.services.project_experience import work as work_service

WORDS = "Waiting for GitHub · your work is saved here and will push when GitHub is back"


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture
def waiting(engine, world):
    """A project with one build waiting for GitHub, its push due in 7 minutes."""
    pid = f"gw-{uuid.uuid4().hex[:8]}"
    run_id = f"crun_gw{uuid.uuid4().hex[:10]}"
    nxt = datetime.now(timezone.utc) + timedelta(minutes=7)
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                        {"p": world.project_a}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, :n, :id)"), {"id": pid, "ws": ws, "n": f"Waits {pid[-4:]}"})
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, finalize_ctx) "
            "VALUES (:id, :p, 'Login page', 'running', 'opening_pr', CAST(:s AS JSONB), "
            "CAST(:f AS JSONB))"),
            {"id": run_id, "p": pid, "s": json.dumps({"owner": "acme", "repo": "shop"}),
             "f": json.dumps({"work_branch": f"boss/code/{run_id}"})})
        pending = github_outage.record_pending(
            db, project_id=pid, owner="acme", repo="shop", branch=f"boss/code/{run_id}",
            base_branch="main", run_id=run_id, change_set_id=None,
            reason="GitHub answered 502 three times", enqueue=False)
        db.execute(text("UPDATE repo_push_pending SET next_attempt_at = :n, attempts = 2 "
                        "WHERE id = :i"), {"n": nxt, "i": pending})
        github_outage.mark_run_waiting(db, run_id, reason="GitHub answered 502",
                                       pending_id=pending)
    return {"project_id": pid, "run_id": run_id, "next": nxt}


def _run(engine, run_id: str) -> dict:
    with engine.connect() as db:
        return dict(db.execute(text("SELECT * FROM code_task_runs WHERE id = :r"),
                               {"r": run_id}).mappings().one())


def test_the_wait_reason_says_waiting_with_the_next_try(engine, waiting):
    run = _run(engine, waiting["run_id"])
    assert run["status"] == "waiting_for_github"
    with engine.begin() as db:
        why = stuck_work.wait_reason(db, run)
    assert why["code"] == "github" and why["who"] == "boss"
    assert why["words"] == WORDS
    assert why["next_at"] and abs(datetime.fromisoformat(why["next_at"]) - waiting["next"]) < timedelta(seconds=2)
    assert 5 <= why["eta_minutes"] <= 7
    assert "fail" not in why["words"].lower()


def test_project_work_shows_it_running_never_failed_or_stalled(engine, waiting):
    later = datetime.now(timezone.utc) + timedelta(hours=6)   # long past any stall horizon
    with engine.begin() as db:
        got = work_service.view(db, waiting["project_id"], now=later)
    row = next(w for w in got["items"] if w.get("build_id") == waiting["run_id"])
    assert row["state"] == "running", row["state"]
    assert row["stalled_since"] is None
    assert row["why"]["code"] == "github" and row["why"]["words"] == WORDS
    assert row["github_wait"]["words"] == WORDS and row["github_wait"]["next_at"]
    assert got["counts"]["failed"] == 0 and got["counts"]["stalled"] == 0


def test_the_build_page_has_its_own_waiting_state(engine, waiting):
    run = _run(engine, waiting["run_id"])
    st = factory_view.build_state(run)
    assert st["state"] == "github_wait" and st["word"] == "Waiting for GitHub"
    assert st["tip"] == WORDS
    assert factory_view.SECTION_OF["github_wait"] == "building"
    assert "github_wait" not in factory_view.STOPPED_STATES


def test_needs_you_lists_it_as_an_fyi_never_a_card(engine, waiting):
    with engine.begin() as db:
        ov = overview.view(db, waiting["project_id"])
    assert not [n for n in ov["needs_you"] if waiting["run_id"] in json.dumps(n)]
    fyi = ov["github_waits"]
    assert len(fyi) == 1 and fyi[0]["words"] == WORDS and fyi[0]["next_at"]


def test_the_machine_checks_become_dots_fixed_and_one_line():
    found = {"ok": False, "online": False, "fixed": ["made the local repository"],
             "checks": [{"key": "git", "ok": True, "words": "git version 2.44"},
                        {"key": "local_repo", "ok": True, "words": "on this machine"},
                        {"key": "disk", "ok": True, "words": "120 GB free"},
                        {"key": "network", "ok": True, "words": "Offline: GitHub is not reachable."},
                        {"key": "model", "ok": False, "words": "remote model"}]}
    m = onboarding.machine_view("p1", found)
    assert [d["key"] for d in m["dots"]] == ["git", "local_repo", "disk", "network", "model"]
    assert [d["ok"] for d in m["dots"]] == ["true", "true", "true", "true", "false"]
    assert m["fixed"] == ["made the local repository"]
    assert len(m["cant"]) == 1
    assert m["cant"][0]["line"].startswith("Offline: the model is remote")
    assert "focus=models" in m["cant"][0]["href"]
    # A check the engine could not answer is grey, never green.
    m2 = onboarding.machine_view("p1", {"online": True, "checks": [{"key": "local_repo", "ok": None}]})
    assert {d["key"]: d["ok"] for d in m2["dots"]}["local_repo"] == "unknown"
    assert m2["cant"] == []


def test_a_local_projects_onboarding_item_carries_the_dots(engine, monkeypatch):
    from boss_engine.services import local_projects

    monkeypatch.setattr(local_projects, "machine_checks", lambda c, s, p, fix=True: {
        "ok": True, "online": True, "fixed": ["made the first commit"], "summary": "Ready.",
        "checks": [{"key": k, "ok": True, "words": k} for k in
                   ("git", "local_repo", "disk", "network", "model")]})
    item = onboarding._local_access(engine, None, "p1", {"full_name": "local_p1/notes"})  # noqa: SLF001
    assert item["state"] == "done"
    assert item["machine"]["fixed"] == ["made the first commit"]
    assert all(d["ok"] == "true" for d in item["machine"]["dots"])
