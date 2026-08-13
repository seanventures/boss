"""Verify-green local workspaces are previewable without a PR."""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.services.preview_service import (
    PreviewNotReady,
    assert_previewable,
    find_local_run_workspace,
)


def test_running_without_pr_or_verify_is_refused():
    with pytest.raises(PreviewNotReady):
        assert_previewable(
            {"pull_request_url": "", "status": "running", "phase": "implement"},
            "crun_missing",
        )


def test_verify_green_without_workspace_is_refused():
    with pytest.raises(PreviewNotReady):
        assert_previewable(
            {
                "pull_request_url": "",
                "status": "failed",
                "phase": "review_code",
                "state": {"tests_passed": True, "test_command": "npm run verify", "test_exit_code": 0},
            },
            "crun_no_files",
        )


def test_verify_green_local_workspace_is_previewable(tmp_path, monkeypatch):
    run_id = "crun_verify_green"
    ws = tmp_path / f"{run_id}-abcd"
    (ws / "bugbox").mkdir(parents=True)
    (ws / "bugbox" / "package.json").write_text('{"name":"bugbox"}', encoding="utf-8")
    monkeypatch.setattr(
        "boss_engine.services.preview_service.find_local_run_workspace",
        lambda rid: ws if rid == run_id else None,
    )
    assert_previewable(
        {
            "pull_request_url": "",
            "status": "failed",
            "phase": "review_code",
            "state": {"tests_passed": True, "test_exit_code": 0, "test_command": "npm run verify"},
        },
        run_id,
    )


def test_find_local_run_workspace_discovers_bugbox_tree(tmp_path, monkeypatch):
    run_id = "crun_disk"
    root = tmp_path / "boss-sandboxes"
    ws = root / f"{run_id}-xyz"
    (ws / "bugbox").mkdir(parents=True)
    (ws / "bugbox" / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "boss_engine.services.preview_service.tempfile.gettempdir",
        lambda: str(tmp_path),
    )
    found = find_local_run_workspace(run_id)
    assert found is not None
    assert found == ws
