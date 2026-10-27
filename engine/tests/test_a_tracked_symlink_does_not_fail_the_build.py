"""A repository that tracks a symlink must not fail every walk of its tree.

crun_0cd67b0a (2026-10-27) died between write_tests and implement with
"'tests/e2e/stubs/ssh-only/ssh': symlink, special file or path outside the repository root
refused": the implement stage's repo-context pack (code_agent._pack) walked BOSS's own repo,
and RepoFiles.walk() refused the whole tree because one tracked symlink EXISTED. The class:
every build-path walker (context pack, glob, grep, list_dir, database detection) must list a
symlink/special entry as a leaf, never follow or descend it, and never abort because of it,
while READING one is still refused at the O_NOFOLLOW boundary.
"""
from __future__ import annotations

import os

import pytest

from boss_engine.services import build_services as bs
from boss_engine.services.code_agent import build_repo_context
from boss_engine.services.code_agent_tools import SandboxToolset
from boss_engine.services.repo_files import RepoFileRefused, RepoFiles


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tests/e2e/stubs/bin").mkdir(parents=True)
    (root / "tests/e2e/stubs/ssh-only").mkdir(parents=True)
    (root / "tests/e2e/stubs/bin/ssh").write_text("#!/bin/sh\necho INSIDE_STUB\n")
    (root / "src").mkdir()
    (root / "src/app.py").write_text("INSIDE_MARKER = 1\n")
    (root / "package.json").write_text('{"dependencies":{"pg":"8"}}')
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside/secret.py").write_text("OUTSIDE_MARKER = 1\n")
    # BOSS's real tracked link (in-repo target, no suffix) ...
    (root / "tests/e2e/stubs/ssh-only/ssh").symlink_to("../bin/ssh")
    # ... plus links whose targets escape the root: a file and a directory.
    (root / "src/escape.py").symlink_to(tmp_path / "outside/secret.py")
    (root / "src/escape_dir").symlink_to(tmp_path / "outside", target_is_directory=True)
    os.mkfifo(root / "src/pipe")
    return root


def test_walk_lists_links_as_leaves_and_never_descends(repo):
    with RepoFiles(repo) as files:
        walked = sorted(files.walk())
        regular = sorted(files.regular_files())
    assert "tests/e2e/stubs/ssh-only/ssh" in walked
    assert {"src/escape.py", "src/escape_dir", "src/pipe"} <= set(walked)
    assert not any(p.startswith("src/escape_dir/") for p in walked)
    assert regular == ["package.json", "src/app.py", "tests/e2e/stubs/bin/ssh"]


def test_context_pack_completes_without_reading_a_link(repo):
    context = build_repo_context(repo, task="update app")
    assert "INSIDE_MARKER" in context
    assert "OUTSIDE_MARKER" not in context


def test_agent_tools_search_and_list_past_a_link(repo):
    tools = SandboxToolset(repo, read_only=True)
    grep = tools.call("grep", {"pattern": "MARKER|STUB", "path": "."})
    assert not grep.is_error, grep.text
    assert "src/app.py:1:" in grep.text and "INSIDE_STUB" in grep.text
    assert "OUTSIDE_MARKER" not in grep.text
    glob = tools.call("glob", {"pattern": "**/ssh"})
    assert not glob.is_error and "tests/e2e/stubs/ssh-only/ssh" in glob.text
    listing = tools.call("list_dir", {"path": "src"})
    assert not listing.is_error
    assert "escape.py@" in listing.text and "escape_dir@" in listing.text
    assert "secret.py" not in listing.text


def test_database_detection_completes_past_unread_links(repo):
    # src/escape.py IS a name detection reads, so drop it: detection keeps refusing those.
    (repo / "src/escape.py").unlink()
    assert bs.detect_in_code(repo) == ["package.json depends on pg"]


@pytest.mark.parametrize("path", ["tests/e2e/stubs/ssh-only/ssh", "src/escape.py",
                                  "src/escape_dir/secret.py", "src/pipe"])
def test_reading_through_a_link_is_still_refused(repo, path):
    with RepoFiles(repo) as files:
        with pytest.raises(RepoFileRefused):
            files.read_text(path)
    tools = SandboxToolset(repo, read_only=True)
    for tool, args in (("read_file", {"path": path}), ("grep", {"pattern": ".", "path": path})):
        result = tools.call(tool, args)
        assert result.is_error
        assert "OUTSIDE_MARKER" not in result.text and "INSIDE_STUB" not in result.text
