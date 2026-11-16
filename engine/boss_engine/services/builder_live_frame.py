"""The running app beside the chat while a build is still going (BOSSAPP-1241).

The caller supplies the build. This module never starts one, never calls GitHub,
and never spends. Publishing to GitHub returns the existing not-connected error.
A repeat of a local publish records the same dedupe key once.
"""
from __future__ import annotations

from typing import Any, Mapping

from ..multirepo.registry import NOT_CONNECTED

DEVICES: tuple[dict[str, Any], ...] = (
    {"id": "phone", "label": "Phone", "width": 390, "height": 844},
    {"id": "tablet", "label": "Tablet", "width": 834, "height": 1194},
    {"id": "desktop", "label": "Desktop", "width": 1280, "height": 800},
)

STILL_GOING = frozenset({"building", "queued", "running"})

_SEEN: set[str] = set()


def reset_publishes() -> None:
    _SEEN.clear()


def fake_build() -> dict[str, Any]:
    """A build tests can hold. It is not started, and it does not advance."""
    return {
        "id": "build_fake_1241",
        "title": "Harbor desk",
        "status": "building",
        "app_url": "https://preview.invalid/harbor-desk",
        "chat": [
            {"role": "you", "text": "A desk for the harbor office."},
            {"role": "boss", "text": "The desk is up."},
        ],
        "error": None,
        "github_connected": False,
        "fake": True,
    }


def dedupe_key(build_id: str) -> str:
    return f"bossapp-1241:{build_id}:publish"


def _require_id(build: Mapping[str, Any]) -> str:
    raw = build.get("id")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("A build needs an id.")
    return raw.strip()


def _device(device_id: str | None) -> dict[str, Any]:
    wanted = (device_id or "desktop").strip().lower()
    for device in DEVICES:
        if device["id"] == wanted:
            return dict(device)
    return dict(DEVICES[-1])


def _app_url(build: Mapping[str, Any]) -> str | None:
    raw = build.get("app_url")
    if not isinstance(raw, str):
        return None
    url = raw.strip()
    if not url:
        return None
    if url.lower().startswith(("https://", "http://")):
        return url
    return None


def _refused_address(build: Mapping[str, Any]) -> bool:
    raw = build.get("app_url")
    if not isinstance(raw, str) or not raw.strip():
        return False
    return _app_url(build) is None


def live_frame(build: Mapping[str, Any], *, device: str | None = None) -> dict[str, Any]:
    build_id = _require_id(build)
    status = str(build.get("status") or "building").strip().lower()
    url = _app_url(build)
    error = build.get("error")
    error_text = error.strip() if isinstance(error, str) and error.strip() else None
    if _refused_address(build) and not error_text:
        error_text = "The app address was refused."
    if status == "failed" and not error_text:
        error_text = "The build failed."
    chat: list[dict[str, str]] = []
    for line in build.get("chat") or []:
        if not isinstance(line, Mapping):
            continue
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        chat.append({"role": str(line.get("role") or "boss"), "text": text})
    title = str(build.get("title") or "Untitled app").strip() or "Untitled app"
    named = build.get("device") if isinstance(build.get("device"), str) else None
    return {
        "build_id": build_id,
        "title": title,
        "status": status,
        "still_going": status in STILL_GOING,
        "layout": "beside",
        "chat": chat,
        "app": {"url": url, "shown": url is not None},
        "devices": [dict(item) for item in DEVICES],
        "device": _device(device or named),
        "publish": {
            "label": "Publish",
            "enabled": url is not None,
            "reason": None if url is not None else "Nothing is running yet.",
        },
        "error": error_text,
        "fake": bool(build.get("fake")),
    }


def publish(
    build: Mapping[str, Any],
    *,
    publish_to: str = "local",
    seen: set[str] | None = None,
) -> dict[str, Any]:
    build_id = _require_id(build)
    key = dedupe_key(build_id)
    box = _SEEN if seen is None else seen
    destination = (publish_to or "local").strip().lower()
    if destination == "github":
        return {
            "ok": False,
            "code": "github_not_connected",
            "message": NOT_CONNECTED,
            "dedupe_key": key,
            "recorded": False,
        }
    if destination != "local":
        return {
            "ok": False,
            "code": "unsupported_destination",
            "message": "Publish stays on this machine.",
            "dedupe_key": key,
            "recorded": False,
        }
    if key in box:
        return {
            "ok": True,
            "code": "already_published",
            "message": "Already published.",
            "dedupe_key": key,
            "recorded": False,
            "where": "local",
        }
    if _app_url(build) is None:
        return {
            "ok": False,
            "code": "no_app",
            "message": "Nothing is running yet.",
            "dedupe_key": key,
            "recorded": False,
        }
    box.add(key)
    return {
        "ok": True,
        "code": "published",
        "message": "Published.",
        "dedupe_key": key,
        "recorded": True,
        "where": "local",
    }
