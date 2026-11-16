"""P8b S11 (BOSSAPP-1072), the database half: a deployed agent's run on a fake host does git
through boss-egress's HOST ROUTE, end to end, with the real fleet router, the real run-plane
tokens and the real egress proxy.

    real git (guest config from in_guest_agent.configure_git_relay: no token, no handle)
      → a stand-in for boss-hostd's git forward (the Go gateway's job, proven in
        internal/gateway/p8b_git_test.go): the host's BossHost proof, the handle MOVED into
        X-Boss-Handle, the host's word on VM/run/epoch, the run's boss:egress token + DPoP proof
      → EgressProxy (the real code, the bench DB) → `git http-backend` standing in for GitHub

  * clone, fetch and push to `refs/heads/boss/<run>-*` work; the upstream sees the injected
    installation credential, never the handle;
  * a push to another ref, a force-push and a branch delete are refused; the remote is unchanged;
  * a repository outside the run is refused;
  * a host that advertised `host_tokens` must attach the boss:egress token (missing →
    run_token_required; another destination's token → run_token_bad_aud); a host that predates
    S13 without one is refused for want of a registered token, not let through;
  * the handle presented with another VM's word is a replay: revoked and a host_events row;
  * the VM handle presented straight to egress (no host) is worthless; no proof → refused;
  * an unrouted git host (GitLab) is refused by name, and by its strict-mode name.

Needs BOSS_TEST_DATABASE_URL naming a disposable database (the bench DB).
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import pytest
from sqlalchemy import create_engine, text

from boss_engine.identity import delegation, dpop, egress_handles, github_app

from .test_p8b_mcp_gateway_db import TEST_DB, World, _block as _mcp_block

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not TEST_DB or "test" not in TEST_DB.rsplit("/", 1)[-1].lower(),
    reason="needs BOSS_TEST_DATABASE_URL naming a disposable database")

EGRESS_BASE = "http://egress.test"
FAKE_INSTALLATION = "fake-installation-token-for-test"   # a stand-in, never a real token
FAKE_BITBUCKET = "fake-bitbucket-token-for-test"


def _p8b5_block() -> list[str]:
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    start = sql.index("\n", sql.index("P8b-5: git and REST through boss-hostd"))
    block = sql[start:sql.index("end P8b-5 ==", start)]
    code = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in code.split(";") if s.strip()]


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True, pool_size=10, max_overflow=10)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM agent_replicas LIMIT 1"))
            creates, do_block = _mcp_block()
            for statement in creates:
                c.execute(text(statement))
            c.exec_driver_sql(do_block)
            for statement in _p8b5_block():
                c.execute(text(statement))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable: {error}")
    return engine


class GitWorld(World):
    """World (one deployed agent, one fake host, one claimed run) + the run's git scope."""

    def close(self) -> None:
        run_id = self.work["run_id"] if self.work else None
        with self.eng.begin() as c:
            for sql in ("DELETE FROM identity_egress_credentials WHERE run_id = :r",
                        "DELETE FROM identity_egress_log WHERE run_id = :r"):
                try:
                    with c.begin_nested():
                        c.execute(text(sql), {"r": run_id})
                except Exception:  # noqa: BLE001 — best effort
                    pass
        super().close()


@pytest.fixture()
def world(eng, monkeypatch):
    w = GitWorld(eng, monkeypatch)
    yield w
    w.close()


class Upstream:
    """`git http-backend` standing in for GitHub; records what egress sent."""

    def __init__(self, hub: Path) -> None:
        self.hub, self.seen = hub, []

    def __call__(self, method: str, url: str, headers: Any, body: bytes):
        from apps.egress.boss_egress.proxy import EgressResponse

        self.seen.append({k.lower(): v for k, v in headers.items()})
        parts = urlsplit(url)
        env = {"GIT_PROJECT_ROOT": str(self.hub), "GIT_HTTP_EXPORT_ALL": "1",
               "PATH_INFO": parts.path, "QUERY_STRING": parts.query, "REQUEST_METHOD": method,
               "CONTENT_TYPE": headers.get("content-type", "") or headers.get("Content-Type", ""),
               "CONTENT_LENGTH": str(len(body)), "REMOTE_USER": "app", "PATH": os.environ["PATH"],
               "HTTP_CONTENT_ENCODING": headers.get("content-encoding", ""),
               "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(self.hub)}
        out = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True,
                             timeout=60).stdout
        head, _, payload = out.partition(b"\r\n\r\n")
        status, hdrs = 200, {}
        for line in head.split(b"\r\n"):
            if b":" in line:
                k, v = line.decode().split(":", 1)
                if k.lower() == "status":
                    status = int(v.split()[0])
                else:
                    hdrs[k.lower()] = v.strip()
        return EgressResponse(status, hdrs, payload)


class HostForward:
    """What boss-hostd's git route does for one VM (HOST_API.md §4.9), in front of the proxy:
    a Basic challenge, the credential helper's answer, then the forward with the HOST's proof,
    the handle moved into X-Boss-Handle, its word on VM/run/epoch and the boss:egress token."""

    def __init__(self, world: World, proxy: Any, *, token: Optional[str], key: Any) -> None:
        self.world, self.proxy, self.token, self.key = world, proxy, token, key
        self.vm = world.vm_id
        self.statuses: list[int] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                pass

            def do_GET(self) -> None:
                outer.serve(self)

            def do_POST(self) -> None:
                outer.serve(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def close(self) -> None:
        self.server.shutdown()

    def serve(self, h: BaseHTTPRequestHandler) -> None:
        from apps.egress.boss_egress.proxy import EgressRequest

        path, _, query = h.path.partition("?")
        if path == "/_boss/git-credential":
            body = f"username=boss\npassword={self.world.work['handles']['egr']}\n".encode()
            return self._reply(h, 200, {"Content-Type": "text/plain"}, body)
        auth = h.headers.get("Authorization", "")
        handle = ""
        if auth.lower().startswith("basic "):
            handle = base64.b64decode(auth[6:]).decode().partition(":")[2]
        if not handle.startswith("boss_"):
            return self._reply(h, 401, {"WWW-Authenticate": 'Basic realm="boss"'}, b"")
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        method = h.command
        url = EGRESS_BASE + path
        headers = {k.lower(): v for k, v in h.headers.items()
                   if k.lower() in ("content-type", "accept", "user-agent", "git-protocol",
                                    "content-encoding")}
        headers.update({k.lower(): v for k, v in self.world.host.headers(method, path).items()})
        headers.update({"x-boss-handle": handle, "x-boss-vm": self.vm,
                        "x-boss-run": self.world.work["run_id"],
                        "x-boss-fence-epoch": str(self.world.work["epoch"])})
        if self.token:
            headers["x-boss-delegation"] = self.token
            headers["dpop"] = self.key.proof(method, url, access_token=self.token)
        resp = self.proxy.handle(EgressRequest(method=method, path=path, query=query,
                                               headers=headers, body=body, peer="10.9.9.9",
                                               url=url))
        self.statuses.append(resp.status)
        self._reply(h, resp.status, dict(resp.headers), resp.body)

    @staticmethod
    def _reply(h: BaseHTTPRequestHandler, status: int, headers: dict, body: bytes) -> None:
        h.send_response(status)
        for k, v in headers.items():
            if k.lower() not in ("content-length", "transfer-encoding", "connection"):
                h.send_header(k, v)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)


def _git(env: dict, cwd: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
    out = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                         timeout=120)
    if ok:
        assert out.returncode == 0, f"git {args}: {out.stderr}"
    return out


def _guest_env(home: Path) -> dict:
    return {"HOME": str(home), "PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a",
            "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@a"}


def _agent_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "in_guest_agent_p8b5", ROOT / "sandbox-host" / "guest" / "in_guest_agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scope(world: World, repos: list) -> None:
    with world.eng.begin() as c:
        c.execute(text("UPDATE agent_deployments SET metadata = CAST(:m AS JSONB) WHERE id = :d"),
                  {"m": __import__("json").dumps({"repos": repos, "bitbucket_repos": ["ws/app", "ws/secret"],
                                                  "github_needs": {"contents": "write"}}),
                   "d": world.dep["id"]})


def _attach_grant(world: World, resources: dict) -> str:
    """The run's grant (jobs.grant_id), without making the run on-behalf-of anyone."""
    import uuid

    gid = f"dlg_{uuid.uuid4().hex[:12]}"
    with world.eng.begin() as c:
        person = c.execute(text(
            "SELECT id FROM identity_principals WHERE kind = 'user' AND status = 'active' "
            "AND org_id = :o ORDER BY created_at LIMIT 1"), {"o": world.org}).scalar()
        if not person:
            # The host bench world has an org and a job, and no person. The grant row
            # requires one. This person owns the grant and is not the run's on_behalf_of.
            person = f"usr_{uuid.uuid4().hex[:12]}"
            c.execute(text(
                "INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                "VALUES (:i, :o, 'user', 'active', 'Grant owner')"),
                {"i": person, "o": world.org})
        c.execute(text(
            "INSERT INTO identity_delegation_grants (id, org_id, user_principal_id, agent_ids, "
            "project_ids, resources, status, expires_at) VALUES (:i, :o, :u, :a, :p, "
            "CAST(:r AS JSONB), 'active', now() + interval '1 day')"),
            {"i": gid, "o": world.org, "u": person, "a": ["agt_scope"], "p": [world.project],
             "r": __import__("json").dumps(resources)})
        c.execute(text("UPDATE agent_deployments SET metadata = '{}'::jsonb WHERE id = :d"),
                  {"d": world.dep["id"]})
        # The column is the scope. Clearing on_behalf_of keeps a shadow-stamped host run
        # autonomous: pairing sys_worker with a grant makes the mint look up that principal.
        c.execute(text(
            "UPDATE jobs SET grant_id = :g, on_behalf_of = NULL WHERE id = :j"),
            {"g": gid, "j": world.work["job_id"]})
    return gid


def _bind_project_repo(world: World, full_name: str) -> None:
    import uuid

    owner, name = full_name.split("/", 1)
    cid, bid = f"ghc_{uuid.uuid4().hex[:10]}", f"ghb_{uuid.uuid4().hex[:10]}"
    with world.eng.begin() as c:
        c.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
            "credential_ref, status, created_by) VALUES (:i, :p, :l, 'github_app', "
            "'secret:none', 'active', 'test')"),
            {"i": cid, "p": world.project, "l": cid})
        c.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
            "full_name, role, status, created_by) VALUES (:i, :p, :c, :o, :n, :f, 'primary', "
            "'active', 'test')"),
            {"i": bid, "p": world.project, "c": cid, "o": owner, "n": name, "f": full_name})


@pytest.fixture()
def rig(world, tmp_path, monkeypatch):
    yield from _open_rig(world, tmp_path, monkeypatch, grant=False)


@pytest.fixture()
def granted_rig(world, tmp_path, monkeypatch):
    """BOSSAPP-1178: the deployment names no repository. The run's grant does."""
    yield from _open_rig(world, tmp_path, monkeypatch, grant=True)


def _open_rig(world, tmp_path, monkeypatch, *, grant: bool):
    if shutil.which("git") is None:
        pytest.skip("no git")
    if world.give_the_agent_a_passport() is None:
        pytest.skip("no active person in the test database to sponsor the agent's Passport")
    from apps.egress.boss_egress.proxy import EgressProxy
    from boss_engine.services import fleet

    # Not this ticket's subject: the run's GitHub authority (grant ∩ ceilings) and the App.
    monkeypatch.setattr(github_app, "run_permissions",
                        lambda claims, db: {"contents": "write", "workflows": "write"})
    monkeypatch.setattr(EgressProxy, "_installation_token", lambda self, claims, db: FAKE_INSTALLATION)

    class _NotFound:
        status = 404

        @staticmethod
        def json() -> dict:
            return {}

    monkeypatch.setattr(github_app, "_call", lambda *a, **k: _NotFound())
    monkeypatch.setenv("BOSS_GITHUB_GIT_URL", "http://fakegithub.test")
    monkeypatch.delenv("BOSS_EGRESS_STRICT_SECRETS", raising=False)
    fleet._HOST_TOKENS_CACHE.clear()

    run = world.work["run_id"]
    assert world.work["handles"]["egr"].startswith("boss_egr_")
    if grant:
        # Nothing on the deployment. The run's grant is the only name for these repositories.
        _attach_grant(world, {"github": {"repos": ["acme/web"]},
                              "bitbucket": {"repos": ["ws/app"]}})
    else:
        _scope(world, ["acme/web"])
    # The install's Bitbucket registry (identity/repo_scope.py): ws/app only. The metadata ALSO
    # names ws/secret — a request, not authority — so it never enters the run's token.
    monkeypatch.delenv("BOSS_TARGET_BITBUCKET_WORKSPACE", raising=False)
    monkeypatch.delenv("BOSS_TARGET_BITBUCKET_REPO", raising=False)
    monkeypatch.setenv("BOSS_BITBUCKET_REPOS", "ws/app")
    key = dpop.DPoPKey.generate()                    # boss-hostd's per-run key, in Python
    minted = world.tokens(key.jkt)
    assert minted.status_code == 200, minted.text
    toks = minted.json()["tokens"]

    hub = tmp_path / "hub"
    bare = hub / "acme" / "web.git"
    seed = tmp_path / "seed"
    env = _guest_env(tmp_path)
    _git(env, tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(env, tmp_path, "-C", str(bare), "config", "http.receivepack", "true")
    _git(env, tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "README.md").write_text("hello\n")
    _git(env, seed, "add", "-A")
    _git(env, seed, "commit", "-q", "-m", "seed")
    _git(env, seed, "push", "-q", str(bare), "main")
    other = hub / "acme" / "other.git"
    _git(env, tmp_path, "clone", "-q", "--bare", str(bare), str(other))
    bb = hub / "ws" / "app.git"                      # the Bitbucket repository (D19)
    _git(env, tmp_path, "clone", "-q", "--bare", str(bare), str(bb))
    _git(env, tmp_path, "-C", str(bb), "config", "http.receivepack", "true")
    _git(env, tmp_path, "clone", "-q", "--bare", str(bare), str(hub / "ws" / "secret.git"))
    monkeypatch.setenv("BOSS_BITBUCKET_TOKEN", FAKE_BITBUCKET)
    monkeypatch.setenv("BOSS_BITBUCKET_GIT_URL", "http://fakebitbucket.test")

    upstream = Upstream(hub)
    proxy = EgressProxy(world.eng, upstream=upstream)
    fwd = HostForward(world, proxy, token=toks[delegation.AUD_EGRESS]["access_token"], key=key)
    home = tmp_path / "guest"
    home.mkdir()
    genv = _guest_env(home)
    old = os.environ.get("HOME")
    os.environ["HOME"] = str(home)                   # configure_git_relay writes --global
    try:
        _agent_module().configure_git_relay({"endpoints": {"git": f"http://127.0.0.1:{fwd.port}"},
                                             "handles": {"egr": world.work["handles"]["egr"]}})
    finally:
        if old is not None:
            os.environ["HOME"] = old
    yield {"world": world, "run": run, "bare": bare, "bb": bb, "env": genv, "home": home, "fwd": fwd,
           "proxy": proxy, "upstream": upstream, "tokens": toks, "key": key, "tmp": tmp_path}
    fwd.close()


def test_clone_fetch_push_through_the_host_route_with_no_token_in_the_guest(rig):
    env, tmp, run = rig["env"], rig["tmp"], rig["run"]
    work = tmp / "work"
    _git(env, tmp, "clone", "-q", "https://github.com/acme/web.git", str(work))
    assert (work / "README.md").read_text() == "hello\n"
    (work / "change.txt").write_text("x\n")
    _git(env, work, "add", "-A")
    _git(env, work, "commit", "-q", "-m", "change")
    safe = run.replace(":", "-").replace("/", "-").replace("#", "-")
    _git(env, work, "push", "-q", "origin", f"HEAD:refs/heads/boss/{safe}-1")
    _git(env, work, "fetch", "-q", "origin")
    listed = _git(env, tmp, "-C", str(rig["bare"]), "branch", "--list", f"boss/{safe}-1").stdout
    assert f"boss/{safe}-1" in listed
    # Upstream saw the INJECTED installation credential, never the handle.
    handle = rig["world"].work["handles"]["egr"]
    expected = "Basic " + base64.b64encode(f"x-access-token:{FAKE_INSTALLATION}".encode()).decode()
    assert rig["upstream"].seen and all(s.get("authorization") == expected for s in rig["upstream"].seen)
    assert not any(handle in str(s) for s in rig["upstream"].seen)
    # No token and no handle in any guest git config or the environment.
    for f in (rig["home"] / ".gitconfig", work / ".git" / "config"):
        content = f.read_text()
        assert "boss_egr_" not in content and FAKE_INSTALLATION not in content
        assert "x-access-token" not in content
    assert not any("boss_egr_" in v or FAKE_INSTALLATION in v for v in env.values())
    # The handle's row is bound to this run's VM and epoch.
    with rig["world"].eng.begin() as c:
        row = egress_handles.load(c, handle)
    assert row.vm_id == rig["world"].vm_id and row.run_id == run
    assert row.fence_epoch == rig["world"].work["epoch"]


def test_other_refs_force_push_delete_and_foreign_repos_are_refused(rig):
    env, tmp, run, bare = rig["env"], rig["tmp"], rig["run"], rig["bare"]
    safe = run.replace(":", "-").replace("/", "-").replace("#", "-")
    work = tmp / "work"
    _git(env, tmp, "clone", "-q", "https://github.com/acme/web.git", str(work))
    main_before = _git(env, tmp, "-C", str(bare), "rev-parse", "main").stdout
    (work / "a.txt").write_text("a\n")
    _git(env, work, "add", "-A")
    _git(env, work, "commit", "-q", "-m", "a")
    # Another ref (main): refused.
    assert _git(env, work, "push", "-q", "origin", "HEAD:refs/heads/main", ok=False).returncode != 0
    assert _git(env, tmp, "-C", str(bare), "rev-parse", "main").stdout == main_before
    # The run's own branch: allowed; then a FORCE-push of an unrelated history: refused.
    _git(env, work, "push", "-q", "origin", f"HEAD:refs/heads/boss/{safe}-2")
    tip = _git(env, tmp, "-C", str(bare), "rev-parse", f"boss/{safe}-2").stdout
    _git(env, work, "checkout", "-q", "--orphan", "rewrite")
    (work / "b.txt").write_text("b\n")
    _git(env, work, "add", "-A")
    _git(env, work, "commit", "-q", "-m", "rewrite")
    forced = _git(env, work, "push", "-q", "--force", "origin", f"HEAD:refs/heads/boss/{safe}-2", ok=False)
    assert forced.returncode != 0
    assert _git(env, tmp, "-C", str(bare), "rev-parse", f"boss/{safe}-2").stdout == tip
    # Deleting a branch, even the run's own: refused.
    deleted = _git(env, work, "push", "-q", "origin", f":refs/heads/boss/{safe}-2", ok=False)
    assert deleted.returncode != 0
    assert _git(env, tmp, "-C", str(bare), "rev-parse", f"boss/{safe}-2").stdout == tip
    # A repository outside the run: refused at the first request.
    outside = _git(env, tmp, "clone", "-q", "https://github.com/acme/other.git", str(tmp / "o"), ok=False)
    assert outside.returncode != 0
    with rig["world"].eng.begin() as c:
        reasons = set(c.execute(text(
            "SELECT deny_reason FROM identity_egress_log WHERE run_id = :r AND decision = 'denied'"),
            {"r": run}).scalars().all())
    assert {"ref_not_allowed", "force_push", "branch_delete", "repo_outside_run"} <= reasons, reasons


def _call(rig, *, headers: dict, path: str = "/git/github.com/acme/web.git/info/refs",
          query: str = "service=git-upload-pack", sign: bool = True):
    from apps.egress.boss_egress.proxy import EgressRequest

    world = rig["world"]
    base = {"x-boss-handle": world.work["handles"]["egr"], "x-boss-vm": world.vm_id,
            "x-boss-run": world.work["run_id"], "x-boss-fence-epoch": str(world.work["epoch"])}
    if sign:
        base.update({k.lower(): v for k, v in world.host.headers("GET", path).items()})
    base.update(headers)
    return rig["proxy"].handle(EgressRequest(method="GET", path=path, query=query, headers=base,
                                             peer="10.9.9.9", url=EGRESS_BASE + path))


def _reason(resp) -> str:
    import json

    return json.loads(resp.body or b"{}").get("reason", "")


def test_the_egress_token_is_required_per_host_and_must_be_egress_s(rig):
    from boss_engine.services import fleet

    world, toks, key = rig["world"], rig["tokens"], rig["key"]
    url = EGRESS_BASE + "/git/github.com/acme/web.git/info/refs"

    def proof(tok: str) -> dict:
        return {"x-boss-delegation": tok, "dpop": key.proof("GET", url, access_token=tok)}

    egress_tok = toks[delegation.AUD_EGRESS]["access_token"]
    assert _call(rig, headers=proof(egress_tok)).status == 200
    # Another destination's token: refused, whatever the host.
    wrong = _call(rig, headers=proof(toks[delegation.AUD_MODEL]["access_token"]))
    assert wrong.status == 401 and _reason(wrong) == "run_token_bad_aud"
    # A host that predates S13 and attaches none: no registered token → refused, never let in.
    with world.eng.begin() as c:
        c.execute(text("UPDATE agent_hosts SET host_tokens = false WHERE id = :h"), {"h": world.host.host_id})
    fleet._HOST_TOKENS_CACHE.clear()
    legacy = _call(rig, headers={})
    assert legacy.status == 401 and _reason(legacy) == "egress_not_registered"
    # The host advertises host_tokens: from now on a missing token is run_token_required.
    with world.eng.begin() as c:
        c.execute(text("UPDATE agent_hosts SET host_tokens = true WHERE id = :h"), {"h": world.host.host_id})
    fleet._HOST_TOKENS_CACHE.clear()
    missing = _call(rig, headers={})
    assert missing.status == 401 and _reason(missing) == "run_token_required"
    assert _call(rig, headers=proof(egress_tok)).status == 200


def test_replay_direct_use_no_proof_and_unrouted_hosts_are_refused(rig, monkeypatch):
    from apps.egress.boss_egress.proxy import EgressRequest

    world, toks, key = rig["world"], rig["tokens"], rig["key"]
    egress_tok = toks[delegation.AUD_EGRESS]["access_token"]

    def proof(path: str) -> dict:
        return {"x-boss-delegation": egress_tok,
                "dpop": key.proof("GET", EGRESS_BASE + path, access_token=egress_tok)}

    # No host proof: refused before the handle is even read.
    unsigned = _call(rig, headers=proof("/git/github.com/acme/web.git/info/refs"), sign=False)
    assert unsigned.status == 401 and _reason(unsigned) == "host_proof_required"
    # GitLab: refused by name; in strict mode by its strict name.
    gl = "/git/gitlab.com/acme/web.git/info/refs"
    assert _reason(_call(rig, path=gl, headers=proof(gl))) == "git_host_not_routed"
    monkeypatch.setenv("BOSS_EGRESS_STRICT_SECRETS", "1")
    assert _reason(_call(rig, path=gl, headers=proof(gl))) == "strict_secrets_git_host_refused"
    # The VM handle presented straight to egress, as the local lane would: worthless.
    raw = world.work["handles"]["egr"]
    direct = rig["proxy"].handle(EgressRequest(
        method="GET", path="/github/acme/web.git/info/refs", query="service=git-upload-pack",
        headers={"authorization": "Basic " + base64.b64encode(f"x:{raw}".encode()).decode()},
        peer="10.9.9.9"))
    assert direct.status == 401
    # Another VM's word: a replay — revoked, reported, refused; and dead for its own VM after.
    replay = _call(rig, headers={**proof("/git/github.com/acme/web.git/info/refs"), "x-boss-vm": "vm_other"})
    assert replay.status == 401 and _reason(replay) == "handle_replay"
    with world.eng.begin() as c:
        row = egress_handles.load(c, raw)
        event = c.execute(text(
            "SELECT detail->>'why' FROM host_events WHERE host_id = :h AND kind = 'handle_replay' "
            "AND actor = 'egress'"), {"h": world.host.host_id}).scalar()
    assert row.revoked_at is not None and event == "another_vm"
    after = _call(rig, headers=proof("/git/github.com/acme/web.git/info/refs"))
    assert after.status == 401 and _reason(after) == "handle_revoked"


def test_bitbucket_is_routed_with_the_same_rules(rig):
    """D19: a Bitbucket repository of the run clones and pushes through the same host route;
    the upstream sees the install's Bitbucket credential, never the handle; a force-push is
    refused from the pack alone (egress cannot ask Bitbucket about other commits)."""
    env, tmp, run, bb = rig["env"], rig["tmp"], rig["run"], rig["bb"]
    safe = run.replace(":", "-").replace("/", "-").replace("#", "-")
    work = tmp / "bbwork"
    _git(env, tmp, "clone", "-q", "https://bitbucket.org/ws/app.git", str(work))
    assert (work / "README.md").read_text() == "hello\n"
    expected = "Basic " + base64.b64encode(f"x-token-auth:{FAKE_BITBUCKET}".encode()).decode()
    assert rig["upstream"].seen[-1].get("authorization") == expected
    (work / "c.txt").write_text("c\n")
    _git(env, work, "add", "-A")
    _git(env, work, "commit", "-q", "-m", "c")
    _git(env, work, "push", "-q", "origin", f"HEAD:refs/heads/boss/{safe}-bb")
    tip = _git(env, tmp, "-C", str(bb), "rev-parse", f"boss/{safe}-bb").stdout
    (work / "d.txt").write_text("d\n")
    _git(env, work, "add", "-A")
    _git(env, work, "commit", "-q", "-m", "d")
    _git(env, work, "push", "-q", "origin", f"HEAD:refs/heads/boss/{safe}-bb")   # fast-forward
    _git(env, work, "checkout", "-q", "--orphan", "rewrite")
    _git(env, work, "commit", "-q", "-m", "rewrite")
    forced = _git(env, work, "push", "-q", "--force", "origin", f"HEAD:refs/heads/boss/{safe}-bb", ok=False)
    assert forced.returncode != 0
    after = _git(env, tmp, "-C", str(bb), "rev-parse", f"boss/{safe}-bb").stdout
    assert after != tip, "the fast-forward landed"
    assert _git(env, tmp, "-C", str(bb), "rev-parse", "main").stdout == \
        _git(env, tmp, "-C", str(rig["bare"]), "rev-parse", "main").stdout
    assert not any(rig["world"].work["handles"]["egr"] in str(s) for s in rig["upstream"].seen)
    outside = _git(env, tmp, "clone", "-q", "https://bitbucket.org/ws/other.git", str(tmp / "bo"), ok=False)
    assert outside.returncode != 0
    with rig["world"].eng.begin() as c:
        reasons = set(c.execute(text(
            "SELECT deny_reason FROM identity_egress_log WHERE run_id = :r AND provider = 'bitbucket' "
            "AND decision = 'denied'"), {"r": run}).scalars().all())
    assert {"force_push", "repo_outside_run"} <= reasons, reasons


def test_a_bitbucket_repo_the_payload_names_but_no_grant_allows_is_refused(rig, monkeypatch):
    """Finding 1 (PR #126): the deployment's metadata names ws/app AND ws/secret; only ws/app is
    registered. The run's token binds ws/app alone, a clone of ws/secret is `repo_outside_run`
    though the upstream holds it, and unregistering ws/app mid-run takes it away at egress too."""
    payload = rig["tokens"][delegation.AUD_EGRESS]["access_token"].split(".")[1]
    claims = __import__("json").loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["boss_task"]["resources"].get("bitbucket") == ["ws/app"]
    env, tmp, run = rig["env"], rig["tmp"], rig["run"]
    before = len(rig["upstream"].seen)
    secret = _git(env, tmp, "clone", "-q", "https://bitbucket.org/ws/secret.git", str(tmp / "bs"), ok=False)
    assert secret.returncode != 0
    assert len(rig["upstream"].seen) == before, "a refused repository never reaches the upstream"
    _git(env, tmp, "clone", "-q", "https://bitbucket.org/ws/app.git", str(tmp / "ba"))
    monkeypatch.setenv("BOSS_BITBUCKET_REPOS", "ws/elsewhere")
    gone = _git(env, tmp, "clone", "-q", "https://bitbucket.org/ws/app.git", str(tmp / "bc"), ok=False)
    assert gone.returncode != 0
    with rig["world"].eng.begin() as c:
        denied = c.execute(text(
            "SELECT path_template, deny_reason FROM identity_egress_log WHERE run_id = :r "
            "AND provider = 'bitbucket' AND decision = 'denied'"), {"r": run}).all()
    assert {r[1] for r in denied} == {"repo_outside_run"}, denied


def _proxy_call(world, headers: dict, *, path: str = "/git/github.com/acme/web.git/info/refs"):
    from apps.egress.boss_egress.proxy import EgressProxy, EgressRequest, EgressResponse

    proxy = EgressProxy(world.eng, upstream=lambda *a, **k: EgressResponse(200, {}, b"ok"))
    base = {k.lower(): v for k, v in world.host.headers("GET", path).items()}
    base.update({"x-boss-handle": world.work["handles"]["egr"], "x-boss-vm": world.vm_id,
                 "x-boss-run": world.work["run_id"],
                 "x-boss-fence-epoch": str(world.work["epoch"])})
    base.update(headers)
    return proxy.handle(EgressRequest(method="GET", path=path, query="service=git-upload-pack",
                                      headers=base, peer="10.9.9.9", url=EGRESS_BASE + path))


def test_a_host_route_refuses_a_vm_or_deployment_it_does_not_hold(world):
    """BOSSAPP-1177: the proving host must own the X-Boss-Vm or X-Boss-Deployment it names.
    A foreign id is refused. The model gateway uses the same check."""
    from boss_engine.services import streaming_model_gateway as smg

    foreign_dep = _proxy_call(world, {"x-boss-deployment": "dep_not_on_this_host"})
    assert foreign_dep.status == 403 and _reason(foreign_dep) == "binding_not_owned"
    # A VM the handle is not bound to stays a replay (the existing test). A deployment this
    # host does hold does not excuse a VM it does not: checked once the VM agrees with the handle.
    gw = smg.ModelGateway(world.eng, settings=None, upstream=None, keys=None, budget=False,
                          audit=smg.AuditQueue(world.eng, interval=3600), ledger=False,
                          verify_host=lambda *a, **k: (world.host.host_id, world.org))
    with pytest.raises(smg.Refused) as refused:
        gw.caller_of({"authorization": "BossHost proof", "x-boss-vm": "vm_not_on_this_host",
                      "x-boss-run": world.work["run_id"], "x-boss-deployment": world.dep["id"]},
                     method="POST", path="/anthropic/v1/messages")
    assert (refused.value.status, refused.value.reason) == (403, "binding_not_owned")


def test_the_in_process_egress_table_does_not_admit_a_pre_s13_host(world):
    """BOSSAPP-1177: nothing registers a VM handle into runs._EGRESS. A planted row must not
    let a host that attached no boss:egress token through."""
    import time

    from boss_engine.identity import runs
    from boss_engine.identity.principal import ActorChain, RunMode, RunTokenClaims, TaskBinding
    from boss_engine.services import fleet

    with world.eng.begin() as c:
        loaded = egress_handles.load(c, world.work["handles"]["egr"])
        c.execute(text("UPDATE agent_hosts SET host_tokens = false WHERE id = :h"),
                  {"h": world.host.host_id})
    fleet._HOST_TOKENS_CACHE.clear()
    now = int(time.time())
    runs._EGRESS[loaded.token_hash] = RunTokenClaims(
        iss="boss", aud=delegation.AUD_EGRESS, sub="agt_x", org_id=world.org,
        mode=RunMode.AUTONOMOUS, chain=ActorChain(()), grant_id=None,
        task=TaskBinding(run_id=world.work["run_id"], project_id=world.project),
        scope=frozenset(), cnf_jkt="jkt", jti="jti", parent_jti=None, iat=now, exp=now + 600)
    try:
        resp = _proxy_call(world, {})
    finally:
        runs._EGRESS.pop(loaded.token_hash, None)
        fleet._HOST_TOKENS_CACHE.clear()
    assert resp.status == 401 and _reason(resp) == "egress_not_registered"


def _job_row(world: World) -> dict:
    with world.eng.begin() as c:
        return dict(c.execute(text(
            "SELECT id, project_id, payload FROM jobs WHERE id = :j"),
            {"j": world.work["job_id"]}).mappings().one())


def test_a_host_run_fills_git_scope_from_its_grant_not_an_empty_list(world):
    """BOSSAPP-1178: payload and deployment name nothing, so the run's grant fills repos and
    bitbucket_repos. An explicit empty list is not filled back in."""
    from boss_engine.services.run_plane import run_git_scope

    _attach_grant(world, {"github": {"repos": ["acme/web"]}, "bitbucket": {"repos": ["ws/app"]}})
    with world.eng.begin() as c:
        scope = run_git_scope(c, _job_row(world), world.dep["id"])
    assert scope["repos"] == ("acme/web",)
    assert scope["bitbucket_repos"] == ("ws/app",)
    with world.eng.begin() as c:
        c.execute(text("UPDATE agent_deployments SET metadata = CAST(:m AS JSONB) WHERE id = :d"),
                  {"m": '{"repos": ["acme/kept"], "bitbucket_repos": []}', "d": world.dep["id"]})
        named = run_git_scope(c, _job_row(world), world.dep["id"])
    assert named["repos"] == ("acme/kept",)
    assert named["bitbucket_repos"] == ()


def test_github_scope_comes_from_the_project_when_the_grant_names_none(world):
    """BOSSAPP-1178: a grant that names no GitHub repository leaves repos to the project's
    bindings. Bitbucket still comes only from the grant."""
    from boss_engine.services.run_plane import run_git_scope

    _attach_grant(world, {"bitbucket": {"repos": ["ws/app"]}})
    _bind_project_repo(world, "acme/web")
    with world.eng.begin() as c:
        scope = run_git_scope(c, _job_row(world), world.dep["id"])
    assert scope["repos"] == ("acme/web",)
    assert scope["bitbucket_repos"] == ("ws/app",)


def test_a_granted_repo_fetches_and_pushes_its_own_branch(granted_rig):
    """BOSSAPP-1178: a host run whose deployment names no repository still fetches and pushes
    the repo its grant names, on its own branch."""
    rig = granted_rig
    payload = rig["tokens"][delegation.AUD_EGRESS]["access_token"].split(".")[1]
    claims = __import__("json").loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["boss_task"]["resources"].get("github") == ["acme/web"]
    assert claims["boss_task"]["resources"].get("bitbucket") == ["ws/app"]
    env, tmp, run = rig["env"], rig["tmp"], rig["run"]
    work = tmp / "granted"
    _git(env, tmp, "clone", "-q", "https://github.com/acme/web.git", str(work))
    (work / "change.txt").write_text("x\n")
    _git(env, work, "add", "-A")
    _git(env, work, "commit", "-q", "-m", "change")
    safe = run.replace(":", "-").replace("/", "-").replace("#", "-")
    _git(env, work, "push", "-q", "origin", f"HEAD:refs/heads/boss/{safe}-grant")
    _git(env, work, "fetch", "-q", "origin")
    listed = _git(env, tmp, "-C", str(rig["bare"]), "branch", "--list", f"boss/{safe}-grant").stdout
    assert f"boss/{safe}-grant" in listed
    _git(env, tmp, "clone", "-q", "https://bitbucket.org/ws/app.git", str(tmp / "granted-bb"))
