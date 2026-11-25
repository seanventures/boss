"""Setup's repository proof WRITES, over HTTP, to a GitHub that keeps what it is sent.

THE GAP (RCA B3, BOSSAPP-1433 R5). Setup's checks passed under every failure an enterprise host
met, because the proof was a read and its tests never wrote. The repository proof now writes (an
unreferenced blob: Contents: write) and asks to open a pull request (Pull requests: write), but
its tests answered `setup_checks.probe_repository` through an injected `http` function, so the
code that actually sends the request (default_http) never ran in a test.

Here the proof runs exactly as setup_actions calls it for a repository (`http` left to its
default) against the local GitHub the e2e gate uses (tests/e2e/gate/remote.py: the REST API
backed by a real bare repository). The write must land: the blob is in the repository's object
store afterwards. And it must stay a probe: no branch or ref moved, no pull request opened.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from boss_engine.services import setup_checks

GATE = Path(__file__).resolve().parents[2] / "tests" / "e2e" / "gate"


@pytest.fixture()
def local_github(tmp_path):
    if str(GATE) not in sys.path:
        sys.path.insert(0, str(GATE))
    from remote import LocalGitHub

    hub = LocalGitHub(tmp_path / "github").start()
    try:
        yield hub
    finally:
        hub.stop()


def _refs(bare: Path) -> str:
    return subprocess.run(["git", "--git-dir", str(bare), "for-each-ref"], capture_output=True,
                          text=True, check=True).stdout


def _blobs(bare: Path) -> set[str]:
    listed = subprocess.run(["git", "--git-dir", str(bare), "cat-file",
                             "--batch-all-objects", "--batch-check"],
                            capture_output=True, text=True, check=True).stdout
    return {line.split()[0] for line in listed.splitlines() if line.split()[1:2] == ["blob"]}


def _content(bare: Path, sha: str) -> str:
    return subprocess.run(["git", "--git-dir", str(bare), "cat-file", "-p", sha],
                          capture_output=True, text=True, check=True).stdout


def test_the_repository_proof_writes_a_real_object_and_moves_no_ref(local_github):
    bare = local_github.seed("acme", "app", {"README.md": "# app\n"})
    refs_before, blobs_before = _refs(bare), _blobs(bare)
    found = setup_checks.probe_repository("ghp_local_token", "acme/app",
                                          api_base=local_github.api_url)
    assert found["state"] == "working", found
    assert all(found["checks"][k]["ok"] for k in ("read", "write", "pull_requests")), found
    # THE WRITE HAPPENED: a new object in the repository, holding what the proof sent.
    written = _blobs(bare) - blobs_before
    assert [_content(bare, sha) for sha in written] == ["boss setup check"]
    # ...and nothing anyone would see: no branch, no pull request.
    assert _refs(bare) == refs_before
    assert not local_github.world.prs
    assert local_github.requests["api"] == 3


def test_a_github_that_cannot_take_the_write_is_not_reported_working(local_github):
    local_github.seed("acme", "app", {"README.md": "# app\n"})
    # The write (and only the write) gets GitHub's bad moment.
    local_github.blip("api", times=1, match="POST /api/repos/acme/app/git/blobs")
    found = setup_checks.probe_repository("ghp_local_token", "acme/app",
                                          api_base=local_github.api_url)
    assert found["checks"]["read"]["ok"] is True
    assert found["checks"]["write"]["ok"] is not True, found
    assert found["state"] != "working", found
