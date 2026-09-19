"""The build says which app it produced; the preview stops guessing.

This bug was fixed five times — serve_nqueens.py previewing SUDOKU (PR #62), bugbox/ previewing
the repo root (PR #161), a Python build previewing a vite root with no entry module — and each
fix taught the resolver one more repo shape. The shape kept changing because the shared sandbox
repo gains an app on every run: nine runnable apps by the time the interview-scorecard build
ran, ten by the next one.

So the decision moves to the only place that knows it for certain, and the accumulating
repository loses its ability to mislead anything downstream.
"""

from __future__ import annotations

import json

from boss_engine.services import preview_service as ps


def _write(root, files: dict[str, str]):
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


PKG_START = json.dumps({"scripts": {"start": "node server/index.js"}})


# --------------------------------------------------------------------------------------- #
# What a build declares, from its own files alone.
# --------------------------------------------------------------------------------------- #

def test_a_python_service_names_its_entrypoint():
    assert ps.declare_app_entry(
        ["scorecard_core/scoring.py", "serve_scorecard.py", "tests/test_scorecard_server.py"],
    ) == "py:serve_scorecard.py"


def test_a_node_app_names_its_directory():
    assert ps.declare_app_entry(
        ["bugbox/package.json", "bugbox/server/index.js", "bugbox/web/index.html"],
        {"bugbox/package.json": PKG_START}.get,
    ) == "node:bugbox"


def test_a_static_app_names_its_directory():
    assert ps.declare_app_entry(
        ["web-nqueens/index.html", "web-nqueens/app.js"]) == "static:web-nqueens"


def test_a_server_outranks_the_static_assets_the_same_build_wrote():
    """Serving an app's index.html without its API is a page that loads and does nothing."""
    assert ps.declare_app_entry(
        ["serve_stock.py", "web-stock/index.html"]) == "py:serve_stock.py"


def test_a_build_that_produced_no_app_declares_nothing():
    assert ps.declare_app_entry(["tests/test_a.py", "README.md", "docs/prd/x.md"]) is None


def test_two_servers_from_one_build_declare_nothing_rather_than_guess():
    assert ps.declare_app_entry(["serve_a.py", "serve_b.py"]) is None


def test_a_package_without_a_start_script_is_not_a_server():
    assert ps.declare_app_entry(
        ["web/package.json", "web/index.html"],
        {"web/package.json": json.dumps({"private": True})}.get,
    ) == "static:web"


# --------------------------------------------------------------------------------------- #
# What the preview does with it. THE POINT: the surrounding repo stops mattering.
# --------------------------------------------------------------------------------------- #

def _crowded_repo(tmp_path):
    """boss-sandbox as it actually is: every app every previous run left behind."""
    return _write(tmp_path, {
        "package.json": json.dumps({
            "workspaces": ["web"], "scripts": {"preview": "vite preview"},
            "devDependencies": {"vite": "^5.3.1"}}),
        "web/index.html": "<html>sudoku</html>",
        "web-nqueens/index.html": "<html>queens</html>",
        "web-stock/index.html": "<html>stock</html>",
        "web-monitor/index.html": "<html>monitor</html>",
        "serve.py": "# sudoku",
        "serve_nqueens.py": "# queens",
        "serve_stock.py": "# stock",
        "serve_monitor.py": "# monitor",
        "serve_scorecard.py": "# the app this run built",
    })


def test_the_declaration_beats_a_repo_full_of_other_apps(tmp_path):
    root = _crowded_repo(tmp_path)
    (root / ps.DECLARED_APP_FILE).write_text("py:serve_scorecard.py\n")
    assert ps._detect_command(root, None) == ["python3", "serve_scorecard.py"]


def test_without_the_declaration_that_same_repo_is_refused_as_ambiguous(tmp_path):
    """The old path, unchanged — this is what the declaration exists to avoid needing."""
    try:
        ps._detect_command(_crowded_repo(tmp_path), None)
    except ps.AmbiguousAppError:
        return
    raise AssertionError("expected the crowded repo to be ambiguous without a declaration")


def test_the_declaration_needs_no_lineage_and_no_changed_files(tmp_path):
    """Lineage decays: links get lost, branches get re-cloned. The file does not."""
    root = _crowded_repo(tmp_path)
    (root / ps.DECLARED_APP_FILE).write_text("py:serve_scorecard.py\n")
    assert ps._detect_command(root, []) == ["python3", "serve_scorecard.py"]


def test_a_node_declaration_runs_the_apps_own_server_from_its_own_directory(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"devDependencies": {"vite": "^5"}}),
        "bugbox/package.json": PKG_START,
        "bugbox/index.html": "<html></html>",
    })
    (root / ps.DECLARED_APP_FILE).write_text("node:bugbox\n")
    command = ps._detect_command(root, None)
    assert command[2].startswith("cd bugbox && ")
    assert "npm start" in command[2]


def test_a_declaration_naming_an_app_that_is_not_there_falls_back(tmp_path):
    """A stale declaration must not be able to break a preview that could otherwise work."""
    root = _write(tmp_path, {"web/index.html": "<html></html>"})
    (root / ps.DECLARED_APP_FILE).write_text("node:deleted-app\n")
    assert ps._detect_command(root, None) is not None


def test_garbage_in_the_file_is_ignored_rather_than_trusted(tmp_path):
    for junk in ("", "   ", "nonsense", "wat:thing", "py:", ":x"):
        assert ps.parse_declared_app(junk) is None
    assert ps.parse_declared_app("py:serve_scorecard.py\n") == ("py", "serve_scorecard.py")


def test_the_declaration_round_trips_from_build_to_preview(tmp_path):
    """End to end: what the build writes is what the preview runs."""
    built = ["scorecard_core/scoring.py", "serve_scorecard.py", "tests/test_scorecard.py"]
    entry = ps.declare_app_entry(built)
    root = _crowded_repo(tmp_path)
    (root / ps.DECLARED_APP_FILE).write_text(entry + "\n")
    assert ps._detect_command(root, None) == ["python3", "serve_scorecard.py"]


def test_an_ordinary_module_that_merely_starts_with_server_is_not_an_entrypoint():
    """A wrong declaration is worse than none: every later preview trusts it and stops looking."""
    assert ps.declare_app_entry(
        ["myapp/server_helpers.py", "myapp/index.html"]) == "static:myapp"
    assert ps.declare_app_entry(["pkg/serve_utils.py"]) is None


def test_a_root_entrypoint_is_still_declared():
    assert ps.declare_app_entry(["serve_scorecard.py"]) == "py:serve_scorecard.py"
    assert ps.declare_app_entry(["app.py"]) == "py:app.py"
