"""A replayed write looks exactly like the first one, whatever shape the route answers in.

Review of 45e7693: the route-enumeration test counts a route as "covered" because the
idempotency middleware is global, which proves nobody was left out but not that a covered route
still works through the buffer-and-replay path. Only three JSON flows were exercised end to end
(start a workflow, retry a step, start a preview). The rest of the 576 routes answer in other
shapes — an empty 204, a file download with its own Content-Disposition, a multipart upload, a
non-JSON body — and a replay that dropped the filename or mangled the bytes would hand the
operator a broken download on the second press of the same button.

These mount one probe route per shape on the real app (under the real middleware stack, auth
included), press each twice with one key, and require the replay to match the first answer:
status, body bytes, content type and the route's own headers — and the handler to have run once.
"""
from uuid import uuid4

import pytest
from fastapi import Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.routing import APIRoute

from apps.api.app.main import app

PROBE = "/v1/idempotency-probe"
ZIP = b"PK\x03\x04" + bytes(range(256)) * 4  # binary, not valid UTF-8
calls: dict[str, int] = {}


def _count(name: str) -> None:
    calls[name] = calls.get(name, 0) + 1


async def _no_content() -> Response:
    _count("no_content")
    return Response(status_code=204)


async def _download() -> Response:
    _count("download")
    return Response(content=ZIP, media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="pack-export.zip"',
        "X-Boss-Export-Id": "exp_1"})


async def _upload(request: Request) -> JSONResponse:
    _count("upload")
    form = await request.form()
    upload = form["file"]
    data = await upload.read()
    return JSONResponse({"name": upload.filename, "bytes": len(data)}, status_code=201)


async def _text() -> PlainTextResponse:
    _count("text")
    return PlainTextResponse("queued: 3 steps\n")


async def _refused() -> JSONResponse:
    _count("refused")
    return JSONResponse({"detail": "step 'deploy' is risk tier 2 and always requires a person"},
                        status_code=400)


SHAPES = {
    "no_content": ("DELETE", _no_content),
    "download": ("POST", _download),
    "upload": ("POST", _upload),
    "text": ("PUT", _text),
    "refused": ("PATCH", _refused),
}


@pytest.fixture(scope="module", autouse=True)
def probe_routes():
    added = []
    for name, (method, handler) in SHAPES.items():
        app.router.add_api_route(f"{PROBE}/{name}", handler, methods=[method])
        added.append(app.router.routes[-1])
    yield
    for route in added:
        app.router.routes.remove(route)


def _press(client, name: str, key: str):
    method, _ = SHAPES[name]
    kwargs = {"headers": {"Idempotency-Key": key}}
    if name == "upload":
        kwargs["files"] = {"file": ("logo.png", b"\x89PNG\r\n" + b"\x00" * 64, "image/png")}
        kwargs["data"] = {"note": "brand"}
        # A fixed boundary, so the second press is byte-for-byte the same request.
        kwargs["headers"]["Content-Type"] = "multipart/form-data; boundary=probe-boundary"
        from urllib3 import encode_multipart_formdata
        body, _ = encode_multipart_formdata(
            {"note": "brand", "file": ("logo.png", b"\x89PNG\r\n" + b"\x00" * 64, "image/png")},
            boundary="probe-boundary")
        return client.request(method, f"{PROBE}/{name}", content=body,
                              headers=kwargs["headers"])
    return client.request(method, f"{PROBE}/{name}", **kwargs)


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_the_second_press_gets_the_first_answer_and_the_handler_runs_once(client, name):
    assert any(isinstance(r, APIRoute) and r.path == f"{PROBE}/{name}" for r in app.routes)
    calls.clear()
    key = f"probe-{uuid4()}"
    first = _press(client, name, key)
    second = _press(client, name, key)
    assert first.status_code in (200, 201, 204, 400), first.text
    assert second.headers.get("Idempotency-Replayed") == "true", second.text
    assert second.status_code == first.status_code
    assert second.content == first.content
    assert second.headers.get("content-type") == first.headers.get("content-type")
    for header in ("content-disposition", "x-boss-export-id"):
        assert second.headers.get(header) == first.headers.get(header), header
    assert calls.get(name) == 1


def test_the_upload_was_received_whole_through_the_buffered_body(client):
    """The middleware reads the body to fingerprint it; the handler must still get all of it."""
    response = _press(client, "upload", f"probe-{uuid4()}")
    assert response.status_code == 201, response.text
    assert response.json() == {"name": "logo.png", "bytes": 70}


def test_a_route_that_answers_without_a_key_is_untouched(client):
    calls.clear()
    response = client.request("POST", f"{PROBE}/download")
    assert response.content == ZIP
    assert response.headers["content-disposition"] == 'attachment; filename="pack-export.zip"'
    assert "Idempotency-Replayed" not in response.headers
