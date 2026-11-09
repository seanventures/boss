"""Reverting a workflow by re-saving a previous configuration.

Found by doing it: a bad version was published, the correct spec was submitted again, the API
returned 200 with the old version's number — and the bad version stayed active. The operator
is told their save succeeded and nothing about the workflow has changed.

EXERCISED AGAINST A REAL DATABASE. Twice today a test that read the source passed while the
code did nothing, so this one publishes versions and asks which is live.
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import workflows

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")

PROJECT = "boss-dogfood"


def _spec(seconds: int) -> dict:
    return {"entry_step": "hold",
            "steps": [{"id": "hold", "kind": "timer", "wait_seconds": seconds,
                       "binding": {}, "next": []}]}


@pytest.fixture()
def definition():
    """A throwaway workflow, removed however the test ends."""
    engine = create_engine(DATABASE_URL)
    key = f"revert_probe_{uuid.uuid4().hex[:8]}"
    with engine.begin() as connection:
        made = workflows.create_definition(
            connection, project_id=PROJECT, workflow_key=key, name=key,
            description=None, actor="test", namespace=workflows.PRODUCT_NAMESPACE,
            is_catalogue=False)
    yield engine, made["id"]
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM workflow_definitions WHERE id=:i"), {"i": made["id"]})


def _active(engine, definition_id: str) -> str:
    with engine.begin() as connection:
        return str(connection.execute(
            text("SELECT active_version_id FROM workflow_definitions WHERE id=:i"),
            {"i": definition_id}).scalar_one() or "")


def test_re_saving_an_earlier_spec_makes_that_version_live_again(definition):
    engine, definition_id = definition
    with engine.begin() as connection:
        first = workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(60), actor="test")
    with engine.begin() as connection:
        second = workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(120), actor="test")
    assert _active(engine, definition_id) == second["id"]

    # Submit the FIRST spec again. This is a revert, and it must take effect.
    with engine.begin() as connection:
        again = workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(60), actor="test")

    assert again["duplicate"] is True, "publishing the same content must not make a new version"
    assert again["id"] == first["id"]
    assert again.get("reactivated") is True, "the caller has to be able to tell it took effect"
    assert _active(engine, definition_id) == first["id"], \
        "the workflow is still running the version the operator just replaced"


def test_re_saving_what_is_already_live_changes_nothing(definition):
    """The case the original behaviour was written for, which must keep working."""
    engine, definition_id = definition
    with engine.begin() as connection:
        first = workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(60), actor="test")
    with engine.begin() as connection:
        again = workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(60), actor="test")
    assert again["duplicate"] is True
    assert again.get("reactivated") is not True, "nothing moved, so nothing to report"
    assert _active(engine, definition_id) == first["id"]


def test_the_builder_is_shown_the_version_that_runs(definition):
    """The spec endpoint ordered by version number, so after a revert it returned the spec the
    operator had just replaced — and the builder loads from there. Editing and saving would
    have republished the abandoned version over the one actually in use.

    Asked of the query, not of the source: this is the third source-reading test today that
    would have passed while the code did the wrong thing.
    """
    engine, definition_id = definition
    with engine.begin() as connection:
        first = workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(60), actor="test")
        workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(120), actor="test")
        # Revert: version 1 is live again while version 2 is still the highest number.
        workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(60), actor="test")

    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT v.id AS version_id, v.version FROM workflow_definitions d "
                "LEFT JOIN workflow_versions v ON v.workflow_definition_id = d.id "
                "WHERE d.id=:i "
                "ORDER BY (v.id = d.active_version_id) DESC NULLS LAST, "
                "         v.version DESC NULLS LAST LIMIT 1"
            ),
            {"i": definition_id},
        ).mappings().first()

    assert row["version_id"] == first["id"], (
        "the builder would open the version the operator replaced, "
        f"not the one running (got version {row['version']})")


def test_starting_a_run_uses_the_version_that_is_live(definition):
    """Start, fork, and the agent tool all asked for version DESC.

    After a revert the builder shows the restored spec and Start ran the one the operator
    had just replaced. The schedule path already read active_version_id; these did not.
    """
    engine, definition_id = definition
    with engine.begin() as connection:
        key = connection.execute(
            text("SELECT workflow_key FROM workflow_definitions WHERE id=:i"),
            {"i": definition_id},
        ).scalar_one()
        first = workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(60), actor="test")
        workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(120), actor="test")
        workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(60), actor="test")
        running = workflows.running_version(connection, PROJECT, key)

    assert running is not None
    assert running["version_id"] == first["id"]
    assert running["spec"]["steps"][0]["wait_seconds"] == 60


def test_start_fork_agent_tools_and_defaults_use_version_2_when_3_is_higher(monkeypatch):
    """Version 2 is active and version 3 exists but is not. Highest-number lookup would
    start, fork, describe, and default to version 3 (180 seconds). Those paths must use
    version 2 (120 seconds).
    """
    from boss_engine.services.workflow_explain import explain_version
    from boss_engine.services.workflow_precheck import _resolve_version
    from boss_engine.tools.builtin import build_default_registry

    engine = create_engine(DATABASE_URL)
    token = uuid.uuid4().hex[:8]
    org_id, workspace_id, project_id = f"org_{token}", f"ws_{token}", f"p_{token}"
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO orgs (id, name) VALUES (:id, 'version probe')"), {"id": org_id})
        connection.execute(text(
            "INSERT INTO workspaces (id, org_id, name) VALUES (:id, :org, 'version probe')"),
            {"id": workspace_id, "org": org_id})
        connection.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) "
            "VALUES (:id, :ws, 'version probe', :slug)"),
            {"id": project_id, "ws": workspace_id, "slug": project_id})
        made = workflows.create_definition(
            connection, project_id=project_id,
            workflow_key=workflows.DEFAULT_WORKFLOW_KEY,
            name="Standard Product Delivery", description=None, actor="test",
            namespace=workflows.PRODUCT_NAMESPACE, is_catalogue=False)
        first = workflows.publish_version(
            connection, project_id=project_id, definition_id=made["id"],
            spec=_spec(60), actor="test")
        second = workflows.publish_version(
            connection, project_id=project_id, definition_id=made["id"],
            spec=_spec(120), actor="test")
        third = workflows.publish_version(
            connection, project_id=project_id, definition_id=made["id"],
            spec=_spec(180), actor="test")
        again = workflows.publish_version(
            connection, project_id=project_id, definition_id=made["id"],
            spec=_spec(120), actor="test")
    assert [first["version"], second["version"], third["version"]] == [1, 2, 3]
    assert again["id"] == second["id"] and again["version"] == 2
    assert again.get("reactivated") is True
    try:
        with engine.begin() as connection:
            numbered = connection.execute(text(
                "SELECT id, version FROM workflow_versions "
                "WHERE workflow_definition_id = :d ORDER BY version"),
                {"d": made["id"]}).mappings().all()
            active = connection.execute(text(
                "SELECT active_version_id FROM workflow_definitions WHERE id = :d"),
                {"d": made["id"]}).scalar_one()
        assert [int(row["version"]) for row in numbered] == [1, 2, 3]
        assert active == second["id"]
        assert active != third["id"]

        with engine.begin() as connection:
            defaulted = workflows._default_workflow(connection, project_id)
            governing = workflows.workflow_for_code_task(
                connection, project_id=project_id, task_id=None)
            described = explain_version(
                connection, project_id=project_id,
                workflow_key=workflows.DEFAULT_WORKFLOW_KEY)
            prechecked = _resolve_version(
                connection, project_id, workflows.DEFAULT_WORKFLOW_KEY)

        assert defaulted["version_id"] == second["id"]
        assert defaulted["version"] == 2
        assert defaulted["source"] == "default"
        assert governing["version_id"] == second["id"]
        assert governing["version"] == 2
        assert governing["source"] == "default"
        assert described["version"] == 2
        assert described["is_active"] is True
        assert prechecked["version_id"] == second["id"]

        class _Ctx:
            def begin(self):
                return engine.begin()

        tool = {spec.name: spec for spec in build_default_registry().specs()}["start_workflow"]
        started_by_agent = tool.handler(
            _Ctx(), project_id=project_id, actor="test",
            args={"directive": "hold two", "workflow_key": workflows.DEFAULT_WORKFLOW_KEY,
                  "title": "version probe"})
        assert started_by_agent.get("run_id"), started_by_agent
        with engine.begin() as connection:
            agent_version = connection.execute(text(
                "SELECT workflow_version_id FROM workflow_runs WHERE id = :r"),
                {"r": started_by_agent["run_id"]}).scalar_one()
        assert agent_version == second["id"]

        import apps.api.app.main as api_main
        from apps.api.app.request_bodies import WorkflowForkBody, WorkflowStartBody

        monkeypatch.setattr(api_main, "require_project_actor", lambda *args, **kwargs: None)
        forked = api_main.fork_delivery_workflow(WorkflowForkBody(
            project_id=project_id, actor="test",
            workflow_key=workflows.DEFAULT_WORKFLOW_KEY,
            new_key=f"fork_{token}", new_name=f"Fork {token}"))
        assert forked["forked_from"] == second["id"]
        assert forked["version"]["spec"]["steps"][0]["wait_seconds"] == 120

        started = api_main.start_delivery_workflow(WorkflowStartBody(
            project_id=project_id, actor="test",
            workflow_key=workflows.DEFAULT_WORKFLOW_KEY,
            input={"directive": "hold two", "title": "console start"}))
        assert started["run"]["workflow_version_id"] == second["id"]
    finally:
        with engine.begin() as connection:
            for statement in (
                "DELETE FROM workflow_step_runs WHERE project_id = :p",
                "DELETE FROM workflow_runs WHERE project_id = :p",
                "DELETE FROM workflow_mappings WHERE project_id = :p",
                "DELETE FROM events WHERE project_id = :p",
                "DELETE FROM workflow_definitions WHERE project_id = :p",
                "DELETE FROM projects WHERE id = :p",
            ):
                try:
                    with connection.begin_nested():
                        connection.execute(text(statement), {"p": project_id})
                except Exception:
                    pass
            connection.execute(text("DELETE FROM workspaces WHERE id = :id"),
                               {"id": workspace_id})
            connection.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org_id})
        engine.dispose()
