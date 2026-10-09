"""A github.com template is fetched through boss-egress's GITHUB path, never its CONNECT proxy.

The defect: every import from GitHub failed. The importer sent https://github.com/... through the
CONNECT proxy (`BOSS_EGRESS_CONNECT_PROXY`), whose NEVER list holds github.com by design: 403,
and git's raw error text on the screen.

Here a local smart-HTTP git server plays boss-egress's /github/<owner>/<repo>.git route. It
answers only a request carrying the egress credential (as egress does: 401 + Basic challenge
otherwise), so the fetch passes only if the credential reached BOSS's own git through its own
environment AND the importer's `credential.helper=` reset did not erase it.
"""
from __future__ import annotations

import base64
import contextlib
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from boss_engine.services.repo_host import GitAccess, git_config_env
from boss_engine.templates import importer
from boss_engine.templates.importer import ImportRefused, fetch, github_repo

from ._fixtures import git_repo

CRED = "boss_egr_IMPORTTESTCREDENTIAL0000000"
OWN_HELPER = ("!f() { test \"$1\" = get && printf 'username=boss-egress\\npassword=%s\\n' "
              "\"$BOSS_EGR_CREDENTIAL\"; }; f")


class _Egress(BaseHTTPRequestHandler):
    root: Path
    seen: list

    def log_message(self, *a):  # noqa: D401
        pass

    def _serve(self):
        auth = self.headers.get("Authorization", "")
        ok = auth == "Basic " + base64.b64encode(f"boss-egress:{CRED}".encode()).decode()
        self.seen.append((self.command, self.path, bool(auth), ok))
        if not ok:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="boss-egress"')
            self.end_headers()
            return
        path, _, query = self.path.partition("?")
        assert path.startswith("/github/")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.command == "POST" else b""
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_PROJECT_ROOT": str(self.root),
               "GIT_HTTP_EXPORT_ALL": "1", "REQUEST_METHOD": self.command,
               "PATH_INFO": path[len("/github"):], "QUERY_STRING": query,
               "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
               "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""), "REMOTE_ADDR": "127.0.0.1"}
        done = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True, timeout=60)
        head, _, payload = done.stdout.partition(b"\r\n\r\n")
        status, headers = 200, []
        for line in head.decode().splitlines():
            k, _, v = line.partition(":")
            if k.lower() == "status":
                status = int(v.split()[0])
            elif k:
                headers.append((k, v.strip()))
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _serve


@pytest.fixture()
def egress(tmp_path):
    src, sha = git_repo(tmp_path / "src")
    root = tmp_path / "egress-root"
    (root / "acme").mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(root / "acme" / "tpl.git")], check=True)
    handler = type("H", (_Egress,), {"root": root, "seen": []})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", handler.seen, sha
    finally:
        srv.shutdown()


def _access_for(base: str, calls: list):
    """What identity.runs.repo_access yields through egress (runs._egress_git_access)."""
    socket_cfg = [(f"url.{base}/github/.insteadOf", "https://github.com/"), ("credential.helper", ""),
                  ("credential.helper", "!f() { echo the-shim-socket-only-the-sandbox-uid-may-open; }; f"),
                  ("credential.useHttpPath", "true")]
    own = [(k, v) for k, v in socket_cfg if k != "credential.helper"] + [("credential.helper", ""),
                                                                          ("credential.helper", OWN_HELPER)]

    @contextlib.contextmanager
    def access(owner: str, repo: str):
        calls.append((owner, repo))
        yield GitAccess(f"{base}/github/{owner}/{repo}.git", git_config_env(socket_cfg), via="egress",
                        own_env={**git_config_env(own), "BOSS_EGR_CREDENTIAL": CRED})
    return access


def test_a_github_template_is_fetched_through_egress_github_path_with_its_credential(egress, monkeypatch):
    base, seen, sha = egress
    monkeypatch.setenv("BOSS_EGRESS_CONNECT_PROXY", "http://egress:8091")   # must NOT be used for GitHub
    calls: list = []
    argvs: list = []
    real = subprocess.run
    monkeypatch.setattr(importer.subprocess, "run",
                        lambda argv, *a, **kw: (argvs.append((list(argv), dict(kw.get("env") or {}))),
                                                real(argv, *a, **kw))[1])
    got = fetch("https://github.com/acme/tpl", "main", github_access=_access_for(base, calls))
    assert calls == [("acme", "tpl")]
    assert got.commit_sha == sha and got.git_url == "https://github.com/acme/tpl"
    assert got.tree.files()["brief.md"] == b"Add GET /healthz.\n"
    assert any(ok for *_, ok in seen)                       # egress saw the run's credential
    mine = [(a, e) for a, e in argvs if "http-backend" not in a]     # not the fake egress's own git
    assert mine
    for argv, env in mine:
        joined = " ".join(argv)
        assert "http.proxy=" not in joined and "HTTPS_PROXY" not in env      # never the CONNECT proxy
        assert "core.hooksPath=/dev/null" in joined and "protocol.allow=never" in joined
        assert CRED not in joined                                           # never in argv


def test_a_github_url_without_egress_access_is_not_sent_to_the_connect_proxy_by_the_route():
    """The API route always passes its GitHub seam; github_repo() decides what is GitHub."""
    assert github_repo("https://github.com/acme/tpl") == ("acme", "tpl")
    assert github_repo("https://github.com/acme/tpl.git") == ("acme", "tpl")
    assert github_repo("https://gitlab.com/acme/tpl") is None
    assert github_repo("https://github.com/acme") is None
    src = (Path(__file__).resolve().parents[3] / "apps" / "api" / "app" / "routes_templates.py").read_text()
    assert "github_access=_github_access" in src


def test_a_failed_github_fetch_is_said_plainly(egress):
    base, _, _ = egress
    with pytest.raises(ImportRefused) as e:
        fetch("https://github.com/acme/missing", "main", github_access=_access_for(base, []))
    msg = e.value.reason
    assert msg.startswith("no repository at https://github.com/acme/missing") or "could not" in msg
    assert "fatal" not in msg and "127.0.0.1" not in msg and "egress" not in msg


def test_a_refused_github_authority_is_said_plainly():
    @contextlib.contextmanager
    def refused(owner, repo):
        raise RuntimeError("no grant and no system repo reader (is boss-egress configured?)")
        yield  # pragma: no cover

    with pytest.raises(ImportRefused) as e:
        fetch("https://github.com/acme/tpl", None, github_access=refused)
    assert "GitHub connection" in e.value.reason and "system repo reader" not in e.value.reason
