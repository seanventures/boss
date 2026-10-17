"""Previews are served from their OWN origin, by a listener that serves nothing else (security
finding #4: a percent-encoded preview path on the console's origin dropped the sandbox CSP, and
the previewed app read the CSRF token from /v1/identity/me and wrote with the operator's cookie).

apps/api/app/preview_origin.py: preview capability paths only (everything else 404 before the API
sees it), every cookie and credential header stripped (cookies are not port-scoped: the browser
DOES send the console's cookie to the preview port), never a Set-Cookie, always the sandbox CSP.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app.preview_origin import is_preview_capability_path, listen_settings, preview_only
from apps.api.serve import preview_listen_env
from boss_engine.services.preview_service import (
    PREVIEW_SANDBOX_CSP, operator_preview_open_url, preview_origin_for,
)

SESSION = "__Host-boss_session"


def _recorder():
    seen: dict = {}

    async def app(scope, receive, send):
        seen["path"] = scope["path"]
        seen["headers"] = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"text/html"),
            (b"set-cookie", f"{SESSION}=stolen; Path=/".encode()),
            (b"content-security-policy", b"default-src *"),
            (b"access-control-allow-credentials", b"true"),
        ]})
        await send({"type": "http.response.body", "body": b"<p>app</p>"})

    return app, seen


def _sandboxed(response) -> None:
    tokens = (response.headers.get("content-security-policy") or "").replace(";", " ").split()
    assert "sandbox" in tokens and "allow-same-origin" not in tokens, response.headers


def test_only_the_preview_capability_paths_are_served():
    inner, seen = _recorder()
    client = TestClient(preview_only(inner))
    for path in ("/v1/identity/me", "/v1/auth/options", "/", "/v1/previews", "/v1/previews/p1",
                 "/v1/previews/p1/gate", "/v1/previews/p1/approve", "/v1/previews/p1/logs",
                 "/v1/previews/p1/%77eb/", "/v1/previews/p1/WEB/", "/v1/previews/../web/x",
                 "/v1/previews/p1/feedbackx", "/health"):
        answer = client.get(path, headers={"cookie": f"{SESSION}=bss_secret"})
        assert answer.status_code == 404, path
        _sandboxed(answer)
        assert "set-cookie" not in answer.headers
    assert seen == {}, "a non-preview path reached the app behind the preview origin"
    assert is_preview_capability_path("/v1/previews/p1/web/")
    assert is_preview_capability_path("/v1/previews/p1/web/assets/a%20b.js")
    assert is_preview_capability_path("/v1/previews/p1/feedback")


def test_every_credential_is_stripped_and_no_cookie_is_ever_set():
    inner, seen = _recorder()
    client = TestClient(preview_only(inner))
    answer = client.post("/v1/previews/p1/web/api/save", content=b"{}", headers={
        "cookie": f"{SESSION}=bss_secret; __Host-boss_preauth=x; other=1",
        "authorization": "Bearer boss_pat_x", "proxy-authorization": "Basic x",
        "x-boss-csrf": "csrf", "x-boss-console-auth": "network", "x-boss-api-key": "k",
        "x-forwarded-user": "ceo", "x-forwarded-for": "1.2.3.4", "remote-user": "ceo",
        "tailscale-user-login": "ceo@x", "x-user-id": "usr_1", "x-api-key": "k",
        "content-type": "application/json", "x-request-id": "keep-me",
    })
    assert answer.status_code == 200
    headers = seen["headers"]
    for name in ("cookie", "authorization", "proxy-authorization", "x-boss-csrf",
                 "x-boss-console-auth", "x-boss-api-key", "x-forwarded-user", "x-forwarded-for",
                 "remote-user", "tailscale-user-login", "x-user-id", "x-api-key"):
        assert name not in headers, name
    # The app's own headers pass: only credentials and identity claims are taken.
    assert headers["x-request-id"] == "keep-me"
    assert headers["content-type"] == "application/json"
    assert "set-cookie" not in answer.headers
    assert answer.headers["content-security-policy"] == PREVIEW_SANDBOX_CSP
    assert "access-control-allow-credentials" not in answer.headers


def _fixture_preview(client) -> str:
    created = client.post("/v1/previews/fixture",
                          json={"project_id": "boss-dogfood", "actor": "ceo", "ttl_minutes": 15})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_the_real_preview_handlers_answer_on_the_preview_origin(client):
    from apps.api.app.main import app

    preview_id = _fixture_preview(client)
    previews = TestClient(preview_only(app))
    page = previews.get(f"/v1/previews/{preview_id}/web/",
                        headers={"cookie": f"{SESSION}=bss_whatever"})
    assert page.status_code == 200, page.text
    _sandboxed(page)
    assert "set-cookie" not in page.headers
    # The API's authenticated surface is not reachable through it, signed in or not.
    me = previews.get("/v1/identity/me", headers={"cookie": f"{SESSION}=bss_whatever"})
    assert me.status_code == 404


def test_the_api_sends_a_signed_in_browser_to_the_preview_origin(client, monkeypatch):
    monkeypatch.delenv("BOSS_PREVIEW_ORIGIN", raising=False)
    monkeypatch.delenv("BOSS_PREVIEW_PORT", raising=False)
    preview_id = _fixture_preview(client)
    client.cookies.set(SESSION, "bss_whatever")
    moved = client.get(f"/v1/previews/{preview_id}/web/x?y=1", follow_redirects=False)
    assert moved.status_code == 307, moved.text
    assert moved.headers["location"] == f"http://testserver:3001/v1/previews/{preview_id}/web/x?y=1"
    _sandboxed(moved)
    monkeypatch.setenv("BOSS_PREVIEW_PORT", "off")
    refused = client.get(f"/v1/previews/{preview_id}/web/", follow_redirects=False)
    assert refused.status_code == 403
    _sandboxed(refused)


def test_the_preview_origin_is_another_port_on_the_consoles_host(monkeypatch):
    monkeypatch.delenv("BOSS_PREVIEW_ORIGIN", raising=False)
    monkeypatch.delenv("BOSS_PREVIEW_PORT", raising=False)
    assert preview_origin_for("localhost:3000") == "http://localhost:3001"
    assert preview_origin_for("boss.example:443", "https") == "https://boss.example:3001"
    assert preview_origin_for("evil.com/x@y") == ""
    monkeypatch.setenv("BOSS_PREVIEW_PORT", "4100")
    assert preview_origin_for("127.0.0.1:3000") == "http://127.0.0.1:4100"
    monkeypatch.setenv("BOSS_PREVIEW_ORIGIN", "http://localhost:3001/ignored/path")
    assert preview_origin_for("anything") == "http://localhost:3001"
    # A loopback preview origin IS a separate origin from a loopback console: it is allowed.
    assert operator_preview_open_url("prev_1") == "http://localhost:3001/v1/previews/prev_1/web/"


def test_only_a_single_server_process_binds_the_preview_port(monkeypatch):
    monkeypatch.delenv("BOSS_PREVIEW_LISTEN", raising=False)
    assert listen_settings() is None    # tests and plain imports never bind
    assert preview_listen_env(["--host", "0.0.0.0", "--port", "8000"]) == {
        "BOSS_PREVIEW_LISTEN": "1", "BOSS_PREVIEW_HOST": "0.0.0.0"}
    assert preview_listen_env(["--workers", "4"]) == {}
    monkeypatch.setenv("BOSS_PREVIEW_LISTEN", "1")
    monkeypatch.setenv("BOSS_PREVIEW_PORT", "3001")
    monkeypatch.setenv("BOSS_PREVIEW_HOST", "0.0.0.0")
    assert listen_settings() == ("0.0.0.0", 3001)
    monkeypatch.setenv("BOSS_PREVIEW_PORT", "0")
    assert listen_settings() is None


def test_a_previews_preflight_is_answered_without_credentials():
    inner, seen = _recorder()
    client = TestClient(preview_only(inner))
    ok = client.options("/v1/previews/p1/feedback", headers={
        "origin": "null", "access-control-request-method": "POST",
        "access-control-request-headers": "content-type, x-boss-csrf, authorization, x-request-id"})
    assert ok.status_code == 204
    assert ok.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in ok.headers
    assert ok.headers["access-control-allow-headers"] == "content-type, x-request-id"
    assert seen == {}
    assert client.options("/v1/identity/me").status_code == 404


def test_a_reload_at_the_apps_own_path_goes_back_to_its_preview():
    """On the preview origin an SPA is shown at its own paths (preview_service._inject_shim,
    at_root), so a reload asks for "/tickets". A page navigation there gets the static bootstrap,
    which sends the tab back to the preview its window.name names; anything else is still a 404
    that never reaches the app, and /v1/ paths never get it."""
    inner, seen = _recorder()
    client = TestClient(preview_only(inner))
    nav = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document",
           "cookie": f"{SESSION}=bss_secret"}
    answer = client.get("/tickets/42?open=1", headers=nav)
    assert answer.status_code == 200
    _sandboxed(answer)
    assert "set-cookie" not in answer.headers
    assert "location.replace(\"/v1/previews/\"+id+\"/web\"+location.pathname" in answer.text
    assert "boss-preview:" in answer.text
    for path, headers in (("/tickets", {}), ("/app.js", {"sec-fetch-mode": "no-cors",
                                                       "sec-fetch-dest": "script"}),
                          ("/v1/identity/me", nav)):
        assert client.get(path, headers=headers).status_code == 404, path
    assert seen == {}, "the bootstrap must never reach the app behind the origin"


def test_the_preview_origin_shows_the_app_at_its_own_paths(client, monkeypatch):
    """The API's own preview handler, reached through the preview origin, asks the shim for
    at_root; reached on the API origin it does not."""
    from apps.api.app import main as api_main
    from boss_engine.services import preview_service

    asked: list = []

    def proxy(*_a, serve_at_root=False, **_k):
        asked.append(serve_at_root)
        return 200, "text/html", b"<html><head></head></html>", {}

    monkeypatch.setattr(preview_service, "proxy_preview", proxy)
    monkeypatch.setattr(api_main, "database_is_reachable", lambda _e: True)
    TestClient(preview_only(api_main.app)).get("/v1/previews/prv_x/web/")
    client.get("/v1/previews/prv_x/web/")
    assert asked == [True, False]
