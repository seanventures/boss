import http.server
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import threading

import pytest


ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts/install/node-build-with-ca.sh"


@pytest.fixture
def tls_endpoint(tmp_path):
    if not shutil.which("node") or not shutil.which("openssl"):
        pytest.skip("Node and OpenSSL are required for the TLS integration test")
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


@pytest.mark.parametrize("trust,hostname,expected", [
    ("missing", "localhost", 1),
    ("empty", "localhost", 1),
    ("approved", "localhost", 0),
    ("approved", "127.0.0.1", 1),
])
def test_build_ca_keeps_certificate_and_hostname_verification(
    tls_endpoint, tmp_path, trust, hostname, expected,
):
    certificate, port = tls_endpoint
    environment = dict(os.environ)
    for name in ("NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED", "NODE_OPTIONS"):
        environment.pop(name, None)
    chosen = certificate if trust == "approved" else tmp_path / trust
    if trust == "empty":
        chosen.touch()
    environment["BOSS_BUILD_CA_FILE"] = str(chosen)
    script = """
require('https').get(process.argv[1], {family: 4}, response => {
    response.resume();
    response.on('end', () => process.exit(response.statusCode === 200 ? 0 : 2));
}).on('error', error => { console.error(error.code); process.exit(1); });
"""
    result = subprocess.run([
        "sh", str(WRAPPER), "node", "-e", script, f"https://{hostname}:{port}",
    ], env=environment, text=True, capture_output=True, timeout=15)
    assert result.returncode == expected, result.stdout + result.stderr
    if hostname == "127.0.0.1":
        assert "ERR_TLS_CERT_ALTNAME_INVALID" in result.stderr
    elif expected:
        assert "SELF_SIGNED_CERT" in result.stderr
