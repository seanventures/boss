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


# ── B2 (review of #223): the tree is the app's, read as untrusted ───────────────────────────

SECRET = b"SQLite format 3\x00 BOSS's own file"


def _run_with_timeout(fn, seconds: float = 10.0):
    import threading

    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("v", fn()), daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "the save blocked (a FIFO opened for reading?)"
    return out.get("v")


def test_a_symlink_swapped_in_after_the_walk_is_never_read_through(tmp_path, monkeypatch):
    """The race: the walk saw `data/sub` as a directory; by the read it is a link to a BOSS
    directory. Every directory on the way is opened O_NOFOLLOW, so the read is refused."""
    boss_dir = tmp_path / "boss-only"
    boss_dir.mkdir()
    (boss_dir / "secret.db").write_bytes(SECRET)
    tree = _checkout(tmp_path, "prev_swap-1")
    preview_data.mark_restored("prev_swap", tree, KEY)
    _save_recipe(tree, "Mine")
    os.symlink(boss_dir, tree / "data" / "sub")          # swapped in after the walk listed it
    real = preview_data._candidates
    monkeypatch.setattr(preview_data, "_candidates",
                        lambda t, **k: sorted(real(t, **k) + ["data/sub/secret.db"]))
    kept = preview_data.capture("prev_swap")
    assert kept["files"] == ["data/recipes.db"], kept
    assert preview_data._read(tree, "data/sub/secret.db", owners=None, uid=None, limit=1 << 20) is None
    # The last component swapped for a link is refused too.
    os.symlink(boss_dir / "secret.db", tree / "data" / "late.db")
    assert preview_data._read(tree, "data/late.db", owners=None, uid=None, limit=1 << 20) is None


def test_a_fifo_never_blocks_the_save(tmp_path, monkeypatch):
    tree = _checkout(tmp_path, "prev_fifo-1")
    preview_data.mark_restored("prev_fifo", tree, KEY)
    _save_recipe(tree, "Still saved")
    os.mkfifo(tree / "data" / "pipe")
    os.mkfifo(tree / "app.db")
    real = preview_data._candidates
    # Even named to the reader as a candidate (a file swapped for a FIFO after the walk).
    monkeypatch.setattr(preview_data, "_candidates",
                        lambda t, **k: sorted(set(real(t, **k)) | {"app.db", "data/pipe"}))
    kept = _run_with_timeout(lambda: preview_data.capture("prev_fifo"))
    assert kept["files"] == ["data/recipes.db"], kept


def test_a_hard_link_or_a_stranger_owned_file_is_not_the_apps(tmp_path):
    outside = tmp_path / "boss-secret.db"
    outside.write_bytes(SECRET)
    tree = _checkout(tmp_path, "prev_link-1")
    os.link(outside, tree / "data" / "linked.db")         # two links: not a file the app made
    assert preview_data._read(tree, "data/linked.db", owners=None, uid=None, limit=1 << 20) is None
    (tree / "data" / "mine.db").write_bytes(b"ok")
    assert preview_data._read(tree, "data/mine.db", owners={os.geteuid()}, uid=None,
                              limit=1 << 20) == b"ok"
    assert preview_data._read(tree, "data/mine.db", owners={os.geteuid() + 4242}, uid=None,
                              limit=1 << 20) is None


def test_a_sandboxed_tree_whose_uid_was_not_stopped_is_never_read(tmp_path, monkeypatch):
    tree = _checkout(tmp_path, "prev_live-1")
    preview_data.mark_restored("prev_live", tree, KEY)
    _save_recipe(tree, "Not read")
    monkeypatch.setattr(preview_data, "in_sandbox", lambda _t: True)
    assert preview_data.capture("prev_live")["why"] == "not stopped"
    assert preview_data.describe(KEY)["kept"] is False


def test_a_file_only_the_apps_uid_can_read_is_read_as_that_uid(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root reads every file")
    tree = _checkout(tmp_path, "prev_uid-1")
    preview_data.mark_restored("prev_uid", tree, KEY)
    (tree / "data" / "private.db").write_bytes(b"SQLite format 3\x00app-only")
    os.chmod(tree / "data" / "private.db", 0)
    calls: list = []

    def run_as(uid, script, *args, data=None, timeout=120):
        calls.append((uid, script, args))
        return b"SQLite format 3\x00app-only"

    monkeypatch.setattr(preview_data, "_run_as", run_as)
    kept = preview_data.capture("prev_uid", owner_uid=61007)
    os.chmod(tree / "data" / "private.db", 0o600)
    assert kept["files"] == ["data/private.db"]
    assert calls and calls[0][0] == 61007 and calls[0][1] == preview_data._READ_AS_UID
    assert "! test -h" in preview_data._READ_AS_UID


def test_restored_files_are_readable_by_nobody_else(tmp_path, monkeypatch):
    import stat as _stat

    tree = _checkout(tmp_path, "prev_mode-1")
    preview_data.mark_restored("prev_mode", tree, KEY)
    (tree / "uploads").mkdir()
    (tree / "uploads" / "photo.jpg").write_bytes(b"jpg")
    _save_recipe(tree, "Private")
    preview_data.capture("prev_mode")
    # Without a sandbox: BOSS writes them, 0600, new folders 0700.
    plain = _checkout(tmp_path, "prev_mode-2")
    preview_data.mark_restored("prev_mode2", plain, KEY)
    assert _stat.S_IMODE((plain / "data" / "recipes.db").stat().st_mode) == 0o600
    assert _stat.S_IMODE((plain / "uploads").stat().st_mode) == 0o700
    assert _stat.S_IMODE((plain / "uploads" / "photo.jpg").stat().st_mode) == 0o600
    # In the sandbox: written AS the preview's uid, umask 027 (0640, group boss), never 0666.
    calls: list = []

    def run_as(uid, script, *args, data=None, timeout=120):
        calls.append((uid, script, args, data))
        return b""

    monkeypatch.setattr(preview_data, "_run_as", run_as)
    boxed = _checkout(tmp_path, "prev_mode-3")
    restored = preview_data.mark_restored("prev_mode3", boxed, KEY, uid=61009)
    assert sorted(restored) == ["data/recipes.db", "uploads/photo.jpg"]
    assert {c[0] for c in calls} == {61009}
    assert all(c[1] == preview_data._WRITE_AS_UID for c in calls)
    assert "umask 027" in preview_data._WRITE_AS_UID
    assert {c[2][0] for c in calls} == {str(boxed / "data" / "recipes.db"),
                                        str(boxed / "uploads" / "photo.jpg")}


def test_the_uid_is_stopped_before_every_save(tmp_path, monkeypatch):
    """Stop, idle, expiry (teardown), sweep (reclaim) and respawn all go through
    _end_and_keep_data: stop the uid, then capture with that uid; never capture when the stop
    failed."""
    from types import SimpleNamespace

    from boss_engine.services import sandbox_identity

    order: list = []
    monkeypatch.setattr(sandbox_identity, "stop_owner",
                        lambda pid: order.append(("stop", pid)) or SimpleNamespace(uid=61003))
    monkeypatch.setattr(preview_data, "capture",
                        lambda pid, owner_uid=None: order.append(("capture", pid, owner_uid)) or {})
    monkeypatch.setattr(preview_service, "_release_preview_sandbox", lambda _pid: None)
    monkeypatch.setattr(preview_service, "_kill", lambda _pid: None)
    preview_service._teardown({"id": "prev_a", "mode": "process", "pid": None})
    monkeypatch.setattr(preview_service, "PREVIEW_ROOT", tmp_path / "none")
    preview_service.reclaim_preview_workdir("prev_b")
    assert order == [("stop", "prev_a"), ("capture", "prev_a", 61003),
                     ("stop", "prev_b"), ("capture", "prev_b", 61003)]

    order.clear()

    def refuse(pid):
        order.append(("stop", pid))
        raise sandbox_identity.SandboxUnavailable("kill failed")

    monkeypatch.setattr(sandbox_identity, "stop_owner", refuse)
    preview_service._teardown({"id": "prev_c", "mode": "process", "pid": None})
    assert order == [("stop", "prev_c")], "a uid that could not be stopped is never read"


def test_an_orphaned_lease_is_found_from_its_run_directory_and_killed(tmp_path, monkeypatch):
    """After an API restart no process holds the lease: stop_owner finds the uid that owns the
    run directory (release()'s rule) and kills it, with the helper's exit status checked."""
    from boss_engine.services import sandbox_identity

    monkeypatch.setenv(sandbox_identity.RUN_ROOT_ENV, str(tmp_path / "runs"))
    run_dir = tmp_path / "runs" / sandbox_identity.owner_key("prev_orphan")
    run_dir.mkdir(parents=True)
    me = os.lstat(run_dir).st_uid
    monkeypatch.setattr(sandbox_identity, "UID_BASE", me)
    monkeypatch.setattr(sandbox_identity, "UID_COUNT", 1)
    log = tmp_path / "helper.log"
    helper = tmp_path / "boss-sandbox-exec"
    helper.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit ${{FAIL:-0}}\n')
    helper.chmod(0o755)
    monkeypatch.setattr(sandbox_identity, "helper_path", lambda: str(helper))
    ident = sandbox_identity.stop_owner("prev_orphan")
    assert ident is not None and ident.uid == me
    assert log.read_text().split() == ["kill", str(me)]
    assert sandbox_identity.stop_owner("prev_nobody") is None
    monkeypatch.setenv("FAIL", "1")
    with pytest.raises(sandbox_identity.SandboxUnavailable):
        sandbox_identity.stop_owner("prev_orphan")
