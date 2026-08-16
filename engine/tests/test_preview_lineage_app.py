"""A preview must serve the app the run belongs to, whatever the run happened to touch.

"Official bugbox — honest verify + JTBD e2e · retry 1" edited verification and e2e files. No
index.html among them, so the resolver fell through to repo conventions and picked the repo
ROOT, whose package.json has a `preview` script and no app to preview. Rollup had no entry
module, the container never served, and the operator was told "app did not serve HTTP within
timeout" — a timeout for a build that could not have succeeded. bugbox/, with its own
index.html and server, was never considered.

Two properties, both general: the app is found through every link BOSS records (not only
root_run_id), and a directory that cannot build is never a candidate.
"""

from __future__ import annotations

import json

import pytest

from boss_engine.services import preview_service as ps


class FakeEngine:
    def __init__(self, runs):
        self.runs = runs


class _Ctx:
    def __init__(self, runs):
        self.runs = runs

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, _statement, params):
        row = self.runs.get(params.get("i"))
        return _Result(row)


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


@pytest.fixture()
def patched(monkeypatch):
    def _install(runs):
        monkeypatch.setattr(ps, "begin", lambda engine: _Ctx(runs))
        return FakeEngine(runs)
    return _install


def test_a_change_started_on_an_app_branch_resolves_to_that_app(patched):
    """The link that was missing: spec.base_branch."""
    runs = {"crun_app": {"id": "crun_app", "spec": {"base_branch": "main"}, "finalize_ctx": {}}}
    engine = patched(runs)
    followup = {"id": "crun_fix", "spec": {"base_branch": "boss/code/crun_app"}, "finalize_ctx": {}}
    assert ps.app_root_run_id(engine, followup) == "crun_app"


def test_it_walks_a_chain_of_changes_back_to_the_original_app(patched):
    runs = {
        "crun_app": {"id": "crun_app", "spec": {"base_branch": "main"}, "finalize_ctx": {}},
        "crun_mid": {"id": "crun_mid", "spec": {"base_branch": "boss/code/crun_app"},
                     "finalize_ctx": {}},
    }
    engine = patched(runs)
    latest = {"id": "crun_new", "spec": {"base_branch": "boss/code/crun_mid"}, "finalize_ctx": {}}
    assert ps.app_root_run_id(engine, latest) == "crun_app"


def test_root_run_id_still_wins_where_it_exists(patched):
    runs = {"crun_root": {"id": "crun_root", "spec": {"base_branch": "main"}, "finalize_ctx": {}}}
    engine = patched(runs)
    retry = {"id": "crun_retry", "spec": {"base_branch": "main"},
             "finalize_ctx": {"root_run_id": "crun_root"}}
    assert ps.app_root_run_id(engine, retry) == "crun_root"


def test_a_branch_naming_a_deleted_run_stops_instead_of_failing(patched):
    """ApproveIt's chain points at a run that no longer exists."""
    engine = patched({})
    orphan = {"id": "crun_x", "spec": {"base_branch": "boss/code/crun_gone"}, "finalize_ctx": {}}
    assert ps.app_root_run_id(engine, orphan) == "crun_x"


def test_a_cycle_cannot_hang_the_preview(patched):
    runs = {
        "crun_a": {"id": "crun_a", "spec": {"base_branch": "boss/code/crun_b"}, "finalize_ctx": {}},
        "crun_b": {"id": "crun_b", "spec": {"base_branch": "boss/code/crun_a"}, "finalize_ctx": {}},
    }
    engine = patched(runs)
    assert ps.app_root_run_id(engine, dict(runs["crun_a"])) in {"crun_a", "crun_b"}


def _write(tmp_path, rel, scripts, index=False):
    base = tmp_path / rel if rel != "." else tmp_path
    base.mkdir(parents=True, exist_ok=True)
    (base / "package.json").write_text(json.dumps({"scripts": scripts}))
    if index:
        (base / "index.html").write_text("<html></html>")
    return base


def test_a_monorepo_root_with_a_preview_script_and_no_app_is_not_previewable(tmp_path):
    """Exactly boss-sandbox's root: a vite script and nothing for rollup to load."""
    _write(tmp_path, ".", {"preview": "vite preview"})
    kind, _ = ps.resolve_app_entry(tmp_path, [])
    assert kind != "node" or _ != "."


def test_the_real_app_directory_is_previewable(tmp_path):
    _write(tmp_path, "bugbox", {"dev": "vite", "start": "node server/index.js"}, index=True)
    kind, value = ps.resolve_app_entry(tmp_path, ["bugbox/src/App.tsx"])
    assert (kind, value) == ("node", "bugbox")
