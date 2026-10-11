"""A stack deployment's host writes all carry the ChangeSet they apply, and nothing leaks.

The applier runs against a fake host here: what is asserted is the ORDER of what reaches the
machine, that every write names its ChangeSet, that secret values travel only on stdin and never
reach the log, that a second deploy runs `update` instead of `install`, and that a failure says
what happened and what to do — not a stack trace.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from boss_engine.services import ssh_host_adapter
from boss_engine.services.app_deploy_manifest import load_manifest, render
from boss_engine.services.stack_deploy import (
    HostAccess,
    StackApplier,
    StackStepFailed,
    explain_failure,
    preflight_host,
)

COMMIT = "c" * 40
MANIFEST = """
version: 1
name: boss
env:
  PUBLIC_URL: "{target.public_url}"
  DB_PASSWORD: {generate: password}
install: ["./install.sh", "./seed.sh"]
update: ["./upgrade.sh"]
health: [{url: "http://127.0.0.1:8000/health", timeout_seconds: 30}]
"""
SECRET = "s3cr3t-generated-value"
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----"


class FakeAdapter:
    """Answers like a host would, and records every call."""

    def __init__(self, *, installed=False, release_present=False, healthy=True,
                 fail_on=None):
        self.calls: list[dict] = []
        self.installed = installed
        self.release_present = release_present
        self.healthy = healthy
        self.fail_on = fail_on

    def stack_exec(self, **kwargs):
        self.calls.append(kwargs)
        command = kwargs["command"]
        if self.fail_on and self.fail_on in command:
            return subprocess.CompletedProcess([], 2, f"echo {SECRET}\n", "boom\n")
        out = ""
        if "BOSS_PREPARED" in command:
            out = ("BOSS_MODE=update\n" if self.installed else "BOSS_MODE=install\n")
            out += "BOSS_RELEASE_PRESENT\n" if self.release_present else ""
            out += "BOSS_PREPARED\n"
        for marker in ("BOSS_UPLOADED", "BOSS_ENV_WRITTEN", "BOSS_SWITCHED", "BOSS_FINALIZED"):
            if f"echo {marker}" in command:
                out += marker + "\n"
        if "BOSS_HEALTHY" in command:
            if not self.healthy:
                return subprocess.CompletedProcess([], 1, "BOSS_UNHEALTHY 502\n", "")
            out += "BOSS_HEALTHY 200\n"
        if "bash -lc" in command:
            out += f"ran with password {SECRET}\n"
        return subprocess.CompletedProcess([], 0, out, "")


def _applier(adapter, *, fetched=None):
    manifest = load_manifest(MANIFEST)
    rendered = render(manifest, {"commit": COMMIT, "ref": "main", "target.host": "203.0.113.9",
                                 "target.public_url": "https://boss.test",
                                 "target.user": "deploy"})
    records = []
    applier = StackApplier(
        access=HostAccess(host="203.0.113.9", user="deploy", port=22, key_material=KEY,
                          adapter=adapter),
        rendered=rendered, commit=COMMIT, ref="main",
        env_values={"PUBLIC_URL": "https://boss.test", "DB_PASSWORD": SECRET},
        redact_values=[SECRET, KEY], changeset_id="chg_approved",
        fetch_tree=(fetched.append if fetched is not None else (lambda dest: None)),
        record=lambda steps, log: records.append(([dict(s) for s in steps], log)),
        now=lambda: "2026-10-03T00:00:00Z")
    return applier, records


def test_a_first_deploy_uploads_writes_env_switches_installs_checks_health_then_finalizes():
    adapter, fetched = FakeAdapter(), []
    applier, records = _applier(adapter, fetched=fetched)
    outcome = applier.run()

    assert outcome["mode"] == "install"
    assert fetched, "the commit's tree was never fetched"
    commands = [c["command"] for c in adapter.calls]
    order = ["BOSS_PREPARED", "BOSS_UPLOADED", "BOSS_ENV_WRITTEN", "BOSS_SWITCHED",
             "./install.sh", "./seed.sh", "BOSS_HEALTHY", "BOSS_FINALIZED"]
    positions = [next(i for i, c in enumerate(commands) if marker in c) for marker in order]
    assert positions == sorted(positions), commands
    assert [s["status"] for s in outcome["steps"]] == ["succeeded"] * len(outcome["steps"])
    assert records, "progress was never recorded"


def test_every_write_names_its_changeset_and_only_the_health_probe_is_read_only():
    adapter = FakeAdapter()
    applier, _ = _applier(adapter)
    applier.run()
    for call in adapter.calls:
        if "BOSS_HEALTHY" in call["command"]:
            assert call["write"] is False
        else:
            assert call["write"] is True and call["changeset_id"] == "chg_approved"


def test_secret_values_travel_on_stdin_and_never_reach_a_command_line_or_the_log():
    adapter = FakeAdapter()
    applier, records = _applier(adapter)
    outcome = applier.run()
    env_writes = [c for c in adapter.calls if "BOSS_ENV_WRITTEN" in c["command"]]
    assert len(env_writes) == 1 and SECRET in env_writes[0]["stdin_text"]
    assert "DB_PASSWORD='" in env_writes[0]["stdin_text"]
    assert all(SECRET not in c["command"] for c in adapter.calls)
    assert SECRET not in outcome["log_tail"] and "[redacted]" in outcome["log_tail"]
    assert all(SECRET not in log for _steps, log in records)
    assert all(KEY not in log for _steps, log in records)


def test_a_host_already_installed_runs_update_and_skips_an_upload_it_already_has():
    adapter = FakeAdapter(installed=True, release_present=True)
    applier, _ = _applier(adapter, fetched=[])
    outcome = applier.run()
    commands = " ".join(c["command"] for c in adapter.calls)
    assert outcome["mode"] == "update"
    assert "./upgrade.sh" in commands and "./install.sh" not in commands
    assert "BOSS_UPLOADED" not in commands
    assert {s["name"]: s["status"] for s in outcome["steps"]}["upload"] == "skipped"


def test_a_failed_step_stops_the_deploy_with_a_headline_and_an_action():
    adapter = FakeAdapter(fail_on="./seed.sh")
    applier, _ = _applier(adapter)
    with pytest.raises(StackStepFailed) as caught:
        applier.run()
    assert caught.value.step == "install: install 2"
    assert "failed" in caught.value.headline and "exit 2" in caught.value.headline
    assert "roll back" in caught.value.action
    statuses = {s["name"]: s["status"] for s in applier.steps}
    assert statuses["install: install 2"] == "failed"
    assert statuses["finalize"] == "pending", "nothing after the failure ran"
    assert SECRET not in applier.log
    assert not any("BOSS_FINALIZED" in c["command"] for c in adapter.calls)


def test_an_unhealthy_app_fails_at_health_and_is_not_finalized():
    adapter = FakeAdapter(healthy=False)
    applier, _ = _applier(adapter)
    with pytest.raises(StackStepFailed) as caught:
        applier.run()
    assert caught.value.step == "health"
    assert "http://127.0.0.1:8000/health" in caught.value.headline
    assert not any("BOSS_FINALIZED" in c["command"] for c in adapter.calls)


def test_the_adapter_refuses_a_stack_write_without_a_changeset_before_running_ssh(monkeypatch):
    ran = []
    monkeypatch.setattr(ssh_host_adapter.owned_process, "run",
                        lambda *a, **k: ran.append(a) or subprocess.CompletedProcess([], 0))
    # A default Community install: no BOSS_SSH_HOST_DEPLOY_ALLOWED, outward SSH on by default.
    adapter = ssh_host_adapter.SSHHostAdapter(SimpleNamespace(ssh_host_deploy_allowed=False))
    with pytest.raises(PermissionError, match="ChangeSet"):
        adapter.stack_exec(host="h", user="u", port=22, key_material=KEY, command="touch x",
                           write=True, changeset_id=None)
    assert ran == []
    adapter.stack_exec(host="h", user="u", port=22, key_material=KEY, command="echo ok")
    assert len(ran) == 1, "a read-only probe needs no ChangeSet"


def test_the_adapter_honours_the_outward_ssh_switch(monkeypatch):
    adapter = ssh_host_adapter.SSHHostAdapter(
        SimpleNamespace(ssh_outward_allowed=False, ssh_host_deploy_allowed=False))
    with pytest.raises(Exception, match="BOSS_SSH_OUTWARD_ALLOWED"):
        adapter.stack_exec(host="h", user="u", port=22, key_material=KEY, command="echo ok")


def test_preflight_reports_what_the_host_lacks_as_actions():
    class Host:
        def stack_exec(self, **kwargs):
            assert kwargs["write"] is False, "preflight must never write"
            return subprocess.CompletedProcess(
                [], 0, "BOSS_SSH_OK\nDIR_NEEDS_SUDO\nSUDO_NO\nDOCKER_MISSING\nDOCKER_UNUSABLE\n"
                       "TAR_OK\nCURL_OK\n", "")

    manifest = load_manifest("version: 1\nname: app\ninstall: [x]\n"
                             "requires: {docker: true}\n")
    problems = preflight_host(HostAccess("203.0.113.9", "deploy", 22, KEY, Host()), manifest)
    text = " ".join(p["headline"] + " " + p["action"] for p in problems)
    assert "sudo mkdir -p /opt/boss-apps/app" in text
    assert "Docker is not installed" in text


def test_failures_are_explained_in_words():
    assert "refused BOSS's SSH key" in explain_failure(
        "prepare", 255, "Permission denied (publickey).", host="deploy@h")["headline"]
    assert "timed out" in explain_failure("install: x", 124, "", host="h")["headline"]
    assert "openssh-client" in explain_failure(
        "prepare", 127, "the `ssh` client is not installed")["action"]


def test_a_full_deployment_host_gets_read_only_disk_inspection_guidance():
    advice = explain_failure("install", 1, "no space left on device", host="shared-host")
    assert "shared-host ran out of disk space" in advice["headline"]
    assert "`docker system df`" in advice["action"]
    assert "confirmed BOSS-owned, unused resources" in advice["action"]
    assert "system prune" not in advice["action"]
    # An arbitrary application target need not have the BOSS CLI installed.
    assert "boss prune" not in advice["action"]


@pytest.mark.parametrize("edition", ["community", "oss", "local"])
def test_community_keeps_the_ungoverned_one_container_ssh_deploy_closed(edition):
    from boss_engine.services.deploy_service import deploy_ssh
    from boss_engine.services.deployment_connectors import SSHContainerConnector

    settings = SimpleNamespace(boss_edition=edition, ssh_host_deploy_allowed=True)
    blocked = SSHContainerConnector().apply(object(), settings, {"target_ref": "u@h"}, "crun",
                                            {})
    assert blocked["status"] == "blocked"
    # The refusal points at the governed path that now exists (governed_host_deploy.py).
    assert "approved Resource Plane ChangeSet" in blocked["problems"][0]
    assert "Approvals" in blocked["problems"][0]
    with pytest.raises(PermissionError, match="ChangeSet"):
        deploy_ssh(object(), settings, "crun", host="203.0.113.9")


def test_install_and_update_steps_run_in_the_releases_own_directory_not_through_current():
    """Through the `current` symlink every path a tool resolved matched the last release, so
    `docker compose up` recreated nothing: stack-demo v2 reported healthy while v1 still served."""
    adapter = FakeAdapter()
    applier, _ = _applier(adapter)
    applier.run()
    steps = [c["command"] for c in adapter.calls if "./install.sh" in c["command"]
             or "./seed.sh" in c["command"]]
    assert steps
    for command in steps:
        assert f"/releases/{COMMIT} && " in command, command
        assert "/current &&" not in command, command
