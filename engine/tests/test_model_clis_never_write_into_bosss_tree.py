"""Model CLIs run in a scratch folder outside BOSS's own tree (BOSSAPP-1449).

The enterprise lane's engines gate ran on a read-only BOSS tree: `claude_cli_adapter` made its
scratch folder under `<repo_root>/.local-logs`, the mkdir raised OSError, and every typed decision
recorded it as "no model" — the person was sent to Setup to connect a model that was fine. The
class fix: every model CLI's working folder comes from `scratch.scratch_dir` (the temp dir,
never the tree), and when even that cannot be made the failure says what happened and what to do.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from boss_engine.config import Settings
from boss_engine.services import agent_decision, scratch
from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter
from boss_engine.services.codex_cli_adapter import CodexCliAdapter

ENGINE = Path(__file__).resolve().parents[1] / "boss_engine"


@pytest.fixture
def read_only_tree(tmp_path, monkeypatch):
    tree = tmp_path / "boss"
    tree.mkdir()
    tree.chmod(stat.S_IRUSR | stat.S_IXUSR)
    monkeypatch.setattr(Settings, "repo_root", property(lambda self: tree))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "tmp").mkdir()
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", None)
    yield tree
    tree.chmod(stat.S_IRWXU)


@pytest.mark.parametrize("adapter", [ClaudeCliAdapter, CodexCliAdapter])
def test_a_read_only_boss_tree_still_gets_a_scratch_folder_outside_it(read_only_tree, tmp_path, adapter):
    got = Path(adapter(Settings())._scratch_dir())
    assert got.is_dir() and os.access(got, os.W_OK)
    assert read_only_tree not in got.parents and str(tmp_path / "cache") in str(got)
    assert list(read_only_tree.iterdir()) == []


def test_no_model_cli_names_a_folder_inside_the_tree():
    for path in ENGINE.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not re.search(r"repo_root\s*/\s*\"\.local-logs\"\s*/\s*\"\w+-scratch\"", text), path


def test_an_unwritable_temp_dir_says_what_happened_and_what_to_do(tmp_path, monkeypatch):
    blocked = tmp_path / "tmp"
    blocked.mkdir()
    blocked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(blocked))
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocked / "cache"))
    monkeypatch.setenv("HOME", str(blocked / "home"))
    try:
        with pytest.raises(scratch.ScratchUnavailable) as caught:
            scratch.scratch_dir("claude")
    finally:
        blocked.chmod(stat.S_IRWXU)
    said = str(caught.value)
    assert said.startswith(scratch.LEAD) and str(blocked.parent) in said
    assert "model was never asked" in said                       # what happened
    assert "An operator" in said and "writable" in said and "retries this step" in said
    # The decision's recorded error ("ScratchUnavailable: …", kept whole) carries it, and the
    # person gets this sentence, not "connect a model".
    recorded = f"ScratchUnavailable: {said}"[:600]
    reply = agent_decision.unavailable_reply(recorded, "claude")
    assert reply and reply.startswith(scratch.LEAD) and "Connect" not in reply
    assert len(f"ScratchUnavailable: {said}") <= 600


def test_a_folder_someone_else_planted_is_never_used(tmp_path, monkeypatch):
    """On a shared host another user (or a symlink) must not supply the CLI's working folder."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "cache"))     # ~/.cache/boss is the same planted link
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "boss").symlink_to(elsewhere)
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    got = Path(scratch.scratch_dir("claude"))
    assert elsewhere not in got.resolve().parents and (tmp_path / "cache" / "boss") not in got.parents
    assert not list(elsewhere.iterdir())
    assert oct(got.stat().st_mode & 0o777) == "0o700"
