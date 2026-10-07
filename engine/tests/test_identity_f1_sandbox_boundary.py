"""F1 · THE SANDBOX BOUNDARY (finding #1, R1; A51).

Two layers:

* Everywhere (macOS dev included), with a stand-in helper that records how it was called:
  every process a code-task run starts goes through `boss-sandbox-exec run <uid>` with a uid from
  the pool, its own HOME/TMPDIR inside the run's directory and the egress CONNECT proxy; BOSS's own
  git (`sandboxed=False`) and work outside a run do not; `required` with no helper or no network
  fence REFUSES the build; the shim socket is adopted by the run's uid and answers only the uid the
  kernel reports for the peer (SO_PEERCRED / LOCAL_PEERCRED); the agent's mcp.json lives in the run
  directory, never in a world-listable /tmp.

* On Linux, in a container (`BOSS_SANDBOX_A51_DOCKER=1`): the REAL helper (compiled from
  scripts/sandbox/boss-sandbox-exec.c with the Dockerfile's flags, file capabilities set), the REAL
  entrypoint's iptables fence, and the REAL sandbox code path run a probe AS THE SANDBOX USER, which
  must find unreachable: the API's /proc environ and memory, the identity key file and directory,
  another run's directory, mcp.json and shim socket, the sandbox root listing, the database port,
  the internet, the application tree — while its own shim socket, the API port and its own HOME
  work (A51). That needs Docker on a Linux kernel (Docker Desktop is one) and NET_ADMIN; it pulls
  python:3.11-slim to compile the helper.
"""
from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from boss_engine.identity import runs
from boss_engine.services import owned_process, sandbox_identity

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def fake_sandbox(tmp_path, monkeypatch):
    """A stand-in helper that logs its argv and runs the command (as this uid: the uid switch is
    what the Linux container test proves), and roots short enough for AF_UNIX paths."""
    import tempfile

    short = Path(tempfile.mkdtemp(prefix="sbx", dir="/tmp"))
    run_root, shim_root, log = short / "runs", short / "shim", short / "helper.log"
    run_root.mkdir()
    shim_root.mkdir()
    helper = short / "boss-sandbox-exec"
    helper.write_text("#!/bin/sh\n"
                      f"echo \"$@\" >> {log}\n"
                      "if [ \"$1\" = run ]; then shift 3; exec \"$@\"; fi\n"
                      "exit 0\n")
    helper.chmod(0o755)
    monkeypatch.setenv(sandbox_identity.HELPER_ENV, str(helper))
    monkeypatch.setenv(sandbox_identity.RUN_ROOT_ENV, str(run_root))
    monkeypatch.setenv(sandbox_identity.MODE_ENV, "required")
    monkeypatch.setenv(sandbox_identity.NETWORK_ENV, "off")          # no iptables here; fenced in the Linux test
    monkeypatch.setenv(sandbox_identity.EGRESS_CONNECT_ENV, "http://egress:8091")
    monkeypatch.setattr(sandbox_identity, "SHIM_ROOT", str(shim_root))
    yield {"log": log, "run_root": run_root, "shim_root": shim_root}
    shutil.rmtree(short, ignore_errors=True)


def _lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_every_process_of_a_run_starts_as_the_runs_own_uid(fake_sandbox):
    with owned_process.owner_scope("code_run", "crun_sb1"), sandbox_identity.bind("crun_sb1") as ident:
        assert sandbox_identity.UID_BASE <= ident.uid < sandbox_identity.UID_BASE + sandbox_identity.UID_COUNT
        done = owned_process.run([sys.executable, "-c", "import os; print(os.environ['HOME'], os.environ['TMPDIR'], "
                                  "os.environ['HTTPS_PROXY'], os.environ.get('DATABASE_URL'))"],
                                 capture_output=True, text=True, timeout=30)
        home, tmp, proxy, db = done.stdout.split()
        assert home == str(ident.home) and tmp == str(ident.tmp) and proxy == "http://egress:8091" and db == "None"
        # BOSS's own git on the checkout is never the agent's uid
        owned_process.run(["true"], sandboxed=False, timeout=10)
        run_dir = ident.run_dir
    log = _lines(fake_sandbox["log"])
    assert log[:2] == [f"kill {ident.uid}", f"adopt {ident.uid} {run_dir}"]   # a clean uid; its own directory
    runs_ = [line for line in log if line.startswith("run ")]
    assert len([r for r in runs_ if sys.executable in r]) == 1 and runs_[0].startswith(f"run {ident.uid} -- ")
    assert not any(r.endswith(" true") for r in runs_)
    assert f"kill {ident.uid}" in log                                     # nothing of it outlives the run
    assert not run_dir.exists()


def test_work_outside_a_run_is_not_touched(fake_sandbox):
    owned_process.run(["true"], timeout=10)
    with owned_process.owner_scope("deploy", "dep_1"):
        owned_process.run(["true"], timeout=10)
    assert _lines(fake_sandbox["log"]) == []


def test_two_runs_never_share_a_uid(fake_sandbox):
    with sandbox_identity.bind("crun_a") as a, sandbox_identity.bind("crun_b") as b:
        assert a.uid != b.uid and a.run_dir != b.run_dir


@pytest.mark.parametrize("missing", ["helper", "fence"])
def test_required_isolation_refuses_the_build_rather_than_run_it_unisolated(fake_sandbox, monkeypatch, missing):
    if missing == "helper":
        monkeypatch.setenv(sandbox_identity.HELPER_ENV, "/nonexistent/boss-sandbox-exec")
    else:
        monkeypatch.setenv(sandbox_identity.NETWORK_ENV, "enforce")       # no root-written fence marker
    with pytest.raises(sandbox_identity.SandboxUnavailable):
        with sandbox_identity.bind("crun_refused"):
            pass


def test_auto_without_a_helper_runs_unisolated_and_says_so(fake_sandbox, monkeypatch, caplog):
    monkeypatch.setenv(sandbox_identity.MODE_ENV, "auto")
    monkeypatch.setenv(sandbox_identity.HELPER_ENV, "/nonexistent/boss-sandbox-exec")
    with sandbox_identity.bind("crun_dev") as ident:
        assert ident is None
    assert any("NOT ISOLATED" in r.getMessage() for r in caplog.records)


def test_the_shim_socket_belongs_to_the_run_and_answers_only_its_uid(fake_sandbox):
    shim = runs.Shim(run_id="run_ct:crun_sock")
    shim.egress_credential = "boss_egr_" + "x" * 40
    with owned_process.owner_scope("code_run", "crun_sock"), sandbox_identity.bind("crun_sock") as ident:
        path = runs.serve_shim_socket(shim)
        try:
            assert Path(path).parent == fake_sandbox["shim_root"]
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
            assert f"sock {ident.uid} {path}" in _lines(fake_sandbox["log"])
            # this test runs as the API's uid, not the run's: the kernel says so, and nothing is said
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(3)
            s.connect(path)
            s.sendall(b"get\n")
            assert s.recv(4096) == b""
            s.close()
        finally:
            runs.close_shim_socket(shim.run_id)


def test_without_isolation_the_shim_still_checks_the_peer(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setenv(sandbox_identity.MODE_ENV, "off")
    monkeypatch.setenv("BOSS_SHIM_DIR", tempfile.mkdtemp(prefix="bshim", dir="/tmp"))
    shim = runs.Shim(run_id="run_ct:crun_dev_sock")
    shim.egress_credential = "boss_egr_" + "y" * 40
    path = runs.serve_shim_socket(shim)
    try:
        assert runs.peer_uid is not None
        s = socket.socket(socket.AF_UNIX)
        s.connect(path)
        s.sendall(b"get\n")
        assert b"password=boss_egr_" in s.recv(4096)          # the same uid: answered
        s.close()
        monkeypatch.setattr(runs, "peer_uid", lambda conn: os.getuid() + 1)
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(3)
        s.connect(path)
        try:
            s.sendall(b"get\n")
            answer = s.recv(4096)
        except (BrokenPipeError, ConnectionResetError):
            answer = b""                                        # hung up before we asked
        assert answer == b""                                    # anyone else: silence
        s.close()
    finally:
        runs.close_shim_socket(shim.run_id)


def test_the_agents_mcp_json_lives_in_the_run_directory(fake_sandbox, monkeypatch):
    from boss_engine.services import code_task_mcp

    monkeypatch.setattr(code_task_mcp, "plan", lambda c, s, p, r: {"servers": {"docs": {"type": "http", "url": "u"}},
                                                                  "disallowed": []})
    monkeypatch.setattr("boss_engine.services.resource_ledger.claim_dir", lambda p: None)
    with owned_process.owner_scope("code_run", "crun_mcp"), sandbox_identity.bind("crun_mcp") as ident:
        harness = code_task_mcp.prepare(None, None, "p", "forge")
        assert Path(harness.path).resolve().is_relative_to(ident.run_dir.resolve())
        assert not str(harness.path).startswith(("/tmp/boss-mcp-", "/private/tmp/boss-mcp-"))


def test_the_helper_source_holds_its_limits():
    """The helper is the one privileged piece: it takes no uid outside the pool, refuses a sandbox
    uid as its caller, drops groups, sets no_new_privs and touches paths only directly in its roots."""
    src = (ROOT / "scripts/sandbox/boss-sandbox-exec.c").read_text()
    for needle in ("v < UID_BASE || v >= (long)UID_BASE + UID_COUNT", "a sandbox uid may not use this",
                   "setgroups(0, NULL)", "PR_SET_NO_NEW_PRIVS", "PR_SET_PDEATHSIG", "AT_SYMLINK_NOFOLLOW",
                   "O_NOFOLLOW", "plain_component"):
        assert needle in src, needle
    dockerfile = (ROOT / "Dockerfile.community").read_text()
    assert "chmod 0750 /usr/local/libexec/boss/boss-sandbox-exec" in dockerfile
    assert "setcap cap_setuid,cap_setgid,cap_chown,cap_fowner+ep" in dockerfile


def test_compose_ships_the_boundary_and_no_default_database_password():
    import yaml

    compose = yaml.safe_load((ROOT / "scripts/install/community-compose.yml").read_text())
    api = compose["services"]["api"]
    assert "egress" in compose["services"]
    assert "NET_ADMIN" in api["cap_add"] and api["user"] == "0:0"
    env = api["environment"]
    assert env["BOSS_SANDBOX_ISOLATION"].endswith("required}") and env["BOSS_EGRESS_URL"]
    # every service that runs BOSS's engine fails closed (round 3: the worker had no setting, so
    # it defaulted to `auto` — unisolated with a warning — had it ever been handed a build)
    for name in ("api", "worker"):
        assert compose["services"][name]["environment"]["BOSS_SANDBOX_ISOLATION"].endswith("required}"), name
    raw = (ROOT / "scripts/install/community-compose.yml").read_text()
    assert "BOSS_DB_PASSWORD:-boss" not in raw and ":-boss}" not in raw
    assert "ports" not in compose["services"]["egress"]                   # only the compose network


# ---- A51 on Linux --------------------------------------------------------------------------------------

def _docker_ok() -> bool:
    if os.environ.get("BOSS_SANDBOX_A51_DOCKER") != "1" or not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


@pytest.mark.skipif(not _docker_ok(), reason="BOSS_SANDBOX_A51_DOCKER=1 and a Docker daemon (Linux kernel) needed")
def test_a51_the_sandbox_user_reaches_no_secret(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    build = subprocess.run(["docker", "run", "--rm", "-v", f"{ROOT}:/src:ro", "-v", f"{out}:/out",
                            "python:3.11-slim", "sh", "/src/engine/tests/sandbox_a51/build_helper.sh"],
                           capture_output=True, text=True, timeout=600)
    assert build.returncode == 0, build.stderr[-2000:]
    image = os.environ.get("BOSS_SANDBOX_A51_IMAGE", "boss-community-api:local")
    run = subprocess.run(["docker", "run", "--rm", "--user", "root", "--cap-add", "NET_ADMIN", "-m", "768m",
                          "--entrypoint", "sh", "-v", f"{ROOT}:/src:ro", "-v", f"{out}:/out:ro", image,
                          "/src/engine/tests/sandbox_a51/run_in_container.sh"],
                         capture_output=True, text=True, timeout=600)
    assert run.returncode == 0 and "A51 HOLDS" in run.stdout, (run.stdout[-3000:], run.stderr[-3000:])


# ---- the CONNECT proxy: the sandbox's model gateway and package route --------------------------------

def test_the_connect_proxy_allows_only_allowlisted_public_https(monkeypatch):
    sys.path.insert(0, str(ROOT))
    from apps.egress.boss_egress import connect

    monkeypatch.delenv("BOSS_EGRESS_CONNECT_ALLOW", raising=False)
    public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.0.1", 443))
    private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))
    monkeypatch.setattr(connect.socket, "getaddrinfo", lambda h, p, type=0: [public] if h == "api.anthropic.com"
                        else [private] if h == "registry.npmjs.org" else [public, private])
    assert connect.decide(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n")[1] == ""
    assert connect.decide(b"CONNECT api.anthropic.com:80 HTTP/1.1\r\n\r\n")[1] == "port_not_allowed"
    assert connect.decide(b"CONNECT github.com:443 HTTP/1.1\r\n\r\n")[1] == "host_not_allowed"
    assert connect.decide(b"CONNECT db:443 HTTP/1.1\r\n\r\n")[1] == "host_not_allowed"
    assert connect.decide(b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n\r\n")[1] == "not_a_public_address"
    assert connect.decide(b"GET http://api.anthropic.com/ HTTP/1.1\r\n\r\n")[1] == "not_a_connect"
    monkeypatch.setenv("BOSS_EGRESS_CONNECT_ALLOW", "*.github.com,pypi.org")
    assert connect.decide(b"CONNECT api.github.com:443 HTTP/1.1\r\n\r\n")[1] == "host_not_allowed"   # never GitHub
