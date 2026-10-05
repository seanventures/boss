"""The shepherd asks for repository access before it retries a build — it does not burn the slot.

Seen live: a build died on "No GitHub credentials for paramg/boss-sandbox: the App has no
installation for it, BOSS_GITHUB_TOKEN is not set, and this project has no repository connected
in Setup". The shepherd read that as the platform killing unfinished work and retried it; each
retry took the one build slot, resolved the same missing credential, and died the same way.

Runs the real shepherd watch on the real database: a failed run, the real credential resolver.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.config import get_settings
from boss_engine.services import shepherd_service

ERROR = ("No GitHub credentials for paramg/boss-sandbox: the App has no installation for it, "
         "BOSS_GITHUB_TOKEN is not set, and this project has no repository connected in Setup.")


def _project() -> str:
    return get_settings().default_project_id


def _failed_run(engine, run_id="crun_noaccess"):
    spec = {"instructions": "Build it.", "owner": "paramg", "repo": "boss-sandbox",
            "base_branch": "main", "acceptance_criteria": [], "test_commands": []}
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, "
            "finalize_ctx, error) VALUES (:id, :p, 'failed', 'provisioning', 'Sandbox app', "
            "CAST(:spec AS JSONB), '{}'::jsonb, '{}'::jsonb, :e)"
        ), {"id": run_id, "p": _project(), "spec": json.dumps(spec), "e": ERROR})
    return run_id


def _settings(**update):
    base = {"github_token": "", "github_app_id": None, "github_app_installation_id": None,
            "github_app_private_key": None}
    return api_main.settings.model_copy(update={**base, **update})


def _runs(engine) -> int:
    with engine.begin() as connection:
        return int(connection.execute(text("SELECT count(*) FROM code_task_runs")).scalar())


def test_no_credential_means_no_retry_and_one_question(engine):
    run_id = _failed_run(engine)
    before = _runs(engine)

    result = shepherd_service.watch(engine, _settings(), _project(), jobs=api_main.jobs)

    assert _runs(engine) == before, "a retry was started for a repository BOSS cannot reach"
    assert [e for e in result["escalated"] if e.get("no_repo_access")], result
    with engine.begin() as connection:
        rows = connection.execute(text(
            "SELECT question, evidence FROM shepherd_escalations "
            "WHERE task_ref = :r AND status = 'open'"), {"r": run_id}).mappings().all()
        jobs = connection.execute(text(
            "SELECT count(*) FROM jobs WHERE kind = 'code.task'")).scalar()
    assert len(rows) == 1
    assert rows[0]["question"].startswith("No repository access")
    assert rows[0]["evidence"]["reason"] == "no_repo_access"
    assert int(jobs) == 0, "no build was queued"

    # The next sweep does not ask again or retry.
    shepherd_service.watch(engine, _settings(), _project(), jobs=api_main.jobs)
    assert _runs(engine) == before
    with engine.begin() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM shepherd_escalations WHERE task_ref = :r"),
            {"r": run_id}).scalar() == 1


def test_with_a_credential_the_retry_goes_ahead(engine):
    _failed_run(engine, "crun_hasaccess")
    assert shepherd_service.repo_access_blocker(
        engine, _settings(github_token="token-for-the-test"), "crun_hasaccess") is None
    assert "No GitHub credentials" in (shepherd_service.repo_access_blocker(
        engine, _settings(), "crun_hasaccess") or "")
