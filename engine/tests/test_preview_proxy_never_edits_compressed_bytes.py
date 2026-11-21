"""A preview opened as binary garbage (recipe-box, Next.js, 2026-11-18).

The browser's `Accept-Encoding: gzip, br` was forwarded to the app, Next.js gzipped its HTML,
and the proxy spliced its <base> + shim into the COMPRESSED bytes (decoding them as UTF-8 lost
every invalid byte, 0x8b first). The API's GZip middleware then compressed the result again, so
the browser undid one layer and showed the other. Every Next chunk was a gzip stream served as
JavaScript.

The class: the proxy never edits or relabels an encoded body. It asks the app for identity,
decodes whatever encoding the app sends anyway, and passes an encoding it cannot decode on
byte for byte, still labelled.
"""
from __future__ import annotations

import contextlib
import gzip
import http.server
import threading
import zlib

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient
from starlette.middleware.gzip import GZipMiddleware

from boss_engine.services import preview_service

HTML = (b"<!DOCTYPE html><html><head><title>Recipe box</title></head>"
        b"<body><script src=\"/_next/static/chunks/main.js\"></script>"
        + b"<p>pad</p>" * 200 + b"</body></html>")
PNG = bytes(range(256)) * 8          # every byte value, 0x8b included
JS = b"self.__BUILD_MANIFEST={};" + b"/* pad */" * 300

#: path -> (content-type, content-encoding or None, body as sent)
ROUTES: dict[str, tuple[str, str | None, bytes]] = {
    "/": ("text/html; charset=utf-8", "gzip", gzip.compress(HTML)),
    "/deflate": ("text/html", "deflate", zlib.compress(HTML)),
    "/raw-deflate": ("text/html", "deflate",
                     (lambda c: c.compress(HTML) + c.flush())(zlib.compressobj(wbits=-zlib.MAX_WBITS))),
    "/plain": ("text/html", None, HTML),
    "/chunk.js": ("application/javascript", "gzip", gzip.compress(JS)),
    "/logo.png": ("image/png", None, PNG),
    "/logo-gz.png": ("image/png", "gzip", gzip.compress(PNG)),
    "/unknown": ("text/html", "zstd", b"\x28\xb5\x2f\xfd-not-really-zstd"),
    "/missing": ("text/html", "gzip", gzip.compress(b"<html><head></head><body>404</body></html>")),
    # A hostile app that ignores identity: 64 MB of zeros in ~64 KB of gzip.
    "/bomb": ("text/html", "gzip", gzip.compress(b"\0" * (64 << 20), compresslevel=9)),
    "/bomb.js": ("application/javascript", "gzip, gzip",
                 gzip.compress(gzip.compress(b"\0" * (64 << 20), compresslevel=9))),
}


class _App(http.server.BaseHTTPRequestHandler):
    seen: list[str | None] = []

    def do_GET(self):  # noqa: N802 — http.server's name
        _App.seen.append(self.headers.get("Accept-Encoding"))
        path = self.path.split("?", 1)[0]
        ctype, encoding, body = ROUTES[path]
        self.send_response(404 if path == "/missing" else 200)
        self.send_header("Content-Type", ctype)
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


class _Rows:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class _Conn:
    def __init__(self, row):
        self.row = row

    def execute(self, *_a, **_k):
        return _Rows(self.row)


@pytest.fixture()
def upstream(monkeypatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _App)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    row = {"url": f"http://127.0.0.1:{server.server_address[1]}", "status": "running",
           "mode": "process"}

    @contextlib.contextmanager
    def begin(_engine):
        yield _Conn(row)

    monkeypatch.setattr(preview_service, "begin", begin)
    _App.seen.clear()
    yield server
    server.shutdown()


def _proxy(path: str):
    return preview_service.proxy_preview(
        None, "prev_abc123", path.lstrip("/"), "GET",
        incoming_headers={"Accept-Encoding": "gzip, deflate, br", "Accept": "*/*"},
        serve_at_root=True)


def test_the_app_is_asked_for_identity_whatever_the_browser_accepts(upstream):
    _proxy("/plain")
    assert _App.seen == ["identity"]


def test_isolated_preview_preserves_same_origin_writes(upstream, monkeypatch):
    def write(handler):
        same_origin = handler.headers.get("Origin") == "http://" + handler.headers.get("Host", "")
        handler.send_response(200 if same_origin else 403)
        handler.end_headers()
    monkeypatch.setattr(_App, "do_POST", write, raising=False)
    host = "prev-abc123.localhost:3001"
    for origin, expected in [(f"http://{host}", 200), ("http://untrusted.example", 403)]:
        status, *_ = preview_service.proxy_preview(
            None, "prev_abc123", "save", "POST", incoming_headers={"Host": host, "Origin": origin},
            serve_at_root=True)
        assert status == expected


@pytest.mark.parametrize("path", ["/", "/deflate", "/raw-deflate", "/missing"])
def test_compressed_html_is_decoded_before_the_shim_goes_in(upstream, path):
    status, ctype, data, headers = _proxy(path)
    assert "content-encoding" not in {k.lower() for k in headers}
    assert data.startswith(b"<!DOCTYPE html><html><head><base href=") or \
        data.startswith(b"<html><head><base href=")
    assert b"data-boss-preview-shim" in data
    if path != "/missing":
        assert b"/v1/previews/prev_abc123/web/_next/static/chunks/main.js" in data
        assert data.count(b"<p>pad</p>") == 200
    else:
        assert status == 404


def test_a_compressed_script_arrives_as_the_script(upstream):
    _status, _ctype, data, headers = _proxy("/chunk.js")
    assert data == JS
    assert "Content-Encoding" not in headers


@pytest.mark.parametrize("path", ["/logo.png", "/logo-gz.png"])
def test_binary_bodies_pass_through_byte_for_byte(upstream, path):
    _status, ctype, data, headers = _proxy(path)
    assert ctype == "image/png"
    assert data == PNG
    assert "Content-Encoding" not in headers


def test_an_encoding_this_host_cannot_undo_is_passed_on_labelled_and_untouched(upstream):
    _status, _ctype, data, headers = _proxy("/unknown")
    assert data == ROUTES["/unknown"][2], "an encoded body is never edited"
    assert headers["Content-Encoding"] == "zstd"


def test_brotli_is_passed_on_untouched_never_spliced_into():
    """br cannot be decoded with a bounded output here, so it is passed on, still labelled br."""
    assert preview_service._plain_body(b"BR:" + HTML, "br") == (b"BR:" + HTML, "br")


def test_stacked_encodings_are_undone_in_reverse_order():
    twice = zlib.compress(gzip.compress(HTML))
    assert preview_service._plain_body(twice, "gzip, deflate") == (HTML, "")
    assert preview_service._plain_body(HTML, "identity") == (HTML, "")
    assert preview_service._plain_body(b"not gzip", "gzip") == (b"not gzip", "gzip")


def test_the_browser_gets_the_page_through_the_apis_gzip_middleware(upstream):
    """End to end the way the API serves it: the proxy's answer through Starlette's GZip
    middleware, read by a client that accepts gzip, is the app's page with the shim — one
    layer of compression, not two."""
    api = FastAPI()
    api.add_middleware(GZipMiddleware, minimum_size=100)

    @api.get("/web/{path:path}")
    def web(path: str = ""):
        status, ctype, data, headers = _proxy("/" + path)
        return Response(content=data, status_code=status, media_type=ctype, headers=headers)

    client = TestClient(api)
    page = client.get("/web/", headers={"Accept-Encoding": "gzip"})
    assert page.headers["content-encoding"] == "gzip"
    assert page.content.startswith(b"<!DOCTYPE html><html><head><base href=")
    script = client.get("/web/chunk.js", headers={"Accept-Encoding": "gzip"})
    assert script.content == JS
    odd = client.get("/web/unknown", headers={"Accept-Encoding": "gzip"})
    assert odd.headers["content-encoding"] == "zstd", "never compressed a second time"


def test_the_browsers_accept_encoding_is_never_forwarded():
    got = preview_service.forwarded_preview_headers({"Accept-Encoding": "gzip, br", "X-A": "1"})
    assert {k.lower() for k in got} == {"x-a"}


def test_a_decompression_bomb_is_refused_without_being_decoded(upstream, monkeypatch):
    """Review of #223 (B1): 0.5 MB of gzip decoded to 512 MB in the API process. Decoding is
    bounded: past BOSS_PREVIEW_MAX_BODY_MB the answer is a 502, and memory stays near the cap."""
    import tracemalloc

    monkeypatch.setenv("BOSS_PREVIEW_MAX_BODY_MB", "1")
    tracemalloc.start()
    try:
        for path in ("/bomb", "/bomb.js"):
            status, ctype, data, _headers = _proxy(path)
            assert status == 502, path
            assert b"too large to show" in data
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, f"decoding held {peak} bytes"
    # The default cap (32 MB) refuses the 64 MB body too, and a body under the cap decodes.
    monkeypatch.delenv("BOSS_PREVIEW_MAX_BODY_MB")
    assert _proxy("/bomb")[0] == 502
    assert _proxy("/")[0] == 200


def test_more_than_two_stacked_encodings_are_never_decoded():
    thrice = gzip.compress(gzip.compress(gzip.compress(HTML)))
    assert preview_service._plain_body(thrice, "gzip, gzip, gzip") == (thrice, "gzip, gzip, gzip")
    twice = gzip.compress(gzip.compress(HTML))
    assert preview_service._plain_body(twice, "gzip, gzip") == (HTML, "")


def test_a_multi_member_gzip_body_decodes_whole():
    body = gzip.compress(b"<html><head>") + gzip.compress(b"</head></html>")
    assert preview_service._plain_body(body, "gzip") == (b"<html><head></head></html>", "")


def test_a_truncated_stream_is_passed_on_as_it_came():
    cut = gzip.compress(HTML)[:-20]
    assert preview_service._plain_body(cut, "gzip") == (cut, "gzip")
