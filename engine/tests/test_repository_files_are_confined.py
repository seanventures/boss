"""BOSS-481: own marker files only; no credentials, providers or process inspection."""
import os

import pytest

from boss_engine.services import repo_files
from boss_engine.services.code_agent import NativeCodeAgent, build_repo_context, load_repo_guide
from boss_engine.services.code_agent_tools import SandboxToolset
from boss_engine.services.repo_files import MAX_FILE_BYTES, RepoFileRefused, RepoFiles
from boss_engine.services.sandbox import DiffSummary, EphemeralSandbox


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("INSIDE_MARKER = 1\n")
    (tmp_path / "outside.txt").write_text("OUTSIDE_MARKER\n")
    return root


@pytest.mark.parametrize("name", ["README.md", "AGENTS.md", "src/linked.py"])
def test_context_and_guides_never_follow_a_repository_symlink(repo, name):
    (repo / name).symlink_to(repo.parent / "outside.txt")
    with pytest.raises(RepoFileRefused):
        build_repo_context(repo)
    if name == "AGENTS.md":
        with pytest.raises(RepoFileRefused):
            load_repo_guide(repo)


@pytest.mark.parametrize("path", [".", "src", "src/link.py"])
def test_read_only_grep_never_returns_a_link_target(repo, path):
    (repo / "src" / "link.py").symlink_to(repo.parent / "outside.txt")
    result = SandboxToolset(repo, read_only=True).call("grep", {"pattern": "MARKER", "path": path})
    assert result.is_error
    assert "OUTSIDE_MARKER" not in result.text


def test_symlink_directory_is_refused_before_descent(repo):
    (repo / "escape").symlink_to(repo.parent, target_is_directory=True)
    with RepoFiles(repo) as files:
        with pytest.raises(RepoFileRefused):
            files.read_text("escape/outside.txt")
        with pytest.raises(RepoFileRefused):
            list(files.walk())


def test_root_descriptor_survives_a_root_path_replacement(repo):
    with RepoFiles(repo) as files:
        repo.rename(repo.parent / "old-root")
        repo.symlink_to(repo.parent, target_is_directory=True)
        assert "INSIDE_MARKER" in files.read_text("src/app.py")
        with pytest.raises(FileNotFoundError):
            files.read_text("outside.txt")
    with pytest.raises(RepoFileRefused):
        RepoFiles(repo)


@pytest.mark.parametrize("operation", ["read", "write"])
def test_file_swap_between_parent_open_and_file_open_is_refused(repo, monkeypatch, operation):
    original = os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "app.py" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            (repo / "src" / "app.py").unlink()
            (repo / "src" / "app.py").symlink_to(repo.parent / "outside.txt")
        return original(path, flags, *args, **kwargs)

    with RepoFiles(repo) as files:
        monkeypatch.setattr(repo_files.os, "open", swapping_open)
        with pytest.raises(RepoFileRefused):
            if operation == "read":
                files.read_text("src/app.py")
            else:
                files.write_text("src/app.py", "MUST_NOT_WRITE")
    assert (repo.parent / "outside.txt").read_text() == "OUTSIDE_MARKER\n"


def test_ancestor_swap_after_open_cannot_redirect_the_read(repo, monkeypatch):
    original = os.open
    swapped = False
    (repo.parent / "app.py").write_text("OUTSIDE_MARKER\n")

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        fd = original(path, flags, *args, **kwargs)
        if path == "src" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            (repo / "src").rename(repo / "old-src")
            (repo / "src").symlink_to(repo.parent, target_is_directory=True)
        return fd

    with RepoFiles(repo) as files:
        monkeypatch.setattr(repo_files.os, "open", swapping_open)
        assert "INSIDE_MARKER" in files.read_text("src/app.py")


@pytest.mark.parametrize("path", [".git/config", "src/../.git/config", "../outside.txt", "/outside.txt"])
def test_metadata_and_escape_paths_are_never_read_or_written(repo, path):
    with RepoFiles(repo) as files:
        with pytest.raises(RepoFileRefused):
            files.read_text(path)
        with pytest.raises(RepoFileRefused):
            files.write_text(path, "BAD")


def test_fifo_and_hardlink_are_refused_without_blocking(repo):
    os.mkfifo(repo / "pipe")
    os.link(repo.parent / "outside.txt", repo / "hardlink")
    with RepoFiles(repo) as files:
        for name in ("pipe", "hardlink"):
            with pytest.raises(RepoFileRefused):
                files.read_text(name)
            with pytest.raises(RepoFileRefused):
                files.write_text(name, "BAD")
    assert (repo.parent / "outside.txt").read_text() == "OUTSIDE_MARKER\n"


def test_bounds_apply_before_reading_the_whole_file(repo):
    with (repo / "large.md").open("wb") as stream:
        stream.truncate(MAX_FILE_BYTES + 1)
    with RepoFiles(repo) as files:
        with pytest.raises(RepoFileRefused, match="exceeds"):
            files.read_text("large.md")
        assert len(files.read_text("large.md", max_bytes=16, truncate=True)) < 100


def test_healthy_context_search_and_native_edits_still_work(repo):
    assert "INSIDE_MARKER" in build_repo_context(repo)
    tools = SandboxToolset(repo)
    assert "src/app.py:1:" in tools.grep("INSIDE_MARKER")
    assert "src/app.py:1:" in tools.grep("INSIDE_MARKER", path="src/app.py")
    tools.write_file("new/deep.py", "old\n")
    tools.edit_file("new/deep.py", "old", "new")
    assert (repo / "new" / "deep.py").read_text() == "new\n"
    NativeCodeAgent._apply_files(None, repo, [{"path": "generated.py", "content": "ok\n"}])
    assert (repo / "generated.py").read_text() == "ok\n"


def test_diff_export_refuses_link_targets_and_accepts_regular_text(repo, monkeypatch):
    from types import SimpleNamespace

    sandbox = EphemeralSandbox(SimpleNamespace(), task_id="owned-test")
    sandbox.path = repo
    monkeypatch.setattr(sandbox, "diff_summary", lambda: DiffSummary(1, 1, 0, ["src/app.py"]))
    assert sandbox.diff()[0].content == "INSIDE_MARKER = 1\n"
    (repo / "src" / "app.py").unlink()
    (repo / "src" / "app.py").symlink_to(repo.parent / "outside.txt")
    with pytest.raises(RepoFileRefused):
        sandbox.diff()


def test_context_scan_has_an_aggregate_budget(repo, monkeypatch):
    from boss_engine.services import code_agent

    monkeypatch.setattr(code_agent, "MAX_SCAN_BYTES", 1)
    with pytest.raises(RepoFileRefused, match="read budget"):
        build_repo_context(repo)


def test_deep_traversal_is_refused_before_opening_components(repo):
    with RepoFiles(repo) as files:
        with pytest.raises(RepoFileRefused, match="traversal limit"):
            files.read_text("/".join(["deep"] * 129))
