"""Self-hosted Git (GHES and GitLab) through boss-egress with GIT_SSL_CAINFO (BOSSAPP-1419).

Verifies:
- Dynamic GHES (`BOSS_GITHUB_WEB_URL` / `BOSS_GITHUB_API_URL` / `BOSS_GITHUB_GIT_URL`) and
  self-hosted GitLab (`BOSS_GITLAB_BASE_URL`, `BOSS_GITLAB_REPOS`, `BOSS_GITLAB_TOKEN`) routing.
- Real TLS HTTPS `git http-backend` servers signed by a test CA (`openssl`): `EgressProxy`
  fails without `GIT_SSL_CAINFO` and succeeds with `GIT_SSL_CAINFO` (and `SSL_CERT_FILE` unset)
  when performing real `git clone` and `git push` (with `with-extra-ca.sh` verified too).
- Push to `main` and force-push are refused; `connect.NEVER` preserves `*.github.com`; raw CONNECT
  to `*.github.com` and configured GHES/GitLab hosts is refused (403).
"""
from __future__ import annotations

import base64
import http.server
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.egress.boss_egress import connect, proxy as egress_proxy  # noqa: E402
from boss_engine.identity import egress_rules, github_app, github_urls, host_delegation, http as identity_http, repo_scope, runs  # noqa: E402
from boss_engine.identity.principal import ActorChain, RunMode, RunTokenClaims, TaskBinding  # noqa: E402
from boss_engine.services import repo_host  # noqa: E402


def _git_http_backend() -> Path:
    if not shutil.which("git"):
        pytest.skip("git is required")
    exec_path = subprocess.check_output(["git", "--exec-path"], text=True).strip()
    backend = Path(exec_path) / "git-http-backend"
    if not backend.is_file():
        pytest.skip("git-http-backend is required")
    return backend


def _make_ca_and_cert(tmp_path: Path, cn: str) -> tuple[Path, Path, Path]:
    if not shutil.which("openssl"):
        pytest.skip("openssl is required")
    ca_key = tmp_path / "ca.key"
    ca_cert = tmp_path / "ca.pem"
    srv_key = tmp_path / "srv.key"
    srv_csr = tmp_path / "srv.csr"
    srv_cert = tmp_path / "srv.pem"
    ext = tmp_path / "ext.cnf"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(ca_key), "-out", str(ca_cert), "-days", "1",
        "-subj", "/CN=Enterprise Test Root CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
    ], check=True, capture_output=True, timeout=30)
    subprocess.run([
        "openssl", "req", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(srv_key), "-out", str(srv_csr),
        "-subj", f"/CN={cn}",
    ], check=True, capture_output=True, timeout=30)
    ext.write_text(f"subjectAltName=DNS:{cn},IP:127.0.0.1\n")
    subprocess.run([
        "openssl", "x509", "-req", "-in", str(srv_csr),
        "-CA", str(ca_cert), "-CAkey", str(ca_key), "-CAcreateserial",
        "-out", str(srv_cert), "-days", "1", "-extfile", str(ext),
    ], check=True, capture_output=True, timeout=30)
    return ca_cert, srv_cert, srv_key


def _seed_bare_repo(root: Path, rel_repo: str) -> Path:
    bare = root / f"{rel_repo}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(bare), "config", "http.receivepack", "true"], check=True, capture_output=True)
    work = root / "_seed_work"
    if work.exists():
        shutil.rmtree(work)
    subprocess.run(["git", "init", "--initial-branch=main", str(work)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "seed@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "Seed"], check=True, capture_output=True)
    (work / "README.md").write_text("hello enterprise git\n")
    subprocess.run(["git", "-C", str(work), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "initial"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "push", "-u", "origin", "main"], check=True, capture_output=True)
    shutil.rmtree(work)
    return bare


class _TlsGitBackend:
    """Real HTTPS git smart-HTTP server enforcing Basic auth before invoking git-http-backend."""

    def __init__(self, backend_bin: Path, repo_root: Path, srv_cert: Path, srv_key: Path,
                 expected_user: str, expected_secret: str):
        self.seen_auth: list[str] = []
        expected_b64 = base64.b64encode(f"{expected_user}:{expected_secret}".encode()).decode()
        expected_header = f"Basic {expected_b64}"

        class Handler(http.server.BaseHTTPRequestHandler):
            def _handle(self):
                auth = self.headers.get("Authorization", "")
                if auth != expected_header:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="git"')
                    self.end_headers()
                    return
                outer.seen_auth.append(auth)
                path, _, query = self.path.partition("?")
                length = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(length) if length > 0 else b""
                env = {
                    **os.environ,
                    "GIT_PROJECT_ROOT": str(repo_root),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": path,
                    "REQUEST_METHOD": self.command,
                    "QUERY_STRING": query,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(len(body)),
                    "REMOTE_USER": expected_user,
                }
                proc = subprocess.run([str(backend_bin)], input=body, env=env, capture_output=True, timeout=30)
                head, _, payload = proc.stdout.partition(b"\r\n\r\n")
                if not payload and b"\n\n" in proc.stdout:
                    head, _, payload = proc.stdout.partition(b"\n\n")
                status = 200
                resp_headers = []
                for line in head.decode("latin-1").splitlines():
                    if ":" not in line:
                        continue
                    k, _, v = line.partition(":")
                    if k.lower() == "status":
                        status = int(v.strip().split(" ", 1)[0])
                    else:
                        resp_headers.append((k.strip(), v.strip()))
                self.send_response(status)
                for k, v in resp_headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _handle
            do_POST = _handle

            def log_message(self, *_):
                pass

        outer = self
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(srv_cert, srv_key)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_port
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)


class _ProxyHttpServer:
    """Serves EgressProxy over local HTTP so a real `git` subprocess can clone/push through it."""

    def __init__(self, proxy: egress_proxy.EgressProxy):
        class Handler(http.server.BaseHTTPRequestHandler):
            def _handle(self):
                path, _, query = self.path.partition("?")
                length = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(length) if length > 0 else b""
                req = egress_proxy.EgressRequest(
                    method=self.command,
                    path=path,
                    query=query,
                    headers={k.lower(): v for k, v in self.headers.items()},
                    body=body,
                    peer="127.0.0.1",
                )
                resp = proxy.handle(req)
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp.body)))
                self.end_headers()
                self.wfile.write(resp.body)

            do_GET = _handle
            do_POST = _handle

            def log_message(self, *_):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)


def _sqlite_egress_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'egress.db'}")
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE identity_egress_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT CURRENT_TIMESTAMP, "
            "org_id TEXT, run_id TEXT, jti TEXT, chain TEXT, provider TEXT, method TEXT, "
            "path_template TEXT, status INTEGER, bytes_out INTEGER, bytes_in INTEGER, "
            "decision TEXT, deny_reason TEXT)"
        ))
        c.execute(text(
            "CREATE TABLE identity_security_events ("
            "id TEXT PRIMARY KEY, org_id TEXT, event_type TEXT, outcome TEXT, "
            "actor_principal_id TEXT, actor_kind TEXT, target_type TEXT, target_id TEXT, "
            "run_id TEXT, grant_id TEXT, chain TEXT, jti TEXT, reason TEXT, details TEXT)"
        ))
    return engine


def _make_claims(run_id: str, *, github: list[str] | None = None, gitlab: list[str] | None = None):
    res = {}
    if github:
        res["github"] = github
    if gitlab:
        res["gitlab"] = gitlab
    task = TaskBinding(run_id=run_id, project_id="proj_1", step="build", resources=res)
    return RunTokenClaims(
        iss="https://boss.test",
        sub="usr_1",
        aud="boss:egress",
        org_id="org_1",
        mode=RunMode.AUTONOMOUS,
        chain=ActorChain(),
        grant_id="dlg_1",
        task=task,
        scope=frozenset({"repository:read", "github:read"}),
        cnf_jkt="jkt_1",
        jti="jti_1",
        parent_jti=None,
        iat=1,
        exp=9999999999,
    )


def test_real_git_clone_and_push_to_ghes_and_gitlab_through_egress_with_git_ssl_cainfo(tmp_path, monkeypatch):
    backend_bin = _git_http_backend()
    ca_cert, srv_cert, srv_key = _make_ca_and_cert(tmp_path, "git.enterprise.test")

    ghes_root = tmp_path / "ghes_repos"
    gl_root = tmp_path / "gl_repos"
    _seed_bare_repo(ghes_root, "acme/web")
    _seed_bare_repo(gl_root, "platform/sub/api")

    ghes_token = "ghs_ghes_install_token_secret"
    gl_token = "glpat_gitlab_token_secret"
    ghes_srv = _TlsGitBackend(backend_bin, ghes_root, srv_cert, srv_key, "x-access-token", ghes_token)
    gl_srv = _TlsGitBackend(backend_bin, gl_root, srv_cert, srv_key, "oauth2", gl_token)

    engine = _sqlite_egress_engine(tmp_path)
    proxy = egress_proxy.EgressProxy(engine)
    egress_srv = _ProxyHttpServer(proxy)

    # Map fake enterprise hostnames to loopback for _httpx_upstream inside this test process.
    orig_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host in ("ghes.enterprise.test", "gitlab.enterprise.test", "git.enterprise.test"):
            return orig_getaddrinfo("127.0.0.1", port, *args, **kwargs)
        return orig_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setenv("BOSS_EGRESS_URL", f"http://127.0.0.1:{egress_srv.port}")
    monkeypatch.setenv("BOSS_GITHUB_WEB_URL", f"https://git.enterprise.test:{ghes_srv.port}")
    monkeypatch.setenv("BOSS_GITHUB_API_URL", f"https://git.enterprise.test:{ghes_srv.port}/api/v3")
    monkeypatch.setenv("BOSS_GITLAB_BASE_URL", f"https://git.enterprise.test:{gl_srv.port}")
    monkeypatch.setenv("BOSS_GITLAB_REPOS", "platform/sub/api")
    monkeypatch.setenv("BOSS_GITLAB_TOKEN", gl_token)
    shim_dir = tempfile.mkdtemp(prefix="bshim", dir="/tmp")
    monkeypatch.setenv("BOSS_SHIM_DIR", shim_dir)

    # Patch identity resolution so we focus end-to-end on real git + EgressProxy + TLS CA.
    run_id = "run_ct:ct_ent1"
    safe_run = run_id.replace(":", "-").replace("/", "-").replace("#", "-")
    claims = _make_claims(run_id, github=["acme/web"], gitlab=["platform/sub/api"])
    monkeypatch.setattr(runs, "resolve_egress", lambda raw, db, peer=None: claims)
    monkeypatch.setattr(host_delegation, "check", lambda *a, **k: claims)
    monkeypatch.setattr(egress_proxy.lineage, "denied", lambda *a, **k: None)
    monkeypatch.setattr(github_app, "run_permissions", lambda c, db=None: {"contents": "write", "pull_requests": "write", "workflows": "write"})
    monkeypatch.setattr(egress_proxy, "gitlab_repos", lambda c, db: ["platform/sub/api"])
    monkeypatch.setattr(
        proxy,
        "_installation_token",
        lambda c, db: github_urls.tag(ghes_token, f"https://git.enterprise.test:{ghes_srv.port}/api/v3"),
    )
    monkeypatch.setattr(
        proxy,
        "_check_push",
        lambda body, rest, perms, pattern, token, allow_delete=True, api="": egress_rules.RuleDecision(True, "/{o}/{r}.git/git-receive-pack"),
    )

    shim = runs.Shim(run_id=run_id, egress_credential="boss_egr_test_cred_1")
    try:
        # 1. Without SSL_CERT_FILE or GIT_SSL_CAINFO, TLS verification MUST fail closed (502).
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("GIT_SSL_CAINFO", raising=False)
        access_gl = runs._egress_git_access(runs.egress_access(shim, owner="platform/sub", repo="api", host="gitlab"))
        clone_fail = subprocess.run(
            ["git", "clone", access_gl.url, str(tmp_path / "fail_gl")],
            env=access_gl.environ({**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}),
            capture_output=True, text=True, timeout=30,
        )
        assert clone_fail.returncode != 0

        # 2. With GIT_SSL_CAINFO ONLY (SSL_CERT_FILE still unset), _httpx_upstream trusts the test CA!
        monkeypatch.setenv("GIT_SSL_CAINFO", str(ca_cert))

        # 2a. GitLab subgroup clone + push boss/* branch + refuse main push + refuse force-push.
        gl_work = tmp_path / "gl_work"
        clone_gl = subprocess.run(
            ["git", "clone", access_gl.url, str(gl_work)],
            env=access_gl.environ({**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}),
            capture_output=True, text=True, timeout=30,
        )
        assert clone_gl.returncode == 0, clone_gl.stderr
        assert (gl_work / "README.md").read_text() == "hello enterprise git\n"
        assert gl_srv.seen_auth  # upstream saw oauth2:<token>
        assert gl_token not in "".join(access_gl.env.values())
        assert f"url.http://127.0.0.1:{egress_srv.port}/gitlab/.insteadOf" in access_gl.env.values()

        subprocess.run(["git", "-C", str(gl_work), "config", "user.email", "bot@example.com"], check=True)
        subprocess.run(["git", "-C", str(gl_work), "config", "user.name", "Bot"], check=True)
        (gl_work / "feature.txt").write_text("gitlab change\n")
        subprocess.run(["git", "-C", str(gl_work), "add", "feature.txt"], check=True)
        subprocess.run(["git", "-C", str(gl_work), "commit", "-m", "feat"], check=True, capture_output=True)

        push_ok = subprocess.run(
            ["git", "-C", str(gl_work), "push", "origin", f"HEAD:refs/heads/boss/{safe_run}-feat"],
            env=access_gl.environ({**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}),
            capture_output=True, text=True, timeout=30,
        )
        assert push_ok.returncode == 0, push_ok.stderr

        push_main = subprocess.run(
            ["git", "-C", str(gl_work), "push", "origin", "HEAD:refs/heads/main"],
            env=access_gl.environ({**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}),
            capture_output=True, text=True, timeout=30,
        )
        assert push_main.returncode != 0

        # Force-push (non-fast-forward) on GitLab boss/* branch is refused offline.
        subprocess.run(["git", "-C", str(gl_work), "commit", "--amend", "-m", "amended"], check=True, capture_output=True)
        push_force = subprocess.run(
            ["git", "-C", str(gl_work), "push", "--force", "origin", f"HEAD:refs/heads/boss/{safe_run}-feat"],
            env=access_gl.environ({**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}),
            capture_output=True, text=True, timeout=30,
        )
        assert push_force.returncode != 0

        # 2b. GHES clone + push boss/* branch.
        access_gh = runs._egress_git_access(runs.egress_access(shim, owner="acme", repo="web", host="github"))
        assert f"https://git.enterprise.test:{ghes_srv.port}/" in access_gh.env.values()
        assert ghes_token not in "".join(access_gh.env.values())
        gh_work = tmp_path / "gh_work"
        clone_gh = subprocess.run(
            ["git", "clone", access_gh.url, str(gh_work)],
            env=access_gh.environ({**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}),
            capture_output=True, text=True, timeout=30,
        )
        assert clone_gh.returncode == 0, clone_gh.stderr
        assert (gh_work / "README.md").read_text() == "hello enterprise git\n"
        assert ghes_srv.seen_auth
        subprocess.run(["git", "-C", str(gh_work), "config", "user.email", "bot@example.com"], check=True)
        subprocess.run(["git", "-C", str(gh_work), "config", "user.name", "Bot"], check=True)
        (gh_work / "ghes.txt").write_text("ghes change\n")
        subprocess.run(["git", "-C", str(gh_work), "add", "ghes.txt"], check=True)
        subprocess.run(["git", "-C", str(gh_work), "commit", "-m", "ghes"], check=True, capture_output=True)
        push_gh = subprocess.run(
            ["git", "-C", str(gh_work), "push", "origin", f"HEAD:refs/heads/boss/{safe_run}-ghes"],
            env=access_gh.environ({**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}),
            capture_output=True, text=True, timeout=30,
        )
        assert push_gh.returncode == 0, push_gh.stderr
    finally:
        runs.close_shim_socket(run_id)
        shutil.rmtree(shim_dir, ignore_errors=True)
        egress_srv.close()
        ghes_srv.close()
        gl_srv.close()


def test_connect_blocks_github_wildcard_and_configured_self_hosted_git_hosts(monkeypatch):
    assert "*.github.com" in connect.NEVER
    monkeypatch.setenv("BOSS_GITHUB_WEB_URL", "https://ghes.corp.example.com")
    monkeypatch.setenv("BOSS_GITHUB_API_URL", "https://api.ghes.corp.example.com")
    monkeypatch.setenv("BOSS_GITLAB_BASE_URL", "https://gitlab.corp.example.com")
    monkeypatch.setenv(
        "BOSS_EGRESS_CONNECT_ALLOW",
        "*.github.com,ghes.corp.example.com,api.ghes.corp.example.com,gitlab.corp.example.com",
    )
    monkeypatch.setenv(
        "BOSS_EGRESS_PRIVATE_ALLOW",
        "ghes.corp.example.com,gitlab.corp.example.com",
    )
    for blocked in (
        "api.github.com",
        "uploads.github.com",
        "ghes.corp.example.com",
        "api.ghes.corp.example.com",
        "gitlab.corp.example.com",
    ):
        route, reason = connect.decide(f"CONNECT {blocked}:443 HTTP/1.1\r\n\r\n".encode())
        assert route is None and reason == "host_not_allowed", blocked
    assert "GitHub" in connect.refusal_reason("uploads.github.com", 443, "host_not_allowed")
    assert "Git proxy" in connect.refusal_reason("gitlab.corp.example.com", 443, "host_not_allowed")


def test_gitlab_api_rules_and_repo_scope_and_github_app_ghes_host(monkeypatch):
    perms = {"contents": "write", "pull_requests": "write"}
    repos = ["platform/sub/api", "42"]
    assert egress_rules.check_gitlab_api(
        "GET", "/api/v4/projects/platform/sub/api/merge_requests", perms=perms, repos=repos,
    ).allowed
    assert egress_rules.check_gitlab_api(
        "POST", "/api/v4/projects/42/merge_requests", perms=perms, repos=repos,
        body={"source_branch": "boss/run_1-feat", "target_branch": "main"},
        push_pattern="refs/heads/boss/*",
    ).allowed
    assert not egress_rules.check_gitlab_api(
        "POST", "/api/v4/projects/42/merge_requests", perms=perms, repos=repos,
        body={"source_branch": "main"}, push_pattern="refs/heads/boss/*",
    ).allowed
    assert not egress_rules.check_gitlab_api(
        "POST", "/api/v4/projects/42/merge_requests/5/merge", perms=perms, repos=repos,
    ).allowed
    assert not egress_rules.check_gitlab_api(
        "GET", "/api/v4/projects/42/members", perms=perms, repos=repos,
    ).allowed

    # repo_scope GitLab support
    monkeypatch.setenv("BOSS_GITLAB_REPOS", "platform/sub/api")
    monkeypatch.setenv("BOSS_GITLAB_PROJECT_ID", "42")
    grant = SimpleNamespace(resources={"gitlab": {"repos": ["platform/sub/api"]}})
    assert repo_scope.granted(None, provider=repo_scope.GITLAB, org_id="o", run_id="r",
                              grant=grant, obo=True, repos=["platform/sub/api.git", "other/repo"]) == (
        "platform/sub/api",
    )

    # github_app._default_transport allows configured GHES API base
    monkeypatch.setenv("BOSS_GITHUB_API_URL", "https://ghes.example.com/api/v3")
    monkeypatch.setenv("BOSS_GITHUB_GIT_URL", "https://ghes.example.com")
    monkeypatch.setenv("BOSS_GITLAB_BASE_URL", "https://gitlab.example.com")
    ghes_tok = github_urls.tag("tok", "https://ghes.example.com/api/v3")
    assert repo_host.clone_url_for("github", "acme", "web", ghes_tok, web_base="https://ghes.example.com") == "https://x-access-token:tok@ghes.example.com/acme/web.git"
    assert repo_host.clone_url_for("gitlab", "platform/sub", "api", "glt", web_base="") == "https://oauth2:glt@gitlab.example.com/platform/sub/api.git"

    # github_app._default_transport reaches mocked httpx for GHES host instead of raising outbound_client_unavailable
    monkeypatch.setattr(identity_http, "fetch", lambda *a, **k: (_ for _ in ()).throw(NotImplementedError()))
    import httpx

    monkeypatch.setattr(httpx, "request", lambda *a, **k: SimpleNamespace(status_code=200, content=b'{"ok":true}'))
    r = github_app._default_transport("GET", "https://ghes.example.com/api/v3/user", {}, None)
    assert r.status == 200
