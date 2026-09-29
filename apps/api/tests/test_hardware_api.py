"""Community hardware place API.

The default place is the machine where the installer ran. Other hardware uses
the same agent and does not become the default.
"""

from __future__ import annotations

import json

from sqlalchemy import text

PROJECT = "boss-dogfood"


def _clear(engine) -> None:
    with engine.begin() as connection:
        places = connection.execute(text("SELECT to_regclass('hardware_places')")).scalar()
        if places is not None:
            connection.execute(
                text("DELETE FROM hardware_places WHERE project_id = :project_id"),
                {"project_id": PROJECT},
            )
        connection.execute(
            text(
                """
                DELETE FROM changesets
                WHERE project_id = :project_id AND title = 'Install hardware agent'
                """
            ),
            {"project_id": PROJECT},
        )


def test_local_resolve_is_the_installer_machine(client, engine) -> None:
    _clear(engine)
    response = client.get("/v1/hardware/resolve", params={"project_id": PROJECT})
    assert response.status_code == 200, response.text
    body = response.json()
    place = body["place"]
    assert body["place_id"] == body["default_place_id"] == place["id"]
    assert place["kind"] == "local"
    assert place["is_default"] is True
    assert place["installer_machine"] is True
    assert place["agent_state"] == "absent"
    assert place["ready"] is False
    assert place["status"] != "ready"


def test_local_place_records_installed_agent(client, engine) -> None:
    _clear(engine)
    first = client.post(
        "/v1/hardware/places/local",
        json={
            "project_id": PROJECT,
            "actor": "ceo",
            "hostname": "installer",
            "arch": "arm64",
            "os": "darwin",
            "agent_state": "installed",
        },
    )
    assert first.status_code == 200, first.text
    place = first.json()["place"]
    assert place["agent_state"] == "installed"
    assert place["status"] == "ready"
    assert place["ready"] is True
    assert place["is_default"] is True
    assert place["installer_machine"] is True
    assert place["hostname"] == "installer"
    assert place["arch"] == "arm64"
    assert place["os"] == "darwin"
    second = client.post(
        "/v1/hardware/places/local",
        json={
            "project_id": PROJECT,
            "actor": "ceo",
            "hostname": "installer-2",
            "arch": "arm64",
            "os": "darwin",
            "agent_state": "installed",
        },
    )
    assert second.status_code == 200, second.text
    again = second.json()["place"]
    assert again["id"] == place["id"]
    assert again["hostname"] == "installer-2"
    assert again["agent_state"] == "installed"
    assert again["is_default"] is True
    with engine.begin() as connection:
        count = connection.execute(
            text(
                """
                SELECT count(*) FROM hardware_places
                WHERE project_id = :project_id AND kind = 'local'
                """
            ),
            {"project_id": PROJECT},
        ).scalar()
    assert count == 1


def test_additional_hardware_stays_non_default_until_enroll(client, engine) -> None:
    _clear(engine)
    resolved = client.get("/v1/hardware/resolve", params={"project_id": PROJECT})
    assert resolved.status_code == 200, resolved.text
    local_id = resolved.json()["default_place_id"]
    created = client.post(
        "/v1/hardware/places/agents",
        json={"project_id": PROJECT, "actor": "ceo", "name": "edge-1"},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    place = body["place"]
    assert place["is_default"] is False
    assert place["kind"] == "agent"
    assert place["agent_state"] == "absent"
    assert place["id"] != local_id
    assert "enroll_token" not in body
    token = body["enroll_command"].split("--token ", 1)[1]
    assert body["enroll_command"].startswith("scripts/install/node_agent.sh --enroll --token ")
    assert body["enroll_command"].count(token) == 1
    assert token not in json.dumps(place)
    listed = client.get("/v1/hardware/places", params={"project_id": PROJECT})
    assert listed.status_code == 200, listed.text
    assert listed.json()["default_place_id"] == local_id
    enrolled = client.post("/v1/hardware/agents/enroll", json={"token": token})
    assert enrolled.status_code == 200, enrolled.text
    installed = enrolled.json()["place"]
    assert installed["agent_state"] == "installed"
    assert installed["is_default"] is False
    assert installed["id"] != local_id
    assert enrolled.json()["default_place_id"] == local_id
    again = client.get("/v1/hardware/resolve", params={"project_id": PROJECT})
    assert again.json()["place_id"] == local_id
    assert again.json()["default_place_id"] == local_id
    reused = client.post("/v1/hardware/agents/enroll", json={"token": token})
    assert reused.status_code == 401


def test_bad_enroll_token_rejected(client) -> None:
    response = client.post(
        "/v1/hardware/agents/enroll",
        json={"token": "not-a-real-enroll-token"},
    )
    assert response.status_code == 401


def test_raw_provider_secret_rejected(client) -> None:
    leaked = client.post(
        "/v1/hardware/places/providers",
        json={
            "project_id": PROJECT,
            "actor": "ceo",
            "provider": "aws",
            "secret_ref": "env:AWS_SECRET",
            "secret": "raw-secret-value",
        },
    )
    assert leaked.status_code == 422
    raw_ref = client.post(
        "/v1/hardware/places/providers",
        json={
            "project_id": PROJECT,
            "actor": "ceo",
            "provider": "aws",
            "secret_ref": "AKIAIOSFODNN7EXAMPLE",
        },
    )
    assert raw_ref.status_code == 422


def test_propose_names_agent_install_and_does_not_mark_ready(client, engine) -> None:
    _clear(engine)
    created = client.post(
        "/v1/hardware/places/agents",
        json={"project_id": PROJECT, "actor": "ceo", "name": "edge-2"},
    )
    assert created.status_code == 200, created.text
    place_id = created.json()["place"]["id"]
    assert created.json()["place"]["status"] != "ready"
    proposed = client.post(
        f"/v1/hardware/places/{place_id}/propose",
        json={"project_id": PROJECT, "actor": "ceo", "summary": "install the agent"},
    )
    assert proposed.status_code == 200, proposed.text
    body = proposed.json()
    assert "scripts/install/node_agent.sh" in proposed.text
    assert body["applied"] is False
    assert body["install"]["script"] == "scripts/install/node_agent.sh"
    assert body["install"]["applied"] is False
    assert body["place"]["status"] != "ready"
    assert body["place"]["ready"] is False
    assert body["place"]["agent_state"] == "absent"
    assert body["changeset"]["applied_at"] is None
    assert any(
        operation.get("script") == "scripts/install/node_agent.sh"
        for operation in body["changeset"]["operations"]
    )
    with engine.begin() as connection:
        stored = connection.execute(
            text(
                """
                SELECT status, labels->>'agent_state'
                FROM hardware_places WHERE id = :id
                """
            ),
            {"id": place_id},
        ).one()
        changeset = connection.execute(
            text("SELECT status, applied_at FROM changesets WHERE id = :id"),
            {"id": body["changeset"]["id"]},
        ).one()
    assert stored[0] != "ready"
    assert stored[1] == "absent"
    assert changeset[0] == "pending_approval"
    assert changeset[1] is None
