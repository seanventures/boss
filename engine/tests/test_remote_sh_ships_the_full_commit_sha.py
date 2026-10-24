"""remote.sh writes the full 40-hex COMMIT sha to .boss-shipped-commit — never a short sha,
and never a tag's own object hash (BOSSAPP-604).

`git rev-parse --short <tag>`, for an ANNOTATED tag, names the TAG's own object, not the
commit it points at. Shipping a release by its tag (as rel/rc10.9 does) wrote the tag's hash
into the host's marker file, not the commit's — so later tools reading it (backup.sh,
upgrade checks) resolved the wrong, or an unresolvable, object. `^{commit}` dereferences the
tag; this pins that remote.sh's `ship_source` does so, and writes the full sha, not a short
one.

Runs `ship_source` alone, by taking the real script verbatim up to (not including) its
command dispatch and calling that function directly — the exact code every subcommand runs,
with none of install.sh/docker compose's weight. A stand-in `ssh` runs each "remote" command
locally, as the home-resolution test does.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REMOTE_SRC = (ROOT / "scripts" / "install" / "remote.sh").read_text()

FAKE_SSH = """#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|-i|-p|-l) shift 2 ;;
    -t|-T|-q|-N) shift ;;
    *) break ;;
  esac
done
shift  # the host
exec bash -c "$*"
"""


def _rig(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway git repo (REPO_ROOT) with one commit and an annotated tag on it, and a
    fake host directory `ship_source` targets."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "install").mkdir(parents=True)
    # The script verbatim, minus its command dispatch (the last `case "$CMD" in ... esac`):
    # `ship_source` is called directly instead, exactly as every subcommand calls it.
    before_dispatch = REMOTE_SRC[:REMOTE_SRC.index('case "$CMD" in')]
    (repo / "scripts" / "install" / "remote_test.sh").write_text(before_dispatch + "ship_source\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=repo, check=True)
    subprocess.run(["git", "tag", "-a", "v1", "-m", "release"], cwd=repo, check=True)
    host_dir = tmp_path / "host" / "boss"
    host_dir.mkdir(parents=True)
    return repo, host_dir


def _commit_sha(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "v1^{commit}"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _tag_object_sha(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "v1"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_ship_source_writes_the_full_commit_not_the_tags_own_object(tmp_path: Path) -> None:
    repo, host_dir = _rig(tmp_path)
    commit_sha = _commit_sha(repo)
    tag_sha = _tag_object_sha(repo)
    assert commit_sha != tag_sha, "an annotated tag's own object differs from its commit"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh_path = bin_dir / "ssh"
    ssh_path.write_text(FAKE_SSH)
    ssh_path.chmod(ssh_path.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["bash", str(repo / "scripts" / "install" / "remote_test.sh"), "install",
         "--host", "fake-host", "--dir", str(host_dir), "--ref", "v1"],
        cwd=repo, env=env, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr

    written = (host_dir / ".boss-shipped-commit").read_text().strip()
    assert written == commit_sha, f"wrote {written!r}, want the commit {commit_sha!r}"
    assert written != tag_sha, "wrote the tag's own object hash, not the commit it points at"
    assert len(written) == 40, "a short sha is ambiguous; the full 40-hex commit is unique"
