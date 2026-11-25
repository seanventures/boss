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
    # MT-1 sysstat / top refusals (before DB or SSH)
    "sar -o /tmp/x 1 5",
    "sar -f /etc/passwd",
    "sar -u 1 5 -o out.bin",
    "sar -u 1 5 | tail",
    "sar -u 1 500",
    "sar -u 99 1",
    "sar -u 10 10",
    "sar -u 1",
    "sar -u 0 5",
    "sar --help",
    "sar -n ALL 1 5",
    "sar -n BAD 1 5",
    "vmstat 1",
    "vmstat 1 5; id",
    "vmstat -p /dev/sda1",
    "vmstat -S X 1 5",
    "iostat 1",
    "iostat -o /tmp/io.json 1 5",
    "iostat 1 5 && rm -rf /",
    "mpstat 1",
    "mpstat -P /etc/passwd 1 2",
    "top",
    "top -b",
    "top -n 1",
    "top -b -n 50",
    "top -b -n 0",
    "top -b -n 1 > /tmp/x",
    "top -b -n 1 -o BADFIELD",
    "top -b -n 1 -u root",
    "top -b -n 5 -d 10",
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
    # MT-1 sysstat / top allowlist
    ("sar -u 1 5", "sar -u 1 5"),
    ("sar -u ALL 1 5", "sar -u ALL 1 5"),
    ("sar -r 1 3", "sar -r 1 3"),
    ("sar -q 1 2", "sar -q 1 2"),
    ("sar -n DEV 1 3", "sar -n DEV 1 3"),
    ("sar -b 1 3", "sar -b 1 3"),
    ("sar -d 1 3", "sar -d 1 3"),
    ("sar -u", "sar -u"),
    ("vmstat", "vmstat"),
    ("vmstat -s", "vmstat -s"),
    ("vmstat -d", "vmstat -d"),
    ("vmstat -w 1 5", "vmstat -w 1 5"),
    ("iostat", "iostat"),
    ("iostat -xz 1 3", "iostat -xz 1 3"),
    ("iostat -c -m 2 4", "iostat -c -m 2 4"),
    ("mpstat", "mpstat"),
    ("mpstat -P ALL 1 3", "mpstat -P ALL 1 3"),
    ("mpstat -P 0,1 1 2", "mpstat -P 0,1 1 2"),
    ("top -b -n 1", "top -b -n 1"),
    ("top -b -n 2 -d 1 -o %CPU -w 160", "top -b -n 2 -d 1 -o %CPU -w 160"),
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


@pytest.mark.parametrize("command,stderr", [
    ("sar -u 1 3", "bash: line 1: sar: command not found"),
    ("iostat -xz 1 3", "sh: 1: iostat: not found"),
    ("mpstat -P ALL 1 3", "bash: mpstat: command not found"),
])
def test_inspect_host_detects_missing_sysstat_without_stack_trace(monkeypatch, command, stderr):
    monkeypatch.setattr(secret_backends, "resolve_target_secret", lambda *_a, **_k: None)
    monkeypatch.setattr(deploy_service, "_ssh_key_file", lambda *_a: "/tmp/fake-key")
    monkeypatch.setattr(
        deploy_service, "_ssh",
        lambda *_a, **_kw: CompletedProcess([], 127, stdout="", stderr=stderr),
    )
    result = _inspect(HostContext(), command)
    assert result["ok"] is False
    assert result["error"] == "missing_package"
    assert result["missing_package"] == "sysstat"
    assert "sysstat" in result["message"]
    assert "install_hint" in result


def test_inspect_host_does_not_confuse_missing_local_ssh_with_missing_sysstat(monkeypatch):
    monkeypatch.setattr(secret_backends, "resolve_target_secret", lambda *_a, **_k: None)
    monkeypatch.setattr(deploy_service, "_ssh_key_file", lambda *_a: "/tmp/fake-key")
    monkeypatch.setattr(
        deploy_service, "_ssh",
        lambda *_a, **_kw: CompletedProcess(
            [], 127, stdout="",
            stderr="the `ssh` client is not installed in this environment, so BOSS cannot reach any host.",
        ),
    )
    result = _inspect(HostContext(), "sar -u 1 3")
    assert result["ok"] is True  # _run_read_on_host returns transport dict; not mislabeled sysstat
    assert result.get("error") != "missing_package"


def test_collect_diagnostics_refuses_bad_bounds_before_db_or_ssh(monkeypatch):
    effects = []
    monkeypatch.setattr(deploy_service, "_ssh", lambda *_a, **_kw: effects.append("ssh"))
    reg = build_default_registry()
    for bad_args in (
        {"host": "test-host", "interval_seconds": 20, "count": 1},
        {"host": "test-host", "interval_seconds": 5, "count": 5},
        {"host": "test-host", "sections": ["bogus"]},
        {"host": ""},
    ):
        res = reg.invoke(
            "collect_diagnostics", NoDatabaseContext("community"),
            project_id="project-a", actor="ceo", args=bad_args,
        )
        assert res["ok"] is False
        assert effects == []


def test_collect_diagnostics_captures_bounded_snapshot_and_ticket_payload(monkeypatch):
    monkeypatch.setattr(secret_backends, "resolve_target_secret", lambda *_a, **_k: None)
    monkeypatch.setattr(deploy_service, "_ssh_key_file", lambda *_a: "/tmp/fake-key")
    bundle_stdout = "\n".join([
        "===BOSS_DIAG:system_uptime===",
        " 07:00:00 up 5 days, 1 user, load average: 0.25, 0.20, 0.18",
        "===BOSS_DIAG_RC:system_uptime:0===",
        "===BOSS_DIAG:sar_bundle===",
        "Linux 6.8.0 (test-host)  %usr 4.50 %sys 1.20 %iowait 0.30 %idle 94.00",
        "===BOSS_DIAG_RC:sar_bundle:0===",
        "===BOSS_DIAG:cpu_mpstat===",
        "all 4.50 0.00 1.20 0.30 94.00",
        "===BOSS_DIAG_RC:cpu_mpstat:0===",
        "===BOSS_DIAG:memory_free===",
        "Mem: 7982 2100 4120 120 1762 5400",
        "===BOSS_DIAG_RC:memory_free:0===",
        "===BOSS_DIAG:memory_vmstat===",
        "procs -----------memory---------- ---swap--",
        "===BOSS_DIAG_RC:memory_vmstat:0===",
        "===BOSS_DIAG:io_iostat===",
        "nvme0n1 1.20 10.0 2.0",
        "===BOSS_DIAG_RC:io_iostat:0===",
        "===BOSS_DIAG:network_ss===",
        "tcp LISTEN 0 4096 0.0.0.0:8000 0.0.0.0:*",
        "===BOSS_DIAG_RC:network_ss:0===",
        "===BOSS_DIAG:disk_df===",
        "/dev/sda1 50G 12G 38G 24% /",
        "===BOSS_DIAG_RC:disk_df:0===",
        "===BOSS_DIAG:processes_top===",
        "PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ COMMAND",
        "123 boss 20 0 100m 42m 10m S 3.2 0.5 0:04.12 python3",
        "===BOSS_DIAG_RC:processes_top:0===",
    ])
    monkeypatch.setattr(
        deploy_service, "_ssh",
        lambda *_a, **_kw: CompletedProcess([], 0, stdout=bundle_stdout, stderr=""),
    )
    res = build_default_registry().invoke(
        "collect_diagnostics", HostContext(),
        project_id="project-a", actor="ceo",
        args={
            "host": "test-host",
            "interval_seconds": 1,
            "count": 2,
            "task_id": "task_123",
            "ticket_ref": "BOSSAPP-1368",
        },
    )
    assert res["ok"] is True
    assert res["missing_packages"] == []
    assert set(res["sections"]) == {"system", "cpu", "memory", "io", "network", "disk", "processes"}
    assert len(res["ticket_markdown"]) <= 6000
    assert "BOSSAPP-1368" in res["ticket_markdown"]
    assert res["artifact_payload"]["artifact_type"] == "host_diagnostics"
    assert res["artifact_payload"]["task_id"] == "task_123"
    assert res["artifact_payload"]["ticket_ref"] == "BOSSAPP-1368"


def test_collect_diagnostics_degrades_gracefully_when_sysstat_missing(monkeypatch):
    monkeypatch.setattr(secret_backends, "resolve_target_secret", lambda *_a, **_k: None)
    monkeypatch.setattr(deploy_service, "_ssh_key_file", lambda *_a: "/tmp/fake-key")
    bundle_stdout = "\n".join([
        "===BOSS_DIAG:system_uptime===",
        "up 2 days",
        "===BOSS_DIAG_RC:system_uptime:0===",
        "===BOSS_DIAG:sar_bundle===",
        "bash: sar: command not found",
        "===BOSS_DIAG_RC:sar_bundle:127===",
        "===BOSS_DIAG:cpu_mpstat===",
        "bash: mpstat: command not found",
        "===BOSS_DIAG_RC:cpu_mpstat:127===",
        "===BOSS_DIAG:memory_free===",
        "Mem: 4000 1000 3000",
        "===BOSS_DIAG_RC:memory_free:0===",
        "===BOSS_DIAG:memory_vmstat===",
        "r b swpd free",
        "===BOSS_DIAG_RC:memory_vmstat:0===",
        "===BOSS_DIAG:io_iostat===",
        "bash: iostat: command not found",
        "===BOSS_DIAG_RC:io_iostat:127===",
        "===BOSS_DIAG:network_ss===",
        "tcp LISTEN 0 128 0.0.0.0:22",
        "===BOSS_DIAG_RC:network_ss:0===",
        "===BOSS_DIAG:disk_df===",
        "/dev/vda1 40G 8G 32G 20% /",
        "===BOSS_DIAG_RC:disk_df:0===",
        "===BOSS_DIAG:processes_top===",
        "1 root 20 0 10m 4m S 0.0 0.1 0:01 systemd",
        "===BOSS_DIAG_RC:processes_top:0===",
    ])
    monkeypatch.setattr(
        deploy_service, "_ssh",
        lambda *_a, **_kw: CompletedProcess([], 0, stdout=bundle_stdout, stderr=""),
    )
    res = build_default_registry().invoke(
        "collect_diagnostics", HostContext(),
        project_id="project-a", actor="ceo",
        args={"host": "test-host"},
    )
    assert res["ok"] is True
    assert res["missing_packages"] == ["sysstat"]
    assert any("sysstat" in w for w in res["warnings"])
    assert "sysstat" in res["ticket_markdown"]
    assert res["sections"]["disk"]["status"] == "ok"
    assert res["sections"]["processes"]["status"] == "ok"


def test_policy_rule_table_matches_validator_contract():
    import json
    from boss_engine.tools.host_diagnostics_rules import policy_rule_table

    table = policy_rule_table()
    encoded = json.dumps(table)
    assert encoded
    for verb in ("sar", "vmstat", "iostat", "mpstat", "top"):
        assert verb in table["verbs"]
    assert "-o" in table["verbs"]["sar"]["forbidden_flags"]
    assert "-f" in table["verbs"]["sar"]["forbidden_flags"]
    assert "-K" in table["verbs"]["ss"]["forbidden_flags"]
    assert "--kill" in table["verbs"]["ss"]["forbidden_flags"]
    assert table["caps"]["max_interval_seconds"] == 10
    assert table["caps"]["max_sample_count"] == 10
    assert table["caps"]["max_sample_duration_seconds"] == 30
