"""Every BOSS role trusts the company's CA and uses the outbound proxy at RUN time, not only at build.

Before, BOSS had no runtime CA path at all: under a TLS-inspecting gateway the images could be
built (with a build-time CA) and still every sandboxed npm/pip/git, boss-egress's GitHub calls,
the model SDKs and the coding CLIs failed. Now each role starts through with-extra-ca: the guard
(non-API roles) and boss-entrypoint (api, runner) exec it, it writes one combined bundle from
BOSS_EXTRA_CA_PEM_B64 and exports it for OpenSSL/Python/Go/Rust, requests, curl, pip, git and
Node, and sets HTTP(S)_PROXY with BOSS's own services always in NO_PROXY.
"""
import base64
import http.server
import importlib.util
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts/install/with-extra-ca.sh"


@pytest.fixture
def private_ca_server(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("OpenSSL is required")
    ca_key, ca = tmp_path / "ca.key", tmp_path / "ca.pem"
    key, csr, cert = tmp_path / "srv.key", tmp_path / "srv.csr", tmp_path / "srv.pem"
    ext = tmp_path / "ext.cnf"
    ext.write_text("subjectAltName=DNS:localhost\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                   "extendedKeyUsage=serverAuth\nauthorityKeyIdentifier=keyid\n")
    run = lambda *a: subprocess.run(a, check=True, capture_output=True, timeout=60)  # noqa: E731
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(ca_key), "-out", str(ca),
        "-days", "1", "-subj", "/CN=Example Inspection Root",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(csr),
        "-subj", "/CN=localhost")
    run("openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca), "-CAkey", str(ca_key), "-CAcreateserial",
        "-out", str(cert), "-days", "1", "-extfile", str(ext))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield ca, server.server_port
    finally:
        server.shutdown()
        server.server_close()


def _env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "PIP_CERT", "GIT_SSL_CAINFO",
                        "NODE_EXTRA_CA_CERTS", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                        "NO_PROXY", "no_proxy", "BOSS_UPSTREAM_PROXY", "BOSS_UPSTREAM_NO_PROXY")}
    env["TMPDIR"] = str(tmp_path)
    env["BOSS_JDK_HOME"] = str(tmp_path / "no-jdk")
    env.update(extra)
    return env


@pytest.mark.parametrize("hostname,expected", [("localhost", 0), ("127.0.0.1", 1)])
def test_a_role_trusts_the_extra_ca_and_still_checks_host_names(private_ca_server, tmp_path, hostname, expected):
    ca, port = private_ca_server
    env = _env(tmp_path, BOSS_EXTRA_CA_PEM_B64=base64.b64encode(ca.read_bytes()).decode())
    script = ("import os, sys, urllib.request\n"
              "assert 'BOSS_EXTRA_CA_PEM_B64' not in os.environ\n"
              "for k in ('SSL_CERT_FILE','REQUESTS_CA_BUNDLE','CURL_CA_BUNDLE','PIP_CERT','GIT_SSL_CAINFO'):\n"
              "    assert os.environ[k] == os.environ['SSL_CERT_FILE'], k\n"
              "assert os.environ['NODE_EXTRA_CA_CERTS'].endswith('extra-ca.pem')\n"
              "assert oct(os.stat(os.environ['SSL_CERT_FILE']).st_mode & 0o777) == '0o644'\n"
              "urllib.request.urlopen(sys.argv[1], timeout=10)\n")
    result = subprocess.run(["sh", str(WRAPPER), "python3", "-c", script, f"https://{hostname}:{port}/"],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == expected, result.stderr


def test_without_a_ca_nothing_changes(private_ca_server, tmp_path):
    ca, port = private_ca_server
    result = subprocess.run(["sh", str(WRAPPER), "python3", "-c",
                             "import os, sys, urllib.request\nassert 'SSL_CERT_FILE' not in os.environ\n"
                             "urllib.request.urlopen(sys.argv[1], timeout=10)", f"https://localhost:{port}/"],
                            env=_env(tmp_path), capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and "CERTIFICATE_VERIFY_FAILED" in result.stderr


def test_node_and_the_coding_clis_trust_it_too(private_ca_server, tmp_path):
    if not shutil.which("node"):
        pytest.skip("node is required")
    ca, port = private_ca_server
    env = _env(tmp_path, BOSS_EXTRA_CA_PEM_B64=base64.b64encode(ca.read_bytes()).decode())
    script = ("require('https').get(process.argv[1], r => { r.resume(); r.on('end', () => process.exit(0)); })"
              ".on('error', e => { console.error(e.code); process.exit(1); });")
    result = subprocess.run(["sh", str(WRAPPER), "node", "-e", script, f"https://localhost:{port}/"],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_a_bad_value_is_reported_and_the_role_still_starts(tmp_path):
    key_only = base64.b64encode(b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n").decode()
    for value in ("not base64 !!", key_only):
        result = subprocess.run(["sh", str(WRAPPER), "sh", "-c", 'echo "[${SSL_CERT_FILE-unset}]"'],
                                env=_env(tmp_path, BOSS_EXTRA_CA_PEM_B64=value), capture_output=True, text=True,
                                timeout=10)
        assert result.returncode == 0
        assert result.stdout.strip() == "[unset]"
        assert "NOT trusted" in result.stderr


def test_the_proxy_is_set_with_boss_services_always_direct_and_never_with_a_login(tmp_path):
    show = 'echo "$HTTPS_PROXY|$http_proxy|$NO_PROXY"'
    result = subprocess.run(["sh", str(WRAPPER), "sh", "-c", show],
                            env=_env(tmp_path, BOSS_UPSTREAM_PROXY="http://proxy.example.com:8080",
                                     BOSS_UPSTREAM_NO_PROXY=".corp.example.com"),
                            capture_output=True, text=True, timeout=10)
    https, http, no_proxy = result.stdout.strip().split("|")
    assert https == http == "http://proxy.example.com:8080"
    assert {"api", "egress", "db", "localhost", ".corp.example.com"} <= set(no_proxy.split(","))
    leaked = subprocess.run(["sh", str(WRAPPER), "sh", "-c", 'echo "[${HTTPS_PROXY-unset}]"'],
                            env=_env(tmp_path, BOSS_UPSTREAM_PROXY="http://me:pw@proxy.example.com:8080"),
                            capture_output=True, text=True, timeout=10)
    assert leaked.stdout.strip() == "[unset]" and "pw@" not in leaked.stderr


def test_every_role_starts_through_it():
    guard_src = (ROOT / "scripts/install/recovery_guard.py").read_text()
    assert 'NETWORK_WRAPPER = "/usr/local/libexec/boss/with-extra-ca"' in guard_src
    entry = (ROOT / "scripts/sandbox/entrypoint.sh").read_text()
    assert 'exec $NETWORK_WRAPPER "$@"' in entry
    assert entry.rstrip().endswith('-- $NETWORK_WRAPPER "$@"')
    dockerfile = (ROOT / "Dockerfile.community").read_text()
    assert "COPY scripts/install/with-extra-ca.sh /usr/local/libexec/boss/with-extra-ca" in dockerfile


def test_the_guard_execs_the_role_through_the_wrapper_after_its_check(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("guard", ROOT / "scripts/install/recovery_guard.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    wrapper = tmp_path / "with-extra-ca"
    wrapper.write_text("#!/bin/sh\nexec \"$@\"\n")
    wrapper.chmod(0o755)
    calls = []
    monkeypatch.setattr(guard, "NETWORK_WRAPPER", str(wrapper))
    monkeypatch.setattr(guard, "check_recovery", lambda: None)
    monkeypatch.setattr(guard.os, "execvpe", lambda f, a, e: calls.append(a))
    monkeypatch.setattr(guard.sys, "argv", ["guard", "python", "-m", "apps.worker.boss_worker.main"])
    guard.main()
    assert calls == [[str(wrapper), "python", "-m", "apps.worker.boss_worker.main"]]
    monkeypatch.setattr(guard, "NETWORK_WRAPPER", str(tmp_path / "absent"))
    guard.main()
    assert calls[-1] == ["python", "-m", "apps.worker.boss_worker.main"]
