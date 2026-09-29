"""An unbound deployment target must be bindable by the control that binds targets.

Found on goldeneye: BugBox had a `deployment_targets` row with host_id NULL — created by an
earlier "Set target" attempt that died on the run-status guard before it could attach. Every
later attempt then hit

    409 "'<app>' already has a staging target. Attach it to this host from the Hosts panel
        instead."

which is a dead end twice over: "Set target" is the only control that binds a target, and the
Hosts panel lists hosts, not orphaned targets. The app kept a destination it could never deploy
to. The 409 exists to stop a second target shadowing a working one, which is only a real
conflict when the existing target is bound to a DIFFERENT host.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"


def _uid() -> str:
    return uuid.uuid4().hex[:12]


@pytest.fixture
def seeded(engine):
    """A completed run, a verified host, and an app_name unique to this test."""
    run_id = f"crun_{_uid()}"
    host_id = f"dh_{_uid()}"
    app_name = f"orphan-target-{_uid()}"
    created: list[tuple[str, str]] = []

    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, "
            "pull_request_url) VALUES (:i, :p, :t, 'completed', 'done', CAST(:s AS JSONB), :u)"),
            {"i": run_id, "p": PROJECT_ID, "t": "orphan target fixture",
             "s": json.dumps({"repo": "boss-sandbox", "owner": "example-user"}),
             "u": "https://github.com/example-user/boss-sandbox/pull/1"})
        c.execute(text(
            "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, ssh_port, "
            "status) VALUES (:i, :p, :n, '203.0.113.10', 'root', 22, 'verified')"),
            {"i": host_id, "p": PROJECT_ID, "n": f"host-{_uid()}"})
    created.append((run_id, host_id))

    yield {"run_id": run_id, "host_id": host_id, "app_name": app_name}

    # Children first: setup-deployment bootstraps the app's slot, which writes health samples
    # and deploy rows holding FKs onto the target. Each delete gets its OWN transaction — a
    # single failure in a shared one aborts it and every later statement dies with
    # InFailedSqlTransaction, hiding the real error.
    with engine.begin() as c:
        ids = [r[0] for r in c.execute(
            text("SELECT id FROM deployment_targets WHERE project_id = :p AND app_name = :a"),
            {"p": PROJECT_ID, "a": app_name}).all()]

    def _try(sql: str, params: dict) -> None:
        try:
            with engine.begin() as conn:
                conn.execute(text(sql), params)
        except Exception:  # noqa: BLE001 - table may not exist in this edition
            pass

    for table, column in (("deployment_health_samples", "deployment_target_id"),
                          ("deployment_runs", "target_id"),
                          ("deployment_promotions", "target_id")):
        for tid in ids:
            _try(f"DELETE FROM {table} WHERE {column} = :i", {"i": tid})
    _try("DELETE FROM deployment_targets WHERE project_id = :p AND app_name = :a",
         {"p": PROJECT_ID, "a": app_name})
    _try("DELETE FROM code_task_runs WHERE id = :i", {"i": run_id})
    _try("DELETE FROM deployment_hosts WHERE id = :i", {"i": host_id})


def _orphan_target(engine, app_name: str) -> str:
    """A target with no host — exactly the row BugBox was left holding."""
    target_id = f"deptgt_{_uid()}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
            "target_kind, target_ref, status, host_id, created_by) "
            "VALUES (:i, :p, :a, 'boss-sandbox', 'staging', 'ssh', 'root@0.0.0.0', "
            "'active', NULL, :by)"),
            {"i": target_id, "p": PROJECT_ID, "a": app_name, "by": ACTOR})
    return target_id


def _setup(client, seeded, **extra):
    return client.post(
        f"/v1/code-tasks/{seeded['run_id']}/setup-deployment",
        json={"project_id": PROJECT_ID, "actor": ACTOR, "host_id": seeded["host_id"],
              "app_name": seeded["app_name"], **extra},
    )


def test_binds_an_unbound_target_instead_of_refusing(client, engine, seeded):
    target_id = _orphan_target(engine, seeded["app_name"])

    response = _setup(client, seeded)

    assert response.status_code in (200, 201), response.text
    body = response.json()
    assert body.get("adopted_existing_target") is True
    assert body["target_id"] == target_id

    with engine.begin() as c:
        row = c.execute(text("SELECT host_id, auto_deploy FROM deployment_targets WHERE id = :i"),
                        {"i": target_id}).mappings().first()
    assert row["host_id"] == seeded["host_id"], "the orphan target must now point at the host"
    assert row["auto_deploy"] is True, "deploy-on-merge is the point of setting a target"


def test_rebinding_the_same_host_is_idempotent(client, engine, seeded):
    _orphan_target(engine, seeded["app_name"])
    first = _setup(client, seeded)
    assert first.status_code in (200, 201), first.text

    second = _setup(client, seeded)

    assert second.status_code in (200, 201), second.text
    assert second.json().get("already_bound") is True


def test_still_refuses_a_target_bound_to_a_different_host(client, engine, seeded):
    """The conflict the 409 was actually written for must survive."""
    target_id = _orphan_target(engine, seeded["app_name"])
    other_host = f"dh_{_uid()}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, ssh_port, "
            "status) VALUES (:i, :p, :n, '203.0.113.99', 'root', 22, 'verified')"),
            {"i": other_host, "p": PROJECT_ID, "n": f"other-{_uid()}"})
        c.execute(text("UPDATE deployment_targets SET host_id = :h WHERE id = :i"),
                  {"h": other_host, "i": target_id})

    response = _setup(client, seeded)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "different" in detail.lower()
    assert other_host in detail, "name the host it is bound to, so the operator can act"
    # No inline cleanup of `other_host`: the target still references it, and clean_tables now
    # removes deployment_hosts in FK order.


def test_error_no_longer_sends_the_operator_to_a_panel_that_cannot_help(client, engine, seeded):
    """The old advice pointed at the Hosts panel, which does not list orphaned targets."""
    _orphan_target(engine, seeded["app_name"])
    response = _setup(client, seeded)
    assert "Hosts panel" not in response.text


def test_setup_deployment_refuses_a_host_from_another_project(client, engine, seeded):
    """Set target must not bind another project's VM (or inherit its SSH key).

    The endpoint authorizes the actor on the code-task's project, then used to attach
    whatever host_id was supplied. A host from project B can then be scheduled from
    project A; deploy resolves the victim host's key_secret_ref and SSHes in.
    """
    victim_project = f"proj_victim_{_uid()}"
    victim_host = f"dh_{_uid()}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) "
            "VALUES (:id, 'ws_boss', 'Victim fleet', :id) ON CONFLICT (id) DO NOTHING"),
            {"id": victim_project})
        c.execute(text(
            "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, ssh_port, "
            "status) VALUES (:i, :p, :n, '198.51.100.10', 'root', 22, 'verified')"),
            {"i": victim_host, "p": victim_project, "n": f"victim-{_uid()}"})

    response = _setup(client, seeded, host_id=victim_host)

    assert response.status_code in (403, 404, 422), response.text
    detail = str(response.json().get("detail") or response.text).lower()
    assert "project" in detail or response.status_code == 404

    with engine.begin() as c:
        bound = c.execute(
            text("SELECT id FROM deployment_targets WHERE host_id = :h"),
            {"h": victim_host}).first()
        created = c.execute(
            text("SELECT id FROM deployment_targets WHERE project_id = :p AND app_name = :a"),
            {"p": PROJECT_ID, "a": seeded["app_name"]}).first()
    assert bound is None, "victim host must not be attached to any target"
    assert created is None, "setup-deployment must not create a target for a foreign host"

    with engine.begin() as c:
        c.execute(text("DELETE FROM deployment_hosts WHERE id = :i"), {"i": victim_host})
        c.execute(text("DELETE FROM projects WHERE id = :i"), {"i": victim_project})


def test_attach_refuses_a_host_from_another_project(client, engine, seeded):
    """The Hosts-panel attach path has the same tenant fence."""
    target_id = _orphan_target(engine, seeded["app_name"])
    victim_project = f"proj_victim_{_uid()}"
    victim_host = f"dh_{_uid()}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) "
            "VALUES (:id, 'ws_boss', 'Victim fleet', :id) ON CONFLICT (id) DO NOTHING"),
            {"id": victim_project})
        c.execute(text(
            "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, ssh_port, "
            "status) VALUES (:i, :p, :n, '198.51.100.11', 'root', 22, 'verified')"),
            {"i": victim_host, "p": victim_project, "n": f"victim-attach-{_uid()}"})

    response = client.post(
        f"/v1/deployment-hosts/{victim_host}/attach",
        json={"target_id": target_id, "actor": ACTOR},
    )

    assert response.status_code in (403, 404, 422), response.text
    from boss_engine.services.deployment_fleet import attach_target

    with engine.begin() as c, pytest.raises(ValueError, match="different projects"):
        attach_target(c, target_id=target_id, host_id=victim_host)
    with engine.begin() as c:
        row = c.execute(text("SELECT host_id FROM deployment_targets WHERE id = :i"),
                        {"i": target_id}).mappings().first()
    assert row["host_id"] is None, "orphan target must stay unbound"

    with engine.begin() as c:
        c.execute(text("DELETE FROM deployment_hosts WHERE id = :i"), {"i": victim_host})
        c.execute(text("DELETE FROM projects WHERE id = :i"), {"i": victim_project})


def test_retiring_a_target_requires_an_authorized_actor(client, engine, seeded):
    target_id = _orphan_target(engine, seeded["app_name"])
    path = f"/v1/deployment-targets/{target_id}"

    missing = client.delete(path)
    assert missing.status_code == 422, missing.text
    unauthorized = client.delete(path, params={"actor": "unknown-member"})
    assert unauthorized.status_code == 403, unauthorized.text
    with engine.begin() as c:
        status = c.execute(text("SELECT status FROM deployment_targets WHERE id = :i"),
                           {"i": target_id}).scalar_one()
    assert status == "active"

    retired = client.delete(path, params={"actor": ACTOR})
    assert retired.status_code == 200, retired.text
    with engine.begin() as c:
        status = c.execute(text("SELECT status FROM deployment_targets WHERE id = :i"),
                           {"i": target_id}).scalar_one()
    assert status == "retired"
