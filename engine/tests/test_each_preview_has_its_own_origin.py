"""Each preview is its own origin (BOSSAPP-947).

The preview listener allows allow-same-origin so a login app can keep its session cookie.
That is safe only when two previews cannot read each other. One host for every preview
(localhost:3001) is one origin: a script in preview A can fetch preview B. The address BOSS
hands out names the preview in the host (`prev-aaa.localhost`), and the listener serves the
page only on that host.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from apps.api.app.preview_origin import preview_only
from boss_engine.services.preview_service import preview_origin_for, public_preview_url


def _recorder():
    seen: dict = {}

    async def app(scope, receive, send):
        seen["path"] = scope.get("path")
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"text/html"),
        ]})
        await send({"type": "http.response.body", "body": b"<p>app</p>"})

    return app, seen


def test_two_previews_are_handed_different_origins(monkeypatch):
    monkeypatch.delenv("BOSS_PREVIEW_ORIGIN", raising=False)
    monkeypatch.delenv("BOSS_PREVIEW_PORT", raising=False)
    first = urlsplit(public_preview_url("prev_aaa", "localhost:3000"))
    second = urlsplit(public_preview_url("prev_bbb", "localhost:3000"))
    assert first.scheme == "http" and second.scheme == "http"
    assert first.hostname == "prev-aaa.localhost"
    assert second.hostname == "prev-bbb.localhost"
    assert first.netloc != second.netloc
    assert first.port == 3001 and second.port == 3001
    assert first.path == "/v1/previews/prev_aaa/web"
    assert second.path == "/v1/previews/prev_bbb/web"
    # No id: the listener's own address, which is not a page origin.
    assert preview_origin_for("localhost:3000") == "http://localhost:3001"
    # An IP cannot take a label, so the page is still a localhost name.
    assert preview_origin_for("127.0.0.1:3000", preview_id="prev_abc") == "http://prev-abc.localhost:3001"
    assert preview_origin_for("testserver", preview_id="prev_abc") == "http://prev-abc.testserver:3001"
    monkeypatch.setenv("BOSS_PREVIEW_ORIGIN", "https://previews.example.test")
    assert preview_origin_for("boss.example", "https", "prev_abc") == "https://prev-abc.previews.example.test"


def test_a_preview_page_is_not_served_on_another_previews_host():
    inner, seen = _recorder()
    client = TestClient(preview_only(inner), base_url="http://prev-bbb.localhost:3001")
    answer = client.get("/v1/previews/prev_aaa/web/", follow_redirects=False)
    assert answer.status_code == 404
    assert seen == {}
    assert b"app" not in answer.content


def test_the_shared_host_does_not_return_the_preview_page():
    inner, seen = _recorder()
    client = TestClient(preview_only(inner), base_url="http://localhost:3001")
    answer = client.get("/v1/previews/prev_aaa/web/tickets?open=1", follow_redirects=False)
    assert answer.status_code == 307
    assert answer.headers["location"] == "http://prev-aaa.localhost:3001/v1/previews/prev_aaa/web/tickets?open=1"
    assert "allow-same-origin" not in (answer.headers.get("content-security-policy") or "")
    assert seen == {}
