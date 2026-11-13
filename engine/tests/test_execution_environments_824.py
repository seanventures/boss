"""BOSSAPP-824 phase 1: pluggable execution environments.

What is pinned here, in the ticket's own order:
  * the factory's DEFAULT is byte-identical — with no environments configured
    and no knobs set, the governed seam gets exactly LocalSubprocessProvider;
  * resolution precedence: the step's explicit override -> the agent's bound
    row -> the project default -> the instance (org-wide) default;
  * the lease lifecycle: acquire writes a held row, teardown releases it, and
    the reaper expires what a dead build never released;
  * the SSH driver against a FAKE transport (the FakeAdapter pattern the stack
    deploy suite proved): no real host, every remote call recorded — the clone
    credential never reaches argv, warm lineage dirs are never claimed, and
    BOSS_SANDBOX_COMMAND_RUNTIME=docker is refused in words;
  * governed_code_task_service no longer hard-codes the provider (enforced at
    the source level, so the hard-code cannot quietly come back).

Schema idempotency (apply the new block twice on real Postgres) lives in
test_execution_environments_schema_824.py — it needs a disposable database.
"""
from __future__ import annotations

import base64
import gzip
import io
import re
import shlex
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import execution_environments as envs
from boss_engine.services.sandbox import CommandResult as SandboxCommandResult
from boss_engine.services.sandbox import SandboxError
from boss_engine.services.sandbox_provider import (
    LocalSubprocessProvider,
    SandboxPlaneError,
    make_governed_sandbox_provider,
    make_sandbox_provider,
)
from boss_engine.services.ssh_environment import (
    DEFAULT_WORKROOT,
    SSHEnvironmentDriver,
    SSHSandbox,
)
from boss_engine.services.stack_deploy import HostAccess

ROOT = Path(__file__).resolve().parents[2]


def settings(**overrides) -> SimpleNamespace:
    base = dict(
        sandbox_provider="local",
        governed_sandbox_provider="local",
        microvm_claims_frozen=True,
        sandbox_command_runtime="subprocess",
        sandbox_clone_depth=1,
        sandbox_validation_timeout_seconds=600,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE execution_environments ("
            " id TEXT PRIMARY KEY, project_id TEXT, name TEXT NOT NULL,"
            " driver TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}',"
            " secret_refs TEXT NOT NULL DEFAULT '{}',"
            " status TEXT NOT NULL DEFAULT 'registered', created_by TEXT,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            " updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        c.exec_driver_sql(
            "CREATE TABLE environment_leases ("
            " id TEXT PRIMARY KEY, environment_id TEXT NOT NULL,"
            " fingerprint TEXT NOT NULL DEFAULT '',"
            " state TEXT NOT NULL DEFAULT 'held', run_id TEXT,"
            " acquired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            " expires_at TIMESTAMP, released_at TIMESTAMP)"
        )
    return eng


# ── (a) the factory default is byte-identical ────────────────────────────────


class TestFactoryDefault:
    def test_no_connection_no_knobs_is_local_subprocess(self):
        s = settings()
        provider = make_governed_sandbox_provider(s)
        assert type(provider) is LocalSubprocessProvider
        assert provider.settings is s  # the SAME settings object, as before

    def test_empty_table_is_local_subprocess(self, engine):
        s = settings()
        with engine.begin() as c:
            provider = make_governed_sandbox_provider(
                s, connection=c, engine=engine, project_id="prj_1", spec={})
        assert type(provider) is LocalSubprocessProvider

    def test_a_pre_824_database_behaves_as_before(self):
        # No execution_environments table at all: the lookup fails inside the
        # factory's OWN transaction (never aborting the caller's) and the
        # factory answers exactly as the hard-coded line did.
        bare = create_engine("sqlite://")
        provider = make_governed_sandbox_provider(
            settings(), engine=bare, project_id="prj_1")
        assert type(provider) is LocalSubprocessProvider

    def test_engine_only_resolution_matches_connection_resolution(self, engine):
        # the governed pipeline hands the factory an engine, not a connection
        with engine.begin() as c:
            envs.create_environment(
                c, name="box", driver="ssh", project_id="prj_1",
                config={"default": True, "address": "198.51.100.8"})
        provider = make_governed_sandbox_provider(
            settings(), engine=engine, project_id="prj_1")
        assert isinstance(provider, SSHEnvironmentDriver)

    def test_microvm_knob_keeps_the_granular_seam_local(self, engine):
        # The micro-VM lane belongs to _resolve_use_microvm/MicroVMProvider;
        # the granular seam stays the control plane's git state carrier.
        with engine.begin() as c:
            provider = make_governed_sandbox_provider(
                settings(governed_sandbox_provider="microvm"),
                connection=c, engine=engine, project_id="prj_1")
        assert type(provider) is LocalSubprocessProvider

    def test_native_factory_is_unchanged(self):
        assert type(make_sandbox_provider(settings())) is LocalSubprocessProvider

    def test_environment_only_knob_without_a_row_is_refused_in_words(self):
        with pytest.raises(SandboxPlaneError) as caught:
            make_governed_sandbox_provider(settings(governed_sandbox_provider="ssh"))
        assert caught.value.status_code == 422
        assert "execution environment" in str(caught.value)

    def test_native_knob_recognizes_the_new_kinds(self):
        with pytest.raises(SandboxPlaneError):
            make_sandbox_provider(settings(sandbox_provider="ssh"))
        with pytest.raises(SandboxPlaneError):
            make_sandbox_provider(settings(sandbox_provider="daytona"))


# ── (b) resolution precedence: step -> agent -> project -> instance ──────────


class TestResolutionPrecedence:
    def _create(self, c, **kwargs):
        return envs.create_environment(c, **kwargs)

    def test_each_level_wins_over_the_one_below(self, engine):
        with engine.begin() as c:
            instance = self._create(
                c, name="org-default", driver="ssh", project_id=None,
                config={"default": True, "address": "198.51.100.1"})
            assert envs.resolve_environment(c, project_id="prj_1")["id"] == instance["id"]

            project = self._create(
                c, name="prj-default", driver="ssh", project_id="prj_1",
                config={"default": True, "address": "198.51.100.2"})
            assert envs.resolve_environment(c, project_id="prj_1")["id"] == project["id"]

            agent_row = self._create(
                c, name="builder-box", driver="ssh", project_id="prj_1",
                config={"agents": ["builder-1"], "address": "198.51.100.3"})
            assert envs.resolve_environment(
                c, project_id="prj_1", agent="builder-1")["id"] == agent_row["id"]
            # a different agent falls through to the project default
            assert envs.resolve_environment(
                c, project_id="prj_1", agent="other")["id"] == project["id"]

            explicit = self._create(
                c, name="step-box", driver="ssh", project_id="prj_1",
                config={"address": "198.51.100.4"})
            assert envs.resolve_environment(
                c, project_id="prj_1", agent="builder-1",
                spec={"environment_id": explicit["id"]})["id"] == explicit["id"]

    def test_substrate_override_picks_that_driver_or_refuses_in_words(self, engine):
        with engine.begin() as c:
            with pytest.raises(envs.EnvironmentUnusable):
                envs.resolve_environment(c, project_id="prj_1",
                                         spec={"substrate": "ssh"})
            row = self._create(c, name="ssh-box", driver="ssh", project_id="prj_1",
                               config={"address": "198.51.100.9"})
            resolved = envs.resolve_environment(c, project_id="prj_1",
                                                spec={"substrate": "ssh"})
            assert resolved["id"] == row["id"]

    def test_legacy_substrate_vocabulary_stays_with_the_legacy_paths(self, engine):
        with engine.begin() as c:
            self._create(c, name="d", driver="ssh", project_id="prj_1",
                         config={"default": True, "address": "h"})
            for legacy in ("local", "subprocess", "microvm", "firecracker"):
                # the explicit legacy ask bypasses environments entirely
                assert envs.resolve_environment(
                    c, project_id="prj_1", spec={"substrate": legacy}) is None

    def test_a_retired_row_never_resolves(self, engine):
        with engine.begin() as c:
            row = self._create(c, name="old", driver="ssh", project_id="prj_1",
                               config={"default": True, "address": "h"})
            envs.update_environment(c, row["id"], status="retired")
            assert envs.resolve_environment(c, project_id="prj_1") is None
            with pytest.raises(envs.EnvironmentUnusable):
                envs.resolve_environment(c, project_id="prj_1",
                                         spec={"environment_id": row["id"]})

    def test_factory_hands_the_ssh_driver_for_a_resolved_row(self, engine):
        with engine.begin() as c:
            self._create(c, name="box", driver="ssh", project_id="prj_1",
                         config={"default": True, "address": "198.51.100.7"})
        with engine.begin() as c:
            provider = make_governed_sandbox_provider(
                settings(), connection=c, engine=engine, project_id="prj_1")
        assert isinstance(provider, SSHEnvironmentDriver)
        assert provider.kind == "ssh"

    def test_an_unimplemented_driver_is_refused_in_words(self, engine):
        with engine.begin() as c:
            self._create(c, name="k8s", driver="kubernetes", project_id="prj_1",
                         config={"default": True})
        with engine.begin() as c:
            with pytest.raises(SandboxPlaneError) as caught:
                make_governed_sandbox_provider(
                    settings(), connection=c, engine=engine, project_id="prj_1")
        assert "not implemented" in str(caught.value)
        assert caught.value.status_code == 422

    def test_secret_shaped_config_never_leaves_the_public_view(self, engine):
        with engine.begin() as c:
            row = self._create(
                c, name="box", driver="ssh", project_id="prj_1",
                config={"address": "h", "api_token": "sk-live-oops"},
                secret_refs={"key_secret_ref": "secref_1"})
        view = envs.public_view(row)
        assert "sk-live-oops" not in str(view)
        assert view["secret_refs"]["key_secret_ref"] == "secref_1"  # ids, not values


# ── (c) lease lifecycle, including the reaper ────────────────────────────────


class TestLeases:
    def _env(self, c) -> dict:
        return envs.create_environment(
            c, name="box", driver="ssh", project_id="prj_1",
            config={"address": "h", "image": "ubuntu-22", "cpu": 2, "mem_mb": 4096})

    def test_acquire_release(self, engine):
        with engine.begin() as c:
            row = self._env(c)
            lease = envs.acquire_lease(
                c, environment_id=row["id"], run_id="crun_1",
                fingerprint=envs.lease_fingerprint(row))
            assert lease["state"] == "held"
            assert lease["fingerprint"] == "ssh:image=ubuntu-22:cpu=2:mem_mb=4096"
            assert envs.release_lease(c, run_id="crun_1") == 1
            stored = c.execute(text(
                "SELECT state, released_at FROM environment_leases WHERE id = :i"),
                {"i": lease["id"]}).mappings().first()
        assert stored["state"] == "released"
        assert stored["released_at"] is not None

    def test_release_is_idempotent_and_scoped(self, engine):
        with engine.begin() as c:
            row = self._env(c)
            envs.acquire_lease(c, environment_id=row["id"], run_id="crun_1")
            assert envs.release_lease(c, run_id="crun_1") == 1
            assert envs.release_lease(c, run_id="crun_1") == 0  # nothing held
            assert envs.release_lease(c) == 0                   # no selector: no-op

    def test_reaper_expires_only_the_stale_held_lease(self, engine):
        with engine.begin() as c:
            row = self._env(c)
            stale = envs.acquire_lease(c, environment_id=row["id"], run_id="crun_dead")
            fresh = envs.acquire_lease(c, environment_id=row["id"], run_id="crun_live")
            past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            c.execute(text("UPDATE environment_leases SET expires_at = :p WHERE id = :i"),
                      {"p": past, "i": stale["id"]})
        report = envs.reap_leases(engine)
        assert report["expired"] == 1
        with engine.begin() as c:
            states = dict(c.execute(text(
                "SELECT id, state FROM environment_leases")).fetchall())
        assert states[stale["id"]] == "expired"
        assert states[fresh["id"]] == "held"

    def test_reaper_tolerates_a_pre_824_schema(self):
        bare = create_engine("sqlite://")
        report = envs.reap_leases(bare)
        assert report["expired"] == 0
        assert "skipped" in report

    def test_reaper_is_registered_on_the_maintenance_tick(self):
        source = (ROOT / "engine/boss_engine/services/resource_reaper.py").read_text()
        assert "execution_environments" in source
        assert "reap_leases" in source


# ── (d) the SSH driver, against a recorded fake transport ────────────────────


def tarball_b64(files: dict[str, bytes]) -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return base64.b64encode(gzip.compress(buffer.getvalue())).decode()


class FakeTransport:
    """Answers like a host would, records every call — the FakeAdapter pattern
    from the stack-deploy suite, substituted for ssh_host_adapter."""

    def __init__(self):
        self.calls: list[dict] = []
        self.diff_files: dict[str, bytes] = {"src/app.py": b"print('hi')\n"}

    def stack_exec(self, **kwargs):
        self.calls.append(kwargs)
        script = kwargs.get("stdin_text") or ""
        command = kwargs.get("command") or ""
        blob = script + command
        out = ""
        if "git clone" in blob:
            out = "BOSS_SHA=abc123def456\nBOSS_PROVISIONED\n"
        elif "--numstat" in blob:
            out = "3\t1\tsrc/app.py\n"
        elif "base64" in blob and "tar -czf" in blob:
            out = tarball_b64(self.diff_files) + "\n"
        elif "pytest -q" in blob:
            out = "1 passed\n"
        return subprocess.CompletedProcess([], 0, out, "")


def ssh_sandbox(transport: FakeTransport, **setting_overrides) -> SSHSandbox:
    access = HostAccess(host="203.0.113.5", user="root", port=22,
                        key_material="FAKE-KEY", adapter=transport)
    return SSHSandbox(settings(**setting_overrides), task_id="crun_t824",
                      access=access, engine=None, host_id=None,
                      environment={"config": {}})


class TestSSHSandbox:
    def test_provision_clones_remotely_and_keeps_the_token_off_argv(self):
        transport = FakeTransport()
        sandbox = ssh_sandbox(transport)
        path = sandbox.provision("acme", "app", base_branch="main",
                                 token="ghs_SECRETTOKEN")
        assert str(path) == f"{DEFAULT_WORKROOT}/crun_t824"
        assert sandbox.base_sha == "abc123def456"
        assert sandbox.diff_base_sha == "abc123def456"
        clone_call = next(c for c in transport.calls
                          if "git clone" in (c.get("stdin_text") or ""))
        # The script rides stdin; the COMMAND (ssh argv) carries no secret and
        # no repo URL, and the raw token never appears even on stdin (it is
        # base64'd into git's env-borne extraheader, as the local sandbox does).
        assert clone_call["command"] == "sh -es"
        for call in transport.calls:
            assert "ghs_SECRETTOKEN" not in (call.get("command") or "")
        assert "GIT_CONFIG_KEY_0" in clone_call["stdin_text"]
        # the per-host key reached the transport — HostAccess was reused
        assert all(c["key_material"] == "FAKE-KEY" for c in transport.calls)

    def test_provision_never_claims_a_lineage_warm_dir(self):
        transport = FakeTransport()
        sandbox = ssh_sandbox(transport)
        path = sandbox.provision(
            "acme", "app", base_branch="main", token="t",
            warm_dir="/var/lib/boss/lineage-sandboxes/lin_1", reuse_warm=True)
        # fresh remote clone in the driver's own workroot — the local warm dir
        # is not touched, referenced, or claimed
        assert str(path) == f"{DEFAULT_WORKROOT}/crun_t824"
        for call in transport.calls:
            blob = (call.get("stdin_text") or "") + (call.get("command") or "")
            assert "lineage-sandboxes" not in blob

    def test_local_repository_is_refused_in_words(self):
        from boss_engine.services.setup_service import SetupError

        sandbox = ssh_sandbox(FakeTransport())
        egress = SimpleNamespace(clone_url="file:///var/lib/boss/repos/x.git",
                                 git_config=[], own_git=None)
        with pytest.raises(SetupError, match="local repository"):
            sandbox.provision("local_p", "x", egress=egress)

    def test_docker_command_runtime_is_refused_with_a_setup_error(self):
        from boss_engine.services.setup_service import SetupError

        sandbox = ssh_sandbox(FakeTransport(), sandbox_command_runtime="docker")
        with pytest.raises(SetupError, match="BOSS_SANDBOX_COMMAND_RUNTIME=docker"):
            sandbox.provision("acme", "app", token="t")

    def test_run_executes_in_the_workdir_and_refuses_shell_syntax(self):
        transport = FakeTransport()
        sandbox = ssh_sandbox(transport)
        sandbox.provision("acme", "app", token="t")
        result = sandbox.run("pytest -q")
        assert isinstance(result, SandboxCommandResult)
        assert result.ok and "1 passed" in result.stdout
        script = transport.calls[-1]["stdin_text"]
        assert f"cd {shlex.quote(f'{DEFAULT_WORKROOT}/crun_t824')}" in script
        with pytest.raises(SandboxError, match="shell syntax"):
            sandbox.run("pytest -q && rm -rf /")

    def test_run_drops_credential_shaped_env(self):
        transport = FakeTransport()
        sandbox = ssh_sandbox(transport)
        sandbox.provision("acme", "app", token="t")
        sandbox.run("pytest -q", env={"API_TOKEN": "nope", "PORT": "8080"})
        script = transport.calls[-1]["stdin_text"]
        assert "PORT=8080" in script
        assert "nope" not in script

    def test_diff_summary_and_diff_come_back_over_the_tar_idiom(self):
        transport = FakeTransport()
        sandbox = ssh_sandbox(transport)
        sandbox.provision("acme", "app", token="t")
        summary = sandbox.diff_summary()
        assert summary.files_changed == 1
        assert summary.additions == 3 and summary.deletions == 1
        assert summary.paths == ["src/app.py"]
        changes = sandbox.diff()
        assert [c.path for c in changes] == ["src/app.py"]
        assert changes[0].content == "print('hi')\n"

    def test_diff_skips_binary_files_like_the_local_sandbox(self):
        transport = FakeTransport()
        transport.diff_files = {"src/app.py": b"ok\n", "logo.png": b"\x89PNG\x00\xff"}
        sandbox = ssh_sandbox(transport)
        sandbox.provision("acme", "app", token="t")
        transport_numstat = "2\t0\tsrc/app.py\n-\t-\tlogo.png\n"
        original = transport.stack_exec

        def answering(**kwargs):
            blob = (kwargs.get("stdin_text") or "") + (kwargs.get("command") or "")
            if "--numstat" in blob:
                transport.calls.append(kwargs)
                return subprocess.CompletedProcess([], 0, transport_numstat, "")
            return original(**kwargs)

        transport.stack_exec = answering
        changes = sandbox.diff()
        assert [c.path for c in changes] == ["src/app.py"]

    def test_sync_in_pipes_a_tarball_into_tar_xzf(self, tmp_path):
        transport = FakeTransport()
        sandbox = ssh_sandbox(transport)
        sandbox.provision("acme", "app", token="t")
        (tmp_path / "data.txt").write_text("payload")
        sandbox.sync_in(tmp_path, "fixtures")
        call = transport.calls[-1]
        assert "tar -xzf - -C" in call["command"]
        assert call["stdin_path"]  # the tarball rode stdin, binary-safe

    def test_teardown_removes_the_workdir_and_only_the_workdir(self):
        transport = FakeTransport()
        sandbox = ssh_sandbox(transport)
        sandbox.provision("acme", "app", token="t")
        sandbox.teardown()
        assert sandbox.path is None
        last = transport.calls[-1]["stdin_text"]
        assert f"rm -rf {shlex.quote(f'{DEFAULT_WORKROOT}/crun_t824')}" in last
        assert "lineage-sandboxes" not in last

    def test_driver_acquire_writes_a_lease_and_teardown_releases_it(self, engine):
        transport = FakeTransport()
        with engine.begin() as c:
            row = envs.create_environment(
                c, name="box", driver="ssh", project_id="prj_1",
                config={"address": "203.0.113.5"},
                secret_refs={"key_secret_ref": "secref_1"})

        driver = SSHEnvironmentDriver(settings(), engine=engine, environment=row,
                                      adapter=transport)

        # host_access_for resolves the key from the row's secret ref; fake it —
        # the driver's job here is the lease, not the secret backend.
        import boss_engine.services.ssh_environment as sshmod

        def fake_access(connection, s, host, adapter=None):
            return HostAccess(host=host["address"], user="root", port=22,
                              key_material="FAKE-KEY", adapter=transport)

        original = sshmod._access_for
        sshmod._access_for = fake_access
        try:
            sandbox = driver.acquire(task_id="crun_lease")
            with engine.begin() as c:
                held = c.execute(text(
                    "SELECT state, run_id, fingerprint FROM environment_leases"
                )).mappings().first()
            assert held["state"] == "held"
            assert held["run_id"] == "crun_lease"
            assert held["fingerprint"].startswith("ssh")
            sandbox.engine = engine  # release path
            sandbox.teardown()
            with engine.begin() as c:
                state = c.execute(text(
                    "SELECT state FROM environment_leases")).scalar()
            assert state == "released"
        finally:
            sshmod._access_for = original

    def test_driver_acquire_refuses_docker_runtime_before_any_lease(self, engine):
        from boss_engine.services.setup_service import SetupError

        with engine.begin() as c:
            row = envs.create_environment(
                c, name="box", driver="ssh", project_id="prj_1",
                config={"address": "203.0.113.5"})
        driver = SSHEnvironmentDriver(settings(sandbox_command_runtime="docker"),
                                      engine=engine, environment=row)
        with pytest.raises(SetupError):
            driver.acquire(task_id="crun_x")
        with engine.begin() as c:
            assert c.execute(text(
                "SELECT COUNT(*) FROM environment_leases")).scalar() == 0


# ── (f) the governed seam is no longer hard-coded ────────────────────────────


class TestGovernedSeamIsPluggable:
    def test_no_hard_coded_local_subprocess_provider(self):
        source = (ROOT / "engine/boss_engine/services/"
                  "governed_code_task_service.py").read_text()
        assert not re.search(r"provider\s*=\s*LocalSubprocessProvider\(", source), (
            "governed_code_task_service hard-codes the sandbox provider again — "
            "it must go through make_governed_sandbox_provider (BOSSAPP-824)")
        assert "make_governed_sandbox_provider(" in source

    def test_substrate_vocabulary_knows_the_new_kinds(self):
        from boss_engine.services.execution_substrate import (
            ExecutionSubstrate,
            normalize_build_substrate,
        )

        assert normalize_build_substrate("ssh") is ExecutionSubstrate.SSH
        assert normalize_build_substrate("kubernetes") is ExecutionSubstrate.KUBERNETES
        assert normalize_build_substrate("e2b") is ExecutionSubstrate.E2B
        assert normalize_build_substrate("daytona") is ExecutionSubstrate.DAYTONA
        assert (normalize_build_substrate("local")
                is ExecutionSubstrate.LOCAL_SUBPROCESS)  # unchanged
        with pytest.raises(ValueError):
            normalize_build_substrate("mainframe")  # unknown is still rejected
