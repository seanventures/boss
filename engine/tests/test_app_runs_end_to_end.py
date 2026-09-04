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
    forwarded_preview_headers,
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
    # `npm start` and `npm run start` are the same command. The assertion was on the wording,
    # and the wording changed when the command started being built in the APP's directory
    # rather than the repo root — the property that matters is that the app's own server runs.
    assert "npm start" in command or "npm run start" in command
    assert "cd approveit" in command       # built AND run from the app's own directory
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


# --------------------------------------------------------------------------------------
# The preview must run a full-stack app's server, not serve its build
# --------------------------------------------------------------------------------------

def test_a_vite_app_with_a_server_runs_that_server(tmp_path):
    """BugBox previewed as "Error: HTTP 404" with a working backend sitting in the repo.

    The Vite branch of _detect_command fired on `vite` alone and served dist/ with
    `python3 -m http.server`. Correct for a frontend-only app; silently wrong for a full-stack
    one, because the page loads and looks finished while every /api call 404s.

    An earlier version of this fix added node detection to resolve_app_entry and was verified
    against an app in a SUBDIRECTORY -- which never reaches the Vite branch. The scaffold puts
    apps at the ROOT, so the case that mattered most was the one not covered.
    """
    write(tmp_path, {
        "package.json": pkg(build="vite build", start="node server/index.js", dev="vite"),
        "vite.config.ts": "export default {}",
        "index.html": "<html></html>",
        "server/index.js": "// express + sqlite",
    })
    command = " ".join(_detect_command(tmp_path, ["server/index.js"]) or [])
    assert "npm start" in command
    assert "http.server" not in command
    # Assets must stay relative or they resolve against the API origin under the proxy prefix.
    assert "--base=./" in command


def test_a_vite_app_without_a_server_is_still_served_statically(tmp_path):
    """The behaviour that was correct all along must not regress."""
    write(tmp_path, {
        "package.json": pkg(build="vite build", dev="vite"),
        "vite.config.ts": "export default {}",
        "index.html": "<html></html>",
    })
    command = " ".join(_detect_command(tmp_path, ["src/App.tsx"]) or [])
    assert "http.server" in command
    assert "npm start" not in command


def test_the_resolver_agrees_with_the_command_for_a_root_full_stack_app(tmp_path):
    """Host path and container path must reach the same answer; drift here has previewed the
    wrong application twice."""
    write(tmp_path, {
        "package.json": pkg(build="vite build", start="node server/index.js"),
        "vite.config.ts": "export default {}",
        "index.html": "<html></html>",
        "server/index.js": "// server",
    })
    assert resolve_app_entry(tmp_path, ["server/index.js"]) == ("node", ".")


def test_sample_app_web_index_does_not_shadow_its_start_server(tmp_path):
    """Live BugBox / ApproveIt: lineage includes web/index.html so step 2 returned static
    and /health + /api/* came back as the Vite HTML 404 page.

    The owning start server (bugbox/package.json scripts.start) must outrank the SPA
    file this task also wrote. A sibling static product (web-nqueens) must stay static.
    """
    write(tmp_path, {
        "bugbox/package.json": pkg(start="node server/index.js", build="vite build", dev="vite"),
        "bugbox/vite.config.ts": "export default {}",
        "bugbox/server/index.js": "// express + sqlite",
        "bugbox/web/index.html": "<html><title>BugBox</title></html>",
        "approveit/package.json": pkg(start="node server/index.js"),
        "approveit/server/index.js": "// server",
        "approveit/public/index.html": "<html></html>",
        "web-nqueens/index.html": "<title>Queen Quest</title>",
        "serve.py": "# sudoku",
        "index.html": "<title>Sudoku</title>",
    })
    bugbox_files = [
        "bugbox/web/index.html",
        "bugbox/server/index.js",
        "bugbox/src/App.tsx",
        "bugbox/package.json",
    ]
    assert resolve_app_entry(tmp_path, bugbox_files) == ("node", "bugbox")
    command = " ".join(_detect_command(tmp_path, bugbox_files) or [])
    assert "npm start" in command
    assert "bugbox" in command
    assert "http.server" not in command
    assert "--base=./" in command

    # Only the SPA file in the lineage — still the server, not static web/.
    assert resolve_app_entry(tmp_path, ["bugbox/web/index.html"]) == ("node", "bugbox")
    assert resolve_app_entry(tmp_path, ["approveit/public/index.html"]) == ("node", "approveit")

    # Sibling static app this task built must not be stolen by bugbox's start.
    assert resolve_app_entry(tmp_path, ["web-nqueens/index.html"]) == ("static", "web-nqueens")


def test_preview_proxy_forwards_acting_user_and_strips_boss_secrets():
    """ApproveIt /api/requests 401'd because the gateway dropped X-Acting-User-Id."""
    got = forwarded_preview_headers(
        {
            "X-Acting-User-Id": "usr_morgan",
            "Authorization": "Bearer boss-control-plane-secret",
            "X-Boss-Api-Key": "also-secret",
            "Accept": "application/json",
            "Cookie": "boss_session=nope",
        },
        content_type="application/json",
    )
    lowered = {k.lower(): v for k, v in got.items()}
    assert lowered["x-acting-user-id"] == "usr_morgan"
    assert lowered["accept"] == "application/json"
    assert lowered["content-type"] == "application/json"
    assert "authorization" not in lowered
    assert "x-boss-api-key" not in lowered
    assert "cookie" not in lowered


def test_the_proxy_forwards_a_header_the_app_invented():
    """THE BUG BEHIND "❌ X-Request-ID header is required".

    An app BOSS built uses X-Request-ID as an idempotency key and its server refuses a POST
    without one. Its client DID send it. The proxy forwarded a hardcoded allowlist of four
    names, dropped it, and the operator saw an app BOSS wrote failing in a preview BOSS serves,
    with an error that blamed the app.

    BOSS cannot enumerate in advance the headers the apps it writes will invent, so no
    allowlist can be right. This is the whole point of the denylist: a header nobody has ever
    heard of goes through.
    """
    got = forwarded_preview_headers(
        {"X-Request-ID": "req-42", "X-Some-App-Specific-Thing": "yes",
         "Authorization": "Bearer boss-control-plane-secret"},
        content_type="application/json",
    )
    lowered = {k.lower(): v for k, v in got.items()}
    assert lowered["x-request-id"] == "req-42"
    assert lowered["x-some-app-specific-thing"] == "yes"
    assert "authorization" not in lowered, "a new header must not cost us the secret boundary"


def test_hop_headers_describing_this_connection_are_not_forwarded():
    """Content-Length and Host describe OUR hop; passing them corrupts the upstream request.

    The body sent upstream is not necessarily byte-identical, and the app must resolve its own
    Host — a stale one breaks redirects and any host-based routing inside the app.
    """
    got = forwarded_preview_headers(
        {"Host": "boss-api.local.sean.network", "Content-Length": "99999",
         "Connection": "keep-alive", "Transfer-Encoding": "chunked",
         "X-Request-ID": "req-1"},
    )
    lowered = {k.lower() for k in got}
    assert lowered == {"x-request-id"}
