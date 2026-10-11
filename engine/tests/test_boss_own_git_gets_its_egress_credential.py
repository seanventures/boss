"""BOSS's OWN git gets its egress credential; the sandbox's git asks the shim socket.

The defect (App ▸ Security ▸ Scan now, every GitHub-built app): the scan clones the app as BOSS
(`owned_process.boss_run`) INSIDE the scan's bound sandbox. Through egress, `repo_access` handed
it the environment whose credential helper asks the run's shim socket — and that socket belongs
to the run's sandbox uid and answers nobody else. git got no credential and egress answered 401.

The class: a `GitAccess` has two environments. `environ()` for git that runs as the run's
sandbox uid; `boss_environ()` for BOSS's own git, where the credential rides in that git's own
process environment. Every BOSS-side clone that runs as the control plane uses `boss_environ()`.
"""
from __future__ import annotations

import ast
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRED = "boss_egr_TESTCREDENTIALDONOTLEAK0000"


def _fill(env: dict) -> str:
    """What git's credential machinery answers for an egress clone URL, with this environment."""
    clean = {k: v for k, v in env.items() if not k.startswith("GIT_") or k.startswith("GIT_CONFIG")}
    clean.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                  "HOME": tempfile.mkdtemp(prefix="bgh")})
    done = subprocess.run(["git", "credential", "fill"], input="protocol=http\nhost=egress:8090\n"
                          "path=github/acme/web.git\n\n", capture_output=True, text=True, env=clean,
                          timeout=30)
    return done.stdout


def test_boss_own_git_is_answered_from_its_own_environment(monkeypatch):
    from boss_engine.identity import runs

    monkeypatch.setenv("BOSS_EGRESS_URL", "http://egress:8090")
    monkeypatch.setenv("BOSS_SHIM_DIR", tempfile.mkdtemp(prefix="bshim", dir="/tmp"))
    shim = runs.Shim(run_id="run_sys:git:test01", egress_credential=CRED)
    try:
        access = runs._egress_git_access(runs.egress_access(shim, owner="acme", repo="web"))  # noqa: SLF001
        assert access.via == "egress" and access.url == "http://egress:8090/github/acme/web.git"
        own = access.boss_environ()
        # BOSS's own git: the own helper, and the credential in ITS environment only
        assert own.get("BOSS_EGR_CREDENTIAL") == CRED
        assert f"password={CRED}" in _fill(own)
        # and nothing of the shim socket helper is left in it (it would answer only the sandbox uid)
        assert not any("socket" in v for k, v in own.items() if k.startswith("GIT_CONFIG_VALUE"))
        # The sandbox's git: never the credential in its environment, the socket helper instead
        sandbox = access.environ()
        assert "BOSS_EGR_CREDENTIAL" not in access.env
        assert CRED not in "".join(str(v) for v in access.env.values())
        assert any("socket" in v for k, v in sandbox.items() if k.startswith("GIT_CONFIG_VALUE"))
        # the url rewrite keeps github.com on the egress path in both
        assert "url.http://egress:8090/github/.insteadOf" in own.values()
    finally:
        runs.close_shim_socket(shim.run_id)


def test_legacy_token_access_is_the_same_for_both():
    from boss_engine.services import repo_host

    access = repo_host.git_access("github", "acme", "web", "ghs_SOMETOKEN000000000000000000000000")
    assert access.boss_environ({}) == access.environ({})


def _calls(node: ast.AST):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            yield sub


def _name(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")


def test_every_boss_side_clone_under_repo_access_uses_boss_environ():
    """Structural: inside a `with … repo_access(…) as a:` block, a git spawned as BOSS
    (`boss_run`, or `sandboxed=False`) must take `a.boss_environ()`, never `a.environ()`."""
    offenders: list[str] = []
    checked = 0
    for path in (ROOT / "engine" / "boss_engine").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            for item in node.items:
                ce = item.context_expr
                if not (isinstance(ce, ast.Call) and _name(ce) == "repo_access" and item.optional_vars is not None):
                    continue
                var = getattr(item.optional_vars, "id", None)
                for stmt in node.body:
                    for call in _calls(stmt):
                        as_boss = _name(call) == "boss_run" or any(
                            k.arg == "sandboxed" and isinstance(k.value, ast.Constant) and k.value.value is False
                            for k in call.keywords)
                        if not as_boss:
                            continue
                        checked += 1
                        for k in call.keywords:
                            if k.arg != "env":
                                continue
                            v = k.value
                            if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                                    and getattr(v.func.value, "id", None) == var and v.func.attr == "environ"):
                                offenders.append(f"{path.relative_to(ROOT)}:{call.lineno}")
    assert checked >= 4, "the structural check found none of the BOSS-side clones it guards"
    assert not offenders, "BOSS's own git given the sandbox's credential path: " + ", ".join(offenders)
