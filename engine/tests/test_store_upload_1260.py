"""BOSSAPP-1260: a signed build reaches one fake store and writes one receipt.

A missing signing key is refused before any upload. A repeat of the same signed
request does not upload again. Nothing here is a device farm or a real store.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apps.api.app.routers.gap_1260 as gap_1260
from boss_engine.services.store_upload import (
    MemoryReceiptBook,
    StoreUploadError,
    UploadRefused,
    preview_frame,
    stream_preview_frames,
    upload_signed_build,
)


class _StoreHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 — stdlib name
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        self.server.uploads.append(json.loads(raw.decode()))  # type: ignore[attr-defined]
        code = self.server.status_code  # type: ignore[attr-defined]
        payload = self.server.payload  # type: ignore[attr-defined]
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


class _FakeStore:
    def __init__(self, status: int = 201, payload: dict | None = None) -> None:
        self.uploads: list[dict] = []
        self._status = status
        self._payload = payload or {"id": "fake_1", "accepted": True}
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StoreHandler)
        self._httpd.uploads = self.uploads  # type: ignore[attr-defined]
        self._httpd.status_code = status  # type: ignore[attr-defined]
        self._httpd.payload = self._payload  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/upload"

    def set_status(self, status: int, payload: dict | None = None) -> None:
        self._httpd.status_code = status  # type: ignore[attr-defined]
        if payload is not None:
            self._httpd.payload = payload  # type: ignore[attr-defined]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)


@pytest.fixture
def store():
    endpoint = _FakeStore()
    try:
        yield endpoint
    finally:
        endpoint.close()


def _upload(store: _FakeStore, book, **overrides):
    body = {
        "platform": "ios",
        "artifact_id": "art_bench",
        "signing_key": "test-signing-key",
        "store_url": store.url,
        "book": book,
    }
    body.update(overrides)
    return upload_signed_build(**body)


def test_missing_signing_key_writes_no_success_receipt(store):
    """Dropping the key check would post the build and record a success."""
    book = MemoryReceiptBook()
    with pytest.raises(UploadRefused, match="signing key"):
        _upload(store, book, signing_key="  ")
    assert store.uploads == []
    assert book.success_receipts() == []


def test_signed_upload_hits_the_fake_store_once_and_writes_one_receipt(store):
    """A second identical request must not post again or add a second receipt."""
    book = MemoryReceiptBook()
    first = _upload(store, book)
    second = _upload(store, book)
    fingerprint = hashlib.sha256(b"test-signing-key").hexdigest()[:16]
    signature = hmac.new(
        b"test-signing-key", b"ios:art_bench", hashlib.sha256
    ).hexdigest()
    assert len(store.uploads) == 1
    posted = store.uploads[0]
    assert posted == {
        "kind": "signed-build",
        "platform": "ios",
        "artifact_id": "art_bench",
        "signature": signature,
        "device_farm": False,
        "real_store": False,
        "store": "fake",
    }
    assert "test-signing-key" not in json.dumps(posted)
    assert len(book.success_receipts()) == 1
    assert first == second
    assert first["status"] == "accepted"
    assert first["dedupe_key"] == f"store-upload:ios:art_bench:{fingerprint}"
    assert first["store_ref"] == "fake_1"
    assert first["device_farm"] is False
    assert first["real_store"] is False
    assert "test-signing-key" not in json.dumps(first)


def test_store_failure_is_visible_and_a_retry_succeeds_once(store):
    """A 500 must not become a success receipt. The retry may post once more."""
    book = MemoryReceiptBook()
    store.set_status(500, {"id": "nope", "accepted": False})
    with pytest.raises(StoreUploadError, match="500"):
        _upload(store, book)
    assert book.success_receipts() == []
    store.set_status(201, {"id": "fake_1", "accepted": True})
    receipt = _upload(store, book)
    assert len(store.uploads) == 2
    assert len(book.success_receipts()) == 1
    assert receipt["status"] == "accepted"
    assert receipt["store_ref"] == "fake_1"


def test_unknown_platform_never_reaches_the_store(store):
    book = MemoryReceiptBook()
    with pytest.raises(UploadRefused, match="iOS or Android"):
        _upload(store, book, platform="web")
    assert store.uploads == []
    assert book.success_receipts() == []


def test_preview_frames_are_a_local_stream_not_a_device_farm():
    """The stream is painted frames. Claiming a farm or a store would fail this."""
    frames = list(stream_preview_frames("android", 3))
    assert [frame["index"] for frame in frames] == [1, 2, 3]
    assert frames[0]["platform"] == "android"
    assert frames[0]["chrome"] == "punch"
    assert frames[0]["bars"] == [9, 8, 7, 6, 5]
    assert frames[1]["bars"] == [7, 6, 5, 4, 3]
    assert all(frame["device_farm"] is False for frame in frames)
    assert all(frame["real_store"] is False for frame in frames)
    ios = preview_frame("ios", 1)
    assert ios["platform"] == "ios"
    assert ios["chrome"] == "notch"
    assert ios["bars"] == [5, 4, 3, 2, 10]
    assert ios["device_farm"] is False
    assert ios["real_store"] is False


def test_router_refuses_an_unsigned_upload_and_accepts_one_signed_upload(store, monkeypatch):
    book = MemoryReceiptBook()
    gap_1260.BOOK = book
    monkeypatch.setenv("BOSS_FAKE_STORE_URL", store.url)
    app = FastAPI()
    app.include_router(gap_1260.router)
    client = TestClient(app)

    refused = client.post(
        "/v1/builder/store-uploads",
        json={"platform": "ios", "artifact_id": "art_bench", "signing_key": ""},
    )
    assert refused.status_code == 422
    assert "signing key" in refused.json()["detail"].lower()
    assert store.uploads == []
    assert book.success_receipts() == []

    frame = client.get(
        "/v1/builder/emulator-frame", params={"platform": "ios", "index": 1}
    )
    assert frame.status_code == 200
    assert frame.json()["bars"] == [5, 4, 3, 2, 10]
    assert frame.json()["device_farm"] is False
    assert frame.json()["real_store"] is False

    accepted = client.post(
        "/v1/builder/store-uploads",
        json={
            "platform": "ios",
            "artifact_id": "art_bench",
            "signing_key": "test-signing-key",
        },
    )
    again = client.post(
        "/v1/builder/store-uploads",
        json={
            "platform": "ios",
            "artifact_id": "art_bench",
            "signing_key": "test-signing-key",
        },
    )
    assert accepted.status_code == 201
    assert again.status_code == 201
    assert accepted.json()["dedupe_key"] == again.json()["dedupe_key"]
    assert len(store.uploads) == 1
    assert len(book.success_receipts()) == 1
