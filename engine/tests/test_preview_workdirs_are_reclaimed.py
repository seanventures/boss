"""A preview that can never serve again is holding a full npm install.

`preview_service.py` contained no `rmtree`, no `unlink`, no cleanup path of any kind. One
directory per attempt under `/tmp/boss-previews`, each with its own `node_modules` — commonly
100-300 MB for a Vite/Playwright app — and 31 failed previews had accumulated for this project
alone before anything deleted one. A failed preview has still done its install before dying.

That disk is the same disk the code agent works on, and disk exhaustion presents as unrelated
failures everywhere at once, which is the most expensive kind to diagnose.

The sweep is the MECHANISM, not a backstop: `start_preview` has several launch paths, each with
its own `_fail` closure, and cleanup wired per call site is exactly how one rule ends up
expressed in four places and satisfied in three (S3, see I30).
"""

import os
from pathlib import Path

import pytest

from boss_engine.services import preview_service as ps


@pytest.fixture()
def preview_root(tmp_path, monkeypatch):
    root = tmp_path / "boss-previews"
    root.mkdir()
    monkeypatch.setattr(ps, "PREVIEW_ROOT", root)
    return root


def _make_workdir(root: Path, preview_id: str, *, log: str = "boom\n", mb: int = 1) -> Path:
    d = root / f"{preview_id}-abcd1234"
    (d / "node_modules" / "rollup").mkdir(parents=True)
    (d / "node_modules" / "rollup" / "big.bin").write_bytes(b"\0" * (mb * 1024 * 1024))
    (d / "_preview.log").write_text(log)
    return d


class _FakeConn:
    def __init__(self, rows): self._rows = rows
    def execute(self, *_a, **_k): return self
    def mappings(self): return self._rows


def _engine_with(rows):
    import contextlib

    @contextlib.contextmanager
    def _begin(_engine):
        yield _FakeConn(rows)
    return _begin


def test_a_failed_preview_loses_its_tree(preview_root, monkeypatch):
    d = _make_workdir(preview_root, "prev_dead")
    monkeypatch.setattr(ps, "begin", _engine_with([{"id": "prev_dead", "status": "failed"}]))
    out = ps.sweep_preview_workdirs(object())
    assert out["reclaimed"] == 1
    assert not d.exists()
    assert out["bytes"] > 1_000_000


def test_a_running_preview_keeps_its_tree(preview_root, monkeypatch):
    """It is SERVING from that directory. Deleting it pulls the floor out from under a live app."""
    d = _make_workdir(preview_root, "prev_live")
    monkeypatch.setattr(ps, "begin", _engine_with([{"id": "prev_live", "status": "running"}]))
    out = ps.sweep_preview_workdirs(object())
    assert out["reclaimed"] == 0
    assert d.exists()


def test_a_starting_preview_keeps_its_tree(preview_root, monkeypatch):
    d = _make_workdir(preview_root, "prev_boot")
    monkeypatch.setattr(ps, "begin", _engine_with([{"id": "prev_boot", "status": "starting"}]))
    assert ps.sweep_preview_workdirs(object())["reclaimed"] == 0
    assert d.exists()


def test_the_log_survives_because_it_is_the_diagnosis(preview_root, monkeypatch):
    """The log lives INSIDE the workdir, and it is what identified MODULE_NOT_FOUND three levels
    down when a finished app could not be previewed. Keep the few KB, drop the 300 MB."""
    _make_workdir(preview_root, "prev_dead", log="Error: Cannot find module 'dist/server.js'\n")
    monkeypatch.setattr(ps, "begin", _engine_with([{"id": "prev_dead", "status": "failed"}]))
    ps.sweep_preview_workdirs(object())
    archived = ps._preview_log_archive("prev_dead")
    assert archived.is_file()
    assert "Cannot find module" in archived.read_text()


def test_a_young_orphan_is_left_alone(preview_root, monkeypatch):
    """A workdir is created BEFORE its row is inserted, so a fresh unknown directory may be a
    preview starting right now. Deleting it would break the thing it belongs to."""
    d = _make_workdir(preview_root, "prev_unknown")
    monkeypatch.setattr(ps, "begin", _engine_with([]))
    assert ps.sweep_preview_workdirs(object())["reclaimed"] == 0
    assert d.exists()


def test_an_old_orphan_is_reclaimed(preview_root, monkeypatch):
    """A process that died between mkdtemp and INSERT leaves a tree nothing will ever claim."""
    d = _make_workdir(preview_root, "prev_orphan")
    old = ps.ORPHAN_WORKDIR_AFTER_HOURS * 3600 + 600
    os.utime(d, (os.stat(d).st_atime, os.stat(d).st_mtime - old))
    monkeypatch.setattr(ps, "begin", _engine_with([]))
    out = ps.sweep_preview_workdirs(object())
    assert out["reclaimed"] == 1 and out["orphans"] == 1
    assert not d.exists()


def test_the_log_archive_is_never_swept_as_an_orphan(preview_root, monkeypatch):
    """_logs is ours; reclaiming it would delete every diagnosis we just went to trouble to keep."""
    archive = preview_root / "_logs"
    archive.mkdir()
    (archive / "prev_old.log").write_text("kept")
    old = ps.ORPHAN_WORKDIR_AFTER_HOURS * 3600 + 600
    os.utime(archive, (os.stat(archive).st_atime, os.stat(archive).st_mtime - old))
    monkeypatch.setattr(ps, "begin", _engine_with([]))
    ps.sweep_preview_workdirs(object())
    assert (archive / "prev_old.log").read_text() == "kept"


def test_dry_run_reports_without_deleting(preview_root, monkeypatch):
    d = _make_workdir(preview_root, "prev_dead")
    monkeypatch.setattr(ps, "begin", _engine_with([{"id": "prev_dead", "status": "failed"}]))
    out = ps.sweep_preview_workdirs(object(), dry_run=True)
    assert out["reclaimed"] == 1 and out["dry_run"] is True
    assert d.exists()


def test_a_database_outage_does_not_delete_anything(preview_root, monkeypatch):
    """Without the DB there is no way to know what is running. Fail closed — an unswept disk is
    recoverable, a deleted live preview is not."""
    d = _make_workdir(preview_root, "prev_live")

    import contextlib

    @contextlib.contextmanager
    def _boom(_engine):
        raise RuntimeError("db down")
        yield  # pragma: no cover
    monkeypatch.setattr(ps, "begin", _boom)
    out = ps.sweep_preview_workdirs(object())
    assert out["reclaimed"] == 0
    assert d.exists()


def test_reclaiming_never_raises(preview_root):
    """Freeing space must not be able to fail a caller that was doing something else."""
    assert ps.reclaim_preview_workdir("prev_does_not_exist") == 0


def test_the_autopilot_runs_it_regardless_of_the_spend_gate():
    """Disk costs nothing to reclaim, so it belongs with the free sensors, above the gate."""
    import inspect
    from boss_engine.services import shepherd_service
    src = inspect.getsource(shepherd_service.autopilot)
    assert "sweep_preview_workdirs" in src
    assert src.index("sweep_preview_workdirs") < src.index("shepherd_autopilot_enabled")
