""""Spec it first" for an app that exists: the panel's approved PRD is the app's next build.

The composer convened a panel with a `[boss:extend_run=…]` marker in its directive, and the
approval handler looked for that marker in the PRD markdown — where the panel never writes it.
It also refused to build at all unless BOSS_TARGET_GITHUB_* named one repository for the whole
install, and unless auto_code_task_after_prd was on. So approving the spec of a feature for an
existing app built a NEW app or nothing.

Now `constraints.extends_run_id` is data: validated by POST /v1/meetings/prd, kept on the
meeting run, carried to the PRD decision, and read back when that decision is approved. The
build goes onto the app's own branch and repository, in its lineage.

Everything goes through the HTTP API on the real database. The panel's phases are the one thing
not run (they call a model): the test writes the PRD where the panel would, then finalizes the
meeting with the real finalizer, which raises the real PRD decision.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.tests.test_a_feature_builds_onto_its_app import (
    APP_ROOT, APP_RUN, _insert_run, _the_app,
)


def _project() -> str:
    return get_settings().default_project_id


def _convene(client, directive, **constraints):
    return client.post("/v1/meetings/prd", json={
        "project_id": _project(), "actor": "ceo", "directive": directive,
        "constraints": {"meeting_type": "prd_panel", **constraints}})


def _finish_panel(engine, meeting_run_id: str, prd: str) -> str:
    """The panel's PRD, written where the panel writes it; then the real finalizer."""
    from boss_engine.services.meeting_service import _finalize_meeting

    settings = get_settings().model_copy(update={"meeting_prd_auto_approve": False})
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE meeting_runs SET state = jsonb_build_object('prd_markdown', CAST(:m AS text)) "
            "WHERE id = :id"), {"m": prd, "id": meeting_run_id})
        finished = _finalize_meeting(connection, settings, meeting_run_id)
    return finished["decision_id"]


def _builds_from(engine, decision_id: str):
    with engine.begin() as connection:
        return [dict(r) for r in connection.execute(text(
            "SELECT id, title, spec, finalize_ctx FROM code_task_runs "
            "WHERE finalize_ctx->'origin'->>'decision_id' = :d"), {"d": decision_id}
        ).mappings().all()]


def test_an_approved_spec_for_an_app_is_that_apps_next_build(client, engine, monkeypatch):
    from apps.api.app import main as api_main

    # Neither switch may stand between an explicit "build onto this app" and its build: no
    # install-wide repository, and auto-building after a PRD turned off.
    monkeypatch.setattr(api_main.decisions.settings, "auto_code_task_after_prd", False)
    monkeypatch.setattr(api_main.decisions.settings, "target_github_owner", "")
    monkeypatch.setattr(api_main.decisions.settings, "target_github_repo", "")
    _the_app(engine)

    convened = _convene(client, "Add a Download CSV button to the approvals page.",
                        extends_run_id=APP_RUN)
    assert convened.status_code == 201, convened.text
    body = convened.json()
    meeting_run_id = body["meeting_run_id"]
    assert meeting_run_id.startswith("mrun_") and body["extends_run_id"] == APP_RUN

    with engine.begin() as connection:
        meeting = connection.execute(text(
            "SELECT directive, constraints, title FROM meeting_runs WHERE id = :id"),
            {"id": meeting_run_id}).mappings().one()
    # Kept on the run as data, and the panel is told it is a change to a working app.
    assert meeting["constraints"]["extends_run_id"] == APP_RUN
    assert "EXISTING, working app" in meeting["directive"]
    assert f"boss/code/{APP_ROOT}" in meeting["directive"]
    assert "EXISTING" not in meeting["title"]

    decision_id = _finish_panel(engine, meeting_run_id,
                                "# PRD\nA Download CSV button on the approvals page.")
    with engine.begin() as connection:
        recommendation = connection.execute(text(
            "SELECT recommendation FROM decisions WHERE id = :d"), {"d": decision_id}).scalar()
    assert recommendation["extends_run_id"] == APP_RUN
    assert "boss:extend_run" not in recommendation["args"]["markdown"]
    assert _builds_from(engine, decision_id) == []  # nothing is built before the approval

    approved = client.post(f"/v1/decisions/{decision_id}/approve", json={"actor": "ceo"})
    assert approved.status_code == 200, approved.text

    built = _builds_from(engine, decision_id)
    assert len(built) == 1, built
    run = built[0]
    spec, fctx = run["spec"], run["finalize_ctx"]
    assert spec["base_branch"] == f"boss/code/{APP_ROOT}"
    assert (spec["owner"], spec["repo"]) == ("acme", "scorecard")  # the app's, not the env's
    assert fctx["app_id"] == "app_scorecard"
    assert fctx["root_run_id"] == APP_ROOT
    assert fctx["parent_run_id"] == APP_RUN
    assert fctx["is_followup"] is True
    assert fctx["origin"]["meeting_run_id"] == meeting_run_id
    assert "A Download CSV button" in spec["instructions"]
    assert any("existing tests still pass" in c.lower() for c in spec["acceptance_criteria"])
    assert run["title"].startswith("Scorecard: ")

    # Approving twice does not build twice.
    with engine.begin() as connection:
        connection.execute(text("UPDATE decisions SET status = 'pending' WHERE id = :d"),
                           {"d": decision_id})
    client.post(f"/v1/decisions/{decision_id}/approve", json={"actor": "ceo"})
    assert len(_builds_from(engine, decision_id)) == 1

    apps = client.get("/v1/factory/apps", params={"project_id": _project()})
    assert apps.status_code == 200, apps.text
    rows = [a for a in apps.json()["apps"]
            if {APP_ROOT, APP_RUN, run["id"]} & set(a["run_ids"])]
    assert len(rows) == 1, rows
    assert rows[0]["builds"] == 3
    assert run["id"] in rows[0]["run_ids"]


def test_a_panel_for_an_app_elsewhere_is_refused_before_anything_starts(client, engine):
    with engine.begin() as connection:
        before = connection.execute(text("SELECT count(*) FROM meeting_runs")).scalar()
    refused = _convene(client, "Add a thing.", extends_run_id="crun_nowhere")
    assert refused.status_code == 404
    assert refused.json()["detail"] == "that app is not in this project"

    _insert_run(engine, "crun_fixture", status="simulated", spec={"simulated": True})
    refused = _convene(client, "Add another thing.", extends_run_id="crun_fixture")
    assert refused.status_code == 422
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM meeting_runs")).scalar() == before


def test_an_old_text_marker_still_builds_onto_the_app(client, engine, monkeypatch):
    """Rows convened before the structured field: the marker in the directive is honoured,
    taken out of what the panel reads, and the PRD still builds onto the app."""
    from apps.api.app import main as api_main

    monkeypatch.setattr(api_main.decisions.settings, "auto_code_task_after_prd", False)
    _the_app(engine)
    convened = _convene(client, f"Add a feature to Scorecard. [boss:extend_run={APP_RUN}] "
                                "Let a score be exported.")
    assert convened.status_code == 201, convened.text
    meeting_run_id = convened.json()["meeting_run_id"]
    with engine.begin() as connection:
        directive = connection.execute(text(
            "SELECT directive FROM meeting_runs WHERE id = :id"), {"id": meeting_run_id}).scalar()
    assert "boss:extend_run" not in directive
    decision_id = _finish_panel(engine, meeting_run_id, "# PRD\nExport a score.")
    assert client.post(f"/v1/decisions/{decision_id}/approve",
                       json={"actor": "ceo"}).status_code == 200
    built = _builds_from(engine, decision_id)
    assert len(built) == 1
    assert built[0]["spec"]["base_branch"] == f"boss/code/{APP_ROOT}"


def test_a_plain_panel_returns_its_meeting_and_builds_nothing_extra(client, engine):
    convened = _convene(client, "Build a brand new expense tracker with approvals.")
    assert convened.status_code == 201, convened.text
    body = convened.json()
    assert body["meeting_run_id"].startswith("mrun_")
    assert body["extends_run_id"] is None
    with engine.begin() as connection:
        meeting = connection.execute(text(
            "SELECT directive, constraints FROM meeting_runs WHERE id = :id"),
            {"id": body["meeting_run_id"]}).mappings().one()
    assert meeting["directive"] == "Build a brand new expense tracker with approvals."
    assert "extends_run_id" not in meeting["constraints"]


def test_the_build_is_named_for_the_feature_not_the_panels_framing(client, engine, monkeypatch):
    """Found walking it: the console's directive frames the ask ("Add a feature to "X" — an
    existing, working application… What is wanted: …"), the PRD took that as its title, and
    the build read "X: Add a feature to "X" — …". It is named for what was wanted."""
    from apps.api.app import main as api_main

    monkeypatch.setattr(api_main.decisions.settings, "auto_code_task_after_prd", False)
    _the_app(engine)
    directive = ('Add a feature to "Scorecard" — an existing, working application in acme/scorecard.\n\n'
                 "What is wanted: Weekly summary of each candidate's scores\n"
                 "Done when: Everything that worked before still works\n\n"
                 "Write a PRD for THIS FEATURE ONLY.")
    convened = _convene(client, directive, extends_run_id=APP_RUN)
    assert convened.status_code == 201, convened.text
    decision_id = _finish_panel(engine, convened.json()["meeting_run_id"], "# PRD\nA weekly summary.")
    assert client.post(f"/v1/decisions/{decision_id}/approve", json={"actor": "ceo"}).status_code == 200
    built = _builds_from(engine, decision_id)
    assert [b["title"] for b in built] == ["Scorecard: Weekly summary of each candidate's scores"]

