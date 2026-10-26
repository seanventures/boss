"""detect_in_code must not abort a build's database detection on a symlink it would never read.

repo_files.RepoFiles.entries() refuses every non-regular, non-directory entry at listing time, so
walk() raises RepoFileRefused the instant it lists a directory holding ANY symlink — before
detect_in_code's name/suffix filter can decide the scan would never read it. detect_in_code turns
that refusal into BuildServicesUnavailable, so an unread stub symlink (tests/e2e/stubs/ssh-only/ssh)
fails the whole build. The scan must skip a symlink it would never read, while still refusing one
whose name it actually reads (package.json, *.py, *.yml, requirements*.txt, ...).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from boss_engine.services import build_services as bs  # noqa: E402


def test_unread_symlink_is_skipped_so_detection_still_completes(tmp_path):
    """A repo whose only symlink is one the scan would never read (no read-relevant name or
    suffix) must not fail the build: detect_in_code skips it, never follows it, and still reports
    the pg dependency the repo's own package.json declares."""
    repo = tmp_path / "repo"
    stub_dir = repo / "tests" / "e2e" / "stubs" / "ssh-only"
    stub_dir.mkdir(parents=True)
    # A neutral stub symlink with no read-relevant suffix, pointing outside the scanned folder.
    (stub_dir / "ssh").symlink_to(Path("..") / "bin" / "ssh")
    (repo / "package.json").write_text('{"dependencies":{"pg":"8"}}')

    error = None
    result = None
    try:
        result = bs.detect_in_code(repo)
    except bs.BuildServicesUnavailable as exc:
        error = exc

    assert error is None and result == ["package.json depends on pg"], (
        "build_services.detect_in_code must skip an unread symlink "
        "(tests/e2e/stubs/ssh-only/ssh, no read-relevant suffix) and still detect the pg "
        f"dependency; instead it refused mid-walk: error={error!r} result={result!r}"
    )


def test_a_symlink_named_like_a_read_file_is_still_refused(tmp_path):
    """INVARIANT (must hold before and after the fix): a symlink named like a file the scan reads
    is still refused — a fix that skips unread links must not start following read-relevant ones."""
    outside = tmp_path / "outside.json"
    outside.write_text('{"dependencies":{"pg":"8"}}')
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").symlink_to(outside)

    with pytest.raises(bs.BuildServicesUnavailable, match="repository database detection"):
        bs.detect_in_code(repo)
