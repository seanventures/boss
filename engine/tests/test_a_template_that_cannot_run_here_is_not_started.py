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


def _make_ready(c, project_id: str, roles: list[str]) -> None:
    """Everything a delivery template needs: a model, a repository, a host, its agents."""
    c.execute(text(
        "INSERT INTO entitlement_sources (id, project_id, provider, source_id, source_kind, "
        "entitlement_kind, owner_type, owner_id, connection_status) VALUES "
        "(:i, :p, 'openai', :s, 'api', 'api_key', 'project', :p, 'connected')"),
        {"i": f"ent_{uuid.uuid4().hex[:8]}", "p": project_id, "s": f"src_{uuid.uuid4().hex[:6]}"})
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
    assert set(missing) >= {"model", "repository", "deployment_target", "role:pm",
                            "role:release_manager"}
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

        _make_ready(c, project, ["pm", "release_manager"])
        result = ready.refuse_unless_ready(c, Settings(), project, version_id=version_id)
        assert result["ready"] and result["missing"] == []


def test_starting_anyway_covers_only_what_the_operator_was_shown(eng, project):
    with eng.begin() as c:
        version_id = _catalogue_version(c, project, "standard_product_delivery")
        _make_ready(c, project, ["pm", "release_manager"])
        c.execute(text("DELETE FROM deployment_hosts WHERE project_id = :p"), {"p": project})
        # One thing missing, accepted by name: it starts, and the run says so on its record.
        result = ready.refuse_unless_ready(c, Settings(), project, version_id=version_id,
                                           start_anyway=["deployment_target"])
        assert result["started_anyway"] == ["deployment_target"]
        ready.record_started_anyway(c, project, "ceo", "wfr_test", result)
        event = c.execute(text("SELECT data FROM events WHERE project_id = :p AND "
                               "event_type = 'workflow.started_unready'"), {"p": project}).scalar()
        assert event["missing"][0]["key"] == "deployment_target"
        # Something else goes missing that nobody was shown: refused again.
        c.execute(text("DELETE FROM roles WHERE project_id = :p AND role_key = 'pm'"),
                  {"p": project})
        with pytest.raises(ready.WorkflowNotReadyError) as refused:
            ready.refuse_unless_ready(c, Settings(), project, version_id=version_id,
                                      start_anyway=["deployment_target"])
        assert [m["key"] for m in refused.value.readiness["missing"]
                if m["key"] not in refused.value.readiness["started_anyway"]] == ["role:pm"]


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


def test_a_schedule_that_cannot_run_here_records_why_instead_of_running(eng, project):
    """Unattended runs are where a missing mail server hurts most: every Monday, research then
    a failed send. The schedule's last_error is the readiness sentence and no run is made."""
    from boss_engine.services import workflow_schedules

    with eng.begin() as c:
        key = f"weekly_{uuid.uuid4().hex[:6]}"
        definition = workflows.create_definition(
            c, project_id=project, workflow_key=key, name=key, description=None,
            actor="test", namespace="product")
        workflows.publish_version(
            c, project_id=project, definition_id=definition["id"], actor="test",
            spec={"entry_step": "ship", "steps": [
                {"id": "ship", "kind": "capability",
                 "binding": {"capability": "deploy", "inputs": {"run_id": "crun_x"}},
                 "next": []}]})
        schedule_id = f"wsch_{uuid.uuid4().hex[:8]}"
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        c.execute(text(
            "INSERT INTO workflow_schedules (id, project_id, workflow_key, name, cadence, "
            "minute, hour, weekday, timezone, input, actor, enabled, next_run_at) VALUES "
            "(:i, :p, :k, 'weekly', 'weekly', 0, 8, 0, 'UTC', '{}'::jsonb, 'ceo', true, :t)"),
            {"i": schedule_id, "p": project, "k": key, "t": past})
        fired = workflow_schedules.run_due(c, project_id=project)
        row = c.execute(text("SELECT last_error, last_run_id FROM workflow_schedules "
                             "WHERE id = :i"), {"i": schedule_id}).mappings().first()
        runs = c.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"),
                         {"p": project}).scalar()
    assert fired and fired[0].get("error")
    assert "cannot run here yet" in row["last_error"]
    assert "deploy" in row["last_error"].lower()
    assert row["last_run_id"] is None and runs == 0


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
    assert "/v1/email/config" in error


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
