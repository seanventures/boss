"""A build that deletes a file deletes it on the change branch, on every build path
(dry run 2, gap 1).

THE INCIDENT (northgate-gateway, local repository, 2026-11-21). A code task said "Delete
`config/tenants/lumen-pay.yaml`". The agent deleted it, the acceptance check read the sandbox
tree and reported 4/4 met, and the change branch still had the file: `EphemeralSandbox.diff()`
skipped a removed path (`except FileNotFoundError: continue  # absent/deleted`), so no commit
path ever heard of the deletion.

Fixed as a class: the sandbox lists its deleted paths (`deleted_paths`), the micro-VM guest
returns them (`deleted`), every commit path writes them as deletions (`with_deletions` →
`commit_change`, content None), a per-file host deletes through `delete_file`, a retry's saved
workspace keeps them, and the PR path reads the committed branch back (`branch_mismatches`)
so a "met" is true of the branch, not only of the tree.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from boss_engine.services import governed_code_task_service as governed
from boss_engine.services import local_repo, sandbox
from boss_engine.services.sandbox_provider import code_agent_result
from boss_engine.services.software_factory import (
    branch_mismatches,
    commit_change,
    with_deletions,
)

ROOT = Path(__file__).resolve().parents[2]
GUEST = ROOT / "sandbox-host" / "guest" / "in_guest_agent.py"
GONE = "config/tenants/lumen-pay.yaml"
FILES = {GONE: "tenant: lumen-pay\n", "config/tenants/orbit.yaml": "tenant: orbit\n",
         "README.md": "# gateway\n"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=cwd, capture_output=True, text=True, check=True).stdout


def _bare(tmp_path: Path) -> Path:
    bare = local_repo.init_bare(tmp_path / "repo.git")
    local_repo.commit_files(bare, "main", FILES, "base")
    return bare


class _LocalHost:
    """The local repository host's two calls (LocalRepoHost.commit_files / read_text_file)."""

    def __init__(self, bare: Path):
        self.bare = bare

    def commit_files(self, branch, files, message):
        local_repo.ensure_branch(self.bare, branch, "main")
        return local_repo.commit_files(self.bare, branch, files, message)

    def read_text_file(self, branch, path):
        return local_repo.read_file(self.bare, branch, path)


class _PerFileHost:
    """A host with no batch commit (GitHub's contents API): one call per file."""

    def __init__(self, files: dict):
        self.files = dict(files)
        self.calls: list = []

    def upsert_file(self, branch, path, content, message):
        self.calls.append(("put", path))
        self.files[path] = content
        return "sha-put"

    def delete_file(self, branch, path, message):
        self.calls.append(("delete", path))
        return "sha-del" if self.files.pop(path, None) is not None else None

    def read_text_file(self, branch, path):
        return self.files.get(path)


# ── the local sandbox: the tree's deletion is listed and reaches the branch ─────────────────────

def _sandbox_tree(tmp_path: Path) -> sandbox.EphemeralSandbox:
    tree = tmp_path / "tree"
    tree.mkdir()
    _git(tree, "init", "-q", "-b", "main")
    for path, text in FILES.items():
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        (tree / path).write_text(text)
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", "base")
    box = sandbox.EphemeralSandbox(SimpleNamespace(sandbox_validation_timeout_seconds=60,
                                                   sandbox_validation_output_limit_chars=10000),
                                   task_id="crun_delete")
    box.path = tree
    box.diff_base_sha = _git(tree, "rev-parse", "HEAD").strip()
    return box


def test_a_local_build_that_deletes_a_file_deletes_it_on_the_branch(tmp_path):
    box = _sandbox_tree(tmp_path)
    (box.path / GONE).unlink()
    (box.path / "README.md").write_text("# gateway\n\nTenants: orbit.\n")

    files, gone = box.diff(), box.deleted_paths()
    assert [f.path for f in files] == ["README.md"]          # text changes only, as before
    assert gone == [GONE]                                      # the deletion is no longer lost

    bare = _bare(tmp_path)
    host = _LocalHost(bare)
    changes = with_deletions(files, gone)
    commit_change(host, "boss/code/crun_delete", changes, "remove the tenant file")

    assert local_repo.read_file(bare, "boss/code/crun_delete", GONE) is None
    assert local_repo.read_file(bare, "boss/code/crun_delete", "config/tenants/orbit.yaml")
    assert branch_mismatches(host, "boss/code/crun_delete", changes) == []


def test_build_output_deleted_from_the_tree_is_not_the_work(tmp_path):
    box = _sandbox_tree(tmp_path)
    (box.path / "node_modules").mkdir()
    (box.path / "node_modules" / "x.js").write_text("x\n")
    _git(box.path, "add", "-f", "node_modules/x.js")
    _git(box.path, "commit", "-qm", "vendored")
    box.diff_base_sha = _git(box.path, "rev-parse", "HEAD").strip()
    (box.path / "node_modules" / "x.js").unlink()
    assert box.deleted_paths() == []


# ── GitHub: a per-file host deletes through delete_file; a host that cannot delete says so ──────

def test_a_github_build_that_deletes_a_file_deletes_it_on_the_branch():
    host = _PerFileHost(FILES)
    changes = with_deletions([SimpleNamespace(path="README.md", content="# new\n")], [GONE])
    commit_change(host, "boss/code/crun_gh", changes, "remove the tenant file")
    assert ("delete", GONE) in host.calls
    assert host.read_text_file("boss/code/crun_gh", GONE) is None
    assert branch_mismatches(host, "boss/code/crun_gh", changes) == []


def test_a_host_that_cannot_delete_refuses_in_words_never_reports_done():
    class _NoDelete(_PerFileHost):
        delete_file = None

    with pytest.raises(RuntimeError, match="cannot delete"):
        commit_change(_NoDelete(FILES), "b", [(GONE, None)], "remove")


def test_the_github_adapter_deletes_with_the_blob_sha(monkeypatch):
    from boss_engine.services import github_adapter

    adapter = github_adapter.GitHubAdapter.__new__(github_adapter.GitHubAdapter)
    sent: list = []
    monkeypatch.setattr(github_adapter.GitHubAdapter, "repository_full_name", "o/r",
                        raising=False)
    monkeypatch.setattr(adapter, "_request", lambda method, path, *a, **k: {"sha": "blob1"},
                        raising=False)
    monkeypatch.setattr(adapter, "_headers", lambda: {}, raising=False)
    monkeypatch.setattr(github_adapter.GitHubAdapter, "api_base_url", "https://api.x",
                        raising=False)

    def _send(method, url, *, headers, json=None):
        sent.append((method, url, json))
        return SimpleNamespace(json=lambda: {"commit": {"sha": "c1"}})

    monkeypatch.setattr(adapter, "_send", _send, raising=False)
    assert adapter.delete_file("work", GONE, "remove") == "c1"
    method, url, body = sent[0]
    assert method == "DELETE" and url.endswith(f"/repos/o/r/contents/{GONE}")
    assert body["sha"] == "blob1" and body["branch"] == "work"


# ── micro-VM: the guest returns its deletions, they cross back and reach the branch ──────────────

def test_a_microvm_build_that_deletes_a_file_deletes_it_on_the_branch(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(f"in_guest_agent_{uuid4().hex}", GUEST)
    guest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guest)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    for path, text in FILES.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(text)
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    monkeypatch.setattr(guest, "WORK", work)
    (work / GONE).unlink()

    files, dsum = guest._diff()
    assert dsum["deleted"] == [GONE]

    res = code_agent_result({"diff": files, "diff_summary": dsum, "deleted": dsum["deleted"],
                             "tests": [], "summary": "removed it"})
    assert res.deleted == [GONE]

    bare = _bare(tmp_path)
    host = _LocalHost(bare)
    commit_change(host, "boss/code/crun_vm", with_deletions(res.diff, res.deleted), "stage")
    assert local_repo.read_file(bare, "boss/code/crun_vm", GONE) is None


# ── the committed branch is what was judged ─────────────────────────────────────────────────────

def test_a_branch_still_holding_a_deleted_file_is_a_mismatch(tmp_path):
    bare = _bare(tmp_path)
    host = _LocalHost(bare)
    local_repo.ensure_branch(bare, "work", "main")
    # The tree deleted it, the branch never heard: exactly the dry run.
    assert branch_mismatches(host, "work", [(GONE, None)]) == [
        {"path": GONE, "why": "deleted in the build, still on the branch"}]
    assert branch_mismatches(host, "work", [("README.md", "# other\n")])[0]["why"] == (
        "the branch holds other content")


# ── a retry continues without the file ─────────────────────────────────────────────────────────

def test_a_saved_workspace_keeps_the_deletion_and_the_retry_starts_without_the_file(tmp_path):
    run = {"finalize_ctx": {"saved_workspace": {"files": [
        {"path": "README.md", "content": "# new\n"}, {"path": GONE, "deleted": True}]}}}
    saved = governed.saved_workspace_files(run)
    assert {"path": GONE, "deleted": True} in saved
    (tmp_path / "config" / "tenants").mkdir(parents=True)
    (tmp_path / GONE).write_text("tenant: lumen-pay\n")
    governed.seed_workspace(tmp_path, saved)
    assert not (tmp_path / GONE).exists()
    assert (tmp_path / "README.md").read_text() == "# new\n"
