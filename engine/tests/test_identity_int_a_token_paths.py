"""int-a · No raw GitHub/provider token is handed to an agent or to the code an agent wrote
(IDENTITY.md §7.9-§7.10).

Every BOSS-side clone — the code-task sandbox, the agent shell workspace, the read tools' repo
cache, preview and deploy workdirs — goes through `identity.runs.repo_access` / `repo_host.git_access`:
through boss-egress under a delegated principal when egress is on, else with the token in git's
own process environment only, so it is never in a URL, argv or the checkout's `.git/config`.
Each test fails if the protection it names is removed.
"""
from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

from boss_engine.services import repo_host

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "ghs_INSTALLATIONTOKENDONOTLEAK000000000"


def test_no_code_builds_a_credential_url_outside_the_repo_host_seam():
    allowed = {"engine/boss_engine/services/repo_host.py", "engine/boss_engine/services/github_adapter.py",
               "engine/boss_engine/services/bitbucket_adapter.py", "engine/boss_engine/services/mcp_repo_host.py",
               "apps/egress/boss_egress/proxy.py"}
    offenders = []
    for base in (ROOT / "engine" / "boss_engine", ROOT / "apps"):
        for path in base.rglob("*.py"):
            rel = str(path.relative_to(ROOT))
            if "/tests/" in rel or "node_modules" in rel or rel in allowed:
                continue
            source = path.read_text(encoding="utf-8")
            if "x-access-token:" in source or "x-token-auth:" in source:
                offenders.append(f"{rel}: credential in a URL")
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "clone_url_for":
                    offenders.append(f"{rel}:{node.lineno}: clone_url_for (use git_access / runs.repo_access)")
    assert offenders == []


def test_git_access_keeps_the_token_out_of_the_url_and_the_repr():
    access = repo_host.git_access("github", "acme", "web", TOKEN)
    assert access.url == "https://github.com/acme/web.git" and access.via == "token"
    assert TOKEN not in repr(access) and TOKEN not in access.url
    assert access.env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert access.env["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")
    assert repo_host.git_access("github", "acme", "web", "").env == {}


@pytest.fixture()
def fake_github(tmp_path, monkeypatch):
    """https://github.com/acme/web.git is a local bare repo (git's own insteadOf), so a REAL git
    clone runs and we can look at what it left behind."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git not installed")
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "app.js").write_text("console.log('hi')\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-qm", "init"], check=True, env=env)
    (tmp_path / "hub" / "acme").mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(tmp_path / "hub" / "acme" / "web.git")], check=True)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f'[url "file://{tmp_path}/hub/"]\n\tinsteadOf = https://github.com/\n'
                         '[protocol "file"]\n\tallow = always\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return tmp_path


def _nothing_names_the_token(root: Path) -> None:
    for f in root.rglob("*"):
        if f.is_file():
            assert TOKEN.encode() not in f.read_bytes(), f


def test_a_code_task_sandbox_clone_holds_no_token(fake_github, monkeypatch):
    from boss_engine.config import get_settings
    from boss_engine.services.sandbox import EphemeralSandbox

    settings = get_settings()
    monkeypatch.setattr(settings, "sandbox_root_path", str(fake_github / "sandboxes"))
    with EphemeralSandbox(settings, task_id="tsk_tok") as sbx:
        path = sbx.provision("acme", "web", base_branch="main", work_branch="boss/code/x", token=TOKEN)
        assert (path / "app.js").exists()
        remote = subprocess.run(["git", "-C", str(path), "config", "--get", "remote.origin.url"],
                                capture_output=True, text=True).stdout.strip()
        assert remote == "https://github.com/acme/web.git"
        _nothing_names_the_token(path)


def test_the_read_tools_repo_cache_holds_no_token(fake_github, monkeypatch):
    from boss_engine.config import get_settings
    from boss_engine.tools import builtin

    monkeypatch.setattr(builtin, "_REPO_CACHE", str(fake_github / "cache"))
    root, error = builtin._repo_root(get_settings(), "acme/web", token=TOKEN)
    assert error is None and (Path(root) / "app.js").exists()
    _nothing_names_the_token(Path(root))
    # A cache written before the fix (token in its remote URL) is rewritten on the next read.
    subprocess.run(["git", "-C", root, "remote", "set-url", "origin",
                    f"https://x-access-token:{TOKEN}@github.com/acme/web.git"], check=True)
    root, error = builtin._repo_root(get_settings(), "acme/web", token=TOKEN)
    _nothing_names_the_token(Path(root))


def test_the_agent_shell_workspace_holds_no_token(fake_github, monkeypatch):
    from boss_engine.services import agent_shell

    monkeypatch.setattr(agent_shell, "workspace_for", lambda run_id: fake_github / "ws" / run_id)
    access = repo_host.git_access("github", "acme", "web", TOKEN)
    path, problem = agent_shell.ensure_workspace("crun_t", owner="acme", repo="web", branch="main",
                                                 clone_url=access.url, git_env=access.environ())
    assert problem is None and (path / "app.js").exists()
    _nothing_names_the_token(path)


# ---- with egress on and a delegated principal: no token at all ------------------------------------------

def test_repo_access_goes_through_egress_under_a_delegated_principal(monkeypatch):
    from sqlalchemy import text

    from boss_engine.identity import lineage, runs

    from ._identity_agents_support import build_env

    env = build_env(monkeypatch)
    with env.db() as c:
        c.execute(text("INSERT INTO roles (id, role_key, agent_id) VALUES ('r-forge', 'forge', :f)"), {"f": env.forge})
    monkeypatch.setenv("BOSS_EGRESS_URL", "https://egress.boss.internal")
    import tempfile

    monkeypatch.setenv("BOSS_SHIM_DIR", tempfile.mkdtemp(prefix="bshim", dir="/tmp"))
    g = env.grant(agents=(env.atlas, env.forge), repos=("acme/web",))

    def never():
        raise AssertionError("the shared token was consulted on the egress path")

    alice = lineage.Acting(principal_id=env.alice, org_id="org_boss", grant_id=g.id, run_id="run_wf:wr9")
    with lineage.acting(alice):
        with runs.repo_access(env.engine, host="github", owner="acme", repo="web", project_id=env.project,
                              token=never) as access:
            assert access.via == "egress" and access.url == "https://egress.boss.internal/github/acme/web.git"
            assert any(v.startswith("!f()") for v in access.env.values())          # git asks the shim
            with env.db() as c:
                live = c.execute(text("SELECT run_id FROM identity_run_credentials WHERE revoked_at IS NULL")).scalars().all()
            assert live and all(r.startswith("run_wf:wr9/git:") for r in live)
    with env.db() as c:
        assert c.execute(text("SELECT count(*) FROM identity_run_credentials WHERE revoked_at IS NULL")).scalar() == 0
    # No delegated principal: the legacy token, in git's environment only.
    with runs.repo_access(env.engine, host="github", owner="acme", repo="web", project_id=env.project,
                          token=lambda: TOKEN) as access:
        assert access.via == "token" and TOKEN not in access.url


def test_the_governed_code_task_clones_through_egress_like_the_ungoverned_one():
    """Structural: the governed pipeline starts the build's identity run, hands every clone the
    egress access (work sandbox, review clone, each guest stage) and blanks the token when it
    applies, and ends the run with the build."""
    source = (ROOT / "engine" / "boss_engine" / "services" / "governed_code_task_service.py").read_text()
    assert "identity_runs.start_code_task_run(" in source and "identity_runs.finish_code_task_run(" in source
    assert source.count("egress=egress_access") == 2                      # sandbox + review sandbox
    assert 'guest_spec["egress"] = guest_egress["spec"]' in source
    start = source.index("identity_run = identity_runs.start_code_task_run(")
    assert 'token = ""' in source[start:start + 900]


def test_round2_e_no_grant_clones_as_the_audited_system_reader_never_the_shared_token(monkeypatch):
    """Integration round 2 (e): work that acts for nobody (a preview, a read tool, a system loop)
    used to fall back to the shared GitHub token. With egress on it now clones as BOSS's read-only
    system agent (an autonomous Passport, read-only ceilings, bound to Viewer and audited); under
    enforce without egress it is refused, never the shared token silently."""
    import tempfile

    from sqlalchemy import text

    from boss_engine.identity import runs

    from ._identity_agents_support import build_env

    env = build_env(monkeypatch)
    with env.db() as c:
        c.execute(text("INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
                       "scope_type, scope_id, granted_by) VALUES ('rbd_owner_t', 'org_boss', 'principal', :u, "
                       "'arl_owner', 'org', 'org_boss', 'test') ON CONFLICT DO NOTHING"), {"u": env.alice})
    env.world.grant(runs.SYSTEM_REPO_READER, runs.SYSTEM_GIT_SCOPE)
    monkeypatch.setenv("BOSS_EGRESS_URL", "https://egress.boss.internal")
    monkeypatch.setenv("BOSS_SHIM_DIR", tempfile.mkdtemp(prefix="bshim", dir="/tmp"))

    def never():
        raise AssertionError("the shared token was consulted")

    with runs.repo_access(env.engine, host="github", owner="acme", repo="web", project_id=env.project,
                          token=never) as access:
        assert access.via == "egress"
        with env.db() as c:
            row = c.execute(text("SELECT subject_principal_id, run_id FROM identity_run_credentials "
                                 "WHERE revoked_at IS NULL")).first()
        assert row[0] == runs.SYSTEM_REPO_READER and row[1].startswith("run_sys:git:")
    with env.db() as c:
        ceiling = set(c.execute(text("SELECT permission_key FROM identity_agent_ceilings WHERE agent_id = :a"),
                                {"a": runs.SYSTEM_REPO_READER}).scalars())
    audited = [e for e in env.world.events
               if e.event_type == "role_binding.added" and e.target_id == runs.SYSTEM_REPO_READER]
    assert ceiling == set(runs.SYSTEM_GIT_SCOPE) and len(audited) == 1
    # enforce, no egress: refused
    from boss_engine.config import get_settings

    monkeypatch.delenv("BOSS_EGRESS_URL")
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "enforce")
    monkeypatch.setattr(get_settings(), "authz_mode", "enforce")
    with pytest.raises(runs.NoAuthority):
        with runs.repo_access(env.engine, host="github", owner="acme", repo="web", project_id=env.project,
                              token=never):
            pass
