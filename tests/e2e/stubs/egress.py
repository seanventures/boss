"""Every byte BOSS sends to the internet, intercepted: an HTTP(S) proxy that answers from fakes.

WHY A PROXY AND NOT A BASE URL. GitHub's API address is a literal in a dozen places
(`https://api.github.com` in github_adapter, setup_service, delivery_sources, builtin tools…),
clone URLs are built as `https://x-access-token:…@github.com/…`, and YouTube and Drive are
called at Google's own hosts. Making each configurable to suit a test would change the product
to fit the harness; an intercepting proxy leaves the product's own URLs exactly as shipped and
still sends every call to a fake.

It is also the no-spend guarantee. The services run with HTTPS_PROXY/HTTP_PROXY pointing here
and SSL_CERT_FILE trusting ONLY this run's CA, so a host with no fake gets a 502 that names it —
and lands in the egress log — instead of quietly reaching a real provider. A scripted run that
"passed" while a step called out to the real world would be the worst kind of green.

Hosts with a fake:
  *.e2e.test                          the deployment host and the apps it serves
  api.github.com                      GitHub REST, backed by the same bare repos (github_fake.py)
  github.com                          git smart-HTTP over `git http-backend`, bare repos on disk
  www.googleapis.com, youtube.*,      YouTube upload + channels, Drive downloads
  oauth2.googleapis.com               token refresh
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import os
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


class CertificateAuthority:
    """A throwaway CA for one run, minting a leaf per intercepted host."""

    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._contexts: dict[str, ssl.SSLContext] = {}
        self.key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "BOSS e2e harness CA")])
        now = _dt.datetime.now(_dt.timezone.utc)
        self.cert = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=7))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            # Python 3.13's VERIFY_X509_STRICT rejects a chain without key identifiers.
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()),
                           critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True,
                                         crl_sign=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False,
                                         decipher_only=False), critical=True)
            .sign(self.key, hashes.SHA256())
        )
        self.ca_path = directory / "ca.pem"
        self.ca_path.write_bytes(self.cert.public_bytes(serialization.Encoding.PEM))

    def context_for(self, host: str) -> ssl.SSLContext:
        with self._lock:
            if host in self._contexts:
                return self._contexts[host]
            key = ec.generate_private_key(ec.SECP256R1())
            now = _dt.datetime.now(_dt.timezone.utc)
            try:
                san: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
            except ValueError:
                san = x509.DNSName(host)
            leaf = (
                x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
                .issuer_name(self.cert.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - _dt.timedelta(days=1))
                .not_valid_after(now + _dt.timedelta(days=6))
                .add_extension(x509.SubjectAlternativeName([san]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    self.key.public_key()), critical=False)
                .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                               critical=False)
                .sign(self.key, hashes.SHA256())
            )
            pem = self.directory / f"leaf-{host.replace(':', '_')}.pem"
            pem.write_bytes(
                leaf.public_bytes(serialization.Encoding.PEM)
                + key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
            os.chmod(pem, 0o600)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(pem))
            context.set_alpn_protocols(["http/1.1"])
            self._contexts[host] = context
            return context


class GitRemotes:
    """Bare repositories that `github.com/<owner>/<repo>.git` resolves to, served by git's own
    `http-backend` so clone, fetch and push speak the real smart-HTTP protocol."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def ensure(self, owner: str, repo: str, seed: Optional[dict[str, str]] = None) -> Path:
        repo = repo[:-4] if repo.endswith(".git") else repo
        bare = self.root / owner / f"{repo}.git"
        with self._lock:
            if bare.exists():
                return bare
            bare.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                           capture_output=True)
            subprocess.run(["git", "-C", str(bare), "config", "http.receivepack", "true"],
                           check=True, capture_output=True)
            with tempfile.TemporaryDirectory() as work:
                files = seed or DEFAULT_APP
                for rel, content in files.items():
                    path = Path(work) / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)
                env = {**os.environ, "GIT_AUTHOR_NAME": "e2e", "GIT_AUTHOR_EMAIL": "e2e@x.test",
                       "GIT_COMMITTER_NAME": "e2e", "GIT_COMMITTER_EMAIL": "e2e@x.test"}
                for argv in (["init", "-b", "main"], ["add", "-A"],
                             ["commit", "-m", "e2e seed"],
                             ["push", str(bare), "main:main"]):
                    subprocess.run(["git", "-C", work, *argv], check=True, capture_output=True,
                                   env=env)
            return bare

    def serve(self, method: str, path: str, query: str, headers: dict[str, str],
              body: bytes) -> tuple[int, dict[str, str], bytes]:
        parts = path.strip("/").split("/")
        if len(parts) >= 2:
            self.ensure(parts[0], parts[1])
            if not parts[1].endswith(".git"):
                parts[1] += ".git"
                path = "/" + "/".join(parts)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_PROJECT_ROOT": str(self.root), "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
            "CONTENT_TYPE": headers.get("content-type", ""),
            "CONTENT_LENGTH": str(len(body)), "REMOTE_USER": "boss-e2e",
            "REMOTE_ADDR": "127.0.0.1", "GATEWAY_INTERFACE": "CGI/1.1",
            "SERVER_PROTOCOL": "HTTP/1.1",
        }
        if headers.get("content-encoding"):
            env["HTTP_CONTENT_ENCODING"] = headers["content-encoding"]
        if headers.get("git-protocol"):
            env["GIT_PROTOCOL"] = headers["git-protocol"]
        done = subprocess.run(["git", "http-backend"], input=body, env=env,
                              capture_output=True, timeout=120)
        head, _, payload = done.stdout.partition(b"\r\n\r\n")
        if not _:
            head, _, payload = done.stdout.partition(b"\n\n")
        status, out_headers = 200, {}
        for line in head.decode(errors="replace").splitlines():
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.strip().split()[0])
            elif name:
                out_headers[name.strip()] = value.strip()
        return status, out_headers, payload


# The repository every build step works in: a static page (so preview and deploy have exactly
# one servable app, "static:.") plus a Python module the scripted QA and engineer extend under
# stdlib unittest — the default verification target for a repo with no package.json.
DEFAULT_APP = {
    "README.md": "# e2e app\n\nA static app the e2e harness builds, previews and deploys.\n",
    "index.html": "<!doctype html><html><head><title>e2e app</title></head>"
                  "<body><h1>e2e app</h1></body></html>\n",
    "e2e_base.py": "def version():\n    return \"1\"\n",
    "test_e2e_base.py": "import unittest\n\nimport e2e_base\n\n\nclass BaseTest(unittest.TestCase):\n"
                        "    def test_version(self):\n        self.assertEqual(e2e_base.version(), \"1\")\n"
                        "\n\nif __name__ == \"__main__\":\n    unittest.main()\n",
}


def host_route(host: str) -> Optional[str]:
    """Which fake answers this host, or None for a blocked host."""
    host = host.split(":", 1)[0].lower()
    if host == "api.github.com":
        return "github-api"
    if host in ("github.com", "www.github.com"):
        return "git"
    if host.endswith(".e2e.test"):
        # The deployment host the harness registers (deploy.e2e.test) and anything served from
        # it: a monitor or a health probe of a deployed app reaches a page that is up.
        return "app"
    if host in ("www.googleapis.com", "youtube.googleapis.com", "oauth2.googleapis.com",
                "accounts.google.com", "drive.google.com", "content.googleapis.com"):
        return "google"
    return None


__all__ = ["CertificateAuthority", "GitRemotes", "host_route", "DEFAULT_APP"]
