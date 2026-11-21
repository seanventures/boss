"""The installer side of the enterprise network: saved settings, the TLS check, the Docker runtime.

* An upgrade that exports a new registry REPLACES the saved one (it used to keep the old one
  silently, KEEP_PREVIOUS), and one that exports nothing keeps it — for every setting of the block.
* Before the image build, the installer finds a TLS-inspecting gateway it has no CA for and says
  which CA to export (on a Mac, the keychain command), instead of a build that fails 20 minutes in.
* A Docker credential helper named in ~/.docker/config.json but not installed (Docker Desktop
  removed for Colima) is caught before the first pull.
* A shell's corporate HTTPS_PROXY is adopted; one with a login in it is never saved.
* A Mac whose Docker runtime is stopped gets "start Docker Desktop or Colima" from the installer
  and from EVERY helper that has the Linux sudo fallback, not the unrelated sudo message.
"""
import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))
from boss_engine.services import enterprise_network as en  # noqa: E402

PRESERVE = ROOT / "scripts/install/restore_preserved_env.sh"
PREFLIGHT = ROOT / "scripts/install/network_preflight.sh"


@pytest.mark.parametrize("name", list(en.SETTINGS) + ["BOSS_DOCKER_CONTEXT"])
def test_an_upgrade_keeps_each_setting_and_a_newly_exported_value_replaces_it(tmp_path, name):
    old, new = tmp_path / "old.env", tmp_path / "new.env"
    old.write_text(f"{name}=saved-value\n")
    new.write_text(f"{name}=\n")
    subprocess.run(["sh", str(PRESERVE), str(old), str(new)], check=True, env=dict(os.environ), timeout=30)
    assert f"{name}=saved-value" in new.read_text().splitlines()
    new.write_text(f"{name}=exported-value\n")
    subprocess.run(["sh", str(PRESERVE), str(old), str(new)], check=True, env=dict(os.environ), timeout=30)
    assert new.read_text().splitlines().count(f"{name}=exported-value") == 1
    assert "saved-value" not in new.read_text()


# ---- the TLS check -----------------------------------------------------------------------------

@pytest.fixture
def intercepting_server(tmp_path):
    if not shutil.which("openssl") or not shutil.which("curl"):
        pytest.skip("openssl and curl are required")
    run = lambda *a: subprocess.run(a, check=True, capture_output=True, timeout=60, cwd=tmp_path)  # noqa: E731
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "1",
        "-subj", "/O=Example Corp/CN=Example Inspection Root",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "s.key", "-out", "s.csr", "-subj", "/CN=localhost")
    (tmp_path / "e.cnf").write_text("subjectAltName=DNS:localhost\n")
    run("openssl", "x509", "-req", "-in", "s.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
        "-out", "s.pem", "-days", "1", "-extfile", "e.cnf")

    class Quiet(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "s.pem", tmp_path / "s.key")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield tmp_path / "ca.pem", server.server_port
    server.shutdown()
    server.server_close()


def _diagnose(tmp_path, ca, *targets, os_name="Darwin"):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "uname").write_text(f"#!/bin/sh\necho {os_name}\n")
    (bindir / "uname").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    env["PATH"] = f"{bindir}:{env['PATH']}"
    # install.sh's own shell options: a refused connection must be a finding, not an abort.
    script = f'set -euo pipefail; . "{PREFLIGHT}"; boss_tls_diagnose "$1" "" "${{@:2}}"'
    return subprocess.run(["bash", "-c", script, "-", ca, *targets], env=env, capture_output=True, text=True,
                          timeout=60)


def test_an_intercepted_registry_is_found_and_the_ca_to_export_is_named(intercepting_server, tmp_path):
    ca, port = intercepting_server
    result = _diagnose(tmp_path, "", f"localhost:{port}")
    assert result.returncode == 1
    assert "intercepted" in result.stdout and "Example Inspection Root" in result.stdout
    assert 'security find-certificate -a -c "Example Inspection Root" -p /Library/Keychains/System.keychain' in result.stdout
    assert "BOSS_EXTRA_CA_FILE" in result.stdout
    linux = _diagnose(tmp_path, "", f"localhost:{port}", os_name="Linux")
    assert linux.returncode == 1 and "security find-certificate" not in linux.stdout


def test_with_the_right_ca_the_check_passes_and_an_unreachable_host_only_notes(intercepting_server, tmp_path):
    ca, port = intercepting_server
    result = _diagnose(tmp_path, str(ca), f"localhost:{port}", "localhost:1")
    assert result.returncode == 0, result.stdout
    assert "trusted through BOSS_EXTRA_CA_FILE" in result.stdout
    assert "BOSS_UPSTREAM_PROXY" in result.stdout          # the unreachable one says what to check


def test_a_reset_handshake_is_a_note_not_an_interception(tmp_path):
    """A firewall that resets the connection (curl exit 35) proves nothing about a gateway: the
    install goes on with a note instead of stopping (review N3)."""
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    def reset():
        import struct
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.recv(1024)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            conn.close()
    threading.Thread(target=reset, daemon=True).start()
    try:
        result = _diagnose(tmp_path, "", f"localhost:{port}")
    finally:
        srv.close()
    assert result.returncode == 0, result.stdout
    assert "intercepted" not in result.stdout and "not reachable" in result.stdout


def test_the_installer_runs_the_checks_before_the_build_and_never_offers_tls_off():
    install = (ROOT / "install.sh").read_text()
    build = install.index('ui_step "Building and starting Community stack')
    assert install.index("boss_docker_credential_helper_check ||") < build
    assert install.index("boss_tls_diagnose ") < build
    assert "BOSS_BUILD_INSECURE_TLS" not in install


# ---- the Docker credential helper ----------------------------------------------------------------

@pytest.mark.parametrize("store,expect", [("desktop", '"credsStore": "osxkeychain"'),
                                          ("osxkeychain", "brew install docker-credential-helper"),
                                          ("pass", "docker-credential-pass")])
def test_a_missing_credential_helper_is_caught_before_the_first_pull(tmp_path, store, expect):
    config = tmp_path / "docker"
    config.mkdir()
    (config / "config.json").write_text(json.dumps({"auths": {}, "credsStore": store}))
    env = {"PATH": "/usr/bin:/bin", "DOCKER_CONFIG": str(config), "HOME": str(tmp_path)}
    result = subprocess.run(["bash", "-c", f'set -euo pipefail; . "{PREFLIGHT}"; boss_docker_credential_helper_check'],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and expect in result.stdout
    helper = tmp_path / "bin" / f"docker-credential-{store}"
    helper.parent.mkdir()
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    env["PATH"] = f"{helper.parent}:/usr/bin:/bin"
    assert subprocess.run(["bash", "-c", f'. "{PREFLIGHT}"; boss_docker_credential_helper_check'],
                          env=env, capture_output=True, timeout=10).returncode == 0


# ---- the shell's proxy ---------------------------------------------------------------------------

def _proxy_block() -> str:
    install = (ROOT / "install.sh").read_text()
    return "#" + install.split("# THE OUTBOUND PROXY", 1)[1].split("# The Docker context is remembered", 1)[0]


def _run_proxy_block(env: dict) -> tuple[str, str]:
    script = ("ui_warn() { echo \"WARN $*\"; }; ui_info() { :; }; die() { echo \"DIE $*\"; exit 1; }\n"
              + _proxy_block() + '\necho "[${BOSS_UPSTREAM_PROXY:-}][${BOSS_UPSTREAM_NO_PROXY:-}]"')
    result = subprocess.run(["bash", "-c", script], env={"PATH": "/usr/bin:/bin", **env},
                            capture_output=True, text=True, timeout=10)
    return result.stdout.strip().splitlines()[-1], result.stdout


def test_the_shell_proxy_is_never_adopted_implicitly():
    """A stock upgrade from a shell with a dev or VPN proxy exported must not route every BOSS
    container through it, and must not replace a saved company proxy (review B1)."""
    last, _ = _run_proxy_block({"HTTPS_PROXY": "http://proxy.example.com:8080", "NO_PROXY": ".example.com"})
    assert last == "[][]"


@pytest.mark.parametrize("shell_proxy,saved", [("http://proxy.example.com:8080", "http://proxy.example.com:8080"),
                                               ("http://me:pw@proxy.example.com:8080", "")])
def test_the_shell_proxy_is_adopted_only_on_request_and_never_with_a_login(shell_proxy, saved):
    last, out = _run_proxy_block({"BOSS_USE_SHELL_PROXY": "1", "HTTPS_PROXY": shell_proxy,
                                  "NO_PROXY": ".example.com"})
    assert last == (f"[{saved}][.example.com]" if saved else "[][]")
    if not saved:
        assert "WARN" in out and "me:pw" not in out


def test_an_explicit_setting_wins_over_the_shell():
    last, _ = _run_proxy_block({"BOSS_USE_SHELL_PROXY": "1", "HTTPS_PROXY": "http://other.example.com:1",
                                "BOSS_UPSTREAM_PROXY": "http://proxy.example.com:8080"})
    assert last.startswith("[http://proxy.example.com:8080]")


def test_the_installer_offers_the_flag():
    install = (ROOT / "install.sh").read_text()
    assert "--use-shell-proxy)" in install and "--use-shell-proxy" in install.split("usage() {", 1)[1][:6000]


def test_every_lifecycle_path_finalizes_before_it_builds():
    """install.sh and remote.sh update both write a new .env and build: both go through the one
    finalize (validation, the narrowed-fence migration, the CA file), before the build, and both
    force-recreate the CA-mounting services when finalize replaced a directory (re-review R1, R2)."""
    install = (ROOT / "install.sh").read_text()
    assert install.index('enterprise_network.py" finalize "$_new_env" --previous') < install.index("ui_step \"Building and starting")
    # The migration is finalize's, not a second copy in the installer's shell.
    assert "recreate-ca-mount" in install
    assert not re.search(r"^\s*BOSS_EGRESS_CONNECT_DEFAULTS=off", install.split("cat > \"$_new_env\"")[0], re.M)
    remote = (ROOT / "scripts/install/remote.sh").read_text()
    update = remote.split("cmd_update() {", 1)[1].split("\n}\n", 1)[0]
    assert "enterprise_network.py finalize" in update and "--previous .env" in update
    assert update.index("enterprise_network.py finalize") < update.index("$compose_build build")
    assert "recreate-ca-mount" in update


def test_remote_update_finalize_step_on_a_fake_host(tmp_path):
    """Run cmd_update's finalize step as the host would: the next .env gets the migration and the
    CA file exists before the build."""
    remote = (ROOT / "scripts/install/remote.sh").read_text()
    step = remote.split("# THE ENTERPRISE NETWORK, as install.sh does it", 1)[1].split("# api and worker, plus every other", 1)[0]
    host = tmp_path / "host"
    (host / "engine/boss_engine/services").mkdir(parents=True)
    shutil.copy2(ROOT / "engine/boss_engine/services/enterprise_network.py", host / "engine/boss_engine/services/")
    (host / ".env").write_text("BOSS_EGRESS_CONNECT_ALLOW=api.anthropic.com\n")
    (host / ".env.next").write_text("BOSS_EGRESS_CONNECT_ALLOW=api.anthropic.com\n")
    script = ('set -euo pipefail\nDIR="$1"; next_env=.env.next\n'
              'rsh() { sh -c "$*"; }; rshq() { sh -c "$*" 2>/dev/null; }; die() { echo "DIE $*"; exit 1; }\n'
              'warn() { echo "WARN $*"; }; say() { :; }\n'
              'step() {\n#' + step + '\necho "recreate=$CA_RECREATE"\n}\nstep')
    result = subprocess.run(["bash", "-c", script, "-", str(host)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BOSS_EGRESS_CONNECT_DEFAULTS=off" in (host / ".env.next").read_text()
    assert (host / ".boss/extra-ca.pem").is_file()
    assert "recreate=0" in result.stdout


# ---- a stopped Docker runtime on a Mac -----------------------------------------------------------

def _sudo_harness():
    path = ROOT / "engine/tests/test_sudo_docker_selection.py"
    spec = importlib.util.spec_from_file_location("sudo_selection", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_helper_tells_a_mac_to_start_its_docker_runtime_not_about_sudo():
    harness = _sudo_harness()
    case = harness.SudoSelection()
    for name in harness.SCRIPTS:
        result, events, _ = case.run_case(name, case={"os": "Darwin", "direct": False})
        err = result.stderr.decode()
        assert result.returncode != 0, name
        assert "Start Docker Desktop, or your Colima profile (colima start <profile>)" in err, (name, err)
        assert "Cannot safely use sudo" not in err, name
        assert not any(e["privileged"] for e in events), name


def test_linux_still_reaches_the_sudo_fallback():
    harness = _sudo_harness()
    case = harness.SudoSelection()
    for name in harness.SCRIPTS:
        result, events, _ = case.run_case(name)
        assert result.returncode == 0, (name, result.stderr.decode())
        assert "Colima" not in result.stderr.decode()


def test_boss_uses_the_docker_context_the_install_was_made_with(tmp_path):
    boss = (ROOT / "scripts/install/boss.sh").read_text()
    block = boss.split("env_value() {", 1)[1].split("\nboss_sudo_scope_safe() {", 1)[0]
    block = "env_value() {" + block.split("# ---- scoped sudo Docker fallback", 1)[0]
    env_file = tmp_path / ".env"
    env_file.write_text("BOSS_DOCKER_CONTEXT=colima-boss\n")
    run = lambda extra: subprocess.run(  # noqa: E731
        ["bash", "-c", f'ENV_FILE="{env_file}"\n{block}\necho "[${{DOCKER_CONTEXT:-}}]"'],
        env={"PATH": "/usr/bin:/bin", **extra}, capture_output=True, text=True, timeout=10).stdout.strip()
    assert run({}) == "[colima-boss]"
    assert run({"DOCKER_CONTEXT": "desktop-linux"}) == "[desktop-linux]"      # the shell's choice wins
    assert run({"DOCKER_HOST": "unix:///tmp/x.sock"}) == "[]"
