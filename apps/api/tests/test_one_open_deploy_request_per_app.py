"""One open deploy request per app target, each with its own approval.

HelpDesk collected three open requests: the workflow asked with no ref, the target drawer
planned "root@192.0.2.202:22" (its SSH address, prefilled into the Ref field), and a later press
asked for the sha. Two of them shared one decision, so the approval queued one and the other sat
at "Needs you" with nothing left to approve.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.tests.test_a_host_deploys_only_through_an_approved_changeset import (  # noqa: F401
    HOME, _approve_and_apply, _plan, hosted,
)
from boss_engine.services import governed_host_deploy
from boss_engine.services.infra_view import deploy_rows


def _run(engine, run_id):
    with engine.begin() as connection:
        return dict(connection.execute(
            text("SELECT r.*, x.status AS decision_status FROM deployment_runs r "
                 "LEFT JOIN decisions x ON x.id = r.decision_id WHERE r.id = :i"),
            {"i": run_id}).mappings().one())


def test_the_same_release_is_adopted_and_a_different_one_replaces_the_waiting_request(
        client, engine, hosted):
    first = _plan(client, hosted, ref="main")
    # Same release, however it is spelt: adopted, not planned again.
    assert _plan(client, hosted, ref="main")["deployment_run_id"] == first["deployment_run_id"]

    second = _plan(client, hosted, ref="8a009a6f00d1e2")
    assert second["deployment_run_id"] != first["deployment_run_id"]
    assert second["decision_id"] != first["decision_id"], "one approval per request"
    old = _run(engine, first["deployment_run_id"])
    assert old["status"] == "superseded" and old["decision_status"] == "withdrawn"
    assert "Nothing ran" in old["failure_reason"]

    # A short sha names the same release.
    assert _plan(client, hosted, ref="8a009a6")["deployment_run_id"] == second["deployment_run_id"]


def test_a_host_address_is_not_a_ref(client, hosted):
    response = client.post("/v1/delivery/deploy", json={
        "target_id": hosted["target_id"], "actor": "ceo", "ref": "root@203.0.113.40:22"})
    assert response.status_code == 422
    assert "is not a branch, tag or commit" in response.json()["detail"]


def test_a_request_whose_approval_another_run_took_is_closed(client, engine, hosted):
    plan = _plan(client, hosted, ref="main")
    with engine.begin() as connection:
        # The shape the old merge left: a second request on the SAME decision.
        connection.execute(text(
            "INSERT INTO deployment_runs (id, project_id, deployment_target_id, changeset_id, "
            "decision_id, app_name, environment, repo, target_kind, target_ref, commit_ref, "
            "status, release_packet, rollback_plan, release_receipt_ids, created_by) "
            "SELECT 'deprun_orphan_test', project_id, deployment_target_id, changeset_id, "
            "decision_id, app_name, environment, repo, target_kind, target_ref, 'other', "
            "'awaiting_approval', '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, created_by "
            "FROM deployment_runs WHERE id = :i"), {"i": plan["deployment_run_id"]})
    result, _ = _approve_and_apply(client, engine, plan)
    assert result["status"] == "succeeded", result

    with engine.begin() as connection:
        # Before the repair, the console already reads it as superseded, not "Needs you".
        rows = {r["ref"]: r for r in deploy_rows(connection, api_main.settings, HOME,
                                                 viewer="ceo")["rows"]}
        orphan_row = next(r for ref, r in rows.items() if ref.endswith("deprun_orphan_test"))
        assert orphan_row["word"] == "Superseded" and not orphan_row["can"]["approve"]
        assert governed_host_deploy.supersede_orphaned_runs(
            connection, api_main.settings) == {"runs_superseded": 1}
        assert governed_host_deploy.supersede_orphaned_runs(
            connection, api_main.settings) == {"runs_superseded": 0}
    assert _run(engine, "deprun_orphan_test")["status"] == "superseded"


def test_the_live_row_says_where_the_app_is_and_how_it_was_checked(client, engine, hosted):
    plan = _plan(client, hosted, ref="main")
    result, _ = _approve_and_apply(client, engine, plan)
    assert result["status"] == "succeeded", result
    with engine.begin() as connection:
        rows = deploy_rows(connection, api_main.settings, HOME, viewer="ceo")["rows"]
    row = next(r for r in rows if r["ref"].endswith(plan["deployment_run_id"]))
    assert row["phase"] == "live" and row["word"] == "Live"
    assert row["open_url"] == "http://203.0.113.40:3010/"
    assert row["sentence"].startswith("Live at http://203.0.113.40:3010/. Health check passed")
    assert not row["can"]["approve"]

    build = _build_of(engine, plan)
    response = client.get(f"/v1/code-tasks/{build}/deployments")
    assert response.status_code == 200, (build, response.text)
    mine = next(d for d in response.json()["deployments"]
                if d["id"] == plan["deployment_run_id"])
    assert mine["phase"] == "live" and mine["open_url"].rstrip("/") == "http://203.0.113.40:3010"
    assert mine["word"] == row["word"] and mine["sentence"].startswith("Live at")
    assert mine["ref"] == f"delivery:{plan['deployment_run_id']}"


def _build_of(engine, plan):
    """The build this deploy shipped: the fixture's manual deploy names none, so one is made."""
    build = "crun_deploy_reading_test"
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, created_at) "
            "VALUES (:b, :p, 'HelpDesk', 'completed', now()) ON CONFLICT DO NOTHING"),
            {"b": build, "p": HOME})
        connection.execute(text(
            "UPDATE deployment_runs SET release_packet = release_packet || "
            "jsonb_build_object('code_task_run_id', CAST(:b AS TEXT)) WHERE id = :i"),
            {"b": build, "i": plan["deployment_run_id"]})
    return build
