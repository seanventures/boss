"""Easy Mode writes a spec before it builds (operator, 2026-11-19).

    "one of the main features is to have a spec for all the features / project before starting
    anything - this worked out best for most of the projects, because we don't have much context
    to build just using the prompt"

Easy Mode, a gallery template and the built-in "lucky" spec sent their prompt straight to a code
task: no panel, no minutes, no PRD (tile-swap, invoice-desk, neighborhood-board). A launch now
starts the Easy Mode MVP workflow — a compact panel (three seats, one round) writes a lean PRD,
the person's start approves it (light lane), and the build runs from it, then the preview and the
deploy. A panel that fails or overruns its time budget gives way to a build from the brief, with
the reason recorded. A bug keeps the small path: one build, no panel.

Everything goes through the HTTP API on the real database. The panel's model phases are the one
thing replaced: the real request_meeting tool convenes the real meeting run, and the real
finalizer raises (and approves) the PRD decision from the PRD a panel would have written.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.app.config import get_settings

PRD = "# PRD: Notes\n\n## Acceptance Criteria\n- A person adds a note and sees it in the list.\n"


def _no_templates(monkeypatch, tmp_path):
    monkeypatch.setattr("boss_engine.services.factory_launch.TEMPLATE_DIR", tmp_path / "absent")


def _setup(monkeypatch, tmp_path):
    from apps.api.app.routers import factory_launch as route
    from boss_engine.services import model_readiness

    monkeypatch.setattr(route._settings, "local_repo_root", str(tmp_path / "repos"))
    monkeypatch.setattr(model_readiness, "usable_model", lambda *a, **kw: True)
    _no_templates(monkeypatch, tmp_path)


def _panel(monkeypatch, engine, *, finish: bool, auto_approve_setting: bool = False):
    """The meeting step's REAL tool convenes the real panel. With `finish`, the PRD a panel would
    write lands where its phases write it and the real finalizer runs; the install's own PRD
    setting is `auto_approve_setting` (off: only the start can approve it)."""
    from boss_engine.services import workflow_capability_runner as runner
    from boss_engine.services.meeting_service import _finalize_meeting

    real = runner._invoke_tool
    settings = get_settings().model_copy(update={
        "meeting_prd_auto_approve": auto_approve_setting,
        # The workflow builds the spec in its own Build step; not a second build in a thread.
        "auto_code_task_after_prd": False})
    convened: list[dict] = []

    def invoke(ctx, tool_name, project_id, actor, args, **kw):
        result = real(ctx, tool_name, project_id, actor, args, **kw)
        if tool_name == "request_meeting":
            run_id = result["meeting"]["run_id"]
            convened.append({"run_id": run_id, "args": dict(args)})
            if finish:
                with engine.begin() as connection:
                    connection.execute(text(
                        "UPDATE meeting_runs SET state = jsonb_build_object('prd_markdown', "
                        "CAST(:prd AS text)) WHERE id = :id"), {"id": run_id, "prd": PRD})
                    _finalize_meeting(connection, settings, run_id)
        return result

    monkeypatch.setattr(runner, "_invoke_tool", invoke)
    return convened


def _launch(client, **body):
    response = client.post("/v1/factory/launch", json={"prompt": "", **body})
    assert response.status_code == 201, response.text
    return response.json()


def _tick(client, project_id):
    ticked = client.post("/v1/delivery-workflows/run-capabilities", json={"project_id": project_id})
    assert ticked.status_code == 200, ticked.text


def _builds(engine, workflow_run_id):
    with engine.begin() as connection:
        return [dict(r) for r in connection.execute(text(
            "SELECT id, title, spec FROM code_task_runs WHERE finalize_ctx->>'workflow_run_id' = :w"),
            {"w": workflow_run_id}).mappings().all()]


def _tick_until_built(client, engine, project_id, workflow_run_id, ticks=8):
    for _ in range(ticks):
        _tick(client, project_id)
        built = _builds(engine, workflow_run_id)
        if built:
            return built
    raise AssertionError("the build step never started a code task")


def _step(engine, workflow_run_id, step_id):
    with engine.begin() as connection:
        return connection.execute(text(
            "SELECT status, output FROM workflow_step_runs WHERE workflow_run_id = :w "
            "AND step_id = :s ORDER BY created_at DESC LIMIT 1"),
            {"w": workflow_run_id, "s": step_id}).mappings().first()


def test_an_easy_mode_launch_writes_the_spec_then_builds_from_it(client, engine, monkeypatch,
                                                                  tmp_path):
    """Launch → the Easy Mode MVP workflow → a compact panel → its PRD approved by the start
    (even with the install's PRD auto-approval off) → the build's brief is that PRD."""
    _setup(monkeypatch, tmp_path)
    convened = _panel(monkeypatch, engine, finish=True, auto_approve_setting=False)

    body = _launch(client, prompt="Build a small notes app: a list and an editor.")
    project_id, workflow_run_id = body["project_id"], body["workflow_run_id"]
    assert body["stage"] == "spec" and body["run_id"] == workflow_run_id
    assert workflow_run_id.startswith("wfr_")
    assert body["spec"]["spec_first"] is True and body["spec"]["spec_approval"] == "start"
    with engine.begin() as connection:
        run = connection.execute(text(
            "SELECT r.input, d.workflow_key FROM workflow_runs r "
            "JOIN workflow_versions v ON v.id = r.workflow_version_id "
            "JOIN workflow_definitions d ON d.id = v.workflow_definition_id WHERE r.id = :w"),
            {"w": workflow_run_id}).mappings().first()
        # Nothing is built before the spec: no code task yet.
        assert not connection.execute(text(
            "SELECT 1 FROM code_task_runs WHERE project_id = :p"), {"p": project_id}).first()
        # The launch's budget hold is released: each step reserves its own.
        assert connection.execute(text(
            "SELECT status FROM budget_reservations WHERE id = :r"),
            {"r": body["reservation_id"]}).scalar() != "reserved"
    assert run["workflow_key"] == "easy_mode_mvp"
    # ZERO APPROVALS BESIDES THE START AND THE MERGE, said from the rules the runtime acts on:
    # the spec is BOSS's (the start approved it), the merge is the person's, and the staging
    # deploy ships under the staging policy this start set (production untouched).
    asks = client.get("/v1/delivery-workflows/asks", params={"project_id": project_id})
    assert asks.status_code == 200, asks.text
    assert [(a["step_id"], a["kind"], a["who"])
            for a in asks.json()["workflows"]["easy_mode_mvp"]] == [
        ("meeting", "spec", "boss"), ("build", "merge", "you"), ("deploy", "gate", "boss")]
    with engine.begin() as connection:
        policies = dict(connection.execute(text(
            "SELECT environment, requires_approval FROM deployment_environment_policies "
            "WHERE project_id = :p"), {"p": project_id}).all())
    assert policies == {"staging": False}
    assert run["input"]["spec_approval"] == "start"
    assert run["input"]["repository"] == body["spec"]["local_repo"]

    built = _tick_until_built(client, engine, project_id, workflow_run_id)

    assert len(convened) == 1
    meeting_run_id = convened[0]["run_id"]
    assert convened[0]["args"]["compact"] is True and convened[0]["args"]["fallback_to_brief"]
    with engine.begin() as connection:
        meeting = connection.execute(text(
            "SELECT role_keys, constraints, decision_id, meeting_id FROM meeting_runs "
            "WHERE id = :m"), {"m": meeting_run_id}).mappings().first()
        decision = connection.execute(text(
            "SELECT status, resolution_note FROM decisions WHERE id = :d"),
            {"d": meeting["decision_id"]}).mappings().first()
        recorded = connection.execute(text(
            "SELECT count(*) FROM events WHERE event_type = 'meeting.prd_approved_by_start' "
            "AND entity_id = :d"), {"d": meeting["decision_id"]}).scalar()
    # A compact panel: three seats at most, its brief the launch's, its PRD approved by the start.
    assert 1 <= len(meeting["role_keys"]) <= 3
    assert meeting["constraints"]["compact"] is True
    assert meeting["constraints"]["spec_approval"] == "start"
    assert meeting["meeting_id"], "the panel's minutes are a meeting on the project"
    assert decision["status"] in ("approved", "executed", "completed")
    assert "Easy Mode start" in str(decision["resolution_note"] or "")
    assert recorded == 1
    # The build is the PRD's: the spec is its brief.
    assert len(built) == 1
    assert "A person adds a note and sees it in the list." in built[0]["spec"]["instructions"]
    assert "THE PRODUCT SPEC BELOW IS THE BRIEF" in built[0]["spec"]["instructions"]


def test_a_failed_panel_builds_from_the_brief_and_says_why(client, engine, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    convened = _panel(monkeypatch, engine, finish=False)
    body = _launch(client, prompt="Build a small notes app with tags.")
    project_id, workflow_run_id = body["project_id"], body["workflow_run_id"]
    _tick(client, project_id)
    assert convened, "the meeting step never convened the panel"
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE meeting_runs SET status = 'failed', error = 'the model did not answer' "
            "WHERE id = :m"), {"m": convened[0]["run_id"]})

    built = _tick_until_built(client, engine, project_id, workflow_run_id)

    step = _step(engine, workflow_run_id, "meeting")
    assert step["status"] == "succeeded"
    fallback = step["output"]["spec_fallback"]
    assert "the model did not answer" in fallback["reason"]
    assert "THE PRODUCT SPEC BELOW IS THE BRIEF" not in built[0]["spec"]["instructions"]
    assert "notes app" in built[0]["spec"]["instructions"]
    with engine.begin() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM events WHERE event_type = 'workflow.spec_fallback' AND entity_id = :w"),
            {"w": workflow_run_id}).scalar() == 1


def test_a_panel_past_its_time_budget_is_stopped_and_the_build_starts(client, engine, monkeypatch,
                                                                       tmp_path):
    _setup(monkeypatch, tmp_path)
    convened = _panel(monkeypatch, engine, finish=False)
    body = _launch(client, prompt="Build a small notes app with search.")
    project_id, workflow_run_id = body["project_id"], body["workflow_run_id"]
    _tick(client, project_id)
    assert convened
    meeting_run_id = convened[0]["run_id"]
    _tick(client, project_id)
    assert not _builds(engine, workflow_run_id), "a panel within its budget is waited for"
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE meeting_runs SET created_at = now() - interval '13 minutes' WHERE id = :m"),
            {"m": meeting_run_id})

    _tick_until_built(client, engine, project_id, workflow_run_id)

    with engine.begin() as connection:
        meeting = connection.execute(text(
            "SELECT status, error FROM meeting_runs WHERE id = :m"),
            {"m": meeting_run_id}).mappings().first()
    assert meeting["status"] == "cancelled" and "budget" in meeting["error"]
    assert "12-minute budget" in _step(engine, workflow_run_id, "meeting")["output"][
        "spec_fallback"]["reason"]


def test_review_the_plan_first_waits_for_the_person(client, engine, monkeypatch, tmp_path):
    """The advanced option: the PRD is not approved by the start, even where the install would
    approve it by itself, and nothing is built until a person does."""
    _setup(monkeypatch, tmp_path)
    convened = _panel(monkeypatch, engine, finish=True, auto_approve_setting=True)
    body = _launch(client, prompt="Build a small notes app with colours.", review_plan=True)
    assert body["spec"]["spec_approval"] == "person"
    for _ in range(3):
        _tick(client, body["project_id"])
    assert convened
    with engine.begin() as connection:
        status = connection.execute(text(
            "SELECT d.status FROM meeting_runs m JOIN decisions d ON d.id = m.decision_id "
            "WHERE m.id = :m"), {"m": convened[0]["run_id"]}).scalar()
    assert status not in ("approved", "executed", "completed")
    assert not _builds(engine, body["workflow_run_id"])


def test_a_bug_keeps_the_small_path(client, engine, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    started: list = []

    def start(connection, **kw):
        started.append(kw)
        return "crun_bug_1"

    monkeypatch.setattr("boss_engine.services.code_task_service.start_code_task", start)
    monkeypatch.setattr("boss_engine.services.build_budget.bind", lambda *a, **kw: None)
    body = _launch(client, prompt="The notes list shows deleted notes.", mode="bug")
    assert body["run_id"] == "crun_bug_1" and body["stage"] == "build"
    assert "workflow_run_id" not in body and body["spec"]["spec_first"] is False
    assert len(started) == 1
    with engine.begin() as connection:
        assert not connection.execute(text(
            "SELECT 1 FROM meeting_runs WHERE project_id = :p"),
            {"p": body["project_id"]}).first()
        assert not connection.execute(text(
            "SELECT 1 FROM workflow_runs WHERE project_id = :p"),
            {"p": body["project_id"]}).first()
