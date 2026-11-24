"""Image builds behind a company network: the extra CA, the mirrors, the proxy — and no TLS-off.

The TLS fixture and the Node cases came from an enterprise install (a TLS-inspecting gateway made
`npm install -g` fail during `docker build`). The fix there covered Node only; the same build also
downloads with apt, curl, pip and Python's urllib (the JDK and Go). So the wrapper now gives every
downloader the CA, ADDED to the system trust, and every networked RUN in Dockerfile.community goes
through it. A registry URL is a build argument, which `docker history` keeps, so the wrapper also
refuses one that carries a credential. There is no switch that turns verification off.
"""
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import threading

import pytest


ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts/install/build-network.sh"
DOCKERFILE = ROOT / "Dockerfile.community"
SECRET = "s3cr3t-never-printed"


@pytest.fixture
def tls_endpoint(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("OpenSSL is required for the TLS integration test")
    certificate = tmp_path / "test-ca.pem"
    private_key = tmp_path / "test-key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(private_key), "-out", str(certificate), "-days", "1",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
    ], check=True, capture_output=True, timeout=30)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"verified")

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield certificate, server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _env(tmp_path, ca: Path, **extra) -> dict:
    environment = dict(os.environ)
    for name in ("NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED", "NODE_OPTIONS", "SSL_CERT_FILE",
                 "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "PIP_CERT", "GIT_SSL_CAINFO", "HTTPS_PROXY",
                 "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy", "BOSS_NPM_REGISTRY",
                 "BOSS_PIP_INDEX_URL", "BOSS_UPSTREAM_PROXY", "BOSS_UPSTREAM_NO_PROXY",
                 "BOSS_PLAYWRIGHT_DOWNLOAD_HOST", "npm_config_registry", "BOSS_UPSTREAM_HTTP_PROXY"):
        environment.pop(name, None)
    environment["BOSS_BUILD_CA_SECRET"] = str(ca)
    environment["BOSS_BUILD_NETWORK_DIR"] = str(tmp_path / "net")
    environment.update(extra)
    return environment


@pytest.mark.parametrize("trust,hostname,expected", [
    ("missing", "localhost", 1),
    ("empty", "localhost", 1),
    ("approved", "localhost", 0),
    ("approved", "127.0.0.1", 1),
])
def test_build_ca_keeps_certificate_and_hostname_verification(
    tls_endpoint, tmp_path, trust, hostname, expected,
):
    if not shutil.which("node"):
        pytest.skip("Node is required for the Node TLS case")
    certificate, port = tls_endpoint
    chosen = certificate if trust == "approved" else tmp_path / trust
    if trust == "empty":
        chosen.touch()
    script = """
require('https').get(process.argv[1], {family: 4}, response => {
    response.resume();
    response.on('end', () => process.exit(response.statusCode === 200 ? 0 : 2));
}).on('error', error => { console.error(error.code); process.exit(1); });
"""
    result = subprocess.run([
        "sh", str(WRAPPER), "node", "-e", script, f"https://{hostname}:{port}",
    ], env=_env(tmp_path, chosen), text=True, capture_output=True, timeout=15)
    assert result.returncode == expected, result.stdout + result.stderr
    if hostname == "127.0.0.1":
        assert "ERR_TLS_CERT_ALTNAME_INVALID" in result.stderr
    elif expected:
        assert "SELF_SIGNED_CERT" in result.stderr


@pytest.mark.parametrize("hostname,expected", [("localhost", 0), ("127.0.0.1", 1)])
def test_python_urllib_and_pip_trust_the_ca_with_host_names_still_checked(tls_endpoint, tmp_path, hostname, expected):
    """The JDK/Go downloads (urllib) and pip (PIP_CERT/REQUESTS_CA_BUNDLE) see the same bundle."""
    certificate, port = tls_endpoint
    script = ("import os, sys, urllib.request\n"
              "assert os.environ['PIP_CERT'] == os.environ['SSL_CERT_FILE'] == os.environ['REQUESTS_CA_BUNDLE']\n"
              "urllib.request.urlopen(sys.argv[1], timeout=10).read()\n")
    result = subprocess.run(["sh", str(WRAPPER), "python3", "-c", script, f"https://{hostname}:{port}/"],
                            env=_env(tmp_path, certificate), text=True, capture_output=True, timeout=20)
    assert result.returncode == expected, result.stderr
    no_ca = subprocess.run(["sh", str(WRAPPER), "python3", "-c",
                            "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=10)",
                            f"https://localhost:{port}/"], env=_env(tmp_path, tmp_path / "none"),
                           text=True, capture_output=True, timeout=20)
    assert no_ca.returncode != 0 and "CERTIFICATE_VERIFY_FAILED" in no_ca.stderr


def test_curl_trusts_the_ca_and_the_public_roots_are_kept(tls_endpoint, tmp_path):
    if not shutil.which("curl"):
        pytest.skip("curl is required")
    certificate, port = tls_endpoint
    result = subprocess.run(["sh", str(WRAPPER), "sh", "-c",
                             'curl -sS -o /dev/null "$1" && cat "$CURL_CA_BUNDLE"', "-", f"https://localhost:{port}/"],
                            env=_env(tmp_path, certificate), text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    bundle = result.stdout
    assert certificate.read_text().strip() in bundle
    # ADDED, not replacing: the system's public roots are in the same bundle.
    assert bundle.count("BEGIN CERTIFICATE") > 1


@pytest.mark.parametrize("name", ["BOSS_NPM_REGISTRY", "BOSS_PIP_INDEX_URL", "BOSS_PLAYWRIGHT_DOWNLOAD_HOST",
                                  "BOSS_UPSTREAM_PROXY", "BOSS_UPSTREAM_HTTP_PROXY"])
@pytest.mark.parametrize("value", [f"https://user:{SECRET}@mirror.example.com/x/",
                                   f"https://mirror.example.com/x/?token={SECRET}",
                                   f"ftp://mirror.example.com/{SECRET}/"])
def test_a_credential_in_a_build_url_stops_the_build_before_history_keeps_it(tmp_path, name, value):
    result = subprocess.run(["sh", str(WRAPPER), "true"], env=_env(tmp_path, tmp_path / "none", **{name: value}),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 64
    assert name in result.stderr and SECRET not in result.stderr + result.stdout


def test_a_private_key_is_never_used_as_the_ca(tmp_path):
    key = tmp_path / "bundle.pem"
    key.write_text("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
                   "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n")
    result = subprocess.run(["sh", str(WRAPPER), "true"], env=_env(tmp_path, key), text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 64 and "private key" in result.stderr


def test_sourced_it_sets_the_mirrors_and_proxy_without_changing_the_shell(tmp_path):
    script = (f'. "{WRAPPER}" && case $- in *e*) echo errexit-leaked;; esac; '
              'echo "$npm_config_registry|$npm_config_strict_ssl|$PIP_INDEX_URL|$PLAYWRIGHT_DOWNLOAD_HOST|'
              '$HTTPS_PROXY|${http_proxy-unset}|$NO_PROXY"; echo "${UNSET_VAR_OK:-unset-is-fine}"')
    env = _env(tmp_path, tmp_path / "none", BOSS_NPM_REGISTRY="https://npm.example.com/r/",
               BOSS_PIP_INDEX_URL="https://pypi.example.com/simple",
               BOSS_PLAYWRIGHT_DOWNLOAD_HOST="https://pw.example.com/",
               BOSS_UPSTREAM_PROXY="http://proxy.example.com:8080", BOSS_UPSTREAM_NO_PROXY=".example.com")
    result = subprocess.run(["sh", "-c", script], env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "https://npm.example.com/r/|true|https://pypi.example.com/simple|https://pw.example.com|"
        "http://proxy.example.com:8080|unset|.example.com", "unset-is-fine"]
    explicit = subprocess.run(["sh", "-c", f'. "{WRAPPER}" && echo "$HTTP_PROXY|$http_proxy"'],
                              env=_env(tmp_path, tmp_path / "none", BOSS_UPSTREAM_HTTP_PROXY="http://p.example.com:3128"),
                              text=True, capture_output=True, timeout=10)
    assert explicit.stdout.strip() == "http://p.example.com:3128|http://p.example.com:3128"
    empty = subprocess.run(["sh", "-c", f'. "{WRAPPER}" && echo "[${{HTTPS_PROXY-unset}}][${{npm_config_registry-unset}}]"'],
                           env=_env(tmp_path, tmp_path / "none"), text=True, capture_output=True, timeout=10)
    assert empty.stdout.strip() == "[unset][unset]"           # an empty setting never reaches a tool


# ---- the Dockerfile, as a class -------------------------------------------------------------------

def _runs() -> list[tuple[str, str]]:
    """(stage, RUN instruction with continuations joined) for every RUN."""
    stage, out = "", []
    for block in re.split(r"\n(?=[A-Z]+ )", DOCKERFILE.read_text()):
        if block.startswith("FROM "):
            stage = block.split(" AS ")[-1].strip() if " AS " in block else block
        if block.startswith("RUN "):
            code = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))
            out.append((stage, code.replace("\\\n", " ")))
    return out


NETWORK = re.compile(r"apt-get update|apt-get install|npm (?:install|ci)|pnpm install|pip install|"
                     r"\bcurl\b|urlretrieve|playwright install")
MOUNTS = ("--mount=type=secret,id=boss_build_ca",
          "--mount=type=bind,source=scripts/install/build-network.sh,target=/usr/local/libexec/boss-build-network",
          "--mount=type=tmpfs,target=/run/boss-build-network")


def test_every_networked_build_step_goes_through_the_network_wrapper():
    networked = [(stage, run) for stage, run in _runs() if NETWORK.search(run)]
    assert len(networked) >= 10
    for stage, run in networked:
        for mount in MOUNTS:
            assert mount in run, f"{stage}: {run[:120]} lacks {mount}"
        assert ". /usr/local/libexec/boss-build-network && " in run, f"{stage}: {run[:120]}"


def test_the_wrapper_and_the_ca_never_become_part_of_an_image():
    text = DOCKERFILE.read_text()
    assert not re.search(r"^COPY .*build-network", text, re.M)
    assert not re.search(r"^(ARG|ENV) .*(CA_FILE|CA_PEM|boss_build_ca)", text, re.M)


def test_there_is_no_switch_that_turns_tls_verification_off():
    checked = [DOCKERFILE, WRAPPER, ROOT / "scripts/install/with-extra-ca.sh",
               ROOT / "scripts/install/community-compose.yml", ROOT / "install.sh",
               ROOT / "scripts/install/restore_preserved_env.sh"]
    banned = re.compile(r"NODE_TLS_REJECT_UNAUTHORIZED|strict[-_]ssl[= ]*false|INSECURE_TLS|"
                        r"--insecure\b|trusted-host|PIP_TRUSTED_HOST=|GIT_SSL_NO_VERIFY|sslVerify|"
                        r"Acquire::https::Verify|CERT_NONE", re.I)
    for path in checked:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            assert not banned.search(line), f"{path.name}:{number}: {line.strip()}"


def test_registry_arguments_are_declared_late_so_a_change_rebuilds_only_from_there():
    text = DOCKERFILE.read_text()
    stage = lambda name: text.split(f" AS {name}\n", 1)[1].split("\nFROM ", 1)[0]  # noqa: E731
    # Each mirror is declared in the stage that downloads from it, right before that step
    # (BOSSAPP-1458 split the runtime into python-base -> python-browser -> python-runtime).
    base, browser, runtime = stage("python-base"), stage("python-browser"), stage("python-runtime")
    assert "BOSS_NPM_REGISTRY" not in base and "BOSS_NPM_REGISTRY" not in browser
    assert runtime.index("ARG BOSS_NPM_REGISTRY=") > runtime.index("apt-get install")
    assert runtime.index("ARG BOSS_NPM_REGISTRY=") < runtime.index("npm ci --prefix /opt/boss-clis")
    # pip's mirror reaches only the dependency stages (copied into each image LAST) and the
    # browser stage's throwaway download venv: a change to it never rebuilds the user, the CLIs or
    # the toolchains.
    assert "BOSS_PIP_INDEX_URL" not in base and "BOSS_PIP_INDEX_URL" not in runtime
    for name in ("pydeps", "pydeps-agent"):
        deps = stage(name)
        assert deps.index("ARG BOSS_PIP_INDEX_URL=") < deps.index("pip install"), name
    assert browser.index("ARG BOSS_PLAYWRIGHT_DOWNLOAD_HOST=") < browser.index("playwright install")


def test_the_coding_clis_come_from_a_lockfile_with_integrity_for_every_package():
    text = DOCKERFILE.read_text()
    assert "npm install -g" not in text and "npm install --global" not in text
    for lock_dir, prefix in (("scripts/install/cli-lock", "/opt/boss-clis"),
                             ("scripts/install/cli-lock/pnpm", "/opt/boss-pnpm")):
        manifest = json.loads((ROOT / lock_dir / "package.json").read_text())
        lock = json.loads((ROOT / lock_dir / "package-lock.json").read_text())
        assert f"npm ci --prefix {prefix}" in text
        for name, version in manifest["dependencies"].items():
            assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"{name} must be an exact version"
            assert lock["packages"][f"node_modules/{name}"]["version"] == version, name
        for path, entry in lock["packages"].items():
            if path:
                assert entry.get("integrity", "").startswith("sha512-"), path
                assert entry.get("resolved", "").startswith("https://registry.npmjs.org/"), path
