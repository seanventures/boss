"""A change to an app that exists: held to its existing tests, named for itself, anchored.

Three things the client used to be trusted with, now done by the server on every extend path:

- "Existing tests still pass" was added only by create_code_task's extend branch. A quick change
  (feedback confirm -> retry_code_task) kept whatever criteria the first build had, and
  start_followup_task took a caller's list as given — so the promise could simply be left out.
- A quick change was titled "<previous title> · retry 1": the Factory read a deliberate change as
  a failed build run again. It is now "<App>: <first line of the change>", same lineage.
- A workflow started from a preview did not say which preview: `preview_id` rides with
  `extends_run_id`, is validated, and the build is told (anchor /v1/previews/<id>/web/).
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.tests.test_a_feature_builds_onto_its_app import (
    APP_ROOT, APP_RUN, _start, _the_app, _tick_until_built, panel,  # noqa: F401 — fixture
)


def _project() -> str:
    return get_settings().default_project_id


def _run(engine, run_id):
    with engine.begin() as connection:
        return dict(connection.execute(text(
            "SELECT id, title, spec, finalize_ctx FROM code_task_runs WHERE id = :i"),
            {"i": run_id}).mappings().one())


def _holds_existing_tests(spec) -> bool:
    return any("existing tests still pass" in str(c).lower()
               for c in spec.get("acceptance_criteria") or [])


def test_a_quick_change_is_named_for_itself_and_keeps_the_apps_tests(client, engine):
    _the_app(engine)
    assert not _holds_existing_tests(_run(engine, APP_RUN)["spec"])  # the app's own brief

    proposed = client.post(f"/v1/code-tasks/{APP_RUN}/feedback", json={
        "message": "Add a Download CSV button to the approvals page.\nIt exports this month.",
        "expected": "A CSV of this month's approved swaps downloads."})
    assert proposed.status_code == 200, proposed.text
    body = proposed.json()
    assert body["status"] == "awaiting_confirmation"

    confirmed = client.post(f"/v1/feedback-threads/{body['thread_id']}/confirm",
                            json={"actor": "ceo"})
    assert confirmed.status_code == 200, confirmed.text
    change = _run(engine, confirmed.json()["run_id"])

    assert change["title"] == "Scorecard: Add a Download CSV button to the approvals page."
    assert "retry" not in change["title"]
    assert _holds_existing_tests(change["spec"])
    assert "it scores" in change["spec"]["acceptance_criteria"]  # its own criteria stay
    # Same lineage as any other attempt at this app.
    assert change["finalize_ctx"]["root_run_id"] == APP_ROOT
    assert change["finalize_ctx"]["parent_run_id"] == APP_RUN
    assert change["finalize_ctx"]["app_id"] == "app_scorecard"


def test_a_follow_up_cannot_drop_the_existing_tests_promise(engine):
    """start_followup_task is where every follow-up converges (a workflow build step, an
    approved panel's PRD, the follow-up endpoint): a caller's own criteria get the promise."""
    from apps.api.app.main import jobs
    from boss_engine.services.code_task_service import start_followup_task

    _the_app(engine)
    with engine.begin() as connection:
        started = start_followup_task(
            connection, parent_run_id=APP_RUN, instructions="Pin a score.", jobs=jobs,
            acceptance_criteria=["A score can be pinned."])
    spec = _run(engine, started["run_id"])["spec"]
    assert "A score can be pinned." in spec["acceptance_criteria"]
    assert _holds_existing_tests(spec)


def test_a_workflow_from_a_preview_tells_the_build_which_preview(client, engine, panel):  # noqa: F811
    _the_app(engine)
    client.post("/v1/delivery-workflows/catalogue/install",
                json={"project_id": _project(), "actor": "ceo"})
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO app_previews (id, project_id, run_id, status) "
            "VALUES ('prev_scoreview', :p, :r, 'stopped')"), {"p": _project(), "r": APP_RUN})

    refused = _start(client, {"title": "x", "directive": "x", "extends_run_id": APP_RUN,
                              "preview_id": "prev_elsewhere"})
    assert refused.status_code == 404
    assert refused.json()["detail"] == "that preview is not in this project"

    started = _start(client, {"title": "Export swaps as CSV",
                              "directive": "Add a Download CSV button.",
                              "extends_run_id": APP_RUN, "preview_id": "prev_scoreview"})
    assert started.status_code == 200, started.text
    assert started.json()["extends"]["preview_id"] == "prev_scoreview"
    built = _tick_until_built(client, engine, started.json()["run"]["id"])
    assert "/v1/previews/prev_scoreview/web/" in built[0]["spec"]["instructions"]
    assert built[0]["spec"]["base_branch"] == f"boss/code/{APP_ROOT}"
