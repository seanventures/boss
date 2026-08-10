"""An app BOSS builds must actually run, with its backend, wherever it is published.

Two ApproveIt builds were judged incompetent and neither was. One had a complete Express +
SQLite backend that was never started, because the resolver classified it `static` and the
preview served its `public/` directory with `python3 -m http.server`. The other had no backend
at all, because the only scaffold BOSS could seed was frontend-only.

Nothing caught either. The health gate is `curl /` expecting 200, and a static shell answers
200 all day.

These pin the properties that make "it works" true: a server is RUN rather than served, the
seed for an app that stores anything includes somewhere to store it, and the deploy path can
run the same thing the preview does.
"""

from __future__ import annotations

import json

import pytest

from boss_engine.services.preview_service import (
    _detect_command,
    _node_server_dir,
    resolve_app_entry,
)
from boss_engine.services.scaffold_service import scaffold_project


def write(root, files: dict) -> None:
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def pkg(**scripts) -> str:
    return json.dumps({"name": "a", "scripts": scripts})


# --------------------------------------------------------------------------------------
# The resolver: a server outranks the files it happens to sit next to
# --------------------------------------------------------------------------------------

def test_an_app_in_a_subdirectory_is_run_by_its_own_server(tmp_path):
    """The exact ApproveIt layout: app under approveit/, nothing runnable at the root."""
    write(tmp_path, {
        "approveit/package.json": pkg(start="node server/index.js"),
        "approveit/server/index.js": "// server",
        "approveit/public/index.html": "<html></html>",
    })
    assert resolve_app_entry(tmp_path, ["approveit/server/index.js"]) == ("node", "approveit")

    command = " ".join(_detect_command(tmp_path, ["approveit/server/index.js"]) or [])
    assert "npm start" in command
    assert "approveit" in command          # run from the app's own directory
    assert "http.server" not in command    # never the static fallback


def test_a_root_level_server_beats_its_own_index_html(tmp_path):
    """A Vite app has index.html at the root; that must not shadow its Express server."""
    write(tmp_path, {
        "package.json": pkg(start="node server/index.js", build="vite build"),
        "server/index.js": "// server",
        "index.html": "<html></html>",
    })
    assert resolve_app_entry(tmp_path, ["server/index.js"]) == ("node", ".")


def test_a_frontend_only_app_is_still_published_as_static(tmp_path):
    """`dev`/`build` without `start` means there is no server -- static is correct here."""
    write(tmp_path, {
        "package.json": pkg(dev="vite", build="vite build"),
        "web/index.html": "<html></html>",
    })
    kind, _ = resolve_app_entry(tmp_path, ["web/index.html"])
    assert kind == "static"


def test_a_pure_static_repo_is_unchanged(tmp_path):
    write(tmp_path, {"web/index.html": "<html></html>"})
    assert resolve_app_entry(tmp_path, ["web/index.html"]) == ("static", "web")


def test_a_python_app_is_unchanged(tmp_path):
    write(tmp_path, {"serve.py": "print('hi')"})
    assert resolve_app_entry(tmp_path, ["serve.py"]) == ("py", "serve.py")


def test_the_task_that_was_built_wins_over_another_app_in_the_repo(tmp_path):
    """Multi-app repos are why this function exists; a server must not change that rule."""
    write(tmp_path, {
        "alpha/package.json": pkg(start="node index.js"),
        "alpha/index.js": "// a",
        "beta/package.json": pkg(start="node index.js"),
        "beta/index.js": "// b",
    })
    assert _node_server_dir(tmp_path, ["beta/index.js"]) == "beta"


# --------------------------------------------------------------------------------------
# The seed: an app that stores things has somewhere to store them
# --------------------------------------------------------------------------------------

def test_the_default_web_seed_has_a_backend():
    files = scaffold_project(name="approveit", kind="fullstack")["files"]
    assert "server/index.js" in files
    assert "server/db.js" in files
    assert any(f.startswith("server/migrations/") for f in files)


def test_the_seeded_frontend_actually_calls_the_api():
    """A backend nothing calls is the same empty dashboard from the other direction."""
    app = scaffold_project(name="approveit", kind="fullstack")["files"]["src/App.tsx"]
    assert "/api/items" in app
    assert "fetch(" in app


def test_the_seeded_server_binds_the_environment_not_localhost():
    server = scaffold_project(name="x", kind="fullstack")["files"]["server/index.js"]
    assert "process.env.PORT" in server
    assert "process.env.HOST" in server
    # Only the code binds; the comment above it names 127.0.0.1 precisely to say "not this".
    code = [ln for ln in server.splitlines() if not ln.lstrip().startswith("//")]
    assert not any("127.0.0.1" in ln or "localhost" in ln for ln in code)


def test_the_seeded_app_declares_how_to_start_it():
    """`start` is the signal every downstream path keys on -- preview, deploy and the resolver."""
    manifest = json.loads(scaffold_project(name="x", kind="fullstack")["files"]["package.json"])
    assert manifest["scripts"]["start"] == "node server/index.js"
    assert "better-sqlite3" in manifest["dependencies"]
    assert "express" in manifest["dependencies"]


def test_the_seeded_e2e_suite_checks_that_data_survives_a_reload():
    """The assertion an in-memory SPA cannot pass -- the one that was missing."""
    spec = scaffold_project(name="x", kind="fullstack")["files"]["e2e/home.spec.ts"]
    assert "reload" in spec
    assert "/api/health" in spec


def test_the_frontend_only_scaffold_still_exists_for_apps_that_need_no_server():
    files = scaffold_project(name="x", kind="vite")["files"]
    assert "server/index.js" not in files


@pytest.mark.parametrize("alias", ["fullstack", "app", "webapp"])
def test_fullstack_aliases_resolve(alias):
    assert "server/index.js" in scaffold_project(name="x", kind=alias)["files"]


# --------------------------------------------------------------------------------------
# The deploy path can run what the preview runs
# --------------------------------------------------------------------------------------

def test_deploy_has_a_run_mode_for_a_node_server():
    """Without this the nginx branch takes every non-Python app and the backend never starts."""
    from boss_engine.services.deploy_service import _docker_run_cmd

    command = _docker_run_cmd("myapp", 8080, "/srv/myapp", "node", "")
    assert "node:" in command          # a node runtime, not nginx
    assert "npm start" in command
    assert "nginx" not in command
    assert "-e PORT=80" in command and "-e HOST=0.0.0.0" in command
    assert "/data" in command          # state survives a release


def test_static_and_python_deploy_modes_are_unchanged():
    from boss_engine.services.deploy_service import _docker_run_cmd

    assert "nginx" in _docker_run_cmd("a", 80, "/srv/a", "static", "")
    assert "python" in _docker_run_cmd("a", 80, "/srv/a", "py", "app.py")
