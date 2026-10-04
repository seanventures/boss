"""A workflow that cannot run on this project is refused before anything is spent — and a run
that tells people the outcome really tells them.

THE INCIDENTS (boss-1, 2026-10-02). Every catalogue template but one ends in `deploy`, and the
install had no deployment host and no target: a run would pay for research, a panel, a build and
a launch-readiness repair, wait for a person to approve the release, and fail at the last step.
The email examples failed at an approved send with no SMTP; meeting-to-tickets at an approved
ticket creation, because every tracker was seeded `active` with an empty configuration. And the
`notify` step that ends three templates recorded `status: recorded` and went green having told
nobody.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_catalogue as wc
from boss_engine.services import workflow_readiness as ready
from boss_engine.services import workflows

from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    Ctx,
    RecordingRegistry,
    _publish,
    _start,
    _steps,
    eng,
    project,
)


@pytest.fixture(autouse=True)
def _tidy(eng, project):
    """The rows these tests add, removed before the shared fixture deletes the project — roles
    and the rest have no ON DELETE CASCADE, so leaving them would fail every later setup."""
    yield
    with eng.begin() as c:
        for table in ("roles", "entitlement_sources", "github_repository_bindings",
                      "github_connections", "deployment_hosts", "work_tracker_providers",
                      "workflow_schedules"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"),  # noqa: S608 - test
                      {"p": project})


class Settings:
    """Nothing configured in the environment, so every answer comes from the project."""

    specpack_root = None
    openai_api_key = ""
    target_github_repo = ""
    target_github_owner = ""
    github_token = ""
    email_smtp_host = None
    email_from_address = None
    email_real_send_allowed = False
    mattermost_real_send_allowed = False
    mattermost_base_url = ""
    mattermost_bot_token = ""


def _catalogue_version(c, project_id: str, key: str) -> str:
    entry = next(e for e in wc.CATALOGUE if e["key"] == key)
    _definition, version = _publish(c, project_id, f"{key}_{uuid.uuid4().hex[:6]}",
                                    wc.entry_spec(entry)["steps"], entry["steps"][0]["id"])
    return version["id"]


#: Every agent Standard Product Delivery names: its default step agents and its release approver.
STANDARD_AGENTS = sorted(
    {s["captain"] for s in next(e for e in wc.CATALOGUE
                                if e["key"] == "standard_product_delivery")["steps"]
     if s.get("captain")} | {"release_manager"})


def _make_ready(c, project_id: str, roles: list[str]) -> None:
    """Everything a delivery template needs: a model, a repository, a host, its agents."""
    c.execute(text(
        "INSERT INTO entitlement_sources (id, project_id, provider, source_id, source_kind, "
        "entitlement_kind, owner_type, owner_id, connection_status) VALUES "
        "(:i, :p, 'anthropic', :s, 'api', 'api_key', 'project', :p, 'connected')"),
        # A source the router can drive (model_routing.choose): a connected Claude Code
        # subscription. A connected row nothing can build an adapter for is not a model.
        {"i": f"ent_{uuid.uuid4().hex[:8]}", "p": project_id, "s": "anthropic_claude_code"})
    connection_id = f"ghc_{uuid.uuid4().hex[:8]}"
    c.execute(text(
        "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
        "credential_ref, status, created_by) VALUES (:i, :p, 'test', 'personal_access_token', 'env:X', "
        "'active', 'test')"), {"i": connection_id, "p": project_id})
    c.execute(text(
        "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
        "full_name, created_by) VALUES (:i, :p, :c, 'acme', 'app', 'acme/app', 'test')"),
        {"i": f"ghb_{uuid.uuid4().hex[:8]}", "p": project_id, "c": connection_id})
    c.execute(text(
        "INSERT INTO deployment_hosts (id, project_id, name, address, status) "
        "VALUES (:i, :p, 'box', '10.0.0.1', 'verified')"),
        {"i": f"dh_{uuid.uuid4().hex[:8]}", "p": project_id})
    for role in roles:
        c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name) "
                       "VALUES (:i, :p, :k, :k) ON CONFLICT DO NOTHING"),
                  {"i": f"role_{uuid.uuid4().hex[:10]}", "p": project_id, "k": role})


def test_a_template_on_a_bare_project_says_exactly_what_is_missing(eng, project):
    with eng.begin() as c:
        version_id = _catalogue_version(c, project, "standard_product_delivery")
        result = ready.readiness_of_version(c, Settings(), project, version_id=version_id)
    missing = {m["key"]: m for m in result["missing"]}
    assert set(missing) >= {"model", "repository", "deployment_target",
                            "role:release_manager"}
    # REWRITTEN: this used to expect 'role:pm' too. 'pm' is the approver named on the PRD
    # approval, which the panel answers automatically — no person or agent acts on it — so
    # requiring the agent refused every delivery template for a step nobody performs (review of
    # the readiness change). The release approver, who does act, is still required.
    # Agent World: the catalogue names default agents (workflow_catalogue.DEFAULT_STEP_AGENTS),
    # but a catalogue DEFAULT is not a need — without it the step runs on Auto
    # (step_agents.is_default_captain) — so 'pm' is still not required.
    assert "role:pm" not in missing
    assert missing["deployment_target"]["needed_by"][0]["step_id"] == "deploy"
    # The release is a person's gate, so the host can still be added while the run is going.
    assert missing["deployment_target"]["after_a_person"] is True
    # The model is needed by the very first step — there is no later chance.
    assert missing["model"]["after_a_person"] is False
    assert "Infra" in missing["deployment_target"]["fix"]


def test_start_is_refused_until_it_is_ready_and_then_it_is_not(eng, project):
    with eng.begin() as c:
        version_id = _catalogue_version(c, project, "standard_product_delivery")
        with pytest.raises(ready.WorkflowNotReadyError) as refused:
            ready.refuse_unless_ready(c, Settings(), project, version_id=version_id)
        assert "cannot run here yet" in str(refused.value)
        assert "Somewhere to deploy" in str(refused.value)

        _make_ready(c, project, STANDARD_AGENTS)
        result = ready.refuse_unless_ready(c, Settings(), project, version_id=version_id)
        assert result["ready"] and result["missing"] == []


def test_starting_anyway_covers_only_what_the_operator_was_shown(eng, project):
    with eng.begin() as c:
        version_id = _catalogue_version(c, project, "standard_product_delivery")
        _make_ready(c, project, STANDARD_AGENTS)
        c.execute(text("DELETE FROM deployment_hosts WHERE project_id = :p"), {"p": project})
        # One thing missing, accepted by name: it starts, and the run says so on its record.
        result = ready.refuse_unless_ready(c, Settings(), project, version_id=version_id,
                                           start_anyway=["deployment_target"])
        assert result["started_anyway"] == ["deployment_target"]
        ready.record_started_anyway(c, project, "ceo", "wfr_test", result)
        event = c.execute(text("SELECT data FROM events WHERE project_id = :p AND "
                               "event_type = 'workflow.started_unready'"), {"p": project}).scalar()
        assert event["missing"][0]["key"] == "deployment_target"
        # Something else goes missing that nobody was shown: refused again. (The release
        # approver — 'pm' used to be removed here, and is no longer a need; see above.)
        c.execute(text("DELETE FROM roles WHERE project_id = :p AND role_key = 'release_manager'"),
                  {"p": project})
        with pytest.raises(ready.WorkflowNotReadyError) as refused:
            ready.refuse_unless_ready(c, Settings(), project, version_id=version_id,
                                      start_anyway=["deployment_target"])
        assert [m["key"] for m in refused.value.readiness["missing"]
                if m["key"] not in refused.value.readiness["started_anyway"]] == [
                    "role:release_manager"]


def test_a_system_workflow_is_not_held_to_a_product_preflight(eng, project):
    with eng.begin() as c:
        definition = workflows.create_definition(
            c, project_id=project, workflow_key=f"sys_{uuid.uuid4().hex[:6]}", name="sys",
            description=None, actor="test", namespace="system")
        version = workflows.publish_version(
            c, project_id=project, definition_id=definition["id"], actor="test",
            spec={"entry_step": "a", "steps": [{"id": "a", "kind": "decision",
                                                 "question": "ok?", "next": []}]})
        result = ready.refuse_unless_ready(c, Settings(), project, version_id=version["id"])
    assert result["applies"] is False


def _deploy_workflow(c, project_id: str) -> str:
    """A product workflow whose one step deploys — needs a host this project does not have."""
    key = f"weekly_{uuid.uuid4().hex[:6]}"
    definition = workflows.create_definition(
        c, project_id=project_id, workflow_key=key, name=key, description=None,
        actor="test", namespace="product")
    workflows.publish_version(
        c, project_id=project_id, definition_id=definition["id"], actor="test",
        spec={"entry_step": "ship", "steps": [
            {"id": "ship", "kind": "capability",
             "binding": {"capability": "deploy", "inputs": {"run_id": "crun_x"}},
             "next": []}]})
    return key


def _due_schedule(c, project_id: str, key: str, start_anyway: str = "[]") -> str:
    schedule_id = f"wsch_{uuid.uuid4().hex[:8]}"
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    c.execute(text(
        "INSERT INTO workflow_schedules (id, project_id, workflow_key, name, cadence, "
        "minute, hour, weekday, timezone, input, actor, enabled, next_run_at, start_anyway) "
        "VALUES (:i, :p, :k, 'weekly', 'weekly', 0, 8, 0, 'UTC', '{}'::jsonb, 'ceo', true, :t, "
        "CAST(:a AS JSONB))"),
        {"i": schedule_id, "p": project_id, "k": key, "t": past, "a": start_anyway})
    return schedule_id


def test_a_schedule_that_cannot_run_here_records_why_instead_of_running(eng, project):
    """Unattended runs are where a missing mail server hurts most: every Monday, research then
    a failed send. No run is made, and the refusal is kept where people look.

    REWRITTEN. It asserted that `last_error` was the whole readiness summary — which the
    schedule cut at 500 characters, sometimes before the sentence saying what to do — and the
    refusal counted toward the five-strike fuse, so a deploy-ending schedule on a project with
    no host was switched off after five quiet weeks (review of the readiness change). Now:
    a short `last_error`, the missing items WITH their fixes in `last_missing`, a
    `workflow.start_refused` event naming the schedule, and no strike against the fuse —
    nothing was spent."""
    from boss_engine.services import workflow_schedules

    with eng.begin() as c:
        key = _deploy_workflow(c, project)
        schedule_id = _due_schedule(c, project, key)
        fired = workflow_schedules.run_due(c, project_id=project)
        row = c.execute(text("SELECT * FROM workflow_schedules WHERE id = :i"),
                        {"i": schedule_id}).mappings().first()
        runs = c.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"),
                         {"p": project}).scalar()
        event = c.execute(text(
            "SELECT entity_type, entity_id, data FROM events WHERE project_id = :p "
            "AND event_type = 'workflow.start_refused'"), {"p": project}).mappings().first()
    assert fired and fired[0].get("error") and fired[0]["missing"] == ["deployment_target"]
    assert row["last_run_id"] is None and runs == 0
    assert row["last_error"].startswith("Not started")
    assert "Somewhere to deploy" in row["last_error"] and len(row["last_error"]) < 300
    assert [m["key"] for m in row["last_missing"]] == ["deployment_target"]
    assert "Infra" in row["last_missing"][0]["fix"]
    assert row["enabled"] is True and row["consecutive_failures"] == 0
    assert (event["entity_type"], event["entity_id"]) == ("schedule", schedule_id)
    assert event["data"]["door_name"] == "The schedule 'weekly'"
    assert event["data"]["missing"][0]["fix"]


def test_a_schedule_told_to_start_anyway_starts_and_says_so(eng, project):
    """The unattended form of Start anyway: the operator accepted, when saving the schedule,
    that the host comes later. It fires, and the run records that it began without one."""
    from boss_engine.services import workflow_schedules

    with eng.begin() as c:
        key = _deploy_workflow(c, project)
        schedule_id = _due_schedule(c, project, key, '["deployment_target"]')
        fired = workflow_schedules.run_due(c, project_id=project)
        row = c.execute(text("SELECT last_run_id, last_error, last_missing "
                             "FROM workflow_schedules WHERE id = :i"),
                        {"i": schedule_id}).mappings().first()
        noted = c.execute(text(
            "SELECT data FROM events WHERE project_id = :p AND entity_id = :r "
            "AND event_type = 'workflow.started_unready'"),
            {"p": project, "r": row["last_run_id"]}).scalar()
    assert fired[0].get("run_id") and row["last_error"] is None and row["last_missing"] is None
    assert noted["started_anyway"] == ["deployment_target"] and noted["chosen_by"] == "operator"


def test_saving_a_schedule_shows_what_its_firing_will_refuse(eng, project):
    """Asked at save, not discovered on Monday. Saving is not refused — setting a schedule up
    before the host is a fair order — but the checklist comes back with it."""
    from boss_engine.services import workflow_schedules

    with eng.begin() as c:
        key = _deploy_workflow(c, project)
        saved = workflow_schedules.create(
            c, project_id=project, workflow_key=key, name="weekly", cadence="weekly",
            minute=0, hour=8, weekday=0, actor="ceo")
        assert saved["readiness"]["will_refuse"] == ["deployment_target"]
        assert saved["start_anyway"] == []
        edited = workflow_schedules.replace_inputs(
            c, project_id=project, schedule_id=saved["id"], actor="ceo", input_data={},
            start_anyway=["deployment_target"])
        assert edited["readiness"]["will_refuse"] == []
        assert edited["start_anyway"] == ["deployment_target"]
        # An edit that does not mention it keeps the choice.
        kept = workflow_schedules.replace_inputs(
            c, project_id=project, schedule_id=saved["id"], actor="ceo", input_data={})
        assert kept["start_anyway"] == ["deployment_target"]
        with pytest.raises(workflow_schedules.ScheduleError):
            workflow_schedules.replace_inputs(
                c, project_id=project, schedule_id=saved["id"], actor="ceo", input_data={},
                start_anyway="deployment_target")


def test_an_event_that_cannot_start_its_workflow_says_so_and_can_be_told_to(eng, project):
    """The event door refused and only LOGGED, so a subscription that started runs yesterday
    silently stopped (review of the readiness change). A refusal is now an event naming the
    subscription; `config.start_anyway` is the operator's accepted exception."""
    from boss_engine.services import event_dispatch
    from boss_engine.services import event_subscription_service as subs

    with eng.begin() as c:
        key = _deploy_workflow(c, project)
        made = subs.create_event_subscription(
            c, project, "meeting.completed", "workflow", key, actor="test")
        assert made["readiness"]["will_refuse"] == ["deployment_target"]
        fired = event_dispatch.dispatch_event(
            c, event_id=f"ev_{uuid.uuid4().hex[:8]}", project_id=project,
            event_type="meeting.completed", actor="test", entity_type="meeting",
            entity_id="m1", data={})
        refused = c.execute(text(
            "SELECT entity_id, data FROM events WHERE project_id = :p "
            "AND event_type = 'workflow.start_refused'"), {"p": project}).mappings().first()
        assert fired == [{"kind": "workflow", "refused": True, "workflow_key": key,
                          "missing": ["deployment_target"]}]
        assert refused["entity_id"] == made["id"]
        assert refused["data"]["missing"][0]["fix"]

        updated = subs.update_event_subscription(
            c, project, made["id"], config={"start_anyway": ["deployment_target"]},
            actor="test")
        assert updated["readiness"]["will_refuse"] == []
        again = event_dispatch.dispatch_event(
            c, event_id=f"ev_{uuid.uuid4().hex[:8]}", project_id=project,
            event_type="meeting.completed", actor="test", entity_type="meeting",
            entity_id="m2", data={})
        c.execute(text("DELETE FROM event_subscriptions WHERE project_id = :p"), {"p": project})
    assert again[0].get("run_id")


def test_a_run_started_by_an_approved_build_carries_what_is_missing(eng, project):
    """start_project_run cannot refuse — the build is already going — and was exempt from the
    check outright, so the request_meeting → PRD → build path got no warning while the console
    refused the same template. Its run now carries the list, marked as nobody's choice, and a
    re-run of it does not inherit an acceptance nobody made."""
    with eng.begin() as c:
        version_id = _catalogue_version(c, project, "standard_product_delivery")
        run = workflows.start_project_run(
            c, project_id=project, actor="ceo", title="Shift swap board",
            directive="Let nurses swap shifts without a phone tree.",
            workflow_version_id=version_id)
        noted = c.execute(text(
            "SELECT data FROM events WHERE project_id = :p AND entity_id = :r "
            "AND event_type = 'workflow.started_unready'"),
            {"p": project, "r": run["id"]}).scalar()
        assert noted["chosen_by"] == "nobody"
        assert "deployment_target" in {m["key"] for m in noted["missing"]}
        assert ready.accepted_by_run(c, project, run["id"]) == []


def test_a_rerun_repeats_the_choice_made_for_the_run_it_repeats(eng, project):
    with eng.begin() as c:
        ready.record_started_anyway(c, project, "ceo", "wfr_original", {
            "started_anyway": ["deployment_target"],
            "missing": [{"key": "deployment_target", "label": "x", "first_step": "deploy"}]})
        assert ready.accepted_by_run(c, project, "wfr_original") == ["deployment_target"]
        assert ready.accepted_by_run(c, project, "wfr_other") == []


def test_no_gate_a_person_never_answers_requires_its_approver():
    """`approver_role` on a step the panel answers (the PRD approval) is not a need: nobody
    acts on it. Every catalogue template is enumerated."""
    from boss_engine.services import workflow_capabilities as caps

    for entry in wc.CATALOGUE:
        spec = wc.entry_spec(entry)
        for item in ready.requirements(spec):
            step = next(s for s in spec["steps"] if s["id"] == item["step_id"])
            capability = (step.get("binding") or {}).get("capability")
            approver = (step.get("approval") or {}).get("approver_role")
            if (approver and item["key"] == f"role:{approver}"
                    and capability in caps.CAPABILITY_KEYS
                    and step.get("captain") != approver):
                assert caps.get(capability).answered_by == "person", (entry["key"], item)


# ── notify: a real channel, or a visible skip, never a green no-op ───────────────────────────

def _notify_run(c, project_id: str, channel: str | None):
    _definition, version = _publish(c, project_id, f"tell_{uuid.uuid4().hex[:6]}", [
        {"id": "tell", "kind": "capability",
         "binding": {"capability": "notify",
                     "inputs": {"message": {"from_input": "title"},
                                "channel": {"from_input": "notify_channel"}}},
         "next": []}], "tell")
    run_input = {"title": "Shift swap board"}
    if channel:
        run_input["notify_channel"] = channel
    return _start(c, project_id, version["id"], run_input)


def test_a_notify_with_no_channel_is_skipped_and_says_so(eng, project, monkeypatch):
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    with eng.begin() as c:
        run = _notify_run(c, project, None)
        runner.tick(c, Ctx(eng), project_id=project)
        step = _steps(c, run["id"])["tell"]
    assert step["output"]["status"] == "not_applicable"
    assert step["output"]["posted"] is False
    assert "Nobody was told" in step["output"]["note"]


def test_a_notify_to_a_channel_it_cannot_use_fails_with_the_fix(eng, project, monkeypatch):
    from boss_engine.services import email_adapter
    from boss_engine.services.github_adapter import IntegrationConfigurationError

    def refuse(self, channel, subject, body):
        raise IntegrationConfigurationError("BOSS_EMAIL_SMTP_HOST is not configured.")

    monkeypatch.setattr(email_adapter.EmailAdapter, "send_message", refuse)
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    with eng.begin() as c:
        run = _notify_run(c, project, "email:team@example.com")
        runner.tick(c, Ctx(eng), project_id=project)
        step = _steps(c, run["id"])["tell"]
    assert step["status"] != "succeeded"
    error = step["last_error"] or step["output"].get("error") or ""
    assert "BOSS_EMAIL_SMTP_HOST is not configured" in error
    # REWRITTEN: this asserted the fix was "/v1/email/config" — an API call, which an operator
    # at the console cannot make (review of the readiness change). It names the console form,
    # which moved to Skills & MCP › Email (skills-mcp spec §5.1).
    assert "Skills & MCP → Email" in error


def test_a_notify_is_sent_once_and_carries_the_outcome(eng, project, monkeypatch):
    from boss_engine.services import email_adapter

    sent: list[tuple[str, str, str]] = []

    def deliver(self, channel, subject, body):
        sent.append((channel, subject, body))
        return f"smtp:{channel.split(':', 1)[1]}"

    monkeypatch.setattr(email_adapter.EmailAdapter, "send_message", deliver)
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    with eng.begin() as c:
        run = _notify_run(c, project, "email:team@example.com")
        runner.tick(c, Ctx(eng), project_id=project)
        step = _steps(c, run["id"])["tell"]
        # A retry of a step that already reached its channel does not send again.
        again = runner._notify(c, Ctx(eng), project, {"id": "tell"},  # noqa: SLF001
                               {"run_id": run["id"], "input": {}},
                               {"message": "x", "channel": "email:team@example.com"})
    assert step["status"] == "succeeded"
    assert step["output"]["posted"] is True
    assert step["output"]["receipt"] == "smtp:team@example.com"
    assert len(sent) == 1
    assert "Shift swap board" in sent[0][1]
    assert again["already_sent"] is True and len(sent) == 1


# ── a tracker counts only when it is connected ───────────────────────────────────────────────

def test_a_seeded_tracker_with_no_configuration_is_not_connected(eng, project, monkeypatch):
    from boss_engine.services import skill_executors
    from boss_engine.services.work_orchestration import connected_tracker

    for name in ("PLANE_API_KEY", "JIRA_API_TOKEN", "LINEAR_API_KEY", "GITHUB_TOKEN",
                 "BOSS_GITHUB_TOKEN", "AZURE_DEVOPS_PAT", "ASANA_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with eng.begin() as c:
        # Exactly what bootstrap seeds: active, default, and nothing to connect with.
        c.execute(text(
            "INSERT INTO work_tracker_providers (id, project_id, provider_key, display_name, "
            "status, is_default, configuration) VALUES (:i, :p, 'plane', 'Plane', 'active', "
            "true, CAST(:cfg AS JSONB))"),
            {"i": f"tracker_{uuid.uuid4().hex[:8]}", "p": project,
             "cfg": '{"api_base_url": "", "credential_ref": "secret:PLANE_API_KEY"}'})
        assert connected_tracker(c, project) is None
        ok, detail = ready.check(c, Settings(), project, "work_tracker")
        assert not ok and "not configured" in detail

        class Context:
            connection = c
            project_id = project

        with pytest.raises(ValueError) as refused:
            skill_executors.execute_tracker_issues_create(
                Context(), {"actions": [{"title": "Follow up"}]})
        assert "no connected work tracker" in str(refused.value)

        c.execute(text(
            "UPDATE work_tracker_providers SET configuration = CAST(:cfg AS JSONB) "
            "WHERE project_id = :p"),
            {"p": project, "cfg": '{"api_base_url": "https://plane.example.com", '
                                  '"workspace_slug": "w", "project_id": "x", '
                                  '"api_token": "t"}'})
        assert connected_tracker(c, project)["provider_key"] == "plane"
