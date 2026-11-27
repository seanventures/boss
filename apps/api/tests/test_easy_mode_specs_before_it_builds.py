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
import pytest

from sqlalchemy import text

from apps.api.app.config import get_settings


@pytest.fixture(autouse=True)
def _the_org_asks_in_development(engine):
    """These pin what a PERSON is asked, so the org's autonomy policy says ask in development
    (BOSSAPP-1441); dev autonomy's default is pinned in test_dev_autonomy_api."""
    from boss_engine.config import get_settings
    from boss_engine.services import autonomy_policy

    org = get_settings().default_org_id
    with engine.begin() as c:
        autonomy_policy.save(c, kind=autonomy_policy.ORG, scope_id=org, raw={"dev": "ask"},
                             actor="test")
    yield
    with engine.begin() as c:
        c.execute(text("DELETE FROM autonomy_policies WHERE scope_id = :o"), {"o": org})


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


def _finish(engine, meeting_run_id, *, auto_approve_setting=False):
    """The PRD a panel would write lands where its phases write it; the real finalizer runs."""
    from boss_engine.services.meeting_service import _finalize_meeting

    settings = get_settings().model_copy(update={
        "meeting_prd_auto_approve": auto_approve_setting, "auto_code_task_after_prd": False})
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE meeting_runs SET state = jsonb_build_object('prd_markdown', "
            "CAST(:prd AS text)) WHERE id = :id"), {"id": meeting_run_id, "prd": PRD})
        _finalize_meeting(connection, settings, meeting_run_id)


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
    convened = _panel(monkeypatch, engine, finish=False)

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
    # The start's approval is a SERVER-ONLY input naming who started it, never `spec_approval`.
    assert "spec_approval" not in run["input"]
    assert run["input"]["_spec_approved_by_start"]
    assert run["input"]["repository"] == body["spec"]["local_repo"]

    _tick(client, project_id)
    assert len(convened) == 1
    meeting_run_id = convened[0]["run_id"]
    assert "spec_approval" not in convened[0]["args"]
    _finish(engine, meeting_run_id, auto_approve_setting=False)
    built = _tick_until_built(client, engine, project_id, workflow_run_id)

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
    assert meeting["constraints"]["approved_by_start"] == run["input"]["_spec_approved_by_start"]
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
    # A failure that is not the person's own model (here: no answer) still builds from the brief,
    # and the failed panel's hold never blocks that build (stranger test r8's budget_wait).
    assert not _active_hold(engine, convened[0]["run_id"])
    assert "THE PRODUCT SPEC BELOW IS THE BRIEF" not in built[0]["spec"]["instructions"]
    assert "notes app" in built[0]["spec"]["instructions"]
    with engine.begin() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM events WHERE event_type = 'workflow.spec_fallback' AND entity_id = :w"),
            {"w": workflow_run_id}).scalar() == 1


def _active_hold(engine, meeting_run_id):
    with engine.begin() as connection:
        return connection.execute(text(
            "SELECT r.id FROM meeting_runs m JOIN budget_reservations r ON "
            "r.id = m.finalize_ctx->>'reservation_id' WHERE m.id = :m AND r.status = 'active'"),
            {"m": meeting_run_id}).scalar()


#: env_refs.CredentialMisdirected's words for a plain-http model (#227; stranger test r8).
PLAIN_HTTP = ("Your model at http://10.0.0.7:8080 is plain http; BOSS only sends keys over https. "
              "Use an https address (Setup › Models).")


def test_a_panel_whose_model_is_unusable_waits_on_the_person_and_builds_nothing(
        client, engine, monkeypatch, tmp_path):
    """STRANGER TEST r8: the panel failed on a plain-http model and the workflow went on to
    "Build, review and test" without a spec — a build on the same model, failing the same way.
    A person-side panel failure parks the workflow on the person's item; their "Done — retry"
    resumes the panel, and the build runs from its PRD."""
    from boss_engine.services import failure_owner as fo

    _setup(monkeypatch, tmp_path)
    convened = _panel(monkeypatch, engine, finish=False)
    body = _launch(client, prompt="Build a CSV quality desk.")
    project_id, workflow_run_id = body["project_id"], body["workflow_run_id"]
    _tick(client, project_id)
    assert convened
    meeting_run_id = convened[0]["run_id"]
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE meeting_runs SET status = 'failed', error = :e, state = state || "
            "jsonb_build_object('failure_class', 'needs_human', 'reason_code', "
            "'credential_plain_http') WHERE id = :m"), {"m": meeting_run_id, "e": PLAIN_HTTP})
    for _ in range(3):
        _tick(client, project_id)
    assert not _builds(engine, workflow_run_id), "nothing is built on a model that cannot answer"
    step = _step(engine, workflow_run_id, "meeting")
    assert step["status"] == "waiting" and "spec_fallback" not in step["output"]
    assert step["output"]["waiting_on_person"] == PLAIN_HTTP
    assert step["output"]["awaited_note"].startswith("Waiting on you: Your model at")
    assert not _active_hold(engine, meeting_run_id), "the failed panel holds no budget"

    # The person fixed the model and pressed Done — retry on the panel's item.
    did = fo.SURFACES["meeting"].person_retry(
        engine, get_settings(), None,
        {"id": meeting_run_id, "project_id": project_id, "hints": {}}, "user:ceo")
    assert "resuming the panel" in did
    _finish(engine, meeting_run_id)
    built = _tick_until_built(client, engine, project_id, workflow_run_id)
    assert "THE PRODUCT SPEC BELOW IS THE BRIEF" in built[0]["spec"]["instructions"]
    step = _step(engine, workflow_run_id, "meeting")
    assert step["status"] == "succeeded" and "waiting_on_person" not in step["output"]


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
    assert not _active_hold(engine, meeting_run_id), "a stopped panel's hold is released"
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


def _deploy(engine, project_id, workflow_run_id, run_id="crun_x"):
    from boss_engine.services import workflow_capability_runner as runner
    from boss_engine.services.workflow_capabilities import get as capability

    with engine.begin() as connection:
        return runner._perform_capability(
            connection, None, project_id=project_id, actor="ceo",
            step={"id": "deploy"}, context={"run_id": workflow_run_id, "input": {}}, key="deploy",
            capability=capability("deploy"),
            inputs={"run_id": run_id, "environment": "staging", "skip_without_host": True})


def test_with_no_server_the_deploy_is_skipped_in_words(client, engine, monkeypatch, tmp_path):
    """MOST EASY MODE PEOPLE HAVE NO HOST (a friend on a laptop): the deploy step neither fails
    nor asks to pick one. It completes as skipped, saying the app runs as its preview."""
    from boss_engine.services.workflow_capability_runner import NO_HOST_WORDS

    _setup(monkeypatch, tmp_path)
    _panel(monkeypatch, engine, finish=False)
    body = _launch(client, prompt="Build a small notes app with pins.")
    project_id = body["project_id"]
    with engine.begin() as connection:
        assert not connection.execute(text(
            "SELECT 1 FROM deployment_targets WHERE project_id = :p"), {"p": project_id}).first()

    out = _deploy(engine, project_id, body["workflow_run_id"])

    assert out["status"] == "skipped" and out["skipped"] == "no_host"
    assert out["note"] == NO_HOST_WORDS == ("Your app runs as a preview. Add a server in Infra "
                                            "to deploy it.")
    with engine.begin() as connection:
        said = connection.execute(text(
            "SELECT data->>'reason' FROM events WHERE project_id = :p "
            "AND event_type = 'workflow.step_skipped'"), {"p": project_id}).scalar()
    assert said == NO_HOST_WORDS
    # The catalogue's deploy step carries the opt-in; no other deploy step skips.
    from boss_engine.services.workflow_catalogue import CATALOGUE

    flagged = {e["key"] for e in CATALOGUE for s in e["steps"]
               if (s.get("binding") or {}).get("inputs", {}).get("skip_without_host")}
    assert flagged == {"easy_mode_mvp"}


def test_with_a_server_the_deploy_is_not_skipped(client, engine, monkeypatch, tmp_path):
    from boss_engine.services import kit_connections

    _setup(monkeypatch, tmp_path)
    _panel(monkeypatch, engine, finish=False)
    body = _launch(client, prompt="Build a small notes app with dates.")
    from boss_engine.services import workflow_capability_runner as runner

    monkeypatch.setattr(kit_connections, "verified_hosts", lambda *a, **kw: 1)
    # The real deploy path's first act is to wait for the merge: reaching it is the point.
    monkeypatch.setattr(runner, "_merge_wait", lambda *a, **kw: {"status": "awaiting",
                                                                 "reached": "merge_wait"})
    out = _deploy(engine, body["project_id"], body["workflow_run_id"])
    assert out == {"status": "awaiting", "reached": "merge_wait"}


def test_another_workflow_run_skips_nothing_and_its_deploy_goes_on(client, engine, monkeypatch,
                                                                    tmp_path):
    """skip_without_host is Easy Mode's own: on any other run the step is not skipped."""
    _setup(monkeypatch, tmp_path)
    _panel(monkeypatch, engine, finish=False)
    body = _launch(client, prompt="Build a small notes app with stars.")
    from boss_engine.services import workflow_capability_runner as runner

    monkeypatch.setattr(runner, "_merge_wait", lambda *a, **kw: {"status": "awaiting",
                                                                 "reached": "merge_wait"})
    out = _deploy(engine, body["project_id"], "wfr_not_easy_mode")
    assert out == {"status": "awaiting", "reached": "merge_wait"}


def test_a_start_cannot_claim_the_easy_mode_approval(client, engine, monkeypatch, tmp_path):
    """REVIEW OF #233: approval by an Easy Mode start is server-only. A start of another workflow
    that says `spec_approval: "start"` (and forges the server key) leaves the PRD pending where
    a person approves PRDs (BOSS_MEETING_PRD_AUTO_APPROVE off, as under enforce)."""
    from apps.api.app import main as api_main

    monkeypatch.setattr(api_main.settings, "meeting_prd_auto_approve", False)
    project_id = get_settings().default_project_id
    installed = client.post("/v1/delivery-workflows/catalogue/install",
                            json={"project_id": project_id, "actor": "ceo"})
    assert installed.status_code == 200, installed.text
    convened = _panel(monkeypatch, engine, finish=False)
    body = {"project_id": project_id, "actor": "ceo", "workflow_key": "minor_feature",
            "input": {"title": "CSV export", "directive": "Add a Download CSV button.",
                      "spec_approval": "start", "_spec_approved_by_start": "usr_forged"}}
    started = client.post("/v1/delivery-workflows/start", json=body)
    if started.status_code == 409 and isinstance(started.json().get("detail"), dict):
        missing = [m["key"] for m in started.json()["detail"]["readiness"]["missing"]]
        started = client.post("/v1/delivery-workflows/start",
                              json={**body, "start_anyway": missing})
    assert started.status_code == 200, started.text
    run_input = started.json()["run"]["input"]
    assert "_spec_approved_by_start" not in run_input
    for _ in range(3):
        _tick(client, project_id)
        if convened:
            break
    assert convened
    with engine.begin() as connection:
        constraints = connection.execute(text(
            "SELECT constraints FROM meeting_runs WHERE id = :m"),
            {"m": convened[0]["run_id"]}).scalar()
    assert constraints.get("spec_approval") != "start" and not constraints.get("approved_by_start")
    _finish(engine, convened[0]["run_id"], auto_approve_setting=False)
    with engine.begin() as connection:
        status = connection.execute(text(
            "SELECT d.status FROM meeting_runs m JOIN decisions d ON d.id = m.decision_id "
            "WHERE m.id = :m"), {"m": convened[0]["run_id"]}).scalar()
    assert status == "pending"


def test_a_panel_that_finished_meanwhile_is_not_replaced(client, engine, monkeypatch, tmp_path):
    """REVIEW OF #233: the panel finishes between the tick reading it and the fallback's cancel;
    the cancel changes nothing, so the fallback steps back (no second build from the brief)."""
    from boss_engine.services import workflow_capability_runner as runner

    _setup(monkeypatch, tmp_path)
    convened = _panel(monkeypatch, engine, finish=False)
    body = _launch(client, prompt="Build a small notes app with folders.")
    _tick(client, body["project_id"])
    assert convened
    meeting_run_id = convened[0]["run_id"]
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE meeting_runs SET status = 'completed', "
            "created_at = now() - interval '30 minutes' WHERE id = :m"), {"m": meeting_run_id})
        step = connection.execute(text(
            "SELECT id, step_id FROM workflow_step_runs WHERE workflow_run_id = :w "
            "AND step_id = 'meeting'"), {"w": body["workflow_run_id"]}).mappings().first()
        out = runner._panel_fallback(
            connection, project_id=body["project_id"], run_id=body["workflow_run_id"],
            step=dict(step), output={}, meeting_run_id=meeting_run_id, status="running",
            ok_states=frozenset({"completed"}), bad_states=frozenset({"failed", "cancelled"}))
    assert out is None
    assert _step(engine, body["workflow_run_id"], "meeting")["status"] == "waiting"


def test_easy_mode_deploys_only_a_merge_on_record(client, engine, monkeypatch, tmp_path):
    """REVIEW OF #233: a merge approval that chose a MANUAL merge lets the ordinary wait go on;
    Easy Mode's staging deploy needs no person, so it waits until a merge is on record."""
    from boss_engine.services import kit_connections
    from boss_engine.services import workflow_capability_runner as runner

    _setup(monkeypatch, tmp_path)
    _panel(monkeypatch, engine, finish=False)
    body = _launch(client, prompt="Build a small notes app with labels.")
    project_id = body["project_id"]
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, pull_request_url, "
            "pull_request_number, spec, state, finalize_ctx) VALUES ('crun_easy_pr', :p, "
            "'completed', 'done', 'Notes', 'https://github.com/acme/notes/pull/7', 7, "
            "'{}'::jsonb, '{}'::jsonb, '{}'::jsonb)"), {"p": project_id})
    monkeypatch.setattr(kit_connections, "verified_hosts", lambda *a, **kw: 1)
    # The ordinary wait lets it through (as a manual-merge approval does).
    monkeypatch.setattr(runner, "_merge_wait", lambda *a, **kw: None)

    out = _deploy(engine, project_id, body["workflow_run_id"], run_id="crun_easy_pr")

    assert out["status"] == "awaiting"
    assert out["await"]["kind"] == runner.MERGE_AWAIT_KIND and out["await"]["id"] == "crun_easy_pr"


def test_panel_question_holds_build_even_when_the_start_covered_spec_approval(
        client, engine, monkeypatch, tmp_path):
    from boss_engine.services.meeting_service import _finalize_meeting, panel_build_readiness

    _setup(monkeypatch, tmp_path)
    convened = _panel(monkeypatch, engine, finish=False)
    body = _launch(client, prompt="Build a local page for public or restricted use.")
    project, workflow = body["project_id"], body["workflow_run_id"]
    _tick(client, project)
    meeting = convened[0]["run_id"]
    question = "Should the report be public or restricted to administrators?"
    draft, status, blocker = panel_build_readiness(PRD + "\nBUILD READINESS: ASK " + question)
    assert (draft, status, blocker) == (PRD.rstrip(), "ask", question)
    settings = get_settings().model_copy(update={"meeting_prd_auto_approve": True})
    with engine.begin() as c:
        c.execute(text(
            "UPDATE meeting_runs SET state = jsonb_build_object('prd_markdown', CAST(:prd AS text), "
            "'build_readiness_question', CAST(:q AS text)) WHERE id = :m"),
            {"prd": draft, "q": blocker, "m": meeting})
        decision = _finalize_meeting(c, settings, meeting)["decision_id"]
    for _ in range(3):
        _tick(client, project)
    with engine.begin() as c:
        row = c.execute(text(
            "SELECT status, question, recommendation FROM decisions WHERE id = :d"),
            {"d": decision}).mappings().one()
    assert (row["status"], row["question"]) == ("pending", question)
    # Its own class, never a stop a person set; the card says why in words.
    assert "explicit_stop" not in row["recommendation"]
    assert row["recommendation"]["build_readiness_question"] == question
    assert question in row["recommendation"]["why"]
    assert not _builds(engine, workflow)
    # THE PERSON'S ANSWER IS THE BUILD'S: approving the card with the answer starts the build,
    # and the brief carries the question and the answer, not the unanswered spec.
    approved = client.post(f"/v1/decisions/{decision}/approve",
                           json={"actor": "ceo", "note": "Administrators only, behind sign-in."})
    assert approved.status_code == 200, approved.text
    built = _tick_until_built(client, engine, project, workflow)
    instructions = built[0]["spec"]["instructions"]
    assert question in instructions and "Administrators only, behind sign-in." in instructions


@pytest.mark.parametrize("tail, expected", [
    ("\n**Build readiness: ready.**", ("ready", None)),
    ("\nbuild readiness - READY\n", ("ready", None)),
    ("\n## Build readiness\n\nREADY", ("ready", None)),
    ("\n## Build readiness\n**ASK:** Public or administrators only?", ("ask", "Public or administrators only?")),
    ("\nBUILD READINESS: ASK\nPublic or administrators only?", ("ask", "Public or administrators only?")),
    ("\nBUILD READINESS: ASK Public or administrators only?\n```", ("ask", "Public or administrators only?")),
    ("\nBuild Readiness \u2014 ASK: Public or administrators only?", ("ask", "Public or administrators only?")),
])
def test_a_readiness_line_is_read_through_formatting(tail, expected):
    """A model's bold, case, punctuation, heading or line break is not a misread; the line
    leaves the document."""
    from boss_engine.services.meeting_service import panel_build_readiness

    document, status, question = panel_build_readiness(PRD + tail)
    assert (status, question) == expected
    assert "readiness" not in document.lower() and "Public or" not in document


class _Says:
    """The facilitator's re-ask, as a model: one canned reply, every prompt kept."""

    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def create_text_response(self, prompt, system_prompt=None):
        self.prompts.append(prompt)
        return "ref", self.reply, {"model": "m", "input_tokens": 10, "output_tokens": 5}


@pytest.mark.parametrize("draft", [
    PRD, PRD + "\nBUILD READINESS: ASK", PRD + "\nBUILD READINESS: ALREADY",
    PRD + "\n```mermaid\nflowchart LR\n  A --> B",          # cut off mid-diagram
])
def test_an_unreadable_readiness_is_asked_again_once_then_assumed_ready(monkeypatch, draft):
    """No disposition, an empty ASK, a garbled one or a PRD cut off mid-mermaid: ONE short re-ask
    for the line alone; still unreadable → assumed ready and said so, never a held build."""
    from boss_engine.services import meeting_service as ms

    monkeypatch.setattr(ms, "kernel_meetings", lambda: False)
    assert ms.panel_build_readiness(draft)[1] == "unreadable"
    garbled = _Says("I think it is mostly fine.")
    document, status, question, _cost, _model = ms.settle_build_readiness(garbled, draft)
    assert (status, question) == ("assumed_ready", None) and len(garbled.prompts) == 1
    assert garbled.prompts[0].startswith(ms.READINESS_REASK) and len(garbled.prompts[0]) < 13_000
    asked = _Says("BUILD READINESS: ASK Public or administrators only?")
    assert ms.settle_build_readiness(asked, draft)[1:3] == ("ask", "Public or administrators only?")
    ready = _Says("BUILD READINESS: READY")
    assert ms.settle_build_readiness(ready, draft)[1:3] == ("ready", None)
    broken = _Says("")
    broken.create_text_response = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down"))
    assert ms.settle_build_readiness(broken, draft)[1] == "assumed_ready"


def test_a_readable_line_is_never_asked_again(monkeypatch):
    from boss_engine.services import meeting_service as ms

    never = _Says("BUILD READINESS: ASK x?")
    assert ms.settle_build_readiness(never, PRD + "\nBUILD READINESS: READY")[1] == "ready"
    assert never.prompts == []


def _finalized_card(engine, client, monkeypatch, tmp_path, state, prompt):
    from boss_engine.services import autonomy_gate, autonomy_policy
    from boss_engine.services.meeting_service import _finalize_meeting

    _setup(monkeypatch, tmp_path)
    convened = _panel(monkeypatch, engine, finish=False)
    body = _launch(client, prompt=prompt)
    _tick(client, body["project_id"])
    meeting = convened[0]["run_id"]
    org = get_settings().default_org_id
    settings = get_settings().model_copy(update={"meeting_prd_auto_approve": False,
                                                 "auto_code_task_after_prd": False})
    with engine.begin() as c:
        autonomy_policy.save(c, kind=autonomy_policy.ORG, scope_id=org, raw={"dev": "act"},
                             actor="test")
        c.execute(text("UPDATE meeting_runs SET state = CAST(:s AS jsonb) WHERE id = :m"),
                  {"s": __import__("json").dumps(state), "m": meeting})
        decision = _finalize_meeting(c, settings, meeting)["decision_id"]
        row = dict(c.execute(text(
            "SELECT id, project_id, status, risk_tier, question, recommendation FROM decisions "
            "WHERE id = :d"), {"d": decision}).mappings().one())
        found = autonomy_gate.route(c, row)
        events = c.execute(text(
            "SELECT event_type FROM events WHERE entity_id = :d"), {"d": decision}).scalars().all()
    return row, found, list(events)


def test_under_agents_act_only_a_parsed_ask_waits(client, engine, monkeypatch, tmp_path):
    """Agents act: the panel's parsed question waits as ask_product_choice; an assumed-ready
    spec is the product manager's to approve, with the assumption on the card and the PRD."""
    question = "Public or administrators only?"
    row, found, _ = _finalized_card(engine, client, monkeypatch, tmp_path, {
        "prd_markdown": PRD, "build_readiness": "ask", "build_readiness_question": question},
        "Build a report page; who may read it is open.")
    assert (found.acts, found.cls) == (False, "ask_product_choice") and question in found.why


def test_an_assumed_ready_spec_is_said_and_routed_normally(client, engine, monkeypatch, tmp_path):
    from boss_engine.services.meeting_service import ASSUMED_READY_NOTE

    prd = PRD + f"\n\n> {ASSUMED_READY_NOTE}\n"
    row, found, events = _finalized_card(engine, client, monkeypatch, tmp_path, {
        "prd_markdown": prd, "build_readiness": "assumed_ready"},
        "Build a recipe list with tags.")
    rec = row["recommendation"]
    assert found.acts and found.cls == "act"
    assert "explicit_stop" not in rec and rec["why"] == ASSUMED_READY_NOTE
    assert ASSUMED_READY_NOTE in rec["args"]["markdown"]
    assert "meeting.readiness_assumed" in events
