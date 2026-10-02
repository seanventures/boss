"""A run declaration says how to run an app; it does not decide what kind of deploy it is.

Third review of b63717c (MAJOR, deploy regression). b63717c made deploy_service read the
build's boss.preview.command so a package-module server deployed with the command its preview
and serve proof ran. It also let that declaration decide the deploy MODE:

  - a built frontend (package.json `build`, no `start`) whose build declared how to PREVIEW it
    (`npx vite preview --port $PORT`) used to deploy as static under nginx and now shipped as
    a node container running the declared preview server;
  - a static site with a declaration (`python3 -m http.server $PORT`) switched to py mode.

The design: the mode AND the command are the repository's own answer, exactly as 00907e9
decided them. A declaration is a preview / build-proof contract and deploy never reads it; the
only deploy change since 00907e9 is that a Python entry runs via python_entry_invocation
(`python3 -m pkg.module` for a package module). The first two cases failed on b63717c; the
cases after the fourth-review banner fail on dc3aeb7.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace


def _write(root, files):
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def _build_dist(monkeypatch, repo, changed):
    from boss_engine.services import deploy_service as ds
    from boss_engine.services import repo_host

    branch = "boss/code/crun_x"
    for args in (["init", "-q", "-b", branch], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "app"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    monkeypatch.setattr(repo_host, "clone_url_for", lambda *_a, **_k: "file://" + str(repo))
    return ds._build_dist(SimpleNamespace(target_repo_host="github"), "o", "r", branch,
                          changed=changed)


# A Vite-shaped SPA with no dependencies, so `npm ci` runs offline: `build` writes dist/.
SPA_PACKAGE = {"name": "spa", "version": "1.0.0", "private": True,
               "scripts": {"build": "mkdir -p dist && cp index.html dist/index.html"}}
SPA_LOCK = {"name": "spa", "version": "1.0.0", "lockfileVersion": 3, "requires": True,
            "packages": {"": {"name": "spa", "version": "1.0.0"}}}


def test_a_built_frontend_with_a_declaration_still_deploys_static(tmp_path, monkeypatch):
    """b63717c: a root boss.preview.command turned a no-`start` built frontend into a node
    deploy running `npx vite preview`. 00907e9 (and now): static, dist/ under nginx."""
    repo = _write(tmp_path / "repo", {
        "package.json": json.dumps(SPA_PACKAGE),
        "package-lock.json": json.dumps(SPA_LOCK),
        "index.html": "<html><body>SPA</body></html>\n",
        "boss.preview.command": "npx vite preview --host 0.0.0.0 --port $PORT\n",
    })
    root, mode, entry, _sha = _build_dist(
        monkeypatch, repo, ["package.json", "index.html", "boss.preview.command"])
    assert (mode, entry) == ("static", "")
    assert (root / "dist" / "index.html").read_text().startswith("<html><body>SPA")


def test_a_static_site_with_a_declaration_still_deploys_static(tmp_path, monkeypatch):
    """b63717c: a declared `python3 -m http.server $PORT` (how to PREVIEW a static site) made
    the deploy a python container. 00907e9 (and now): the site's files, under nginx."""
    repo = _write(tmp_path / "repo", {
        "web/index.html": "<html><body>Static site</body></html>\n",
        "web/app.js": "console.log('hi')\n",
        "web/boss.preview.command": "python3 -m http.server $PORT --bind 0.0.0.0\n",
    })
    root, mode, entry, _sha = _build_dist(
        monkeypatch, repo, ["web/index.html", "web/app.js", "web/boss.preview.command"])
    assert (mode, entry) == ("static", "")
    assert (root / "dist" / "index.html").read_text().startswith("<html><body>Static site")
    assert (root / "dist" / "app.js").is_file()


# --------------------------------------------------------------------------------------- #
# Fourth review of dc3aeb7 (DEPLOY BLOCKER). A declaration is a PREVIEW / BUILD-PROOF
# contract, not a production command. dc3aeb7 still shipped it as the node container's command
# in place of `npm start`; that container runs `npm ci --omit=dev` with NODE_ENV=production, so
# a declared `npx vite preview` (or anything else needing devDependencies) exits. 00907e9 ran
# `npm start`, and so does deploy now, declaration or not. Each test below fails on dc3aeb7.
# --------------------------------------------------------------------------------------- #

EXPRESS = "require('http').createServer((q, s) => s.end('ok')).listen(process.env.PORT)\n"


def _node_command(mode, entry):
    from boss_engine.services import deploy_service as ds

    command = ds._docker_run_cmd("a", 80, "/srv/a", mode, entry)
    assert "npm ci --omit=dev" in command and "-e NODE_ENV=production" in command
    return command


def test_a_built_server_with_a_declaration_still_runs_npm_start(tmp_path, monkeypatch):
    """`build` + `start`: node mode, `npm start` — the declared preview command never ships."""
    package = {**SPA_PACKAGE, "scripts": {**SPA_PACKAGE["scripts"], "start": "node server.js"}}
    repo = _write(tmp_path / "repo", {
        "package.json": json.dumps(package),
        "package-lock.json": json.dumps(SPA_LOCK),
        "index.html": "<html></html>\n",
        "server.js": EXPRESS,
        "boss.preview.command": "npx vite preview --host 0.0.0.0 --port $PORT\n",
    })
    _root, mode, entry, _sha = _build_dist(
        monkeypatch, repo, ["package.json", "server.js", "boss.preview.command"])
    assert (mode, entry) == ("node", "")
    command = _node_command(mode, entry)
    assert command.rstrip().count("npm start") == 1 and "vite preview" not in command


def test_a_plain_start_server_with_a_declaration_still_runs_npm_start(tmp_path, monkeypatch):
    """No build, `start` only (the resolver's node branch): `npm start`, as at 00907e9."""
    repo = _write(tmp_path / "repo", {
        "package.json": json.dumps({"name": "api", "version": "1.0.0", "private": True,
                                    "scripts": {"start": "node server.js"}}),
        "server.js": EXPRESS,
        "boss.preview.command": "npx nodemon server.js\n",
    })
    payload, mode, entry, _sha = _build_dist(
        monkeypatch, repo, ["package.json", "server.js", "boss.preview.command"])
    assert (mode, entry) == ("node", "")
    assert (payload / "dist" / "server.js").is_file()
    command = _node_command(mode, entry)
    assert "npm start" in command and "nodemon" not in command


def test_a_node_app_in_a_subdirectory_with_a_declaration_still_runs_npm_start(
        tmp_path, monkeypatch):
    repo = _write(tmp_path / "repo", {
        "api/package.json": json.dumps({"name": "api", "version": "1.0.0", "private": True,
                                        "scripts": {"start": "node server.js"}}),
        "api/server.js": EXPRESS,
        "api/boss.preview.command": "npx nodemon server.js\n",
    })
    payload, mode, entry, _sha = _build_dist(
        monkeypatch, repo, ["api/package.json", "api/server.js", "api/boss.preview.command"])
    assert (mode, entry) == ("node", "")
    assert (payload / "dist" / "server.js").is_file()


def test_a_python_server_deploys_by_its_entry_not_its_declaration(tmp_path, monkeypatch):
    """The declaration does not reach the py container either: a Flask app whose build declared
    `flask run` (needs the flask CLI's dev server) deploys as its entry, run the way
    python_entry_invocation says — `python3 app.py` for a plain script (00907e9: `python app.py`)."""
    repo = _write(tmp_path / "repo", {
        "app.py": "from flask import Flask\napp = Flask(__name__)\n\n"
                  "if __name__ == '__main__':\n    app.run()\n",
        "boss.preview.command": "flask run --port $PORT\n",
    })
    _root, mode, entry, _sha = _build_dist(monkeypatch, repo, ["app.py", "boss.preview.command"])
    assert (mode, entry) == ("py", "python3 app.py")
