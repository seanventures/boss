"""A run declaration says how to run an app; it does not decide what kind of deploy it is.

Third review of b63717c (MAJOR, deploy regression). b63717c made deploy_service read the
build's boss.preview.command so a package-module server deployed with the command its preview
and serve proof ran. It also let that declaration decide the deploy MODE:

  - a built frontend (package.json `build`, no `start`) whose build declared how to PREVIEW it
    (`npx vite preview --port $PORT`) used to deploy as static under nginx and now shipped as
    a node container running the declared preview server;
  - a static site with a declaration (`python3 -m http.server $PORT`) switched to py mode.

The design: the mode is the repository's own build-kind detection, exactly as 00907e9 decided
it; the declaration is used only as the run command of a SERVER deploy (py, node). Both cases
below give 00907e9's answer, and both failed on b63717c.
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


def test_a_built_server_with_a_declaration_runs_the_declared_command(tmp_path, monkeypatch):
    """The declaration still does its job where the repository says SERVER: a package with
    `build` and `start` deploys in node mode, running the declared command for `npm start`."""
    package = {**SPA_PACKAGE, "scripts": {**SPA_PACKAGE["scripts"], "start": "node server.js"}}
    repo = _write(tmp_path / "repo", {
        "package.json": json.dumps(package),
        "package-lock.json": json.dumps(SPA_LOCK),
        "index.html": "<html></html>\n",
        "server.js": "require('http').createServer((q, s) => s.end('ok'))"
                     ".listen(process.env.PORT)\n",
        "boss.preview.command": "node server.js --serve-dist\n",
    })
    _root, mode, entry, _sha = _build_dist(
        monkeypatch, repo, ["package.json", "server.js", "boss.preview.command"])
    assert (mode, entry) == ("node", "node server.js --serve-dist")
