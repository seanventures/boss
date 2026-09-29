"""A public user who already has this tree can install it with no remote."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "install.sh"


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> None:
    _git("add", ".", cwd=repo)
    _git("-c", "user.name=BOSS installer test", "-c", "user.email=installer-test@example.invalid",
         "commit", "-qm", message, cwd=repo)


def _fake_tools(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  info) exit 0 ;;\n"
        "  compose)\n"
        "    if [ \"$2\" = version ]; then echo 'Docker Compose version v2'; exit 0; fi\n"
        "    exit 0 ;;\n"
        "esac\n"
        "exit 1\n"
    )
    docker.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "out=; code=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = -o ]; then out="$2"; shift 2; continue; fi\n'
        '  if [ "$1" = -w ]; then code=200; shift 2; continue; fi\n'
        "  shift\n"
        "done\n"
        'if [ -n "$out" ] && [ "$out" != /dev/null ]; then printf \'{"edition":"community"}\' > "$out"; fi\n'
        'if [ -n "$code" ]; then printf "%s" "$code"; fi\n'
    )
    curl.chmod(0o755)
    env = os.environ.copy()
    env.pop("BOSS_REPO_URL", None)
    env.pop("BOSS_REPO_REF", None)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env


def _install(script: Path, install: Path, env: dict[str, str], *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), "--dir", str(install), "--yes", "--skip-model-key",
         "--no-web", "--no-install-deps", *extra],
        env=env, text=True, capture_output=True, timeout=30, check=False,
    )


def test_checkout_ahead_of_origin_main_installs_without_rewinding(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    (upstream / "scripts/install").mkdir(parents=True)
    for name in ("community-compose.yml", "restore_preserved_env.sh", "reconcile_api_credentials.py"):
        source = REPO / "scripts/install" / name
        (upstream / "scripts/install" / name).write_bytes(source.read_bytes())
    (upstream / "version").write_text("old\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(upstream)], check=True)
    _commit(upstream, "old")
    install = tmp_path / "boss"
    subprocess.run(["git", "clone", "-q", str(upstream), str(install)], check=True)
    (install / "version").write_text("ahead\n")
    _commit(install, "ahead")
    ahead = _git("rev-parse", "HEAD", cwd=install)

    result = _install(INSTALLER, install, _fake_tools(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "already ahead of main" in result.stdout
    assert _git("rev-parse", "HEAD", cwd=install) == ahead
    assert "BOSS_API_KEY=" in (install / ".env").read_text()


def _diverged_pair(tmp_path: Path) -> tuple[Path, str]:
    upstream = tmp_path / "upstream"
    (upstream / "scripts/install").mkdir(parents=True)
    for name in ("community-compose.yml", "restore_preserved_env.sh", "reconcile_api_credentials.py"):
        (upstream / "scripts/install" / name).write_bytes((REPO / "scripts/install" / name).read_bytes())
    (upstream / "version").write_text("base\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(upstream)], check=True)
    _commit(upstream, "base")
    _git("checkout", "-q", "-b", "feature", cwd=upstream)
    (upstream / "version").write_text("feature\n")
    _commit(upstream, "feature")
    feature = _git("rev-parse", "HEAD", cwd=upstream)
    _git("checkout", "-q", "main", cwd=upstream)
    (upstream / "version").write_text("other-main\n")
    _commit(upstream, "other main")
    install = tmp_path / "boss"
    subprocess.run(["git", "clone", "-q", "--branch", "feature", str(upstream), str(install)], check=True)
    return install, feature


def test_diverged_default_branch_keeps_the_local_checkout(tmp_path: Path) -> None:
    install, feature = _diverged_pair(tmp_path)

    result = _install(INSTALLER, install, _fake_tools(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "has diverged from this checkout" in result.stdout
    assert _git("rev-parse", "HEAD", cwd=install) == feature


def test_explicit_diverged_ref_does_not_replace_the_checkout(tmp_path: Path) -> None:
    install, feature = _diverged_pair(tmp_path)

    result = _install(INSTALLER, install, _fake_tools(tmp_path), "--ref", "main")

    assert result.returncode != 0
    assert "cannot fast-forward" in result.stdout + result.stderr
    assert _git("rev-parse", "HEAD", cwd=install) == feature
    assert not (install / ".env").exists()


def test_unreachable_origin_uses_the_local_checkout(tmp_path: Path) -> None:
    install = tmp_path / "boss"
    (install / "scripts/install").mkdir(parents=True)
    for name in ("community-compose.yml", "restore_preserved_env.sh", "reconcile_api_credentials.py"):
        source = REPO / "scripts/install" / name
        target = install / "scripts/install" / name
        target.write_bytes(source.read_bytes())
    subprocess.run(["git", "init", "-q", "-b", "main", str(install)], check=True)
    _commit(install, "local only")
    head = _git("rev-parse", "HEAD", cwd=install)
    _git("remote", "add", "origin", str(tmp_path / "missing.git"), cwd=install)

    result = _install(INSTALLER, install, _fake_tools(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Could not fetch main" in result.stdout
    assert _git("rev-parse", "HEAD", cwd=install) == head


def test_empty_directory_copies_the_script_checkout_not_github(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "scripts/install").mkdir(parents=True)
    for name in ("community-compose.yml", "restore_preserved_env.sh", "reconcile_api_credentials.py"):
        source = REPO / "scripts/install" / name
        (tree / "scripts/install" / name).write_bytes(source.read_bytes())
    (tree / "install.sh").write_bytes(INSTALLER.read_bytes())
    (tree / "install.sh").chmod(0o755)
    (tree / "marker").write_text("local-tree\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(tree)], check=True)
    _commit(tree, "local tree")
    head = _git("rev-parse", "HEAD", cwd=tree)
    empty = tmp_path / "empty"
    empty.mkdir()

    result = _install(tree / "install.sh", empty, _fake_tools(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Copying this checkout" in result.stdout
    assert "github.com" not in result.stdout.split("Copying this checkout", 1)[0]
    assert _git("rev-parse", "HEAD", cwd=empty) == head
    assert (empty / "marker").read_text() == "local-tree\n"
    origin = _git("remote", "get-url", "origin", cwd=empty)
    assert Path(origin).resolve() == tree.resolve()
