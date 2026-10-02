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


# --------------------------------------------------------------------------------------- #
# The command must be built for the app that was RESOLVED, not for the repo root.
#
# resolve_app_entry answered `node:bugbox` correctly every time; _detect_command ignored it and
# read the ROOT package.json, whose devDependencies contain `vite`. So it ran `npx vite build`
# in a directory with no index.html and no src: rollup had nothing to load, nothing listened,
# and three separate preview attempts reported "app did not serve HTTP within timeout".
# --------------------------------------------------------------------------------------- #

def _monorepo(tmp_path):
    """A root that looks like a Vite project and is not one, plus the real app beside it."""
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"preview": "vite preview"},
        "devDependencies": {"vite": "^5"},
    }))
    app = tmp_path / "bugbox"
    app.mkdir()
    (app / "package.json").write_text(json.dumps({
        "scripts": {"dev": "vite", "build": "vite build", "start": "node server/index.js"},
        "devDependencies": {"vite": "^5"},
    }))
    (app / "index.html").write_text("<html></html>")
    return tmp_path


def test_the_command_runs_in_the_resolved_app_directory(tmp_path):
    command = ps._detect_command(_monorepo(tmp_path), [])
    assert command is not None
    assert command[2].startswith("cd bugbox && ")


def test_it_runs_the_apps_own_server_rather_than_serving_static_output(tmp_path):
    command = ps._detect_command(_monorepo(tmp_path), [])
    assert "npm start" in command[2]


def test_it_never_builds_at_a_root_that_has_nothing_to_build(tmp_path):
    """The exact failure: `npx vite build` at a directory with no entry module."""
    command = ps._detect_command(_monorepo(tmp_path), [])
    assert not command[2].startswith("npm install --no-audit --no-fund --loglevel=error && npx vite build")


def test_a_single_app_repo_still_runs_at_its_root(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"dev": "vite", "build": "vite build"},
        "devDependencies": {"vite": "^5"},
    }))
    (tmp_path / "index.html").write_text("<html></html>")
    command = ps._detect_command(tmp_path, [])
    assert command is not None and "cd " not in command[2]


# --------------------------------------------------------------------------------------- #
# A SIBLING'S index.html DOES NOT VOUCH FOR THE ROOT.
#
# The guard above ("package.json declares vite but nothing to build") swept _STATIC_DIRS, so
# ANY app in the repo satisfied it on the root's behalf. boss-sandbox holds web/index.html from
# an older Sudoku task, which let its workspaces root — no index.html of its own — be treated
# as a Vite app. The interview-scorecard task, built in Python as scorecard_core/ +
# serve_scorecard.py, was previewed by running `vite build` at that root: "Build failed in 6ms
# | Could not resolve entry module index.html", nothing listened, and the console reported
# "app did not serve HTTP within timeout" about an app that was never started.
# --------------------------------------------------------------------------------------- #

def _python_app_beside_an_old_web_app(tmp_path):
    """The real boss-sandbox shape: a vite-declaring root, a stale web app, a Python app."""
    (tmp_path / "package.json").write_text(json.dumps({
        "workspaces": ["web"],
        "scripts": {"test": "python3 -m unittest discover -s tests",
                    "preview": "npx --package=vite vite preview"},
        "devDependencies": {"vite": "^5.3.1"},
    }))
    stale = tmp_path / "web"          # a previous task's app, with its own entry
    stale.mkdir()
    (stale / "index.html").write_text("<html></html>")
    # A real server script, main block and all. A comment-only file is not a program, and the
    # resolver no longer mistakes a name for one (see test_a_build_proves_its_app_runs.py).
    (tmp_path / "serve_scorecard.py").write_text(
        "# the app this run built\nif __name__ == '__main__':\n    serve()\n")
    return tmp_path


def test_a_python_app_is_not_previewed_by_building_vite_at_the_root(tmp_path):
    command = ps._detect_command(_python_app_beside_an_old_web_app(tmp_path),
                                 ["scorecard_core/scoring.py", "serve_scorecard.py"])
    assert command is not None
    assert "vite build" not in " ".join(command)


def test_it_runs_the_python_entrypoint_this_run_actually_built(tmp_path):
    command = ps._detect_command(_python_app_beside_an_old_web_app(tmp_path),
                                 ["scorecard_core/scoring.py", "serve_scorecard.py"])
    assert command == ["python3", "serve_scorecard.py"]


def test_ambiguity_is_refused_rather_than_guessed_at_the_root(tmp_path):
    """_detect_command swallowed AmbiguousAppError and guessed the root — the very directory
    resolution had just declined to choose. The refusal names the apps and what to do."""
    root = _python_app_beside_an_old_web_app(tmp_path)
    (root / "serve_monitor.py").write_text(
        "# another run's app\nif __name__ == '__main__':\n    serve()\n")
    with pytest.raises(ps.AmbiguousAppError):
        ps._detect_command(root, [])          # no lineage signal: cannot tell which app
