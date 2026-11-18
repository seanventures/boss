"""An app's preview data outlives the preview (recipe-box, 2026-11-18).

The tester saved recipes, BOSS stopped the preview ("nobody opened it for 20 min"), Restart made
a new preview id with a fresh checkout, and the recipes were gone: the app's SQLite file lived in
the preview's own tree. Data is now kept per APP (project + repository), captured when a preview
ends and restored before the next one starts (boss_engine/services/preview_data.py).
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from boss_engine.services import preview_data, preview_service


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    root = tmp_path / "store"
    monkeypatch.setenv("BOSS_PREVIEW_DATA_DIR", str(root))
    return root


KEY = preview_data.app_key("recipe-box-fd3ab4a3", "local_recipe-box-fd3ab4a3/recipe-box")


def _checkout(tmp_path: Path, name: str) -> Path:
    """A fresh clone of the app: sources, a shipped seed under data/, node_modules."""
    tree = tmp_path / name
    (tree / "lib").mkdir(parents=True)
    (tree / "lib" / "recipes.js").write_text("// DATA_DIR = cwd/data\n")
    (tree / "data").mkdir()
    (tree / "data" / "seed.json").write_text('{"recipes": []}')
    (tree / "node_modules" / "x").mkdir(parents=True)
    return tree


def _save_recipe(tree: Path, title: str) -> None:
    db = sqlite3.connect(tree / "data" / "recipes.db")
    db.execute("CREATE TABLE IF NOT EXISTS recipes (title TEXT)")
    db.execute("INSERT INTO recipes VALUES (?)", (title,))
    db.commit()
    db.close()


def _recipes(tree: Path) -> list[str]:
    path = tree / "data" / "recipes.db"
    if not path.exists():
        return []
    db = sqlite3.connect(path)
    try:
        return [r[0] for r in db.execute("SELECT title FROM recipes")]
    finally:
        db.close()


def test_the_key_is_the_app_not_the_preview_or_branch():
    assert KEY == preview_data.app_key("recipe-box-fd3ab4a3", "local_recipe-box-fd3ab4a3/recipe-box")
    assert KEY != preview_data.app_key("another-project", "local_recipe-box-fd3ab4a3/recipe-box")
    assert KEY != preview_data.app_key("recipe-box-fd3ab4a3", "acme/other-app")
    assert preview_data.app_key("", "acme/app") == "" and preview_data.app_key("p", "") == ""
    assert "/" not in KEY and ".." not in KEY


def test_what_the_app_saved_comes_back_in_the_next_preview(tmp_path):
    first = _checkout(tmp_path, "prev_aaa-1")
    assert preview_data.mark_restored("prev_aaa", first, KEY) == []
    _save_recipe(first, "Shakshuka")
    (first / "node_modules" / "x" / "cache.db").write_bytes(b"SQLite format 3\x00dependency")
    kept = preview_data.capture("prev_aaa")
    assert kept["kept"] and kept["files"] == ["data/recipes.db"], kept
    assert preview_data.describe(KEY)["kept"] is True

    # BOSS stops it; a Restart is a new preview id and a fresh checkout (another branch, even).
    second = _checkout(tmp_path, "prev_bbb-2")
    assert preview_data.mark_restored("prev_bbb", second, KEY) == ["data/recipes.db"]
    assert _recipes(second) == ["Shakshuka"]
    _save_recipe(second, "Dal")
    preview_data.capture("prev_bbb")

    third = _checkout(tmp_path, "prev_ccc-3")
    preview_data.mark_restored("prev_ccc", third, KEY)
    assert _recipes(third) == ["Shakshuka", "Dal"]
    # The repository's own seed is the repository's: never captured, never overwritten.
    assert (third / "data" / "seed.json").read_text() == '{"recipes": []}'


def test_an_edited_data_file_and_sqlite_sidecars_are_kept(tmp_path):
    tree = _checkout(tmp_path, "prev_ddd-1")
    preview_data.mark_restored("prev_ddd", tree, KEY)
    (tree / "data" / "seed.json").write_text('{"recipes": ["edited"]}')
    (tree / "app.sqlite").write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    (tree / "app.sqlite-wal").write_bytes(b"wal")
    kept = preview_data.capture("prev_ddd")
    assert sorted(kept["files"]) == ["app.sqlite", "app.sqlite-wal", "data/seed.json"]
    fresh = _checkout(tmp_path, "prev_eee-2")
    preview_data.mark_restored("prev_eee", fresh, KEY)
    assert (fresh / "data" / "seed.json").read_text() == '{"recipes": ["edited"]}'
    assert (fresh / "app.sqlite-wal").read_bytes() == b"wal"


def test_a_preview_that_wrote_nothing_never_wipes_kept_data(tmp_path):
    tree = _checkout(tmp_path, "prev_fff-1")
    preview_data.mark_restored("prev_fff", tree, KEY)
    _save_recipe(tree, "Soup")
    preview_data.capture("prev_fff")
    # A preview whose tree is already gone, or whose app wrote nothing new, changes nothing.
    empty = _checkout(tmp_path, "prev_ggg-2")
    preview_data.mark_restored("prev_ggg", empty, KEY)
    (empty / "data" / "recipes.db").unlink()
    assert preview_data.capture("prev_ggg")["kept"] is False
    assert preview_data.capture("prev_never_started")["kept"] is False
    again = _checkout(tmp_path, "prev_hhh-3")
    preview_data.mark_restored("prev_hhh", again, KEY)
    assert _recipes(again) == ["Soup"]


def test_too_much_data_is_refused_not_truncated(tmp_path, monkeypatch):
    tree = _checkout(tmp_path, "prev_iii-1")
    preview_data.mark_restored("prev_iii", tree, KEY)
    _save_recipe(tree, "Kept")
    preview_data.capture("prev_iii")
    big = _checkout(tmp_path, "prev_jjj-2")
    preview_data.mark_restored("prev_jjj", big, KEY)
    (big / "data" / "upload.bin").write_bytes(b"x" * 4096)
    monkeypatch.setenv("BOSS_PREVIEW_DATA_MAX_MB", "0.001")
    assert preview_data.capture("prev_jjj")["why"] == "too large"
    monkeypatch.delenv("BOSS_PREVIEW_DATA_MAX_MB")
    after = _checkout(tmp_path, "prev_kkk-3")
    preview_data.mark_restored("prev_kkk", after, KEY)
    assert _recipes(after) == ["Kept"] and not (after / "data" / "upload.bin").exists()


def test_fresh_data_deletes_what_is_kept(tmp_path):
    tree = _checkout(tmp_path, "prev_lll-1")
    preview_data.mark_restored("prev_lll", tree, KEY)
    _save_recipe(tree, "Gone")
    preview_data.capture("prev_lll")
    assert preview_data.reset(KEY) is True
    assert preview_data.describe(KEY)["kept"] is False
    fresh = _checkout(tmp_path, "prev_mmm-2")
    assert preview_data.mark_restored("prev_mmm", fresh, KEY) == []
    assert _recipes(fresh) == []
    assert preview_data.reset(KEY) is False


def test_a_symlink_the_app_plants_is_never_followed(tmp_path):
    secret = tmp_path / "boss-secret.db"
    secret.write_bytes(b"SQLite format 3\x00 BOSS's own")
    tree = _checkout(tmp_path, "prev_nnn-1")
    preview_data.mark_restored("prev_nnn", tree, KEY)
    os.symlink(secret, tree / "data" / "stolen.db")
    os.symlink(tmp_path, tree / "storage")
    _save_recipe(tree, "Real")
    kept = preview_data.capture("prev_nnn")
    assert kept["files"] == ["data/recipes.db"]
    # On restore, a symlink in the new tree where the data would go is not written through.
    victim = tmp_path / "victim"
    victim.mkdir()
    planted = _checkout(tmp_path, "prev_ooo-2")
    (planted / "data" / "seed.json").unlink()
    (planted / "data").rmdir()
    os.symlink(victim, planted / "data")
    assert preview_data.mark_restored("prev_ooo", planted, KEY) == []
    assert list(victim.iterdir()) == []


def test_the_lifecycle_keeps_data_when_boss_stops_a_preview(tmp_path, monkeypatch):
    """The real hooks: a preview's launch restores (keep_preview_data) and its teardown — the
    idle reaper's, Stop's and expiry's — keeps (preview_service._teardown)."""
    monkeypatch.setattr(preview_service, "_release_preview_sandbox", lambda _pid: None)
    monkeypatch.setattr(preview_service, "_kill", lambda _pid: None)
    tree = _checkout(tmp_path, "prev_ppp-1")
    preview_service.keep_preview_data(None, "prev_ppp", tree, project_id="recipe-box-fd3ab4a3",
                                      repo="local_recipe-box-fd3ab4a3/recipe-box")
    _save_recipe(tree, "Idle-stopped")
    preview_service._teardown({"id": "prev_ppp", "mode": "process", "pid": None})
    restarted = _checkout(tmp_path, "prev_qqq-2")
    preview_service.keep_preview_data(None, "prev_qqq", restarted, project_id="recipe-box-fd3ab4a3",
                                      repo="local_recipe-box-fd3ab4a3/recipe-box")
    assert _recipes(restarted) == ["Idle-stopped"]
    # The note naming the tree lives in BOSS's store, never in the app's tree.
    note = preview_data._tree_note("prev_qqq")
    assert json.loads(note.read_text())["tree"] == str(restarted)
    assert not str(note).startswith(str(restarted))


def test_a_workdir_swept_without_a_teardown_still_keeps_its_data(tmp_path, monkeypatch):
    monkeypatch.setattr(preview_service, "PREVIEW_ROOT", tmp_path / "previews")
    (tmp_path / "previews").mkdir()
    tree = _checkout(tmp_path / "previews", "prev_rrr-1")
    preview_data.mark_restored("prev_rrr", tree, KEY)
    _save_recipe(tree, "Survived a restart")
    assert preview_service.reclaim_preview_workdir("prev_rrr") > 0
    assert not tree.exists()
    after = _checkout(tmp_path, "prev_sss-2")
    preview_data.mark_restored("prev_sss", after, KEY)
    assert _recipes(after) == ["Survived a restart"]


def test_fresh_data_resets_the_app_this_build_previews(tmp_path, monkeypatch):
    """The start endpoint's `fresh_data` (the console's Fresh data) resets the app of the run's
    repository, the same key the preview restored from."""
    import contextlib
    from types import SimpleNamespace

    tree = _checkout(tmp_path, "prev_ttt-1")
    preview_data.mark_restored("prev_ttt", tree, KEY)
    _save_recipe(tree, "Reset me")
    preview_data.capture("prev_ttt")

    @contextlib.contextmanager
    def begin(_engine):
        yield None

    run = {"project_id": "recipe-box-fd3ab4a3",
           "spec": {"owner": "local_recipe-box-fd3ab4a3", "repo": "recipe-box"}}
    monkeypatch.setattr(preview_service, "begin", begin)
    monkeypatch.setattr(preview_service, "load_code_task_run", lambda _c, _rid: run)
    settings = SimpleNamespace(target_github_owner="", target_github_repo="")
    assert preview_service.reset_preview_data(None, settings, "crun_1") is True
    assert preview_data.describe(KEY)["kept"] is False
    monkeypatch.setattr(preview_service, "load_code_task_run", lambda _c, _rid: None)
    with pytest.raises(ValueError):
        preview_service.reset_preview_data(None, settings, "crun_missing")
