"""FIX A GIVEN FILE: the original file is the change's first commit (workflow-library DESIGN §4).

A validation build commits the input's bytes to the target path before any fix, so the change's
history reads original → fixed and "Revert to original" is a plain file revert. A retry whose
branch already carries its own target keeps it; an in-place fix commits nothing extra.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.services import input_fidelity, local_repo

pytestmark = pytest.mark.skipif(not local_repo.git_available(), reason="git is not installed")

ORIGINAL = "tenant: lumen-pay\nroutes:\n  - path: /Payouts_Admin\n"
FIXED = "tenant: lumen-pay\nroutes:\n  - path: /payouts-admin\n"


class Host:
    """The local repo host's git calls, on a bare repo in tmp."""

    def __init__(self, git_dir: Path):
        self.git_dir = git_dir

    def ensure_branch(self, branch, base=None):
        return local_repo.ensure_branch(self.git_dir, branch, base)

    def commit_files(self, branch, files, message):
        local_repo.ensure_branch(self.git_dir, branch)
        return local_repo.commit_files(self.git_dir, branch, files, message)

    def read_text_file(self, branch, path):
        return local_repo.read_file(self.git_dir, branch, path)


@pytest.fixture()
def host(tmp_path: Path) -> Host:
    git_dir = local_repo.init_bare(tmp_path / "northgate.git")
    local_repo.commit_files(git_dir, "main", {"README.md": "Northgate\n",
                                              "examples/lumen-pay.bad.yaml": ORIGINAL}, "init")
    return Host(git_dir)


def contract(**extra) -> dict:
    return {"input": "examples/lumen-pay.bad.yaml", "output": "config/tenants/lumen-pay.yaml",
            "_input_text": ORIGINAL, "_output_base": None, **extra}


def test_the_input_bytes_land_on_the_target_as_the_first_commit_then_the_fix_reads_original_to_fixed(host: Host):
    c = contract()
    sha = input_fidelity.commit_original(host, "boss/code/crun_1", "main", c)
    assert sha and c["original_commit"] == sha
    assert host.read_text_file("boss/code/crun_1", "config/tenants/lumen-pay.yaml") == ORIGINAL
    host.commit_files("boss/code/crun_1", {"config/tenants/lumen-pay.yaml": FIXED}, "governed: implement")
    commits = local_repo.commits_between(host.git_dir, "main", "boss/code/crun_1")
    # Newest first: the fix, then the original as given.
    assert [m["message"].splitlines()[0] for m in commits][-1].startswith(input_fidelity.ORIGINAL_COMMIT_PREFIX)
    assert commits[-1]["sha"] == sha
    d = local_repo.diff(host.git_dir, sha, "boss/code/crun_1")
    assert "-  - path: /Payouts_Admin" in d and "+  - path: /payouts-admin" in d


def test_a_branch_that_already_carries_its_own_target_is_left_alone(host: Host):
    local_repo.ensure_branch(host.git_dir, "boss/code/crun_1", "main")
    host.commit_files("boss/code/crun_1", {"config/tenants/lumen-pay.yaml": FIXED}, "earlier attempt")
    c = contract()
    assert input_fidelity.commit_original(host, "boss/code/crun_1", "main", c) is None
    assert "original_commit" not in c
    assert host.read_text_file("boss/code/crun_1", "config/tenants/lumen-pay.yaml") == FIXED


def test_an_in_place_fix_or_a_missing_input_commits_nothing(host: Host):
    same = contract(output="examples/lumen-pay.bad.yaml")
    assert input_fidelity.commit_original(host, "boss/code/crun_2", "main", same) is None
    assert input_fidelity.commit_original(host, "boss/code/crun_2", "main", contract(_input_text=None)) is None
    assert local_repo.branch_sha(host.git_dir, "boss/code/crun_2") is None


def test_a_host_that_fails_never_fails_the_build():
    class Broken:
        def read_text_file(self, *_):
            return None

        def ensure_branch(self, *_):
            raise RuntimeError("offline")

    assert input_fidelity.commit_original(Broken(), "b", "main", contract()) is None
