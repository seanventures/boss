"""BOSS-341: never hand out the preview's internal address as the View URL.

app_previews.url holds http://127.0.0.1:<port> — reachable from the API process
and from nowhere a browser runs. List/start responses must expose the gateway
path operators can open, and keep the loopback address under internal_url.
"""

from __future__ import annotations

from boss_engine.services.preview_service import (
    operator_preview_open_url,
    publicize_preview,
    _is_loopback_url,
)


def test_operator_open_url_is_the_api_gateway_path():
    assert operator_preview_open_url("prev_abc") == "/v1/previews/prev_abc/web/"


def test_loopback_detection():
    assert _is_loopback_url("http://127.0.0.1:41635")
    assert _is_loopback_url("http://localhost:3000")
    assert not _is_loopback_url("/v1/previews/prev_x/web/")
    assert not _is_loopback_url(None)


def test_publicize_replaces_loopback_with_gateway_path():
    out = publicize_preview({
        "id": "prev_6bd9728fb32748b5864f",
        "status": "expired",
        "url": "http://127.0.0.1:41635",
        "port": 41635,
    })
    assert out["url"] == "/v1/previews/prev_6bd9728fb32748b5864f/web/"
    assert out["open_url"] == out["url"]
    assert out["internal_url"] == "http://127.0.0.1:41635"
    assert "127.0.0.1" not in (out["url"] or "")
    assert "localhost" not in (out["url"] or "")


def test_publicize_running_without_url_still_offers_gateway():
    out = publicize_preview({"id": "prev_live", "status": "running", "url": None})
    assert out["url"] == "/v1/previews/prev_live/web/"
    assert out["open_url"] == out["url"]


def test_publicize_failed_with_no_bind_stays_null():
    out = publicize_preview({"id": "prev_fail", "status": "failed", "url": None})
    assert out["url"] is None
    assert out.get("open_url") is None


def test_publicize_list_shape_has_no_loopback_primary_urls():
    rows = [
        {"id": "prev_a", "status": "expired", "url": "http://127.0.0.1:1"},
        {"id": "prev_b", "status": "stopped", "url": "http://127.0.0.1:2"},
        {"id": "prev_c", "status": "failed", "url": None},
    ]
    public = [publicize_preview(r) for r in rows]
    primary = [p.get("url") for p in public if p.get("url")]
    assert primary == [
        "/v1/previews/prev_a/web/",
        "/v1/previews/prev_b/web/",
    ]
    assert all("127." not in u and "localhost" not in u for u in primary)
    assert public[0]["internal_url"] == "http://127.0.0.1:1"
