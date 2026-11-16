"""Local preview frames and one signed upload to a fake store.

This is not a device farm and not a real store. A missing signing key never
posts and never writes a success receipt. The same signed request posts once.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from typing import Iterator

_PLATFORMS = {"ios": ("notch", 3), "android": ("punch", 7)}


class UploadRefused(Exception):
    """The upload did not start. No success receipt is written."""


class StoreUploadError(Exception):
    """The fake store did not accept the build. No success receipt is written."""


class MemoryReceiptBook:
    """In-process receipts for the preview upload. Not the spend ledger."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    def get(self, dedupe_key: str) -> dict | None:
        row = self._rows.get(dedupe_key)
        return dict(row) if row else None

    def put(self, receipt: dict) -> dict:
        stored = dict(receipt)
        self._rows[stored["dedupe_key"]] = stored
        return dict(stored)

    def success_receipts(self) -> list[dict]:
        return [dict(row) for row in self._rows.values() if row.get("status") == "accepted"]


def preview_frame(platform: str, index: int) -> dict:
    """One painted frame. The numbers are local, not a device screenshot."""
    spec = _PLATFORMS.get(platform)
    if spec is None:
        raise UploadRefused("Pick iOS or Android. This is a preview, not a device farm.")
    if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= 240:
        raise UploadRefused("Frame index is out of range.")
    chrome, seed = spec
    bars = [((index * seed + slot * 17) % 9) + 2 for slot in range(5)]
    return {
        "kind": "preview",
        "platform": platform,
        "index": index,
        "chrome": chrome,
        "bars": bars,
        "device_farm": False,
        "real_store": False,
    }


def stream_preview_frames(platform: str, count: int) -> Iterator[dict]:
    """Yield a short local stream. It stops. It does not attach a device."""
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 24:
        raise UploadRefused("Ask for 1 to 24 preview frames.")
    for index in range(1, count + 1):
        yield preview_frame(platform, index)


def _signature(signing_key: str, platform: str, artifact_id: str) -> str:
    message = f"{platform}:{artifact_id}".encode()
    return hmac.new(signing_key.encode(), message, hashlib.sha256).hexdigest()


def _post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read().decode() or "{}"
            parsed = json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise StoreUploadError(
            f"The fake store refused the upload (HTTP {exc.code})."
        ) from exc
    except urllib.error.URLError as exc:
        raise StoreUploadError("The fake store endpoint did not answer.") from exc
    except json.JSONDecodeError as exc:
        raise StoreUploadError("The fake store returned an unreadable receipt.") from exc
    if not isinstance(parsed, dict):
        raise StoreUploadError("The fake store returned an unreadable receipt.")
    return parsed


def upload_signed_build(
    *,
    platform: str,
    artifact_id: str,
    signing_key: str,
    store_url: str,
    book: MemoryReceiptBook,
) -> dict:
    """Sign locally, post once to the fake store, and keep one success receipt."""
    if platform not in _PLATFORMS:
        raise UploadRefused("Pick iOS or Android. This is a preview, not a device farm.")
    key = signing_key.strip() if isinstance(signing_key, str) else ""
    if not key:
        raise UploadRefused("A signing key is required. Nothing was uploaded.")
    artifact = artifact_id.strip() if isinstance(artifact_id, str) else ""
    if not artifact:
        raise UploadRefused("An artifact id is required. Nothing was uploaded.")

    fingerprint = hashlib.sha256(key.encode()).hexdigest()[:16]
    dedupe_key = f"store-upload:{platform}:{artifact}:{fingerprint}"
    existing = book.get(dedupe_key)
    if existing and existing.get("status") == "accepted":
        return existing

    url = store_url.strip() if isinstance(store_url, str) else ""
    if not url:
        raise StoreUploadError(
            "No fake store endpoint is configured. This is not a real store."
        )

    payload = {
        "kind": "signed-build",
        "platform": platform,
        "artifact_id": artifact,
        "signature": _signature(key, platform, artifact),
        "device_farm": False,
        "real_store": False,
        "store": "fake",
    }
    receipt_id = "rcpt_" + hashlib.sha256(dedupe_key.encode()).hexdigest()[:16]
    try:
        body = _post_json(url, payload)
    except StoreUploadError as exc:
        book.put(
            {
                "id": receipt_id,
                "dedupe_key": dedupe_key,
                "platform": platform,
                "artifact_id": artifact,
                "status": "dead_letter",
                "store_ref": None,
                "device_farm": False,
                "real_store": False,
                "error": str(exc),
            }
        )
        raise

    store_ref = body.get("id")
    receipt = {
        "id": receipt_id,
        "dedupe_key": dedupe_key,
        "platform": platform,
        "artifact_id": artifact,
        "status": "accepted",
        "store_ref": store_ref if isinstance(store_ref, str) and store_ref else "fake",
        "device_farm": False,
        "real_store": False,
    }
    return book.put(receipt)
