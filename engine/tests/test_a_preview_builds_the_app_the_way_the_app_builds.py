"""A preview builds an app with the APP'S OWN build, adding only base=./ for a located Vite entry.

docs-freshness-board (boss-sandbox PR #219, 2026-10-02) keeps its frontend in web/ and builds
with `tsc -p server/tsconfig.json && vite build --config web/vite.config.ts`. The preview ran
BOSS's own `npx vite build --base=./` at the app root instead: "Could not resolve entry module
index.html" in 10ms, and the server that `npm start` runs was never compiled. Every shape below
goes through both Node paths (an app at the root, and an app in a subdirectory).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from boss_engine.services import preview_service as ps

FRESHNESS_BUILD = "tsc -p server/tsconfig.json && vite build --config web/vite.config.ts"


def _app(root: Path, scripts: dict, files: tuple[str, ...] = (), vite: bool = True) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps({
        "scripts": scripts, "devDependencies": {"vite": "^5"} if vite else {},
        "dependencies": {"express": "^4"},
    }))
    for name in files:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x")


def _commands(tmp_path: Path, scripts: dict, files: tuple[str, ...], vite: bool = True) -> list[str]:
    """The command each Node path builds for the same app: at the root, and in a subdirectory."""
    root_app = tmp_path / "root"
    _app(root_app, scripts, files, vite)
    (root_app / "boss.preview.app").unlink(missing_ok=True)
    sub_repo = tmp_path / "sub"
    _app(sub_repo / "board", scripts, files, vite)
    return [
        " ".join(ps._detect_command_in(root_app) or []),
        " ".join(ps.command_for_entry(sub_repo, "node", "board") or []),
    ]


def test_the_freshness_board_is_built_by_its_own_build_script(tmp_path):
    for command in _commands(tmp_path, {"build": FRESHNESS_BUILD, "start": "node server/dist/index.js"},
                             ("web/index.html", "web/vite.config.ts")):
        assert "npm run build -- --base=./ && npm start" in command, command
        assert "npx vite build" not in command


def test_a_plain_vite_app_still_gets_relative_assets(tmp_path):
    for command in _commands(tmp_path, {"build": "vite build", "start": "node server.js"},
                             ("index.html",)):
        assert "npm run build -- --base=./ && npm start" in command, command


def test_boss_never_adds_a_build_of_its_own_to_an_app_that_has_one(tmp_path):
    """The class: BOSS's vite never runs beside the app's build — it would empty the outDir the
    app's build just wrote, or build where there is no entry. Shapes from the adversarial review."""
    shapes = [
        # A non-vite Express app that serves a root index.html: tsc output lives in dist/.
        ({"build": "tsc", "start": "node dist/s.js"}, ("index.html",), False),
        # A stale dist/index.html and a root config are not an entry.
        ({"build": "tsc", "start": "node d.js"}, ("vite.config.ts", "dist/index.html"), True),
        # A post-build step that writes into dist/ (prerendering).
        ({"build": "vite build && node prerender.js", "start": "node s.js"}, ("index.html",), True),
        # A named config with no files present.
        ({"build": "vite build --config web/vite.config.ts", "start": "node s.js"}, (), True),
    ]
    for i, (scripts, files, vite) in enumerate(shapes):
        for command in _commands(tmp_path / str(i), scripts, files, vite=vite):
            assert "npx vite build" not in command, command
            assert re.search(r"npm run build( --if-present| -- --base=\./)? && npm (run )?start", command), command


def test_an_env_prefixed_vite_build_keeps_its_env_and_gets_the_base(tmp_path):
    scripts = {"build": "tsc && cross-env NODE_ENV=production vite build", "start": "node s.js"}
    for command in _commands(tmp_path, scripts, ("index.html",)):
        assert "npm run build -- --base=./ && npm start" in command, command


def test_a_vite_app_with_no_build_script_is_built_from_its_root_entry(tmp_path):
    for command in _commands(tmp_path / "a", {"start": "node s.js"}, ("index.html",))[1:]:
        assert "npx vite build --base=./ && npm start" in command, command
    for command in _commands(tmp_path / "b", {"start": "node s.js"}, ("web/index.html",))[1:]:
        assert "vite build" not in command, command


def test_a_server_that_must_be_compiled_is_compiled_before_it_starts(tmp_path):
    for command in _commands(tmp_path, {"build": "tsc -p .", "start": "node dist/index.js"}, ()):
        assert re.search(r"npm run build( --if-present| -- --base=\./)? && npm (run )?start", command), command


def test_only_node_build_step_writes_a_vite_build():
    """One decision: a second hand-written `npx vite build` is how the two paths drifted apart."""
    source = Path(ps.__file__).read_text()
    before, rest = source.split("def node_build_step", 1)
    outside = before + rest.split("\ndef command_for_entry", 1)[1]
    code = "\n".join(line for line in outside.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r'"[^"\n]*npx vite build[^"\n]*"', code)


def test_every_install_boss_runs_brings_the_build_tools():
    """BOSS's image sets NODE_ENV=production, so a plain `npm install` skips devDependencies:
    docs-freshness-board's preview died on "tsc: not found". One install command, with dev."""
    from boss_engine.services.verification_target import NPM_INSTALL_CMD

    assert "--include=dev" in NPM_INSTALL_CMD
    source = Path(ps.__file__).read_text()
    code = "\n".join(l for l in source.splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r'"[^"\n]*npm install --no-audit', code), "retyped install command"
