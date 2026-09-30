"""Community provider places: connect an account, propose capacity, never call the provider.

A provider place is an operator-connected account (AWS, DigitalOcean, or a generic
VM/container provider). The default install place stays the community installer
machine. These tests drive the public functions only. Persistence is an in-memory
stand-in for hardware_places; the provider call is a fake transport.
"""

from __future__ import annotations

import inspect
import socket

import pytest

from boss_engine.services.hardware_providers import (
    PlannedProviderTransport,
    apply_capacity,
    get_provider_place,
    propose_capacity,
    register_provider_place,
)


RAW_SECRET = "env:AKIA_EXAMPLE_NOT_A_KEY"
DO_SECRET = "dop_v1_example"


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _Mappings(self._rows)


class MemoryHardwarePlaces:
    """Records hardware_places SQL. This is the connection seam, not a provider client."""

    def __init__(self):
        self.rows: list[dict] = []
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "CREATE TABLE" in sql:
            return _Result([])
        if "INSERT INTO hardware_places" in sql:
            self.rows.append(dict(params or {}))
            return _Result([])
        if "WHERE id = :id" in sql:
            found = [row for row in self.rows if row["id"] == (params or {})["id"]]
            return _Result(found)
        if "WHERE project_id = :project_id" in sql:
            project_id = (params or {})["project_id"]
            found = [row for row in self.rows if row["project_id"] == project_id]
            return _Result(found)
        raise AssertionError(f"unexpected SQL: {sql}")


class RecordingTransport:
    """Fake provider transport. plan() must be the only way a call would leave the process."""

    def __init__(self):
        self.calls: list[dict] = []

    def plan(self, request):
        self.calls.append(dict(request))
        return {
            "status": "planned",
            "dedupe_key": request["dedupe_key"],
            "provider": request["provider_key"],
            "action": request["action"],
            "resource_type": request["resource_type"],
            "resource_ref": request.get("resource_ref"),
            "provider_ref": None,
            "result": {"executed": False, "network": False},
            "error": None,
        }


@pytest.fixture(autouse=True)
def _use_local_place_table(monkeypatch):
    """The kernel register exists on this tree. These tests use the local table unless a test opts in."""

    monkeypatch.setattr(
        "boss_engine.services.hardware_providers._hardware_manager_register",
        lambda: None,
    )


def _forbid_sockets(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("live socket opened")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


def _register(connection, provider_key="aws", **overrides):
    payload = {
        "project_id": "proj_community",
        "provider_key": provider_key,
        "secret_ref": "env:BOSS_AWS_ACCESS_KEY_ID",
        "display_name": "Operator AWS",
        "actor": "operator",
    }
    payload.update(overrides)
    return register_provider_place(connection, **payload)


def _approved():
    return {"status": "approved", "actor": "operator"}


def _reservation():
    return {"id": "rsv_community_1", "status": "active"}


def _spec(**overrides):
    payload = {
        "name": "community-worker",
        "kind": "vm",
        "region": "us-east-1",
        "size": "t3.micro",
        "image": "ami-community",
        "expected_cost_usd_micros": 1_500_000,
    }
    payload.update(overrides)
    return payload


def test_register_stores_secret_ref_and_refuses_the_secret_value():
    connection = MemoryHardwarePlaces()
    with pytest.raises(ValueError, match="secret reference"):
        _register(connection, secret_ref=RAW_SECRET, secret_value=RAW_SECRET)
    assert connection.rows == []

    with pytest.raises(ValueError, match="secret value"):
        _register(
            connection,
            secret_ref="env:BOSS_AWS_ACCESS_KEY_ID",
            secret_value=RAW_SECRET,
        )
    assert connection.rows == []

    place = _register(connection)
    stored = get_provider_place(connection, place["id"])

    assert stored["kind"] == "provider"
    assert stored["provider_key"] == "aws"
    assert stored["status"] == "pending"
    assert stored["is_default"] is False
    assert stored["secret_ref"] == "env:BOSS_AWS_ACCESS_KEY_ID"
    assert RAW_SECRET not in repr(stored)
    assert RAW_SECRET not in repr(connection.rows)
    assert "secret_value" not in stored


def test_unknown_provider_key_is_rejected():
    connection = MemoryHardwarePlaces()
    with pytest.raises(ValueError, match="unknown provider"):
        _register(connection, provider_key="azure")
    with pytest.raises(ValueError, match="unknown provider"):
        _register(connection, provider_key="shared-server")
    assert connection.rows == []


@pytest.mark.parametrize("provider_key", ["aws", "digitalocean", "generic"])
def test_known_provider_places_register_pending_and_not_default(provider_key):
    connection = MemoryHardwarePlaces()
    secret_ref = {
        "aws": "env:BOSS_AWS_ACCESS_KEY_ID",
        "digitalocean": "env:BOSS_DIGITALOCEAN_TOKEN",
        "generic": "vault:community/generic-vm",
    }[provider_key]
    place = _register(
        connection,
        provider_key=provider_key,
        secret_ref=secret_ref,
        display_name=None,
    )
    assert place["provider_key"] == provider_key
    assert place["kind"] == "provider"
    assert place["status"] == "pending"
    assert place["is_default"] is False
    assert place["secret_ref"] == secret_ref
    assert DO_SECRET not in repr(place)


def test_provider_place_cannot_become_the_default_install_place():
    connection = MemoryHardwarePlaces()
    with pytest.raises(ValueError, match="default install place"):
        _register(connection, is_default=True)
    assert connection.rows == []
    place = _register(connection)
    assert place["is_default"] is False
    assert get_provider_place(connection, place["id"])["is_default"] is False


def test_register_delegates_to_hardware_manager_when_present(monkeypatch):
    from boss_engine.services.hardware_manager import (
        register_provider_place as kernel_register,
    )

    connection = MemoryHardwarePlaces()
    captured: dict = {}

    def register_provider_place(connection, **kwargs):
        captured["connection"] = connection
        captured["kwargs"] = kwargs
        return {
            "id": "hpl_delegated",
            "project_id": kwargs["project_id"],
            "kind": "provider",
            "provider_key": kwargs["provider_key"],
            "status": "pending",
            "is_default": False,
            "secret_ref": kwargs["secret_ref"],
            "name": kwargs["name"],
            "created_by": kwargs["actor"],
            "created_at": "2026-09-29T00:00:00+00:00",
        }

    monkeypatch.setattr(
        "boss_engine.services.hardware_providers._hardware_manager_register",
        lambda: register_provider_place,
    )

    place = _register(connection)
    kernel_params = set(inspect.signature(kernel_register).parameters)
    assert place["id"] == "hpl_delegated"
    assert place["is_default"] is False
    assert place["display_name"] == "Operator AWS"
    assert set(captured["kwargs"]) <= kernel_params
    assert captured["kwargs"]["name"] == "Operator AWS"
    assert "is_default" not in captured["kwargs"]
    assert "display_name" not in captured["kwargs"]
    assert captured["kwargs"]["secret_ref"] == "env:BOSS_AWS_ACCESS_KEY_ID"
    assert "secret_value" not in captured["kwargs"]
    assert RAW_SECRET not in repr(captured)
    assert not any("INSERT INTO hardware_places" in sql for sql in connection.statements)


def test_propose_capacity_returns_a_reviewable_changeset_and_creates_no_vm(monkeypatch):
    _forbid_sockets(monkeypatch)
    connection = MemoryHardwarePlaces()
    place = _register(connection)
    transport = RecordingTransport()

    changeset = propose_capacity(place, _spec())

    assert changeset["status"] == "pending_approval"
    assert changeset["project_id"] == place["project_id"]
    assert changeset["provider_account_id"] == place["id"]
    assert changeset["applied_at"] is None
    assert changeset["approved_by"] is None
    assert changeset["risk_tier"] >= 2
    assert changeset["expected_cost_usd_micros"] == 1_500_000
    assert changeset["reservation_id"] is None
    operation = changeset["operations"][0]
    assert operation["skill_id"] == "aws.ec2.instance.create"
    assert operation["action"] == "create"
    assert operation["resource_type"] == "vm"
    assert operation["destructive"] is False
    assert operation["desired_state"]["name"] == "community-worker"
    assert RAW_SECRET not in repr(changeset)
    assert place["secret_ref"] not in repr(operation["desired_state"])
    receipt = changeset["expected_receipts"][0]
    assert receipt["dedupe_key"].startswith("resource_plane:")
    assert receipt["skill_id"] == "aws.ec2.instance.create"
    policy = changeset["policy_result"]
    assert policy["requires_approval"] is True
    assert policy["requires_budget_reservation"] is True
    assert policy["requires_resource_receipt"] is True
    assert policy["requires_dedupe_key"] is True
    assert policy["creates_vm"] is False
    assert policy["policy_id"] == "resource_plane_policy"
    assert transport.calls == []
    assert get_provider_place(connection, place["id"])["status"] == "pending"


def test_proposal_names_the_hardware_agent_install_and_opens_no_socket(monkeypatch):
    """The same node agent is installed on the proposed machine. Nothing is dialed."""

    _forbid_sockets(monkeypatch)
    connection = MemoryHardwarePlaces()
    place = _register(connection)
    transport = RecordingTransport()
    changeset = propose_capacity(place, _spec())

    assert place["is_default"] is False
    assert changeset["provider_is_default"] is False
    assert changeset["default_hardware"] == "community_installer"
    install_steps = [
        step for step in changeset["steps"] if step["step"] == "install_hardware_agent"
    ]
    assert len(install_steps) == 1
    assert install_steps[0]["installer"] == "scripts/install/node_agent.sh"
    assert install_steps[0]["same_agent_as_local"] is True
    assert install_steps[0]["target"] == "aws:vm:community-worker"
    agent_ops = [op for op in changeset["operations"] if op["action"] == "install"]
    assert len(agent_ops) == 1
    assert agent_ops[0]["skill_id"] == "hardware.agent.install"
    assert agent_ops[0]["desired_state"]["installer"] == "scripts/install/node_agent.sh"
    assert agent_ops[0]["destructive"] is False
    assert changeset["operations"][0]["action"] == "create"
    assert transport.calls == []

    container_place = _register(
        connection,
        provider_key="generic",
        secret_ref="vault:community/generic-vm",
        display_name="Generic",
    )
    container = propose_capacity(container_place, _spec(kind="container", name="sidecar"))
    assert container["steps"][-1]["installer"] == "scripts/install/node_agent.sh"
    assert container["steps"][-1]["target"] == "generic:container:sidecar"
    assert container["provider_is_default"] is False

    result = apply_capacity(
        changeset,
        approval=_approved(),
        budget_reservation=_reservation(),
        dedupe_key=changeset["expected_receipts"][0]["dedupe_key"],
        transport=transport,
    )
    assert result["created_vm"] is False
    assert result["changeset"]["steps"][-1]["installer"] == "scripts/install/node_agent.sh"
    assert transport.calls
    assert all(call["action"] != "install" for call in transport.calls)
    assert "https://" not in repr(transport.calls)
    assert "scripts/install/node_agent.sh" not in repr(transport.calls)


def test_generic_container_proposal_uses_the_generic_skill(monkeypatch):
    _forbid_sockets(monkeypatch)
    place = _register(
        MemoryHardwarePlaces(),
        provider_key="generic",
        secret_ref="vault:community/generic-vm",
    )
    changeset = propose_capacity(place, _spec(kind="container", name="sidecar"))
    operation = changeset["operations"][0]
    assert operation["skill_id"] == "generic.container.create"
    assert operation["resource_type"] == "container"
    assert changeset["policy_result"]["creates_vm"] is False


@pytest.mark.parametrize(
    "approval,reservation,dedupe",
    [
        (None, {"id": "rsv_community_1", "status": "active"}, "use-changeset"),
        ({"status": "approved", "actor": "operator"}, None, "use-changeset"),
        ({"status": "approved", "actor": "operator"}, {"id": "rsv_community_1", "status": "released"}, "use-changeset"),
        ({"status": "approved", "actor": "operator"}, {"id": "rsv_community_1", "status": "active"}, None),
        ({"status": "approved", "actor": "operator"}, {"id": "rsv_community_1", "status": "active"}, "not-the-changeset-key"),
    ],
)
def test_apply_is_refused_without_approval_reservation_and_matching_dedupe_key(
    monkeypatch, approval, reservation, dedupe
):
    _forbid_sockets(monkeypatch)
    place = _register(MemoryHardwarePlaces())
    changeset = propose_capacity(place, _spec())
    transport = RecordingTransport()
    dedupe_key = changeset["expected_receipts"][0]["dedupe_key"] if dedupe == "use-changeset" else dedupe

    with pytest.raises(ValueError):
        apply_capacity(
            changeset,
            approval=approval,
            budget_reservation=reservation,
            dedupe_key=dedupe_key,
            transport=transport,
        )
    assert transport.calls == []
    assert changeset["status"] == "pending_approval"


def test_apply_with_gates_returns_a_planned_receipt_and_opens_no_socket(monkeypatch):
    _forbid_sockets(monkeypatch)
    place = _register(
        MemoryHardwarePlaces(),
        provider_key="digitalocean",
        secret_ref="env:BOSS_DIGITALOCEAN_TOKEN",
    )
    changeset = propose_capacity(
        place,
        _spec(region="nyc1", size="s-1vcpu-1gb", image="ubuntu-24-04"),
    )
    transport = RecordingTransport()
    dedupe_key = changeset["expected_receipts"][0]["dedupe_key"]

    result = apply_capacity(
        changeset,
        approval=_approved(),
        budget_reservation=_reservation(),
        dedupe_key=dedupe_key,
        transport=transport,
    )

    assert result["status"] == "planned"
    assert result["created_vm"] is False
    assert result["changeset"]["status"] == "planned"
    assert result["changeset"]["applied_at"] is None
    assert len(result["resource_receipts"]) == 1
    receipt = result["resource_receipts"][0]
    assert receipt["status"] == "planned"
    assert receipt["dedupe_key"] == dedupe_key
    assert receipt["provider"] == "digitalocean"
    assert receipt["action"] == "create"
    assert receipt["result"]["executed"] is False
    assert DO_SECRET not in repr(result)
    assert len(transport.calls) == 1
    assert "https://" not in repr(transport.calls)
    assert transport.calls[0]["provider_key"] == "digitalocean"
    assert transport.calls[0]["dedupe_key"] == dedupe_key


def test_default_transport_plans_without_a_socket(monkeypatch):
    _forbid_sockets(monkeypatch)
    place = _register(MemoryHardwarePlaces())
    changeset = propose_capacity(place, _spec())
    result = apply_capacity(
        changeset,
        approval=_approved(),
        budget_reservation=_reservation(),
        dedupe_key=changeset["expected_receipts"][0]["dedupe_key"],
        transport=PlannedProviderTransport(),
    )
    assert result["status"] == "planned"
    assert result["resource_receipts"][0]["status"] == "planned"
    assert result["created_vm"] is False


def test_destructive_teardown_stays_unapplied_until_rollback_note_and_then_still_does_not_run(
    monkeypatch,
):
    _forbid_sockets(monkeypatch)
    place = _register(MemoryHardwarePlaces())
    missing_note = propose_capacity(place, _spec(action="destroy", name="community-worker"))
    assert missing_note["operations"][0]["destructive"] is True
    assert missing_note["operations"][0]["skill_id"] == "aws.ec2.instance.destroy"
    assert missing_note["status"] == "pending_approval"
    transport = RecordingTransport()

    with pytest.raises(ValueError, match="approval"):
        apply_capacity(
            missing_note,
            approval=None,
            budget_reservation=_reservation(),
            dedupe_key=missing_note["expected_receipts"][0]["dedupe_key"],
            transport=transport,
        )
    with pytest.raises(ValueError, match="rollback"):
        apply_capacity(
            missing_note,
            approval=_approved(),
            budget_reservation=_reservation(),
            dedupe_key=missing_note["expected_receipts"][0]["dedupe_key"],
            transport=transport,
        )
    assert transport.calls == []

    changeset = propose_capacity(
        place,
        _spec(
            action="teardown",
            name="community-worker",
            rollback_note="Recreate the worker from the community installer image.",
        ),
    )
    assert changeset["rollback_plan"] == [
        "Recreate the worker from the community installer image."
    ]
    result = apply_capacity(
        changeset,
        approval=_approved(),
        budget_reservation=_reservation(),
        dedupe_key=changeset["expected_receipts"][0]["dedupe_key"],
        transport=transport,
    )
    assert result["status"] == "unapplied"
    assert result["created_vm"] is False
    assert result["resource_receipts"] == []
    assert result["changeset"]["status"] == "unapplied"
    assert result["changeset"]["applied_at"] is None
    assert transport.calls == []


def test_provider_module_does_not_reference_a_live_provider_client():
    import boss_engine.services.hardware_providers as module

    source = open(module.__file__, encoding="utf-8").read()
    for banned in (
        "boto3",
        "httpx",
        "api.digitalocean.com",
        "amazonaws",
        "urllib",
        "requests",
        "socket",
        "shared-server",
        "digitalocean_adapter",
        "aws_metal",
    ):
        assert banned not in source
