"""The public sync guard refuses to replace public work the private source does not have.

Each test builds a private and a public repository on disk, the way the real pair relates: the
public repository receives sync commits that name their private source, and collaborators may
push to it in between.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "community_public_guard.py"
spec = importlib.util.spec_from_file_location("community_public_guard", MODULE_PATH)
assert spec and spec.loader
guard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = guard  # dataclasses resolve their module while the class is built
spec.loader.exec_module(guard)


def run(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         *args],
        capture_output=True, text=True, check=True)
    return result.stdout.strip()


def commit(repo: Path, name: str, message: str) -> str:
    (repo / name).write_text(f"{name}\n{message}\n")
    run(repo, "add", name)
    run(repo, "commit", "-q", "-m", message)
    return run(repo, "rev-parse", "HEAD")


@pytest.fixture()
def pair(tmp_path: Path):
    private, public = tmp_path / "private", tmp_path / "public"
    for repo in (private, public):
        repo.mkdir()
        run(repo, "init", "-q", "-b", "main")
    source = commit(private, "app.py", "private work")
    sync = commit(public, "app.py", f"Release BOSS Community\n\nPrivate-source: {source}")
    return private, public, source, sync


def verdict(private: Path, public: Path, **kwargs):
    return guard.check(public, "main", private, "HEAD", **kwargs)


def test_a_sync_with_nothing_pushed_since_passes(pair):
    private, public, source, sync = pair
    result = verdict(private, public)
    assert result.ok, result.problems
    assert result.sync_commit == sync and result.private_source == source


def test_a_public_commit_not_imported_refuses_the_sync(pair, capsys):
    private, public, _, _ = pair
    pushed = commit(public, "fix.py", "fix: a collaborator's fix")
    result = verdict(private, public)
    assert not result.ok
    assert [c.sha for c in result.pending] == [pushed]
    status = guard.main(["--public", str(public), "--private", str(private)])
    out = capsys.readouterr().out
    assert status == 1
    assert "NOT IMPORTED" in out and pushed[:12] in out and "REFUSED" in out
    assert "Imported-from: seanventures/boss@<sha>" in out


def test_an_imported_commit_lets_the_sync_proceed(pair, capsys):
    private, public, _, _ = pair
    pushed = commit(public, "fix.py", "fix: a collaborator's fix")
    commit(private, "fix.py",
           f"fix: a collaborator's fix\n\nImported-from: seanventures/boss@{pushed}")
    result = verdict(private, public)
    assert result.ok, result.problems
    assert [c.sha for c in result.imported] == [pushed]
    assert guard.main(["--public", str(public), "--private", str(private)]) == 0
    assert "Private-source:" in capsys.readouterr().out


def test_an_import_from_another_repository_does_not_count(pair):
    private, public, _, _ = pair
    pushed = commit(public, "fix.py", "fix")
    commit(private, "fix.py", f"fix\n\nImported-from: someone/else@{pushed}")
    assert not verdict(private, public).ok


def test_only_the_commits_after_the_newest_sync_matter(pair):
    private, public, _, _ = pair
    commit(public, "old.py", "an old public commit")  # dropped by the next sync, deliberately
    source = commit(private, "more.py", "more private work")
    commit(public, "app.py", f"Release again\n\nPrivate-source: {source}")
    assert verdict(private, public).ok


def test_a_merged_public_branch_is_checked_commit_by_commit(pair):
    private, public, _, _ = pair
    run(public, "checkout", "-q", "-b", "feature")
    side = commit(public, "side.py", "a contributor branch commit")
    run(public, "checkout", "-q", "main")
    run(public, "merge", "-q", "--no-ff", "-m", "Merge feature", "feature")
    result = verdict(private, public)
    assert [c.sha for c in result.pending] == [side]


def test_a_deliberate_discard_needs_the_sha_and_a_reason(pair, capsys):
    private, public, _, _ = pair
    pushed = commit(public, "oops.py", "an accidental push")
    assert guard.main(["--public", str(public), "--private", str(private),
                       "--acknowledge", pushed]) == 2
    assert guard.main(["--public", str(public), "--private", str(private),
                       "--acknowledge", pushed[:10], "--reason", "pushed by mistake"]) == 0
    out = capsys.readouterr().out
    assert "acknowledged" in out and "pushed by mistake" in out
    assert f"Discards-public: {pushed}" in out


def test_an_acknowledgement_naming_no_pending_commit_is_refused(pair):
    private, public, _, _ = pair
    result = verdict(private, public, acknowledge={"deadbeef": "nothing"})
    assert not result.ok
    assert "names no public commit" in result.problems[0]


def test_no_sync_commit_at_all_is_refused(tmp_path):
    private, public = tmp_path / "private", tmp_path / "public"
    for repo in (private, public):
        repo.mkdir()
        run(repo, "init", "-q", "-b", "main")
    commit(private, "a.py", "private")
    commit(public, "a.py", "a public commit with no sync trailer")
    result = verdict(private, public)
    assert not result.ok
    assert "No sync commit" in result.problems[0]


def test_the_release_commits_from_before_the_trailer_are_recognised(tmp_path):
    private, public = tmp_path / "private", tmp_path / "public"
    for repo in (private, public):
        repo.mkdir()
        run(repo, "init", "-q", "-b", "main")
    source = commit(private, "a.py", "private")
    commit(public, "a.py", f"Release BOSS Community 1.0.0-rc.2.\n\nOne-command install. "
                           f"Private source {source[:8]}.")
    result = verdict(private, public)
    assert result.ok, result.problems
    assert result.private_source == source


def test_a_sync_from_an_unrelated_private_source_is_refused(pair):
    private, public, _, _ = pair
    run(private, "checkout", "-q", "--orphan", "elsewhere")
    commit(private, "z.py", "unrelated history")
    result = verdict(private, public)
    assert not result.ok
    assert "not an ancestor" in result.problems[0]


def test_a_private_source_the_repository_lacks_is_refused(pair):
    private, public, _, _ = pair
    commit(public, "app.py", "Release\n\nPrivate-source: " + "1" * 40)
    result = verdict(private, public)
    assert not result.ok
    assert "does not have" in result.problems[0]


def test_an_unreachable_public_repository_is_an_error_not_a_pass(tmp_path, capsys):
    private = tmp_path / "private"
    private.mkdir()
    run(private, "init", "-q", "-b", "main")
    commit(private, "a.py", "private")
    status = guard.main(["--public", str(tmp_path / "missing.git"), "--private", str(private)])
    assert status == 2
    assert "could not run" in capsys.readouterr().err
