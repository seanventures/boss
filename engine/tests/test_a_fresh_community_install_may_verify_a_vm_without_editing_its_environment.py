"""A fresh Community install can add a VM, verify it and deploy to it — no env var to hand-set.

BOSS_SSH_HOST_DEPLOY_ALLOWED defaults off and nothing an installer writes turns it on, so while
it also gated read-only verification the Infra flow dead-ended: the Hosts panel replaced "Add a
VM" and "Verify" with a note telling the operator to edit the BOSS host's environment. Reading a
host and applying an approved ChangeSet now need only `outward_ssh_allowed`, which defaults on;
the legacy direct host writes keep their own switch.
"""
from types import SimpleNamespace

import pytest

from boss_engine.config import Settings
from boss_engine.services import deployment_connectors, deployment_fleet


@pytest.fixture
def default_settings(monkeypatch):
    for name in ("BOSS_SSH_OUTWARD_ALLOWED", "BOSS_SSH_HOST_DEPLOY_ALLOWED", "BOSS_EDITION"):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None)


def _probe_target():
    return {"id": "probe_dh_x", "project_id": "p", "app_name": "__preflight__vm",
            "target_kind": "ssh", "target_ref": "root@203.0.113.5:22",
            "secret_refs": [], "required_resources": [],
            "domains": ["vm.example.test"], "health_checks": [{"kind": "http"}]}


def _validate(settings, monkeypatch):
    connector = deployment_connectors.SSHContainerConnector()
    monkeypatch.setattr(connector, "_credential_problems", lambda *_a, **_k: [])
    monkeypatch.setattr(connector, "_host_problems", lambda *_a, **_k: [])
    return connector.validate(_probe_target(), settings, None)


def test_default_settings_allow_outward_ssh_but_not_direct_host_writes(default_settings):
    assert default_settings.ssh_host_deploy_allowed is False
    assert deployment_fleet.outward_ssh_allowed(default_settings) is True
    assert deployment_fleet.direct_host_writes_allowed(default_settings) is False


def test_verifying_a_host_on_default_settings_is_not_refused_for_outward_ssh(
    default_settings, monkeypatch,
):
    assert _validate(default_settings, monkeypatch) == []


def test_an_operator_who_switches_outward_ssh_off_is_told_which_switch(monkeypatch):
    settings = SimpleNamespace(boss_edition="community", ssh_outward_allowed=False,
                               ssh_host_deploy_allowed=False)
    problems = _validate(settings, monkeypatch)
    assert any("BOSS_SSH_OUTWARD_ALLOWED" in p for p in problems)
    kinds = [f["kind"] for f in deployment_fleet._classify(problems)]
    assert kinds == ["outward_ssh_disabled"]


def test_the_legacy_container_deploy_still_needs_its_own_switch(default_settings):
    enterprise = default_settings.model_copy(update={"boss_edition": "enterprise_self_hosted"})
    with pytest.raises(PermissionError, match="BOSS_SSH_HOST_DEPLOY_ALLOWED"):
        from boss_engine.services import deploy_service

        deploy_service.deploy_ssh(object(), enterprise, "crun_x", host="203.0.113.5")
