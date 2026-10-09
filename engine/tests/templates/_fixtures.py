"""Template fixtures for the S2 tests: a clean template written to disk (a directory, or a git
repository served over file://), each test then breaking ONE thing."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
HELLO = REPO_ROOT / "templates" / "apps" / "hello"

MANIFEST = {
    "schema": "boss.template/v1",
    "id": "health-endpoint",
    "name": "Health endpoint",
    "version": "1.0.0",
    "summary": "GET /healthz",
    "adds": ["routes"],
    "stacks": {"node": {"frameworks": ["express"], "min_toolchain": "node20"},
               "python": {"frameworks": ["fastapi"], "min_toolchain": "python311"}},
    "secrets": [{"name": "HEALTH_TOKEN", "test_default": "generated",
                 "pattern": "^ht_test_[A-Za-z0-9]{16,}$", "live_pattern": "^ht_live_[A-Za-z0-9]{16,}$"}],
    "egress": [],
    "routes": ["GET /healthz"],
    "tests": {"node": {"command": "node --test tests/health.test.mjs", "report": "junit", "min_tests": 1},
              "python": {"command": "python3 -m pytest -q tests", "report": "junit", "min_tests": 1}},
    "license": "Apache-2.0",
    "min_boss": "1.0.0",
}

FILES = {
    "brief.md": "Add GET /healthz.\n",
    "guide.md": "Open {{preview_url}}/healthz.\n",
    "SECURITY.md": "Nothing to protect.\n",
    "LICENSE": "Apache License 2.0\n",
    "stacks/node/health.js": "module.exports = () => 'ok';\n",
    "stacks/node/tests/health.test.mjs": "import { test } from 'node:test';\ntest('ok', () => {});\n",
    "stacks/python/health.py": "def health():\n    return 'ok'\n",
    "stacks/python/tests/test_health.py": "def test_ok():\n    assert True\n",
}


def write_template(root: Path, *, manifest: Optional[dict] = None, files: Optional[dict] = None,
                   drop: tuple[str, ...] = ()) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    content = {**FILES, **(files or {}),
               "boss-template.yaml": yaml.safe_dump(manifest if manifest is not None else MANIFEST)}
    for path, body in content.items():
        if path in drop:
            continue
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            target.write_bytes(body)
        else:
            target.write_text(body)
    return root


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


def git_repo(root: Path, **kw) -> tuple[Path, str]:
    """A git repository holding the template; returns (path, commit sha)."""
    write_template(root, **kw)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "template")
    return root, _git(root, "rev-parse", "HEAD")


def commit_all(root: Path, message: str = "more") -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


git = _git
