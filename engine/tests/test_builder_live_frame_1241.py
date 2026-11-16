"""The running app sits beside the chat while the build is still going (BOSSAPP-1241).

The build in these tests is a fake the caller supplies. Nothing here starts a build,
calls GitHub, or spends.
"""
from __future__ import annotations

import inspect

import pytest

from boss_engine.multirepo.registry import NOT_CONNECTED
from boss_engine.services import builder_live_frame as frame


def test_a_build_that_is_still_going_shows_the_running_app_beside_the_chat():
    shown = frame.live_frame(frame.fake_build())
    assert shown["layout"] == "beside"
    assert shown["still_going"] is True
    assert shown["status"] == "building"
    assert shown["build_id"] == "build_fake_1241"
    assert shown["title"] == "Harbor desk"
    assert shown["fake"] is True
    assert shown["app"] == {"url": "https://preview.invalid/harbor-desk", "shown": True}
    assert shown["chat"] == [
        {"role": "you", "text": "A desk for the harbor office."},
        {"role": "boss", "text": "The desk is up."},
    ]
    assert shown["publish"] == {"label": "Publish", "enabled": True, "reason": None}
    assert shown["error"] is None


def test_device_frames_are_phone_tablet_and_desktop():
    shown = frame.live_frame(frame.fake_build())
    assert shown["devices"] == [
        {"id": "phone", "label": "Phone", "width": 390, "height": 844},
        {"id": "tablet", "label": "Tablet", "width": 834, "height": 1194},
        {"id": "desktop", "label": "Desktop", "width": 1280, "height": 800},
    ]
    assert shown["device"] == {"id": "desktop", "label": "Desktop", "width": 1280, "height": 800}
    phone = frame.live_frame(frame.fake_build(), device="phone")
    assert phone["device"]["id"] == "phone"
    assert phone["device"]["width"] == 390
    assert phone["device"]["height"] == 844
    unknown = frame.live_frame(frame.fake_build(), device="watch")
    assert unknown["device"]["id"] == "desktop"


def test_publish_is_offered_while_the_build_is_still_going_and_records_once():
    build = frame.fake_build()
    seen: set[str] = set()
    first = frame.publish(build, seen=seen)
    second = frame.publish(build, seen=seen)
    assert first == {
        "ok": True,
        "code": "published",
        "message": "Published.",
        "dedupe_key": "bossapp-1241:build_fake_1241:publish",
        "recorded": True,
        "where": "local",
    }
    assert second == {
        "ok": True,
        "code": "already_published",
        "message": "Already published.",
        "dedupe_key": "bossapp-1241:build_fake_1241:publish",
        "recorded": False,
        "where": "local",
    }
    assert seen == {"bossapp-1241:build_fake_1241:publish"}


def test_publish_to_github_returns_the_existing_not_connected_error():
    seen: set[str] = set()
    refused = frame.publish(frame.fake_build(), publish_to="github", seen=seen)
    again = frame.publish(frame.fake_build(), publish_to="github", seen=seen)
    assert refused["ok"] is False
    assert refused["code"] == "github_not_connected"
    assert refused["message"] == NOT_CONNECTED
    assert refused["recorded"] is False
    assert again == refused
    assert seen == set()


def test_a_failure_stays_visible_and_nothing_running_cannot_publish():
    failed = frame.live_frame({
        "id": "build_fake_1241",
        "title": "Harbor desk",
        "status": "failed",
        "app_url": None,
        "chat": [],
        "error": "The port was taken.",
        "fake": True,
    })
    assert failed["still_going"] is False
    assert failed["app"]["shown"] is False
    assert failed["error"] == "The port was taken."
    assert failed["publish"]["enabled"] is False
    assert failed["publish"]["reason"] == "Nothing is running yet."
    silent = frame.live_frame({
        "id": "build_fake_1241", "status": "failed", "app_url": "", "chat": [],
    })
    assert silent["error"] == "The build failed."
    refused = frame.publish({"id": "build_fake_1241", "status": "building", "app_url": ""})
    assert refused["ok"] is False
    assert refused["code"] == "no_app"
    assert refused["message"] == "Nothing is running yet."
    assert refused["recorded"] is False


def test_a_script_address_is_refused_and_a_missing_id_is_an_error():
    shown = frame.live_frame({
        "id": "build_fake_1241",
        "status": "building",
        "app_url": "javascript:alert(1)",
        "chat": [],
        "fake": True,
    })
    assert shown["app"]["shown"] is False
    assert shown["publish"]["enabled"] is False
    assert shown["error"] == "The app address was refused."
    with pytest.raises(ValueError, match="id"):
        frame.live_frame({"status": "building"})


def test_the_service_does_not_start_a_build_or_call_github():
    src = inspect.getsource(frame)
    for banned in ("subprocess", "httpx", "urllib", "requests", "github.com"):
        assert banned not in src
    assert frame.fake_build()["fake"] is True
    assert frame.live_frame(frame.fake_build())["still_going"] is True


def test_the_http_frame_uses_the_same_rules_and_does_not_double_publish():
    from apps.api.app.routers.gap_1241 import BuildBody, publish_frame, reset_publishes, show_frame

    reset_publishes()
    body = BuildBody.model_validate(frame.fake_build())
    shown = show_frame(body)
    assert shown["layout"] == "beside"
    assert shown["still_going"] is True
    assert shown["app"]["url"] == "https://preview.invalid/harbor-desk"
    first = publish_frame(body)
    second = publish_frame(body)
    assert first["recorded"] is True
    assert second["code"] == "already_published"
    assert second["recorded"] is False
    github = publish_frame(BuildBody.model_validate({**frame.fake_build(), "publish_to": "github"}))
    assert github["code"] == "github_not_connected"
    assert github["message"] == NOT_CONNECTED
    missing = show_frame(BuildBody.model_validate({"id": "  ", "status": "building"}))
    assert missing.status_code == 400
    assert "id" in missing.body.decode()
