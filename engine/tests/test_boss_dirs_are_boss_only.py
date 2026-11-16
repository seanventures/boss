"""BOSS's shared clones, indexes and caches are BOSS's alone (BOSSAPP-1325).

A code task's agent runs as a pool uid with no groups. Anything BOSS wrote with the default umask
(0755) was readable by it: crun_bbf93235's planner read another project's clone in
/tmp/boss-kg-index. In a disposable container built from the image, before this change, a pool
uid could read the KG index clone, the chat tools' repo cache, the agent shell's workspaces,
the unisolated sandboxes, the shared npm cache and the read-only import mount; it could not read
another run's directory (<uid>:boss 2770), the repo mirrors (/var/lib/boss/repos 0700), BOSS's
home or the identity keys. engine/tests/helpers/sandbox_escape_probe.py is that check, run as
real uids through the real helper; these tests hold the rules on any machine.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from boss_engine.services import private_dirs

ENGINE = Path(__file__).resolve().parents[1] / "boss_engine"


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.fixture()
def private_root(tmp_path, monkeypatch):
    root = tmp_path / "private"
    monkeypatch.setenv(private_dirs.PRIVATE_ROOT_ENV, str(root))
    return root


def test_a_private_dir_is_owner_only_whatever_the_umask(private_root):
    old = os.umask(0o022)
    try:
        d = private_dirs.private_dir("kg-index")
    finally:
        os.umask(old)
    assert d == private_root / "kg-index"
    assert _mode(d) == 0o700 and _mode(private_root) == 0o700
    loose = private_root / "loose"
    loose.mkdir(mode=0o755)
    os.chmod(loose, 0o755)
    private_dirs.ensure_private(loose)
    assert _mode(loose) == 0o700 and private_dirs.is_private(loose)


def test_the_kg_index_is_not_in_tmp_and_is_owner_only(private_root):
    import tempfile

    from boss_engine.services.knowledge.repo_index import kg_index_root
    from boss_engine.services.project_files import _kg_root

    root = kg_index_root()
    assert root == private_root / "kg-index" and private_dirs.is_private(root)
    assert not str(root).startswith(str(Path(tempfile.gettempdir()) / "boss-kg-index"))
    assert _kg_root(object()) == root
    configured = private_root.parent / "elsewhere"
    assert private_dirs.is_private(kg_index_root(str(configured)))


def test_the_chat_repo_cache_is_owner_only(private_root):
    from boss_engine.tools import builtin

    cache = Path(builtin._repo_cache())                       # noqa: SLF001
    assert cache == private_root / "repo-cache" and private_dirs.is_private(cache)


def test_the_npm_cache_is_boss_only_and_the_browsers_stay_readable(tmp_path, monkeypatch):
    from boss_engine.services import sandbox

    monkeypatch.setattr(sandbox, "_TOOL_CACHE_ROOT", str(tmp_path / "tool-cache"))
    sandbox._shared_tool_caches()                              # noqa: SLF001
    assert _mode(tmp_path / "tool-cache") == 0o711
    assert _mode(tmp_path / "tool-cache" / "npm") == 0o700
    assert _mode(tmp_path / "tool-cache" / "playwright") == 0o755


def test_the_unisolated_sandbox_root_is_owner_only(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from boss_engine.services import sandbox

    monkeypatch.setattr(sandbox.tempfile, "gettempdir", lambda: str(tmp_path))
    box = object.__new__(sandbox.EphemeralSandbox)
    box.settings = SimpleNamespace(sandbox_root_path=None)
    root = box._root()                                         # noqa: SLF001
    assert root == tmp_path / "boss-sandboxes" and private_dirs.is_private(root)


#: Fixed /tmp homes BOSS still names, each owner-only where it is created or only a fallback
#: on a machine with no install root. A new one must be added here with its reason.
TMP_HOMES = {
    "identity/runs.py": "boss-shim: created 0700 (shim sockets, adopted per uid)",
    "extensions/registry.py": "fallback only: installs keep extensions under artifact_root (0750)",
    "services/self_fix_build.py": "fallback only: installs use artifact_root/self-fix (0750)",
    "services/sandbox.py": "boss-sandboxes: ensure_private at creation",
    "services/project_files.py": "boss-sandboxes: the same root, listed for deletion",
    "services/preview_service.py": "boss-previews: chmod 0700 at creation (_preview_root)",
    "services/self_observability.py": "boss-previews: the same root, read for disk usage",
    "services/agent_shell.py": "boss-agent-ws: ensure_private at creation",
    "services/ssh_environment.py": "a file on a REMOTE host's /tmp, not this machine",
}


def test_no_new_tmp_home_for_project_files():
    found = set()
    for p in ENGINE.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        if re.search(r"\"/tmp/boss-|gettempdir\(\)\) / \"boss-|gettempdir\(\), \"boss-", text):
            found.add(p.relative_to(ENGINE).as_posix())
    assert found <= set(TMP_HOMES), sorted(found - set(TMP_HOMES))
    assert "services/knowledge/repo_index.py" not in found
    assert "tools/builtin.py" not in found


def test_the_image_and_compose_keep_imports_and_the_self_source_private():
    root = ENGINE.parents[1]
    docker = (root / "Dockerfile.community").read_text(encoding="utf-8")
    assert "chown -R boss:boss /var/lib/boss/private && chmod 700 /var/lib/boss/private" in docker
    compose = (root / "scripts/install/community-compose.yml").read_text(encoding="utf-8")
    assert ":/var/lib/boss/import" not in compose and ":/var/lib/boss/selfsrc" not in compose
    assert "../../.boss/import:/var/lib/boss/private/import:ro" in compose


# ── the reviewer's copy has no way back into the live tree; BOSS's git runs no tree hook ────

class _WritingSeat:
    """A review seat that writes everything it can reach, as a misbehaving model would."""

    from boss_engine.services.engine_contract import EngineCapabilities as _Caps

    engine = "shelly"
    capabilities = _Caps(read_only=True, read_only_shell=True)

    def __init__(self):
        self.ran_in = None

    def run_role(self, prompt, cwd, *, read_only=False, **_):
        self.ran_in = Path(cwd)
        hooks = Path(cwd) / ".git" / "hooks"
        if hooks.exists():
            (hooks / "pre-commit").write_text("#!/bin/sh\ntouch /tmp/pwned\n")
        if (Path(cwd) / ".git" / "config").exists():
            with open(Path(cwd) / ".git" / "config", "a") as fh:
                fh.write("[core]\n\tfsmonitor = /tmp/evil\n")
        return {"summary": "VERDICT: SURVIVED", "usage": {}, "cost_usd_micros": 0, "turns": 1}

    def run(self, spec, repo, sandbox):  # pragma: no cover
        raise NotImplementedError


def test_a_review_seat_cannot_write_the_live_git_through_its_copy(tmp_path):
    import subprocess

    from boss_engine.services.engine_contract import ContractedAgent

    live = tmp_path / "live"
    live.mkdir()
    (live / "app.py").write_text("print(1)\n")
    subprocess.run(["git", "init", "-q", str(live)], check=True)
    config_before = (live / ".git" / "config").read_text()
    writable = live / "node_modules"
    writable.mkdir()
    os.chmod(writable, 0o777)                                  # a run's uid wrote it
    seat = _WritingSeat()
    ContractedAgent(seat).run_role("judge", live, read_only=True)
    assert not (live / ".git" / "hooks" / "pre-commit").exists()
    assert (live / ".git" / "config").read_text() == config_before
    assert seat.ran_in is not None and seat.ran_in != live


def test_node_modules_is_linked_only_when_a_seat_cannot_write_it(tmp_path):
    from boss_engine.services import engine_contract as ec

    live = tmp_path / "live"
    (live / "node_modules").mkdir(parents=True)
    os.chmod(live / "node_modules", 0o755)
    tree = tmp_path / "copy"
    tree.mkdir()
    ec._link_heavy(tree, live)                                 # noqa: SLF001
    assert (tree / "node_modules").is_symlink()
    os.chmod(live / "node_modules", 0o777)
    tree2 = tmp_path / "copy2"
    tree2.mkdir()
    ec._link_heavy(tree2, live)                                # noqa: SLF001
    assert not (tree2 / "node_modules").exists()


def test_bosss_git_in_a_workspace_runs_no_hook_the_tree_planted(tmp_path):
    import subprocess
    from types import SimpleNamespace

    from boss_engine.services import sandbox

    repo = tmp_path / "ws"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    marker = tmp_path / "hook-ran"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    (repo / "a.txt").write_text("a\n")
    box = object.__new__(sandbox.EphemeralSandbox)
    box.path = repo
    box._provisioning = False                                  # noqa: SLF001
    box._sandbox = None                                        # noqa: SLF001
    box.settings = SimpleNamespace(sandbox_validation_timeout_seconds=30)
    box._git_once(["add", "-A"])                               # noqa: SLF001
    box._git_once(["-c", "user.name=b", "-c", "user.email=b@x", "commit", "-qm", "x"])  # noqa: SLF001
    assert not marker.exists(), "a hook planted in the workspace ran inside BOSS's git"


# ── BOSSAPP-1327: a root BOSS cannot write is no root; a mode BOSS cannot set costs nothing ──

def test_a_private_root_boss_cannot_write_falls_back_to_its_own(tmp_path, monkeypatch):
    """New compose on an older image: Docker makes /var/lib/boss/private (the mount targets'
    parent) owned by root, so BOSS can neither chmod nor write it."""
    monkeypatch.delenv(private_dirs.PRIVATE_ROOT_ENV, raising=False)
    locked = tmp_path / "var-lib-boss-private"
    (locked / "import").mkdir(parents=True)
    os.chmod(locked, 0o555)                                    # exists, not writable by BOSS
    monkeypatch.setattr(private_dirs, "DEFAULT_PRIVATE_ROOT", str(locked))
    monkeypatch.setattr(private_dirs.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    real = os.chmod

    def not_the_owner(path, mode, *a, **k):                  # root owns it: chmod is refused
        if Path(path) == locked:
            raise PermissionError(1, "Operation not permitted")
        return real(path, mode, *a, **k)

    monkeypatch.setattr(private_dirs.os, "chmod", not_the_owner)
    try:
        d = private_dirs.private_dir("kg-index")               # raised PermissionError before
        assert d == tmp_path / "tmp" / f"boss-private-{os.getuid()}" / "kg-index"
        assert private_dirs.is_private(d)
    finally:
        real(locked, 0o755)


def test_a_chmod_the_cache_cannot_take_keeps_the_browsers(tmp_path, monkeypatch):
    from boss_engine.services import sandbox

    monkeypatch.setattr(sandbox, "_TOOL_CACHE_ROOT", str(tmp_path / "tool-cache"))
    real = os.chmod

    def refuse(path, mode, *a, **k):
        if str(path).startswith(str(tmp_path / "tool-cache")):
            raise PermissionError(1, "Operation not permitted")
        return real(path, mode, *a, **k)

    monkeypatch.setattr(os, "chmod", refuse)
    env = sandbox._shared_tool_caches()                        # noqa: SLF001
    assert env.get("PLAYWRIGHT_BROWSERS_PATH"), env


def test_the_escape_probe_reads_content_not_metadata():
    import inspect

    from tests.helpers import sandbox_escape_probe as probe

    body = inspect.getsource(probe.can_read)
    assert "head -c1" in body and "cat '" not in body


def test_bosss_own_files_in_tmp_are_owner_only(tmp_path, monkeypatch):
    import subprocess

    from apps.api.app import local_supervisor

    monkeypatch.setenv("BOSS_LOG_DIR", str(tmp_path / "boss-stack"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: object())
    old = os.umask(0o022)
    try:
        local_supervisor._spawn()                              # noqa: SLF001
    finally:
        os.umask(old)
    assert _mode(tmp_path / "boss-stack") == 0o700
    assert _mode(tmp_path / "boss-stack" / "worker.log") == 0o600
    root = ENGINE.parents[1]
    for rel in ("engine/boss_engine/infra_plugins/caddy.py",
                "engine/boss_engine/services/ssh_environment.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "umask 077" in text, rel
