"""Large JSON answers are gzipped; a stream is never buffered (operator decision 2026-10-04).

The Pulse at 50 apps × 96 cells is ~63 KB of JSON and ~1.2 KB gzipped, so the API compresses
JSON app-wide. A compressing layer that held a Server-Sent-Events stream until it closed would
silently break every live view (preview logs, talks, MCP streams): those must arrive event by
event, uncompressed. This drives the API's own middleware configuration — the one main.py
installs — over a JSON route and an SSE route.
"""
from __future__ import annotations

import asyncio
import json

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from apps.api.app.main import app as api


def _gzip_layer():
    layers = [m for m in api.user_middleware if m.cls.__name__ == "GZipMiddleware"]
    assert len(layers) == 1, "the API compresses JSON through exactly one GZip layer"
    return layers[0]


def _app_with_the_apis_gzip():
    async def big(_request):
        return JSONResponse({"rows": [[8, 8, 200]] * 5000})

    async def small(_request):
        return JSONResponse({"ok": True})

    async def sse(_request):
        async def events():
            for i in range(3):
                yield f"event: log\ndata: {i}\n\n"
                await asyncio.sleep(0.05)
        return StreamingResponse(events(), media_type="text/event-stream")

    layer = _gzip_layer()
    return Starlette(routes=[Route("/big", big), Route("/small", small), Route("/sse", sse)],
                     middleware=[Middleware(layer.cls, *layer.args, **layer.kwargs)])


def test_the_api_installs_one_gzip_layer_with_a_minimum_size():
    layer = _gzip_layer()
    assert 512 <= layer.kwargs.get("minimum_size", 500) <= 4096


def test_large_json_is_gzipped_and_small_json_is_not():
    client = TestClient(_app_with_the_apis_gzip())
    big = client.get("/big", headers={"Accept-Encoding": "gzip"})
    assert big.headers.get("content-encoding") == "gzip"
    assert len(big.json()["rows"]) == 5000
    small = client.get("/small", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in small.headers


def test_a_stream_arrives_event_by_event_and_uncompressed():
    # Driven at the ASGI layer: each `http.response.body` message the server sends is one
    # delivery to the client. (The test client joins a body before handing it over, so it
    # cannot tell a stream from a buffered one.)
    app = _app_with_the_apis_gzip()
    sent: list[dict] = []

    async def run():
        asked = []

        async def receive():
            # The request body once; after that the client simply stays connected.
            if not asked:
                asked.append(1)
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "GET", "path": "/sse", "raw_path": b"/sse",
                 "query_string": b"", "root_path": "", "scheme": "http", "server": ("t", 80),
                 "client": ("c", 1), "http_version": "1.1",
                 "headers": [(b"accept-encoding", b"gzip"), (b"host", b"t")]}
        await app(scope, receive, send)

    asyncio.run(run())
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert "content-encoding" not in headers
    bodies = [m["body"] for m in sent if m["type"] == "http.response.body" and m.get("body")]
    assert len(bodies) >= 3, "the stream was buffered into one body"
    assert bodies[0].decode().startswith("event: log")
    assert json.loads(bodies[0].decode().split("data: ")[1]) == 0
