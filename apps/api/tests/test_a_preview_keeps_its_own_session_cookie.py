"""A PREVIEW KEEPS ITS OWN SESSION COOKIE ON ITS OWN ORIGIN (BOSSAPP-946, finding 2).

The local-first HelpDesk's preview on localhost:3001 was sandboxed without allow-same-origin: the
page's origin was opaque, every request to its own backend cross-site, and the session cookie its
sign-in set was never kept, so the app stopped at sign-in. Previews are served from their own
origin precisely so a real origin is safe there.

Pinned:
  * a preview PAGE answered by the preview origin's listener carries allow-same-origin, and the
    app's own (tagged) session cookie is relayed to it;
  * the same page answered on BOSS's origin never does (opaque sandbox, as before);
  * the preview origin can never be BOSS's own origin: not by BOSS_PREVIEW_PORT set to the
    console's port, not by BOSS_PREVIEW_ORIGIN naming BOSS_PUBLIC_URL, not by a request that
    reaches the listener under BOSS's public name.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app.preview_origin import page_csp, preview_only
from boss_engine.config import get_settings
from boss_engine.services.preview_cookies import outbound_set_cookie
from boss_engine.services.preview_service import (
    PREVIEW_ORIGIN_SANDBOX_CSP, PREVIEW_SANDBOX_CSP, configured_preview_origin, preview_origin_for,
)


def _tokens(csp: str) -> list[str]:
    return (csp or "").replace(";", " ").split()


def _app_that_signs_in(preview_id: str):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"text/html"),
            (b"set-cookie", outbound_set_cookie(preview_id, "sid=abc; Path=/; HttpOnly; "
                                                            "SameSite=Lax").encode()),
        ]})
        await send({"type": "http.response.body", "body": b"<p>signed in</p>"})
    return app


def test_on_the_preview_origin_the_page_has_a_real_origin_and_keeps_its_cookie():
    assert "allow-same-origin" in _tokens(PREVIEW_ORIGIN_SANDBOX_CSP)
    assert "allow-scripts" in _tokens(PREVIEW_ORIGIN_SANDBOX_CSP)
    edge = TestClient(preview_only(_app_that_signs_in("prev_hd1")), base_url="http://prev-hd1.localhost:3001")
    page = edge.post("/v1/previews/prev_hd1/web/api/login", content=b"{}")
    assert page.status_code == 200
    assert page.headers["content-security-policy"] == PREVIEW_ORIGIN_SANDBOX_CSP
    cookie = page.headers.get("set-cookie") or ""
    assert "_sid=abc" in cookie and "Path=/v1/previews/prev_hd1/" in cookie, cookie
    # What is not a preview page stays opaque: the 404 for anything else.
    other = edge.get("/v1/identity/me")
    assert other.status_code == 404 and "allow-same-origin" not in _tokens(
        other.headers["content-security-policy"])


def test_on_boss_origin_a_preview_page_stays_opaque(client):
    created = client.post("/v1/previews/fixture",
                          json={"project_id": "boss-dogfood", "actor": "ceo", "ttl_minutes": 15})
    assert created.status_code == 201, created.text
    page = client.get(f"/v1/previews/{created.json()['id']}/web/")
    assert page.status_code == 200, page.text
    tokens = _tokens(page.headers["content-security-policy"])
    assert "sandbox" in tokens and "allow-same-origin" not in tokens
    assert PREVIEW_SANDBOX_CSP == page.headers["content-security-policy"]


def test_the_preview_origin_is_never_boss_own_origin(monkeypatch):
    settings = get_settings()
    monkeypatch.delenv("BOSS_PREVIEW_ORIGIN", raising=False)
    monkeypatch.setattr(settings, "public_url", "https://boss.example.com", raising=False)
    # The default: the console's host on another port.
    monkeypatch.delenv("BOSS_PREVIEW_PORT", raising=False)
    assert preview_origin_for("localhost:3000") == "http://localhost:3001"
    # BOSS_PREVIEW_PORT set to the console's own port: no preview origin at all.
    monkeypatch.setenv("BOSS_PREVIEW_PORT", "3000")
    assert preview_origin_for("localhost:3000") == ""
    monkeypatch.setenv("BOSS_PREVIEW_PORT", "443")
    assert preview_origin_for("boss.example.com", "https") == ""
    # BOSS_PREVIEW_ORIGIN naming BOSS's public URL (default port spelled or not): refused.
    monkeypatch.delenv("BOSS_PREVIEW_PORT", raising=False)
    for same in ("https://boss.example.com", "https://boss.example.com:443/x"):
        monkeypatch.setenv("BOSS_PREVIEW_ORIGIN", same)
        assert configured_preview_origin() == ""
    # ... or naming the origin the browser reached BOSS at.
    monkeypatch.setenv("BOSS_PREVIEW_ORIGIN", "http://localhost:3000")
    assert preview_origin_for("localhost:3000") == ""
    monkeypatch.setenv("BOSS_PREVIEW_ORIGIN", "https://previews.example.com")
    assert preview_origin_for("boss.example.com", "https") == "https://previews.example.com"


def test_the_listener_reached_under_boss_public_name_stays_opaque(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_url", "https://boss.example.com", raising=False)
    boss = {"type": "http", "scheme": "https", "headers": [(b"host", b"boss.example.com")]}
    own = {"type": "http", "scheme": "https", "headers": [(b"host", b"previews.example.com")]}
    assert page_csp(boss) == PREVIEW_SANDBOX_CSP
    assert page_csp(own) == PREVIEW_ORIGIN_SANDBOX_CSP
    assert page_csp({"type": "http", "headers": []}) == PREVIEW_SANDBOX_CSP
