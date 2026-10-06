"""S3 · Egress, the revocation cascade, lineage, jobs, MCP, and the J8–J10 journeys (API half).

A40, A51, A55 (egress), A56, A58, A60, R8, J8, J9, J10 and the cascade ≤5 s test (IDENTITY.md §7.9–§7.14,
§15.2, §15.3). Each test fails if the protection it names is removed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import zlib
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.identity import cascade, github_app, grants, jwks, lineage, passports, runs, tokens
from boss_engine.identity.delegation import AUD_API, AUD_EGRESS, CascadeTrigger
from boss_engine.identity.scope import ResolvedScope

from ._identity_agents_support import (
    BASE,
    ROOT,
    build_env,
    install_github,
    session_ctx,
    spawn,
    start,
    verify,
)

sys.path.insert(0, str(ROOT))
from apps.egress.boss_egress.proxy import EgressProxy, EgressRequest, EgressResponse  # noqa: E402


@pytest.fixture()
def env(monkeypatch):
    return build_env(monkeypatch)


class Upstream:
    def __init__(self):
        self.calls: list[tuple[str, str, dict, bytes]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        return EgressResponse(200, {"content-type": "application/json"}, b'{"ok":true}')


def _basic(cred: str) -> str:
    return "Basic " + base64.b64encode(f"boss-egress:{cred}".encode()).decode()


def _setup(env, monkeypatch, *, repos=("acme/web",), gh=None, run="run_ct:task1"):
    hub = install_github(monkeypatch, env)
    github_app.install_cascade_hook()
    g = env.grant(agents=(env.forge,), children=(env.vera,), repos=repos, gh=gh)
    shim = start(env, g.id, agent=env.forge, repos=repos, run=run)
    up = Upstream()
    return hub, g, shim, up, EgressProxy(env.engine, upstream=up)


def _git(proxy, shim, method, path, query="", body=b""):
    return proxy.handle(EgressRequest(method, "/github" + path, query,
                                      {"authorization": _basic(shim.egress_credential)}, body))


def _api(proxy, shim, method, path, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    return proxy.handle(EgressRequest(method, "/github-api" + path, "",
                                      {"authorization": f"Bearer {shim.egress_credential}",
                                       "cookie": "__Host-boss_session=bss_should_not_cross"}, raw))


# ---- egress ---------------------------------------------------------------------------------------------

def test_egress_injects_a_per_run_down_scoped_installation_token(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    resp = _git(proxy, shim, "GET", "/acme/web.git/info/refs", "service=git-upload-pack")
    assert resp.status == 200
    method, url, headers, _ = up.calls[-1]
    assert url == "https://github.com/acme/web.git/info/refs?service=git-upload-pack"
    assert base64.b64decode(headers["Authorization"].split()[1]).decode() == f"x-access-token:{hub.issued[0]}"
    assert shim.egress_credential not in json.dumps(headers) and "cookie" not in {k.lower() for k in headers}
    mint = next(json.loads(b) for m, u, _, b in hub.calls if u.endswith("/access_tokens"))
    assert mint["repositories"] == ["web"]                                      # the run's repo only
    assert mint["permissions"] == {"contents": "write", "pull_requests": "write", "metadata": "read"}
    _git(proxy, shim, "GET", "/acme/web.git/info/refs", "service=git-upload-pack")
    assert len(hub.issued) == 1                                                  # minted once per run


def test_github_refusing_a_permission_is_a_refusal_never_a_wider_token(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    hub.mint_status = 422
    resp = _git(proxy, shim, "GET", "/acme/web.git/info/refs", "service=git-upload-pack")
    assert resp.status == 403 and up.calls == []
    assert sum(1 for u in hub.urls("POST") if u.endswith("/access_tokens")) == 1   # no retry without permissions


def test_a55_a_repo_outside_the_run_is_refused_at_egress(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    assert _git(proxy, shim, "GET", "/acme/infra.git/info/refs", "service=git-upload-pack").status == 403
    assert _api(proxy, shim, "GET", "/repos/acme/infra").status == 403
    assert up.calls == [] and hub.issued == []


@pytest.mark.parametrize("method, path, body", [
    ("PUT", "/repos/acme/web/collaborators/mallory", None),
    ("POST", "/repos/acme/web/hooks", {"config": {"url": "https://evil"}}),
    ("POST", "/repos/acme/web/keys", {"key": "ssh-ed25519 AAAA"}),
    ("DELETE", "/repos/acme/web", None),
    ("PUT", "/repos/acme/web/pulls/7/merge", None),
    ("PUT", "/repos/acme/web/branches/main/protection", {}),
    ("GET", "/user", None),
    ("POST", "/repos/acme/web/pulls", {"head": "main", "base": "main", "title": "x"}),
    ("PUT", "/repos/acme/web/contents/README.md", {"branch": "main", "content": "eA==", "message": "x"}),
    ("PUT", "/repos/acme/web/contents/.github/workflows/ci.yml", {"branch": "boss/run_ct-task1-a", "content": "eA=="}),
])
def test_admin_endpoints_merges_and_writes_outside_the_run_branch_are_refused(env, monkeypatch, method, path, body):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    assert _api(proxy, shim, method, path, body).status == 403
    assert up.calls == []


def test_a_pull_request_from_the_run_branch_is_allowed(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    resp = _api(proxy, shim, "POST", "/repos/acme/web/pulls",
                {"head": "boss/run_ct-task1-feature", "base": "main", "title": "Add x",
                 "body": github_app.commit_trailers(person_name="Alice", person_email="alice@acme.test",
                                                    grant_id=g.id, run_id="run_ct:task1")})
    assert resp.status == 200
    method, url, headers, sent = up.calls[-1]
    assert url == "https://api.github.com/repos/acme/web/pulls" and headers["Authorization"].startswith("Bearer ghs_")
    assert b"On-behalf-of: Alice <alice@acme.test>" in sent and f"Boss-Lineage: {g.id}/run_ct:task1".encode() in sent


# -- pushes: real pack bytes ---------------------------------------------------------------------------------

def _obj(kind: str, data: bytes) -> tuple[str, int, bytes]:
    code = {"commit": 1, "tree": 2, "blob": 3}[kind]
    sha = hashlib.sha1(f"{kind} {len(data)}".encode() + b"\0" + data).hexdigest()
    return sha, code, data


def _tree(entries: list[tuple[str, str, str]]) -> bytes:
    return b"".join(f"{mode} {name}".encode() + b"\0" + bytes.fromhex(sha) for mode, name, sha in entries)


def _commit(tree: str, parents: list[str]) -> bytes:
    lines = [f"tree {tree}"] + [f"parent {p}" for p in parents] + ["author a <a@b> 0 +0000",
                                                                   "committer a <a@b> 0 +0000", "", "msg", ""]
    return "\n".join(lines).encode()


def _pack(objs) -> bytes:
    out = bytearray(b"PACK" + (2).to_bytes(4, "big") + len(objs).to_bytes(4, "big"))
    for _, code, data in objs:
        size = len(data)
        byte = (code << 4) | (size & 0x0F)
        size >>= 4
        header = bytearray()
        while size:
            header.append(byte | 0x80)
            byte = size & 0x7F
            size >>= 7
        header.append(byte)
        out += header + zlib.compress(data)
    return bytes(out) + hashlib.sha1(bytes(out)).digest()


def _push(old: str, new: str, ref: str, objs) -> bytes:
    line = f"{old} {new} {ref}\0 report-status\n".encode()
    return f"{len(line) + 4:04x}".encode() + line + b"0000" + (_pack(objs) if objs else b"")


def _change(path_files: dict[str, bytes], parent: str):
    """A commit whose whole tree is new in the pack (a fresh push)."""
    objs = []
    blobs = {name: _obj("blob", data) for name, data in path_files.items()}
    objs += blobs.values()
    root_entries = []
    gh_entries = []
    for name, (sha, _, _) in blobs.items():
        if name.startswith(".github/workflows/"):
            wf = _obj("tree", _tree([("100644", name.rsplit("/", 1)[1], sha)]))
            objs.append(wf)
            gh_entries.append(("40000", "workflows", wf[0]))
        else:
            root_entries.append(("100644", name, sha))
    if gh_entries:
        gh = _obj("tree", _tree(gh_entries))
        objs.append(gh)
        root_entries.append(("40000", ".github", gh[0]))
    root = _obj("tree", _tree(sorted(root_entries, key=lambda e: e[1])))
    objs.append(root)
    commit = _obj("commit", _commit(root[0], [parent]))
    objs.append(commit)
    return commit[0], objs


OLD = "a" * 40
RUN_BRANCH = "refs/heads/boss/run_ct-task1-feature"


def test_a56_a_fast_forward_push_to_the_run_branch_is_allowed(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    new, objs = _change({"app.py": b"print(1)\n"}, OLD)
    resp = _git(proxy, shim, "POST", "/acme/web.git/git-receive-pack", body=_push(OLD, new, RUN_BRANCH, objs))
    assert resp.status == 200 and up.calls


@pytest.mark.parametrize("case", ["main", "tag", "force", "workflows", "delete_main", "garbage"])
def test_a56_main_force_push_workflow_writes_and_odd_pushes_are_refused(env, monkeypatch, case):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    new, objs = _change({"app.py": b"x\n"}, OLD)
    unrelated, unrelated_objs = _change({"app.py": b"y\n"}, "b" * 40)       # not built on the branch tip
    wf, wf_objs = _change({".github/workflows/ci.yml": b"on: push\n"}, OLD)
    body = {
        "main": lambda: _push(OLD, new, "refs/heads/main", objs),
        "tag": lambda: _push("0" * 40, new, "refs/tags/v1", objs),
        "force": lambda: _push(OLD, unrelated, RUN_BRANCH, unrelated_objs),
        "workflows": lambda: _push(OLD, wf, RUN_BRANCH, wf_objs),
        "delete_main": lambda: _push(OLD, "0" * 40, "refs/heads/main", []),
        "garbage": lambda: b"00zzgarbage",
    }[case]()
    if case == "force":
        hub.compare_status = "diverged"
    resp = _git(proxy, shim, "POST", "/acme/web.git/git-receive-pack", body=body)
    assert resp.status == 403 and up.calls == []


def test_a56_workflow_writes_pass_only_when_the_grant_says_workflows_write(env, monkeypatch):
    with env.db() as c:                     # the passport's external ceiling must allow it too
        c.execute(text("UPDATE identity_agent_passports SET external_ceiling = "
                       "'{\"github\": {\"contents\": \"write\", \"pull_requests\": \"write\", \"workflows\": \"write\"}}' "
                       "WHERE agent_id = :a"), {"a": env.forge})
    hub, g, shim, up, proxy = _setup(env, monkeypatch, gh={"contents": "write", "workflows": "write"})
    new, objs = _change({".github/workflows/ci.yml": b"on: push\n"}, OLD)
    resp = _git(proxy, shim, "POST", "/acme/web.git/git-receive-pack", body=_push(OLD, new, RUN_BRANCH, objs))
    assert resp.status == 200


def test_a58_every_egress_request_has_a_log_row_with_the_chain_and_no_secret(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    _git(proxy, shim, "GET", "/acme/web.git/info/refs", "service=git-upload-pack")
    _api(proxy, shim, "GET", "/repos/acme/infra")                       # denied, still logged
    _api(proxy, shim, "GET", "/repos/acme/web/pulls/3")
    with env.db() as c:
        rows = c.execute(text("SELECT * FROM identity_egress_log ORDER BY id")).mappings().all()
    assert [r["decision"] for r in rows] == ["allowed", "denied", "allowed"]
    assert all(r["chain"] == [f"{env.forge}#run_ct:task1"] and r["run_id"] == "run_ct:task1" and r["jti"] for r in rows)
    assert rows[2]["path_template"] == "/repos/{o}/{r}/pulls/{n}"        # a template, never the raw path
    dump = json.dumps([dict(r) for r in rows], default=str)
    for secret in [shim.egress_credential, *hub.issued, shim.tokens[AUD_EGRESS].access_token]:
        assert secret not in dump


def test_a_dead_or_foreign_egress_credential_is_401(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    fake = runs.EGRESS_PREFIX + "x" * 43
    assert proxy.handle(EgressRequest("GET", "/github-api/repos/acme/web", "",
                                      {"authorization": f"Bearer {fake}"})).status == 401
    assert proxy.handle(EgressRequest("GET", "/github-api/repos/acme/web", "",
                                      {"authorization": f"Bearer {shim.tokens[AUD_API].access_token}"})).status == 401
    with env.db() as c:
        runs.end_run("run_ct:task1", db=c, org_id="org_boss")
    assert _api(proxy, shim, "GET", "/repos/acme/web").status == 401


# ---- A51: prompt-injection exfiltration -----------------------------------------------------------------

def test_a51_a_prompt_injected_agent_finds_no_token_to_leak(env, monkeypatch, tmp_path):
    from boss_engine.services.agent_env import agent_environment

    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    monkeypatch.setenv("BOSS_EGRESS_URL", "https://egress.boss.internal")
    import tempfile

    monkeypatch.setenv("BOSS_SHIM_DIR", tempfile.mkdtemp(prefix="bshim", dir="/tmp"))   # AF_UNIX paths are short
    _git(proxy, shim, "GET", "/acme/web.git/info/refs", "service=git-upload-pack")      # a live install token
    access = runs.egress_access(shim, owner="acme", repo="web")
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    for key, value in access.git_config:
        subprocess.run(["git", "-C", str(work), "config", "--add", key, value], check=True)
    home = tmp_path / "home"
    home.mkdir()
    # The control plane's own environment holds every secret a careless wiring could pass on.
    hostile_parent = {**os.environ, "GITHUB_TOKEN": hub.issued[0], "BOSS_API_KEY": "boss_pat_shared",
                      "BOSS_RUN_TOKEN": shim.tokens[AUD_API].access_token, "HOME": str(home)}
    agent_env = agent_environment(hostile_parent, {"HOME": str(home), **access.helper_env})
    # The malicious README: print every environment variable, ~/.git-credentials, the git config,
    # the shim socket's name, and /proc/*/environ where it exists.
    script = ("env; cat ~/.git-credentials 2>/dev/null; cat .git/config; ls -la \"$BOSS_SHIM_SOCKET\"; "
              "cat /proc/self/environ 2>/dev/null | tr '\\0' '\\n'; git config --list")
    transcript = subprocess.run(["sh", "-c", script], cwd=work, env=agent_env, capture_output=True, text=True).stdout
    secrets_ = [hub.issued[0], shim.egress_credential, shim.tokens[AUD_API].access_token,
                shim.tokens[AUD_EGRESS].access_token, "boss_pat_shared"]
    assert transcript and all(s not in transcript for s in secrets_)
    with env.db() as c:
        log_dump = json.dumps([dict(r) for r in c.execute(text("SELECT * FROM identity_egress_log")).mappings()], default=str)
    events_dump = json.dumps([e.__dict__ for e in env.world.events], default=str)
    assert all(s not in log_dump and s not in events_dump for s in secrets_)
    runs.close_shim_socket(shim.run_id)


# ---- the cascade -----------------------------------------------------------------------------------------

def test_a60_j9_revoking_a_grant_mid_run_halts_it_within_5s_and_revokes_the_github_token(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    with env.db() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id) VALUES ('task1', :p)"), {"p": env.project})
        c.execute(text("INSERT INTO workflow_schedules (id, input) VALUES ('sch1', CAST(:i AS JSONB))"),
                  {"i": json.dumps({"_identity": {"grant_id": g.id, "on_behalf_of": env.alice}})})
    forge = spawn(env, shim, env.vera, "vera", scope=("code_task:read",))
    assert _git(proxy, shim, "GET", "/acme/web.git/info/refs", "service=git-upload-pack").status == 200
    install_token = hub.issued[0]
    t0 = time.monotonic()
    with env.db() as c:
        preview = cascade.revoke_cascade(CascadeTrigger.GRANT_REVOKED, g.id, actor=session_ctx(env.alice),
                                         reason="preview", db=c, dry_run=True)
        assert preview.dry_run and preview.run_tokens_revoked >= 3 and grants.load(g.id, db=c).status.value == "active"
    with env.db() as c:
        report = cascade.revoke_cascade(CascadeTrigger.GRANT_REVOKED, g.id, actor=session_ctx(env.alice),
                                        reason="changed my mind", db=c)
    # Effective at commit: the API, the child, egress and GitHub all see it.
    with pytest.raises(tokens.TokenError) as e:
        verify(env, shim)
    with pytest.raises(tokens.TokenError):
        verify(env, forge)
    assert _git(proxy, shim, "GET", "/acme/web.git/info/refs", "service=git-upload-pack").status == 401
    assert install_token in hub.deleted                                       # DELETE /installation/token
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, elapsed
    assert e.value.reason == "jti_revoked" and set(report.runs_halted) >= {"run_ct:task1", "run_ct:task1/vera"}
    with env.db() as c:
        halted = c.execute(text("SELECT reason FROM code_task_stop_requests WHERE run_id = 'task1'")).scalar()
        assert halted and "authority revoked" in halted
        assert c.execute(text("SELECT enabled FROM workflow_schedules WHERE id = 'sch1'")).scalar() is False
        assert grants.load(g.id, db=c).status.value == "revoked"
    assert "delegation.revoked" in env.world.types()
    # Re-grant -> resume: a NEW grant, a new run token; revoked stays revoked.
    g2 = env.grant(agents=(env.forge,), repos=("acme/web",))
    again = start(env, g2.id, agent=env.forge, repos=("acme/web",), run="run_ct:task1")
    assert verify(env, again).grant_id == g2.id


def test_a_disabled_person_suspends_their_grants_and_kills_their_runs(env):
    g = env.grant()
    shim = start(env, g.id)
    with env.db() as c:
        c.execute(text("UPDATE identity_principals SET status = 'disabled' WHERE id = :u"), {"u": env.alice})
    with env.db() as c:                                       # live check: refused even before the cascade
        assert not tokens.decide(verify(env, shim), "code_task:read", None, db=c).allowed
        report = cascade.revoke_cascade(CascadeTrigger.PERSON_DISABLED, env.alice,
                                        actor=cascade.system_actor("org_boss"), reason="scim", db=c)
    assert g.id in report.grants_suspended and report.run_tokens_revoked >= 1
    with pytest.raises(tokens.TokenError):
        verify(env, shim)


def test_revoking_a_passport_suspends_its_whole_born_by_subtree(env):
    env.world.grant(env.alice, {"agent_passport:revoke"})
    with env.db() as c:
        c.execute(text("INSERT INTO roles (id, role_key, agent_id, born_by) VALUES "
                       "('r1','vera',:v,:fb), ('r2','rook',:r,:vb)"),
                  {"v": env.vera, "fb": f"agent:{env.forge}", "r": env.rook, "vb": f"agent:{env.vera}"})
    g = env.grant(agents=(env.forge,), permissions=("code_task:read",), children=())
    with env.db() as c:
        report = passports.revoke(session_ctx(env.alice), env.forge, reason="compromised", db=c)
    assert set(report.passports_suspended) == {env.vera, env.rook}           # grandchildren too
    with env.db() as c:
        assert grants.load(g.id, db=c).status.value == "revoked"
        assert passports.get(env.rook, db=c).status == "suspended"


def test_an_agent_cannot_run_the_cascade(env):
    shim = start(env, env.grant().id)
    with env.db() as c:
        ctx = tokens.context_for(verify(env, shim), db=c)
        with pytest.raises(PermissionError):
            cascade.revoke_cascade(CascadeTrigger.GRANT_REVOKED, "dlg_x", actor=ctx, reason="x", db=c)


# ---- J8, J10 ---------------------------------------------------------------------------------------------

def test_j8_for_this_build_only_the_chain_narrows_and_lineage_tells_the_story(env, monkeypatch):
    install_github(monkeypatch, env)
    g = env.grant(agents=(env.atlas,), children=(env.forge, env.vera), repos=("acme/web",), task_run_id="run_ct:j8")
    atlas = start(env, g.id, repos=("acme/web",), run="run_ct:j8")
    forge = spawn(env, atlas, env.forge, "forge", scope=("code_task:read", "code_task:steer", "github:sync",
                                                          "preview:manage"))
    events = [e for e in env.world.events if e.event_type in ("token.issued", "token.exchanged")]
    issued = [e for e in events if e.event_type == "token.issued" and e.target_id == "run_ct:j8"]
    exchanged = [e for e in events if e.event_type == "token.exchanged"]
    assert issued and exchanged
    assert exchanged[-1].on_behalf_of == env.alice and exchanged[-1].grant_id == g.id
    assert exchanged[-1].chain == [f"{env.atlas}#run_ct:j8", f"{env.forge}#run_ct:j8/forge"]
    assert exchanged[-1].details["parent_jti"] == atlas.tokens[AUD_API].jti
    assert forge.tokens[AUD_API].scope < atlas.tokens[AUD_API].scope                 # narrowing
    trailer = github_app.commit_trailers(person_name="Alice", person_email="alice@acme.test", grant_id=g.id,
                                         run_id="run_ct:j8")
    assert trailer.splitlines() == ["On-behalf-of: Alice <alice@acme.test>", f"Boss-Lineage: {g.id}/run_ct:j8"]
    with env.db() as c:
        runs.end_run("run_ct:j8", db=c, org_id="org_boss")
        assert grants.load(g.id, db=c).status.value == "expired"                    # "this build only"


def test_j10_lowering_a_ceiling_denies_the_next_call_and_names_the_ceiling(env):
    env.world.grant(env.alice, {"agent_passport:manage"})
    atlas = start(env, env.grant().id)
    forge = spawn(env, atlas, env.forge, "forge")
    claims = verify(env, forge)
    scope = ResolvedScope.in_project("org_boss", env.project)
    with env.db() as c:
        assert tokens.decide(claims, "preview:manage", scope, db=c).allowed
    with env.db() as c:
        p = passports.get(env.forge, db=c)
        passports.set_ceiling(session_ctx(env.alice), env.forge, p.ceiling - {"preview:manage"}, db=c)
    with env.db() as c:
        d = tokens.decide(claims, "preview:manage", scope, db=c)
    assert not d.allowed and f"ceiling:{env.forge}" in d.narrowed_by
    denied = [e for e in env.world.events if e.event_type == "authz.denied"][-1]
    assert denied.permission == "preview:manage" and f"ceiling:{env.forge}" in denied.details["narrowed_by"]
    assert "agent.passport.ceiling_changed" in env.world.types()


def test_a_delegated_workflow_tool_call_is_decided_against_the_run_token(env):
    atlas = start(env, env.grant().id)
    claims = verify(env, atlas)
    with env.db() as c:
        ctx = tokens.context_for(claims, db=c)
    with lineage.acting(ctx):
        with env.db() as c:
            runs.authorize_tool("code_task:read", env.project, db=c)
            with pytest.raises(runs.ToolRefused):
                runs.authorize_tool("decision:approve", env.project, db=c)
            with pytest.raises(runs.ToolRefused):
                runs.authorize_tool(None, env.project, db=c)               # undeclared: deny by default
    with env.db() as c:
        runs.authorize_tool("decision:approve", env.project, db=c)         # outside a run: not this check's job


# ---- R8: jobs carry a principal --------------------------------------------------------------------------

def test_r8_a_job_carries_its_principal_and_fails_closed_without_one(monkeypatch):
    from boss_engine.services import jobs

    assert "stamp_job" in Path(jobs.__file__).read_text()
    who = lineage.Acting(principal_id="usr_alice", org_id="org_boss", grant_id="dlg_1",
                         chain=("agt_forge#run_ct:1",), run_id="run_ct:1")
    stamped = lineage.stamp_job({"x": 1, "_identity": {"on_behalf_of": "usr_root"}}, who)
    assert stamped["_identity"]["on_behalf_of"] == "usr_alice"              # never the payload's claim
    assert lineage.acting_for_job({"payload": json.dumps(stamped)}) == who
    with lineage.acting(who):
        assert lineage.stamp_job({})["_identity"]["chain"] == ["agt_forge#run_ct:1"]
    assert "_identity" not in lineage.stamp_job({"_identity": {"on_behalf_of": "usr_root"}})  # shadow: dropped
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "enforce")
    with pytest.raises(lineage.PrincipalRequired):
        lineage.stamp_job({"x": 1})
    with pytest.raises(lineage.PrincipalRequired):
        lineage.acting_for_job({"payload": {}})


# ---- MCP: no passthrough, audience-bound ---------------------------------------------------------------

def test_mcp_never_passes_a_boss_credential_upstream(env, monkeypatch):
    import httpx

    from boss_engine.services.mcp_client import MCPClient

    shim = start(env, env.grant().id)
    sent = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: sent.append(k) or None)
    client = MCPClient(settings=None)
    for headers in ({"Authorization": f"Bearer {shim.tokens[AUD_API].access_token}"},
                    {"Authorization": "Bearer boss_pat_abc"}, {"X-Api-Key": "boss_egr_abc"},
                    {"DPoP": "x.y.z"}, {"Cookie": "__Host-boss_session=bss_x"}):
        with pytest.raises(tokens.TokenPassthroughRefused):
            client._post("https://mcp.example/mcp", {}, headers, None)
    assert sent == []
    client._post("https://mcp.example/mcp", {}, {"Authorization": "Bearer upstream-own-secret"}, None)
    assert sent and sent[0]["headers"]["Authorization"] == "Bearer upstream-own-secret"
    with pytest.raises(tokens.TokenError):                                   # an API token is not for MCP
        url = BASE + "/mcp"
        tok = shim.tokens[AUD_API].access_token
        with env.db() as c:
            tokens.verify_for_mcp(tok, dpop_proof=shim.key.proof("POST", url, access_token=tok), htm="POST",
                                  htu=url, db=c)


# ---- A40 and Keycloak #53029: introspection -----------------------------------------------------------

def test_a40_introspection_needs_the_egress_credential_and_returns_act(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routers import identity_agents

    monkeypatch.setattr(identity_agents, "_engine", lambda: env.engine)
    monkeypatch.setenv("BOSS_EGRESS_SERVICE_KEY", "egress-service-key-for-tests")
    app = FastAPI()
    app.include_router(identity_agents.build_identity_agents_router())
    client = TestClient(app)
    atlas = start(env, env.grant().id)
    forge = spawn(env, atlas, env.forge, "forge")
    tok = forge.tokens[AUD_API].access_token
    assert client.post("/v1/identity/introspect", data={"token": tok}).status_code == 401
    assert client.post("/v1/identity/introspect", data={"token": tok},
                       headers={"x-boss-egress-key": "wrong"}).status_code == 401
    body = client.post("/v1/identity/introspect", data={"token": tok},
                       headers={"x-boss-egress-key": "egress-service-key-for-tests"}).json()
    assert body["active"] and body["sub"] == env.alice
    assert body["act"] == {"sub": f"{env.forge}#run_ct:task1/forge", "act": {"sub": f"{env.atlas}#run_ct:task1"}}
    jwks_doc = client.get("/.well-known/boss-jwks.json").json()
    assert jwks_doc["keys"] and all("d" not in k for k in jwks_doc["keys"])
    # The token endpoint over HTTP: form fields only; an `act` in the body is ignored (A44).
    parent = atlas.tokens[AUD_API].access_token
    with env.db() as c:
        assertion = tokens.mint_actor_assertion(parent=verify(env, atlas), child_agent_id=env.vera,
                                                child_run_id="run_ct:task1/vera", child_jkt="jkt-child", db=c)
    htu = "http://testserver/v1/identity/token"
    resp = client.post("/v1/identity/token", data={
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "subject_token": parent,
        "subject_token_type": "urn:boss:token-type:run", "actor_token": assertion,
        "actor_token_type": "urn:boss:token-type:actor", "audience": "boss:api", "scope": "code_task:read",
        "act": json.dumps({"sub": "agt_root#run_x"}), "boss_chain": '["agt_root#run_x"]'},
        headers={"DPoP": atlas.key.proof("POST", htu, access_token=parent)})
    assert resp.status_code == 200, resp.text
    claims = jwks.split(resp.json()["access_token"]).claims
    assert claims["boss_chain"] == [f"{env.atlas}#run_ct:task1", f"{env.vera}#run_ct:task1/vera"]
    bad = client.post("/v1/identity/token", data={"grant_type": "password", "subject_token": parent})
    assert bad.status_code == 400 and "Traceback" not in bad.text


# ---- wiring: a code task clones through egress, with no GitHub token in the sandbox --------------------

def test_a_code_task_runs_as_its_delegated_principal_and_clones_through_egress(env, monkeypatch, tmp_path):
    import tempfile

    from boss_engine.services.sandbox import EphemeralSandbox

    install_github(monkeypatch, env)
    monkeypatch.setenv("BOSS_EGRESS_URL", "https://egress.boss.internal")
    monkeypatch.setenv("BOSS_SHIM_DIR", tempfile.mkdtemp(prefix="bshim", dir="/tmp"))
    g = env.grant(agents=(env.forge,), children=(), repos=("acme/web",))
    who = lineage.Acting(principal_id=env.alice, org_id="org_boss", grant_id=g.id)
    with env.db() as c:
        c.execute(text("INSERT INTO roles (id, role_key, agent_id) VALUES ('r-forge', 'forge', :a)"), {"a": env.forge})
        c.execute(text("INSERT INTO jobs (id, project_id, kind, payload) VALUES ('job_1', :p, 'code.task', "
                       "CAST(:pl AS JSONB))"), {"p": env.project, "pl": json.dumps(lineage.stamp_job({}, who))})
    started = runs.start_code_task_run(env.engine, task_run_id="ct42", project_id=env.project, owner="acme",
                                       repo="web", role_key="forge", job_id="job_1")
    assert started is not None
    shim, access = started
    claims = verify(env, shim)
    assert claims.sub == env.alice and claims.chain.agent_ids == (env.forge,) and claims.grant_id == g.id
    assert access.clone_url == "https://egress.boss.internal/github/acme/web.git"
    calls = []
    box = EphemeralSandbox.__new__(EphemeralSandbox)
    box.path, box.settings, box.task_id = None, type("S", (), {"sandbox_root_path": str(tmp_path),
                                                                "sandbox_clone_depth": 1,
                                                                "sandbox_integration_branch": "main"})(), "ct42"
    box._git = lambda args, raw=False, redact=None: calls.append(args) or "deadbeef"
    box._write_local_excludes = lambda: None
    monkeypatch.setattr("boss_engine.services.resource_ledger.claim_dir", lambda p: None)
    box.provision("acme", "web", token="ghs_SHOULD_NOT_BE_USED", egress=access)
    flat = json.dumps(calls)
    assert "ghs_" not in flat and "x-access-token" not in flat and shim.egress_credential not in flat
    assert access.clone_url in flat and "credential.helper" in flat
    runs.finish_code_task_run(env.engine, shim, org_id="org_boss")
    with pytest.raises(tokens.TokenError):
        verify(env, shim)
    # No principal on the job: the legacy path (None), never a token minted from nothing.
    with env.db() as c:
        c.execute(text("INSERT INTO jobs (id, project_id, kind, payload) VALUES ('job_2', :p, 'code.task', '{}')"),
                  {"p": env.project})
    assert runs.start_code_task_run(env.engine, task_run_id="ct43", project_id=env.project, owner="acme",
                                    repo="web", role_key="forge", job_id="job_2") is None
