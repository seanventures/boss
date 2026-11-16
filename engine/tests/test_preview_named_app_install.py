"""A monorepo-style local preview installs in the app the project already names.

The sandbox root is an npm workspace with its own start script. The app lives at
apps/board, two levels down, so one-level discovery never sees it and npm install
runs at the sandbox root. The project already recorded apps/board. The install
runs there. This test never executes npm.
"""

from __future__ import annotations

import json

from boss_engine.services import preview_service as ps
from boss_engine.services.verification_target import NPM_INSTALL_CMD


def _monorepo(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "private": True,
        "workspaces": ["apps/*"],
        "scripts": {"start": "echo root"},
    }))
    board = tmp_path / "apps" / "board"
    board.mkdir(parents=True)
    (board / "package.json").write_text(json.dumps({
        "scripts": {"start": "node server.js"},
    }))
    (board / "server.js").write_text("console.log('board')\n")
    return tmp_path


def test_a_monorepo_preview_installs_in_the_app_the_project_names(tmp_path):
    root = _monorepo(tmp_path)
    run = {
        "id": "crun_board",
        "title": "Board",
        "project_id": "prj_board",
        "spec": {"owner": "local", "repo": "board", "instructions": "Ship the board."},
        "finalize_ctx": {"app_dir": "apps/board"},
    }
    named = ps.named_app_directory(run)
    assert named == "apps/board"
    command = ps.local_preview_command(root, [], named)
    assert command is not None and command[:2] == ["sh", "-lc"]
    shell = command[2]
    assert shell.startswith("cd apps/board && "), shell
    assert NPM_INSTALL_CMD in shell
    assert not shell.startswith("npm install"), shell
    assert not (root / "node_modules").exists()
    assert not (root / "apps" / "board" / "node_modules").exists()
