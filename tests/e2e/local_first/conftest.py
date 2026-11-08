"""LOCAL-FIRST PROJECTS, DRIVEN LIKE A PERSON (BOSSAPP-915: 916, 917, 919, 921, 922, 924).

The same harness as tests/e2e/add_project (a real Chromium at 1440 and 390; every test ends with
the guard: no console error, no unexpected /v1 >= 400, no sideways scroll, every popover whole in
view), plus what local-first needs:

  BOSS_E2E_IMPORT_ROOT  the API's BOSS_LOCAL_IMPORT_ROOT: the fixtures write folders to import
                        there (with history, without, with a GitHub remote); a sibling
                        "host-fixtures/" holds what a server's folder sends when it is copied here.
  BOSS_E2E_REPO_ROOT    the API's BOSS_LOCAL_REPO_ROOT: where BOSS keeps local repositories (the
                        tests read them with git: commits, branches; Delete removes them).
  BOSS_E2E_FAKES        the plumbing gate's fakes (tests/e2e/stubs/fakes.py): GitHub, git-backed.
  BOSS_E2E_DEV_SEAMS=1  the dev API's seams: /__dev/state {github, offline} and /__dev/lf/* (a build
                        stage on the pipeline's own functions, the worker's merge and push-pending
                        jobs, the preview's clone, an outage mid-build). See each test for which.

Run:  BOSS_E2E_CONSOLE=… BOSS_E2E_API=… BOSS_E2E_DEV_SEAMS=1 BOSS_E2E_IMPORT_ROOT=… \\
      BOSS_E2E_REPO_ROOT=… BOSS_E2E_FAKES=… python3 -m pytest -q -p no:cacheprovider tests/e2e/local_first
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import pytest

_AP = Path(__file__).resolve().parents[1] / "add_project" / "conftest.py"
_spec = importlib.util.spec_from_file_location("add_project_harness", _AP)
ap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ap)  # type: ignore[union-attr]

# The add_project harness's fixtures and hook, shared as they are.
browser = ap.browser
person = ap.person
pytest_runtest_makereport = ap.pytest_runtest_makereport
Person, api, dev, unique, started, PROJECT = ap.Person, ap.api, ap.dev, ap.unique, ap.started, ap.PROJECT
IDEA_LOCAL = ap.IDEA

IMPORT_ROOT = os.environ.get("BOSS_E2E_IMPORT_ROOT", "")
REPO_ROOT = os.environ.get("BOSS_E2E_REPO_ROOT", "")
FAKES = os.environ.get("BOSS_E2E_FAKES", "").rstrip("/")
if not (IMPORT_ROOT and REPO_ROOT and FAKES and ap.SEAMS):
    pytest.skip("local-first needs BOSS_E2E_IMPORT_ROOT, BOSS_E2E_REPO_ROOT, BOSS_E2E_FAKES and the dev seams",
                allow_module_level=True)


def git(*args: str, cwd: Any = None, git_dir: Any = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "e2e", "GIT_AUTHOR_EMAIL": "e2e@x.test",
           "GIT_COMMITTER_NAME": "e2e", "GIT_COMMITTER_EMAIL": "e2e@x.test"}
    argv = ["git", *(["--git-dir", str(git_dir)] if git_dir else []), *args]
    return subprocess.run(argv, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _folder(path: Path, files: dict[str, str], *, history: bool, remote: str | None = None, branches: int = 0) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    if history:
        git("init", "-q", "-b", "main", cwd=path)
        git("add", "-A", cwd=path)
        git("commit", "-q", "-m", "first", cwd=path)
        (path / "CHANGELOG.md").write_text("# Changes\n\n- second\n")
        git("add", "-A", cwd=path)
        git("commit", "-q", "-m", "second", cwd=path)
        for i in range(branches):
            git("branch", f"feature-{i + 1}", cwd=path)
        if remote:
            git("remote", "add", "origin", remote, cwd=path)
    return path


APP = {"package.json": '{"name":"notes","version":"1.0.0","scripts":{"test":"node --test"}}\n',
       "README.md": "# Notes\n\nA notes app.\n", "src/index.js": "export const hello = () => 'hi';\n",
       "test/index.test.js": "import { test } from 'node:test';\ntest('ok', () => {});\n"}


@pytest.fixture(scope="session")
def folders() -> dict[str, str]:
    """The folders on this machine to import, each by its path under the import directory, and a
    server's folder (what /srv/apps/ledger-api sends when it is copied here)."""
    root = Path(IMPORT_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    _folder(root / "lf-notes-app", APP, history=True, branches=1)
    _folder(root / "lf-plain-site", {"index.html": "<!doctype html><h1>Plain</h1>\n", "style.css": "h1{color:teal}\n"}, history=False)
    _folder(root / "lf-kaizen-clone", APP, history=True, remote="https://github.com/acme/kaizen-notes.git")
    _folder(root.parent / "host-fixtures" / "ledger-api", {**APP, "pyproject.toml": "[project]\nname='ledger'\n"},
            history=True, remote="https://github.com/acme/ledger-api.git")
    return {"history": "lf-notes-app", "plain": "lf-plain-site", "remote": "lf-kaizen-clone"}


def fake(path: str, body: dict | None = None) -> Any:
    req = urllib.request.Request(f"{FAKES}{path}", method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def seam(name: str, body: dict) -> dict:
    status, out = api("POST", f"/__dev/lf/{name}", body, timeout=300)
    assert status == 200, f"/__dev/lf/{name}: {status} {out}"
    return out


def project_name(project_id: str) -> str | None:
    """The project's name (None without BOSS_E2E_DATABASE_URL)."""
    url = os.environ.get("BOSS_E2E_DATABASE_URL")
    if not url:
        return None
    from sqlalchemy import create_engine, text
    eng = create_engine(url)
    try:
        with eng.connect() as c:
            return c.execute(text("SELECT name FROM projects WHERE id = :p"), {"p": project_id}).scalar()
    finally:
        eng.dispose()


def local_repo(project_id: str) -> dict:
    status, body = api("GET", f"/v1/projects/{project_id}/local-repo")
    assert status == 200, body
    return body


def bare_of(project_id: str) -> Path:
    """The project's bare repository on this machine (<repo root>/<project id>/<name>.git)."""
    found = list((Path(REPO_ROOT) / project_id).glob("*.git"))
    assert found, f"no local repository for {project_id} under {REPO_ROOT}"
    return found[0]


def fresh_project(name_prefix: str, *, code: str = "local", new_repo: dict | None = None) -> str:
    """A new app made through the API (Create and start's own route), for the tests that start
    after it (publish, outage, delete): consent and readiness are accepted the way the sheet
    does, by asking again with the project the first call kept."""
    body: dict[str, Any] = {"context_project_id": PROJECT, "kind": "idea", "name": unique(name_prefix),
                            "words": "A tiny expense list for our team with a CSV export.",
                            "code_location": code, "start_anyway": ["model", "deployment_target", "notify_channel"]}
    if new_repo:
        body["new_repo"] = new_repo
    status, out = api("POST", "/v1/add-project", body, timeout=240)
    pid = out.get("project_id") if isinstance(out, dict) else None
    assert pid, f"{status} {out}"
    return pid
