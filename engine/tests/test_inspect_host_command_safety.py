"""The self-executable host inspection tool must never run shell syntax."""

from contextlib import contextmanager
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from boss_engine.services import deploy_service, secret_backends
from boss_engine.tools.builtin import build_default_registry


class NoDatabaseContext:
    def __init__(self, edition):
        self.settings = SimpleNamespace(boss_edition=edition)

    def begin(self):
        raise AssertionError("an unsafe command reached the host database")


class HostContext:
    settings = SimpleNamespace(boss_edition="community")

    @contextmanager
    def begin(self):
        class Result:
            def mappings(self):
                return self

            def first(self):
                return {
                    "id": "dh_1", "project_id": "project-a", "name": "test-host",
                    "address": "203.0.113.20", "ssh_user": "root", "ssh_port": 22,
                    "key_secret_ref": None,
                }

        class Connection:
            def execute(self, *_args, **_kwargs):
                return Result()

        yield Connection()


def _inspect(ctx, command):
    return build_default_registry().invoke(
        "inspect_host", ctx, project_id="project-a", actor="ceo",
        args={"host": "test-host", "command": command},
    )


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown"])
@pytest.mark.parametrize("command", [
    "ls & touch /tmp/x",
    "ls; touch /tmp/x",
    "ls | tee /tmp/x",
    "ls || touch /tmp/x",
    "ls && touch /tmp/x",
    "ls $(touch /tmp/x)",
    "ls `touch /tmp/x`",
    "ls ${IFS}touch /tmp/x",
    "ls > /tmp/x",
    "ls < /tmp/x",
    "ls\\;touch /tmp/x",
    "ls\ntouch /tmp/x",
    "ls\rtouch /tmp/x",
    "ls\ttouch /tmp/x",
    "ls\x00touch /tmp/x",
    "ls\x7ftouch /tmp/x",
    "ss -K dst 127.0.0.1",
    "ss --kill dst 127.0.0.1",
    "journalctl --vacuum-size=1G",
    "journalctl --rotate",
    "docker psx",
    "systemctl statusx sshd",
])
def test_unsafe_inspection_never_reaches_database_or_ssh(monkeypatch, command, edition):
    effects = []
    monkeypatch.setattr(
        deploy_service, "_ssh", lambda *_args, **_kwargs: effects.append("ssh"),
    )
    result = _inspect(NoDatabaseContext(edition), command)
    assert result["ok"] is False, command
    assert effects == []


@pytest.mark.parametrize("command,expected", [
    ("ls -la /var/log", "ls -la /var/log"),
    ("docker ps --format '{{.Names}}'", "docker ps --format '{{.Names}}'"),
    ("docker stats --no-stream app", "docker stats --no-stream app"),
    ("cat /etc/os-release", "cat /etc/os-release"),
    ("journalctl -u docker -n 20 --no-pager",
     "journalctl -u docker -n 20 --no-pager"),
    ("ss -tulnp", "ss -tulnp"),
])
def test_supported_reads_use_one_canonical_ssh_command(monkeypatch, command, expected):
    sent = []
    monkeypatch.setattr(
        secret_backends, "resolve_target_secret",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(deploy_service, "_ssh_key_file", lambda *_args: "/tmp/fake-key")

    def fake_ssh(_key, _host, _user, _port, remote, **_kwargs):
        sent.append(remote)
        return CompletedProcess([], 0, stdout="read-only output", stderr="")

    monkeypatch.setattr(deploy_service, "_ssh", fake_ssh)
    result = _inspect(HostContext(), command)
    assert result["ok"] is True, result
    assert result["stdout"] == "read-only output"
    assert sent == [expected]


def test_quoted_filename_is_passed_as_a_literal_argument(monkeypatch):
    sent = []
    monkeypatch.setattr(secret_backends, "resolve_target_secret", lambda *_a, **_k: None)
    monkeypatch.setattr(deploy_service, "_ssh_key_file", lambda *_a: "/tmp/fake-key")
    monkeypatch.setattr(
        deploy_service, "_ssh",
        lambda _k, _h, _u, _p, command, **_kw:
            sent.append(command) or CompletedProcess([], 0, stdout="", stderr=""),
    )
    assert _inspect(HostContext(), 'ls "/var/log/my files"')["ok"] is True
    assert sent == ["ls '/var/log/my files'"]
