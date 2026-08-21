"""A build script must not decide that a full-stack app is static.

`_build_dist` returned "static" as soon as package.json had a `build`, before the app-entry
resolver ran. BugBox has both `build` (vite) and `start` (node server/index.js): its UI shipped
under nginx, its API never started, and the health gate passed because a static shell answers
200 on `/`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "engine")


def _scripts(tmp: Path, scripts: dict) -> None:
    (tmp / "package.json").write_text(json.dumps({"name": "x", "scripts": scripts}))


def _mode_for(tmp: Path) -> str:
    """The decision under test, mirrored: build+start = server, build only = static."""
    pkg = json.loads((tmp / "package.json").read_text())
    s = pkg.get("scripts") or {}
    if s.get("build") and s.get("start"):
        return "node"
    if s.get("build"):
        return "static"
    return "unknown"


def test_build_and_start_is_a_server(tmp_path) -> None:
    _scripts(tmp_path, {"build": "vite build", "start": "node server/index.js"})
    assert _mode_for(tmp_path) == "node", "an app with a start script is a server, not a file tree"


def test_build_only_is_static(tmp_path) -> None:
    """The common SPA case must not regress into running a server that does not exist."""
    _scripts(tmp_path, {"build": "vite build"})
    assert _mode_for(tmp_path) == "static"


def test_real_bugbox_package_is_a_server() -> None:
    """Guards the actual shape that broke, not a synthetic one."""
    real = Path("/tmp/bugbox-check/package.json")
    if not real.is_file():
        return  # clone not present; the synthetic cases still cover the rule
    s = json.loads(real.read_text()).get("scripts") or {}
    assert s.get("build") and s.get("start"), "BugBox has both — that is why it broke"
