"""'Sign in with BOSS' for an app built from login-oauth (F6 §6.4, F3 §3.1; the integration's open item 2h).

One click (POST /v1/apps/{app}/sign-in-with-boss) registers the app's client at BOSS's OIDC provider,
registers its redirect URIs from where the app is served (each running preview, its deploy
address), and binds OIDC_BOSS_CLIENT_ID / _SECRET / _ISSUER as the app's secrets for preview and
deploy (never a build). The proof at the end runs the login-oauth template's REAL Node stack with
exactly the environment a preview of the app gets, and signs a BOSS member in through it.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.oidc_world import make_world, query_of
from boss_engine.db import begin
from boss_engine.oidc_provider import app_sign_in
from boss_engine.oidc_provider import clients as oidc_clients
from boss_engine.templates import secrets as app_secrets
from boss_engine.templates.service import get_row, manifest_of, sync_builtins

ROOT = Path(__file__).resolve().parents[3]
NODE_STACK = ROOT / "templates" / "apps" / "login-oauth" / "stacks" / "node"
PREVIEWS = "https://previews.example.test"
APP = "login demo"


@pytest.fixture
def w(engine, monkeypatch):
    world = make_world(engine, monkeypatch)
    monkeypatch.setenv("BOSS_PREVIEW_ORIGIN", PREVIEWS)
    ids = world.ids
    world.run = f"crun_sib{uuid.uuid4().hex[:10]}"
    world.other_run = f"crun_sib{uuid.uuid4().hex[:10]}"
    with begin() as db:
        for rid, title in ((world.run, "Login demo"), (world.other_run, "Plain app")):
            db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec) VALUES "
                            "(:i, :p, :t, 'completed', CAST(:s AS JSONB))"),
                       {"i": rid, "p": ids.project_a, "t": title, "s": json.dumps({"owner": "acme", "repo": "demo"})})
        sync_builtins(db, ids.org)
        row = get_row(db, ids.org, "login-oauth")
        db.execute(text("INSERT INTO app_template_applications (id, project_id, app_key, template_id, version, "
                        "stack, status) VALUES (:i, :p, :a, :t, :v, 'node', 'applied')"),
                   {"i": f"tapp_{uuid.uuid4().hex[:10]}", "p": ids.project_a, "a": APP, "t": row["id"],
                    "v": row["version"]})
        app_secrets.ensure_test_defaults(db, org_id=ids.org, project_id=ids.project_a, app_key=APP,
                                         manifest=manifest_of(row), actor_id="usr_test")
    world.people_ = {}
    for role in ("operator", "viewer", "admin"):
        p = ids.person(f"sib-{role}")
        ids.bind(p, role, project=None if role == "admin" else ids.project_a)
        world.people_[role] = p
    outsider = ids.person("sib-outsider")
    ids.bind(outsider, "operator", project=ids.project_b)
    world.people_["outsider"] = outsider
    yield world
    with begin() as db:
        db.execute(text("DELETE FROM app_previews WHERE project_id = :p"), {"p": ids.project_a})
        db.execute(text("DELETE FROM deployment_targets WHERE project_id = :p"), {"p": ids.project_a})
        db.execute(text("DELETE FROM code_task_runs WHERE id = ANY(:r)"), {"r": [world.run, world.other_run]})


def _as(w, role, method, path, *, mfa=True, **kw):
    with w.ids.acting_as(w.ids.ctx(w.people_[role], mfa=mfa)):
        return getattr(w.http, method)(path, **kw)


def _connect(w, role="operator", app=APP, **body):
    return _as(w, role, "post", f"/v1/apps/{app}/sign-in-with-boss", json={"project_id": w.ids.project_a, **body})


def _preview(w, run=None, pid=None) -> str:
    pid = pid or f"prv_{uuid.uuid4().hex[:10]}"
    with begin() as db:
        db.execute(text("INSERT INTO app_previews (id, project_id, run_id, repo, branch, port, url, status) "
                        "VALUES (:i, :p, :r, 'acme/demo', 'main', 19001, 'http://127.0.0.1:19001', 'running')"),
                   {"i": pid, "p": w.ids.project_a, "r": run or w.run})
    return pid


def _deploy_target(w, public_url: str, app_name: str = "Login demo") -> None:
    with begin() as db:
        db.execute(text("INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
                        "target_ref, status, created_by, public_url) VALUES (:i, :p, :a, 'acme/demo', :e, 'ssh', "
                        "'deploy@x:22', 'active', 'test', :u)"),
                   {"i": f"dt_{uuid.uuid4().hex[:8]}", "p": w.ids.project_a, "a": app_name,
                    "e": f"prod-{uuid.uuid4().hex[:4]}", "u": public_url})


def _values(w, env):
    with begin() as db:
        values, _ = app_secrets.values_for_env(db, org_id=w.ids.org, project_id=w.ids.project_a, app_key=APP, env=env)
    return values


def _client(w):
    with begin() as db:
        return app_sign_in.client_for_app(db, w.ids.org, w.ids.project_a, APP)


# ---- the one click -------------------------------------------------------------------------------

def test_one_click_registers_the_client_and_binds_the_three_settings_for_preview_and_deploy(w):
    pid = _preview(w)
    _deploy_target(w, "https://login.example.test")
    r = _connect(w)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["connected"] is True and out["created"] is True and out["issuer"] == w.issuer
    client = _client(w)
    assert out["client"]["client_id"] == client["id"]
    assert client["redirect_uris"] == [f"{PREVIEWS}/v1/previews/{pid}/web/auth/callback/boss",
                                       "https://login.example.test/auth/callback/boss"]
    assert client["post_logout_redirect_uris"] == [f"{PREVIEWS}/v1/previews/{pid}/web/",
                                                   "https://login.example.test/"]
    assert client["type"] == "confidential" and client["token_endpoint_auth_method"] == "client_secret_basic"
    assert not client["first_party"]                                     # the person consents
    for env in ("preview", "deploy"):
        v = _values(w, env)
        assert v["OIDC_BOSS_CLIENT_ID"] == client["id"] and v["OIDC_BOSS_ISSUER"] == w.issuer
        assert len(v["OIDC_BOSS_CLIENT_SECRET"]) >= 40
        assert v["OIDC_BOSS_CLIENT_SECRET"] not in r.text                 # never returned
    build = _values(w, "build")
    assert not {"OIDC_BOSS_CLIENT_ID", "OIDC_BOSS_CLIENT_SECRET", "OIDC_BOSS_ISSUER"} & set(build)
    # the bound secret IS the client's secret: the token endpoint takes it
    with begin() as db:
        assert oidc_clients._secret_ok(db, client, _values(w, "preview")["OIDC_BOSS_CLIENT_SECRET"])  # noqa: SLF001
    # presence only on the app's secrets list
    listed = _as(w, "operator", "get", f"/v1/apps/{APP}/secrets", params={"project_id": w.ids.project_a})
    assert listed.status_code == 200
    names = {(s["name"], s["env"]) for s in listed.json()["secrets"]}
    assert ("OIDC_BOSS_CLIENT_SECRET", "deploy") in names and ("OIDC_BOSS_ISSUER", "preview") in names
    assert _values(w, "preview")["OIDC_BOSS_CLIENT_SECRET"] not in listed.text


def test_a_second_click_is_idempotent_and_rotates_nothing(w):
    assert _connect(w).status_code == 200
    first, cid = _values(w, "deploy")["OIDC_BOSS_CLIENT_SECRET"], _client(w)["id"]
    again = _connect(w).json()
    assert again["created"] is False and again["secret_rotated"] is False
    assert _client(w)["id"] == cid and _values(w, "deploy")["OIDC_BOSS_CLIENT_SECRET"] == first


def test_a_client_registered_by_hand_is_reused_and_its_secret_reissued(w):
    with begin() as db:
        hand, shown_once = oidc_clients.create(db, org_id=w.ids.org, actor="usr_test", body={
            "project_id": w.ids.project_a, "app_key": APP, "pool_id": w.pool["id"], "name": "Login demo",
            "redirect_uris": ["https://mine.example.test/cb"]})
    out = _connect(w).json()
    assert out["created"] is False and out["secret_rotated"] is True and out["client"]["client_id"] == hand["id"]
    client = _client(w)
    assert "https://mine.example.test/cb" in client["redirect_uris"]        # what the operator added stays
    with begin() as db:
        assert not oidc_clients._secret_ok(db, client, shown_once)          # noqa: SLF001 — BOSS cannot read it back
        assert oidc_clients._secret_ok(db, client, _values(w, "preview")["OIDC_BOSS_CLIENT_SECRET"])  # noqa: SLF001


def test_preview_start_and_stop_keep_the_redirect_uris_in_step(w):
    first = _preview(w)
    assert _connect(w).status_code == 200
    second = f"prv_{uuid.uuid4().hex[:10]}"
    with begin() as db:            # what preview_service.launch hands the new preview
        env = app_sign_in.preview_environment(db, project_id=w.ids.project_a, run_id=w.run, preview_id=second)
    assert env["APP_BASE_URL"] == f"{PREVIEWS}/v1/previews/{second}/web"
    assert env["OIDC_BOSS_CLIENT_ID"] == _client(w)["id"] and env["OIDC_BOSS_ISSUER"] == w.issuer
    assert len(env["SESSION_SECRET"]) >= 32                                 # the template's generated secret
    assert f"{PREVIEWS}/v1/previews/{second}/web/auth/callback/boss" in _client(w)["redirect_uris"]
    with begin() as db:
        db.execute(text("UPDATE app_previews SET status = 'stopped' WHERE id = :i"), {"i": first})
        app_sign_in.preview_stopped(db, project_id=w.ids.project_a, run_id=w.run, preview_id=first)
    uris = _client(w)["redirect_uris"]
    assert not any(first in u for u in uris)
    # the second preview is not in app_previews yet (it is starting): the next sync keeps only what runs
    assert uris == [f"{PREVIEWS}/v1/previews/{second}/web/auth/callback/boss"] or uris == [
        f"{app_sign_in.local_base()}{app_sign_in.CALLBACK_PATH}"]


def test_with_no_address_yet_the_local_default_keeps_the_client_valid_until_a_preview_starts(w):
    assert _connect(w).status_code == 200
    assert _client(w)["redirect_uris"] == ["http://localhost:3000/auth/callback/boss"]
    pid = _preview(w)
    with begin() as db:
        app_sign_in.preview_environment(db, project_id=w.ids.project_a, run_id=w.run, preview_id=pid)
    assert _client(w)["redirect_uris"] == [f"{PREVIEWS}/v1/previews/{pid}/web/auth/callback/boss"]


def test_an_app_without_the_login_template_is_refused_in_words(w):
    r = _connect(w, app="Plain app")
    assert r.status_code == 409 and r.json()["error"] == "login_template_not_applied"
    assert "Apply the login-oauth template" in r.json()["message"]


def test_an_app_of_another_project_or_no_app_is_not_found(w):
    assert _connect(w, app="no such app").status_code == 404
    r = _as(w, "operator", "post", f"/v1/apps/{APP}/sign-in-with-boss", json={"project_id": w.ids.project_b})
    assert r.status_code in (403, 404)


@pytest.mark.parametrize("role, status", [("operator", 200), ("admin", 200), ("viewer", 403), ("outsider", 403)])
def test_who_may_connect(w, role, status):
    assert _connect(w, role).status_code == status


def test_connecting_needs_a_fresh_second_factor(w):
    r = _as(w, "operator", "post", f"/v1/apps/{APP}/sign-in-with-boss", mfa=False,
            json={"project_id": w.ids.project_a})
    assert r.status_code in (401, 403, 422) and _client(w) is None, r.text
    assert "step" in r.text.lower(), r.text


def test_an_operator_cannot_create_the_orgs_first_pool_an_admin_can(w):
    with begin() as db:
        db.execute(text("DELETE FROM oidc_user_pools WHERE org_id = :o"), {"o": w.ids.org})
    r = _connect(w, "operator")
    assert r.status_code == 409 and r.json()["error"] == "no_pool"
    assert _connect(w, "admin").status_code == 200


def test_status_and_disconnect(w):
    before = _as(w, "viewer", "get", f"/v1/apps/{APP}/sign-in-with-boss", params={"project_id": w.ids.project_a})
    assert before.status_code == 200 and before.json()["connected"] is False and before.json()["template_applied"]
    assert _connect(w).status_code == 200
    now = _as(w, "viewer", "get", f"/v1/apps/{APP}/sign-in-with-boss", params={"project_id": w.ids.project_a}).json()
    assert now["connected"] is True and now["secrets"]["deploy"] == list(app_sign_in.NAMES)
    assert _as(w, "viewer", "delete", f"/v1/apps/{APP}/sign-in-with-boss",
               params={"project_id": w.ids.project_a}).status_code == 403
    gone = _as(w, "operator", "delete", f"/v1/apps/{APP}/sign-in-with-boss", params={"project_id": w.ids.project_a})
    assert gone.status_code == 200 and gone.json()["connected"] is False and gone.json()["client"] is None
    assert not {"OIDC_BOSS_CLIENT_ID", "OIDC_BOSS_CLIENT_SECRET"} & set(_values(w, "deploy"))


def test_a_person_cannot_paste_an_issuer_or_bind_an_issued_value_for_a_build(w):
    r = _as(w, "operator", "put", f"/v1/apps/{APP}/secrets/OIDC_BOSS_ISSUER",
            json={"project_id": w.ids.project_a, "env": "preview", "mode": "test", "value": "https://evil.test/oidc/x"})
    assert r.status_code == 404                                             # BOSS owns it; no template declares it
    with begin() as db, pytest.raises(Exception, match="preview and deploy only"):
        app_secrets.bind_issued(db, org_id=w.ids.org, project_id=w.ids.project_a, app_key=APP,
                                name="OIDC_BOSS_CLIENT_ID", env="build", value="x", actor_id="usr_test")


# ---- the proof: the template's real Node app signs a BOSS member in ------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Bridge(BaseHTTPRequestHandler):
    """BOSS on a real port for the Node app's openid-client (discovery, JWKS, token), served by the
    same in-process app the test drives."""
    http: TestClient

    def _forward(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")}
        r = self.http.request(self.command, self.path, content=body, headers=headers, follow_redirects=False)
        self.send_response(r.status_code)
        for k, v in r.headers.items():
            if k.lower() not in ("content-length", "transfer-encoding", "connection", "content-encoding"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(r.content)))
        self.end_headers()
        self.wfile.write(r.content)

    do_GET = do_POST = _forward

    def log_message(self, *args) -> None:  # noqa: D401
        pass


def _node_ready() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    major = int(subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip().lstrip("v").split(".")[0])
    if major < 20:
        pytest.skip("the login-oauth node stack needs Node 20+")
    return node


def test_the_login_template_node_app_signs_a_member_in_through_boss(w, tmp_path, monkeypatch):
    import httpx

    from boss_engine.config import get_settings

    node = _node_ready()
    app_dir = tmp_path / "login-app"
    shutil.copytree(NODE_STACK, app_dir)
    npm = subprocess.run(["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"], cwd=app_dir,
                         capture_output=True, text=True, timeout=300)
    if npm.returncode != 0:
        pytest.skip(f"npm install failed (offline?): {npm.stderr[-300:]}")

    # BOSS on a real loopback port: its issuer is http://127.0.0.1:<port>/oidc/<org>.
    boss_port, app_port = _free_port(), _free_port()
    boss = f"http://127.0.0.1:{boss_port}"
    monkeypatch.setattr(get_settings(), "public_url", boss)
    w.http = TestClient(main.app, raise_server_exceptions=True, base_url=boss)
    w.issuer = f"{boss}/oidc/{w.ids.org}"
    _Bridge.http = w.http
    bridge = ThreadingHTTPServer(("127.0.0.1", boss_port), _Bridge)
    threading.Thread(target=bridge.serve_forever, daemon=True).start()

    # The app is deployed at its own origin: one click registers it and binds the settings.
    app_origin = f"http://127.0.0.1:{app_port}"
    _deploy_target(w, app_origin)
    assert _connect(w).status_code == 200
    assert f"{app_origin}/auth/callback/boss" in _client(w)["redirect_uris"]

    # Exactly what a preview of this app is handed (its secrets from the vault), at the app's address.
    with begin() as db:
        env = app_sign_in.preview_environment(db, project_id=w.ids.project_a, run_id=w.run, preview_id="prv_proof")
    assert {"OIDC_BOSS_CLIENT_ID", "OIDC_BOSS_CLIENT_SECRET", "OIDC_BOSS_ISSUER", "SESSION_SECRET"} <= set(env)
    env["APP_BASE_URL"] = app_origin
    proc = subprocess.Popen([node, "server.mjs"], cwd=app_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env={"PATH": os.environ.get("PATH", ""), "PORT": str(app_port), **env})
    try:
        browser = httpx.Client(base_url=app_origin, follow_redirects=False, timeout=15)
        for _ in range(100):
            try:
                if browser.get("/auth/providers").status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.1)
        providers = browser.get("/auth/providers").json()["providers"]
        assert {"id": "boss", "name": "BOSS", "login_url": "/auth/login/boss"} in providers
        assert browser.get("/api/me").status_code == 401

        # 1. the app sends the browser to BOSS (state, PKCE S256, nonce)
        go = browser.get("/auth/login/boss")
        assert go.status_code == 302, go.text
        auth = urlsplit(go.headers["location"])
        assert f"{auth.scheme}://{auth.netloc}{auth.path}" == f"{w.issuer}/authorize"
        q = query_of(go.headers["location"])
        assert q["code_challenge_method"] == "S256" and q["nonce"] and q["state"]
        assert q["redirect_uri"] == f"{app_origin}/auth/callback/boss"

        # 2. a signed-in BOSS member who may sign in to this project's apps consents
        member = w.person("ada", role="viewer", project="a")
        page = w.http.get(f"{auth.path}?{auth.query}", headers={"Cookie": f"__Host-boss_session={member.cookie}"},
                          follow_redirects=False)
        assert page.status_code == 200 and "Login demo wants to sign you in" in page.text, page.text[:400]
        back = w.consent(page, member, origin=boss)
        assert back.status_code == 303
        assert back.headers["location"].startswith(f"{app_origin}/auth/callback/boss?code=oac_")

        # 3. the app exchanges the code at BOSS (its bound client secret), checks the ID token, signs in
        done = browser.get(back.headers["location"].removeprefix(app_origin))
        assert done.status_code == 302 and done.headers["location"] == "/", done.text
        me = browser.get("/api/me")
        assert me.status_code == 200, me.text
        assert me.json()["email"].startswith("ada-") and me.json()["providers"] == ["boss"]
        assert browser.get("/").text.startswith("Signed in as ada-")
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=5)[0].decode(errors="replace")
        except subprocess.TimeoutExpired:
            proc.kill()
            out = ""
        bridge.shutdown()
        assert env["OIDC_BOSS_CLIENT_SECRET"] not in out                    # the app never logs it


def test_the_login_template_node_app_signs_a_member_in_inside_a_preview(w, tmp_path, monkeypatch):
    """Sign-in INSIDE a preview: the browser reaches the app only through the preview origin
    (/v1/previews/<id>/web, apps/api/app/preview_origin.py), which now carries the app's own session
    cookie (tagged, scoped to the preview's path) and never BOSS's. The browser holds the console
    session too, as a real one does (cookies are not port-scoped): it never reaches the app, and
    another preview gets none of this preview's cookies."""
    from apps.api.app.preview_origin import preview_only
    from boss_engine.services import preview_service

    node = _node_ready()
    app_dir = tmp_path / "login-app"
    shutil.copytree(NODE_STACK, app_dir)
    npm = subprocess.run(["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"], cwd=app_dir,
                         capture_output=True, text=True, timeout=300)
    if npm.returncode != 0:
        pytest.skip(f"npm install failed (offline?): {npm.stderr[-300:]}")

    from boss_engine.config import get_settings

    boss_port, app_port, preview_port = _free_port(), _free_port(), _free_port()
    boss = f"http://127.0.0.1:{boss_port}"
    previews = f"http://localhost:{preview_port}"
    monkeypatch.setattr(get_settings(), "public_url", boss)
    monkeypatch.setenv("BOSS_PREVIEW_ORIGIN", previews)
    w.http = TestClient(main.app, raise_server_exceptions=True, base_url=boss)
    w.issuer = f"{boss}/oidc/{w.ids.org}"
    _Bridge.http = w.http
    bridge = ThreadingHTTPServer(("127.0.0.1", boss_port), _Bridge)
    threading.Thread(target=bridge.serve_forever, daemon=True).start()

    # The app runs as a preview of its build; a second preview (another app) on the same process.
    pid, other = f"prv_{uuid.uuid4().hex[:10]}", f"prv_{uuid.uuid4().hex[:10]}"
    with begin() as db:
        for i, run in ((pid, w.run), (other, w.other_run)):
            db.execute(text("INSERT INTO app_previews (id, project_id, run_id, repo, branch, port, url, status, mode) "
                            "VALUES (:i, :p, :r, 'acme/demo', 'main', :port, :u, 'running', 'process')"),
                       {"i": i, "p": w.ids.project_a, "r": run, "port": app_port, "u": f"http://127.0.0.1:{app_port}"})
    assert _connect(w).status_code == 200
    base = f"{previews}/v1/previews/{pid}/web"
    assert f"{base}/auth/callback/boss" in _client(w)["redirect_uris"]
    with begin() as db:
        env = app_sign_in.preview_environment(db, project_id=w.ids.project_a, run_id=w.run, preview_id=pid)
    assert env["APP_BASE_URL"] == base

    forwarded: list = []
    real_proxy = preview_service.proxy_preview

    def spy(*a, **kw):
        forwarded.append((a[1], kw.get("app_cookie_header")))
        return real_proxy(*a, **kw)

    monkeypatch.setattr(preview_service, "proxy_preview", spy)
    proc = subprocess.Popen([node, "server.mjs"], cwd=app_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env={"PATH": os.environ.get("PATH", ""), "PORT": str(app_port), **env})
    try:
        member = w.person("ada", role="viewer", project="a")
        browser = TestClient(preview_only(main.app), base_url=previews, follow_redirects=False)
        # A browser keeps a Secure cookie on http://localhost (preview_cookies.for_the_sandbox makes
        # the app's cookies SameSite=None; Secure there); httpx never sends one over http. This test
        # is the sign-in flow, so its "browser" is served as on plain http: the cookies as made.
        from boss_engine.services import preview_cookies as _pc
        monkeypatch.setattr(_pc, "is_secure_context", lambda *_a, **_k: False)
        browser.cookies.set("__Host-boss_session", member.cookie)          # the console's, as a browser holds it
        import httpx

        for _ in range(100):   # wait at the app itself: the gateway marks an unreachable preview stopped
            try:
                if httpx.get(f"http://127.0.0.1:{app_port}/auth/providers", timeout=2).status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.1)
        assert browser.get(f"/v1/previews/{pid}/web/").text == "Signed out"

        # 1. through the preview: the app sends the browser to BOSS and sets its own (tagged) cookie
        go = browser.get(f"/v1/previews/{pid}/web/auth/login/boss")
        assert go.status_code == 302, go.text
        q = query_of(go.headers["location"])
        assert q["redirect_uri"] == f"{base}/auth/callback/boss"
        for c in go.headers.get_list("set-cookie"):
            assert f"Path=/v1/previews/{pid}/" in c and "boss" not in c.split("=", 1)[0].lower()[14:], c

        # 2. the member consents at BOSS (the console's origin)
        auth = urlsplit(go.headers["location"])
        page = w.http.get(f"{auth.path}?{auth.query}", headers={"Cookie": f"__Host-boss_session={member.cookie}"},
                          follow_redirects=False)
        assert page.status_code == 200, (page.headers.get("location"), page.text[:400])
        back = w.consent(page, member, origin=boss)
        assert back.status_code == 303 and back.headers["location"].startswith(f"{base}/auth/callback/boss?code=")

        # 3. back through the preview: the app checks its state cookie, exchanges the code, signs in
        done = browser.get(back.headers["location"].removeprefix(previews))
        assert done.status_code == 302, done.text
        assert done.headers["location"] == f"/v1/previews/{pid}/web/"
        assert browser.get(done.headers["location"]).text.startswith("Signed in as ada-")
        me = browser.get(f"/v1/previews/{pid}/web/api/me")
        assert me.status_code == 200 and me.json()["providers"] == ["boss"]

        # the other preview (same process, same browser) is signed out: none of this one's cookies
        assert browser.get(f"/v1/previews/{other}/web/").text == "Signed out"
        assert browser.get(f"/v1/previews/{other}/web/api/me").status_code == 401
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=5)[0].decode(errors="replace")
        except subprocess.TimeoutExpired:
            proc.kill()
            out = ""
        bridge.shutdown()
        assert env["OIDC_BOSS_CLIENT_SECRET"] not in out
    # what reached the app: its own cookies only; never the console session, never a boss_ cookie
    assert any(cookie for _, cookie in forwarded), "the app's session cookie reached it"
    for preview_id, cookie in forwarded:
        assert member.cookie not in (cookie or "") and "boss" not in (cookie or "").lower(), cookie
        if preview_id == other:
            assert cookie is None, cookie
