"""BOSS-341: never hand out the preview's internal address as the View URL.

app_previews.url holds http://127.0.0.1:<port> — reachable from the API process
and from nowhere a browser runs. List/start responses must expose the gateway
path operators can open, and keep the loopback address under internal_url.

When BOSS_PREVIEW_PUBLIC_BASE is set, operator URLs become absolute under that
origin; a loopback public base is rejected (fail closed to relative paths).
"""

from __future__ import annotations

from types import SimpleNamespace

from boss_engine.services.preview_service import (
    operator_preview_open_url,
    publicize_preview,
    _is_loopback_url,
    _preview_public_base,
)


def test_operator_open_url_is_the_api_gateway_path():
    assert operator_preview_open_url("prev_abc") == "/v1/previews/prev_abc/web/"


def test_loopback_detection():
    assert _is_loopback_url("http://127.0.0.1:41635")
    assert _is_loopback_url("http://localhost:3000")
    assert _is_loopback_url("http://0.0.0.0:8080")
    assert _is_loopback_url("http://[::1]/80")
    assert not _is_loopback_url("/v1/previews/prev_x/web/")
    assert not _is_loopback_url("https://boss-api.local.sean.network/v1/previews/x/web/")
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


def test_publicize_with_public_base_rewrites_loopback_to_absolute():
    """BOSS_PREVIEW_PUBLIC_BASE → absolute operator URL; internal stays loopback."""
    base = "https://boss-api.local.sean.network"
    out = publicize_preview(
        {
            "id": "prev_abs",
            "status": "running",
            "url": "http://127.0.0.1:41635",
        },
        public_base=base,
    )
    assert out["url"] == f"{base}/v1/previews/prev_abs/web/"
    assert out["open_url"] == out["url"]
    assert out["internal_url"] == "http://127.0.0.1:41635"
    assert "127.0.0.1" not in out["url"]
    assert "localhost" not in out["url"]


def test_publicize_rejects_loopback_public_base():
    """A mis-set loopback BOSS_PREVIEW_PUBLIC_BASE must not reintroduce 127.0.0.1."""
    out = publicize_preview(
        {
            "id": "prev_badbase",
            "status": "running",
            "url": "http://127.0.0.1:9",
        },
        public_base="http://127.0.0.1:8000",
    )
    assert out["url"] == "/v1/previews/prev_badbase/web/"
    assert "127.0.0.1" not in out["url"]
    assert _preview_public_base("http://localhost:9000") == ""
    assert _preview_public_base("https://boss-api.example") == "https://boss-api.example"


def test_publicize_uses_settings_preview_public_base():
    settings = SimpleNamespace(preview_public_base="https://edge.example")
    out = publicize_preview(
        {"id": "prev_set", "status": "running", "url": "http://127.0.0.1:1"},
        settings=settings,
    )
    assert out["url"] == "https://edge.example/v1/previews/prev_set/web/"


def test_list_payload_never_returns_loopback_open_url_when_public_base_set():
    """GET /v1/previews list shape: operator open_url is the public-base absolute
    gateway URL. Loopback may appear only under internal_url.
    """
    base = "https://boss-api.local.sean.network"
    settings = SimpleNamespace(preview_public_base=base)
    rows = [
        {
            "id": "prev_6bd9728fb32748b5864f",
            "status": "expired",
            "url": "http://127.0.0.1:41635",
        },
        {
            "id": "prev_live",
            "status": "running",
            "url": "http://127.0.0.1:43053",
        },
        {"id": "prev_fail", "status": "failed", "url": None},
    ]
    payload = {"previews": [publicize_preview(dict(r), settings=settings) for r in rows]}
    assert len(payload["previews"]) == 3
    for item in payload["previews"]:
        open_url = item.get("open_url")
        primary = item.get("url")
        for field in (open_url, primary):
            if field:
                assert "127.0.0.1" not in field
                assert "localhost" not in field
                assert "::1" not in field
        if item["status"] == "failed":
            assert open_url is None
            continue
        assert open_url == f"{base}/v1/previews/{item['id']}/web/"
        assert primary == open_url
        assert item["internal_url"].startswith("http://127.0.0.1:")


def test_list_previews_applies_public_base_and_never_emits_loopback_open_url(monkeypatch):
    """list_previews (the GET /v1/previews handler) publicizes with settings."""
    from boss_engine.services import preview_service as ps

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def mappings(self):
            return self

        def all(self):
            return self._rows

    class _Conn:
        def execute(self, stmt, params=None):
            sql = str(stmt)
            if "expires_at < now()" in sql:
                return _Result([])
            return _Result(
                [
                    {
                        "id": "prev_a",
                        "status": "expired",
                        "url": "http://127.0.0.1:41635",
                    },
                    {
                        "id": "prev_b",
                        "status": "stopped",
                        "url": "http://127.0.0.1:42203",
                    },
                ]
            )

    class _Begin:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ps, "begin", lambda engine: _Begin())
    settings = SimpleNamespace(preview_public_base="https://boss-api.local.sean.network")
    listed = ps.list_previews(object(), "boss-dogfood", 5, settings)
    assert [p["open_url"] for p in listed] == [
        "https://boss-api.local.sean.network/v1/previews/prev_a/web/",
        "https://boss-api.local.sean.network/v1/previews/prev_b/web/",
    ]
    assert all("127.0.0.1" not in (p.get("open_url") or "") for p in listed)
    assert all("127.0.0.1" not in (p.get("url") or "") for p in listed)
    assert listed[0]["internal_url"] == "http://127.0.0.1:41635"


def test_publicize_falls_back_to_env_when_settings_field_empty(monkeypatch):
    """Live-shaped Settings: field default empty, BOSS_PREVIEW_PUBLIC_BASE set."""
    monkeypatch.setenv("BOSS_PREVIEW_PUBLIC_BASE", "https://boss-api.local.sean.network")
    settings = SimpleNamespace(preview_public_base="")
    out = publicize_preview(
        {"id": "prev_env", "status": "expired", "url": "http://127.0.0.1:9"},
        settings=settings,
    )
    assert out["open_url"] == (
        "https://boss-api.local.sean.network/v1/previews/prev_env/web/"
    )
    assert "127.0.0.1" not in out["open_url"]
