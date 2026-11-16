"""A named draft has its own chat and preview (BOSSAPP-1245).

Opening one stays off git sync and off the published URL: no commit is written,
and the live address is the same string afterwards.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.app.routers.gap_1245 import router
from boss_engine.services.named_draft import (
    AppPress,
    NamedDraftError,
    clear_named_drafts,
    open_named_draft,
    post_draft_chat,
    press_for,
)

PUBLISHED = "https://helpdesk.example/live"
PREVIEW = "https://draft.boss.test/helpdesk/evening-copy"
CHAT = "chat_helpdesk_evening-copy"


def _press() -> AppPress:
    return AppPress("helpdesk", PUBLISHED, ["cafebabe"])


def test_opening_a_named_draft_does_not_commit_or_change_the_published_url():
    press = _press()
    before = press.commits
    opened = open_named_draft(press, "evening-copy")
    assert opened.name == "evening-copy"
    assert opened.chat_id == CHAT
    assert opened.preview_url == PREVIEW
    assert opened.preview_url != press.published_url
    assert opened.git_sync is False
    assert opened.commit_created is False
    assert opened.messages == ()
    assert opened.published_url == PUBLISHED
    assert opened.commits == ("cafebabe",)
    assert press.published_url == PUBLISHED
    assert press.commits is before
    assert press.commits == ("cafebabe",)


def test_a_second_open_keeps_the_same_chat_and_preview():
    press = _press()
    first = open_named_draft(press, "evening-copy")
    post_draft_chat(press, "evening-copy", "Move the price under the title")
    again = open_named_draft(press, "Evening Copy")
    assert again.chat_id == first.chat_id
    assert again.preview_url == first.preview_url
    assert again.messages == (("operator", "Move the price under the title"),)
    assert again.commit_created is False
    assert press.commits == ("cafebabe",)
    assert press.published_url == PUBLISHED


def test_two_drafts_keep_separate_chats_and_previews():
    press = _press()
    evening = open_named_draft(press, "evening-copy")
    post_draft_chat(press, "evening-copy", "Move the price under the title")
    morning = open_named_draft(press, "morning-copy")
    spoken = post_draft_chat(press, "morning-copy", "Leave the hero")
    assert morning.chat_id == "chat_helpdesk_morning-copy"
    assert morning.preview_url == "https://draft.boss.test/helpdesk/morning-copy"
    assert morning.chat_id != evening.chat_id
    assert morning.preview_url != evening.preview_url
    assert spoken.messages == (("operator", "Leave the hero"),)
    assert spoken.chat_id == morning.chat_id
    assert open_named_draft(press, "evening-copy").messages == (
        ("operator", "Move the price under the title"),
    )
    assert press.commits == ("cafebabe",)
    assert press.published_url == PUBLISHED


def test_a_blank_name_is_visible_and_writes_nothing():
    press = _press()
    with pytest.raises(NamedDraftError) as blank:
        open_named_draft(press, "   ")
    assert blank.value.code == "draft_name_required"
    assert str(blank.value) == "Name the draft before opening it."
    with pytest.raises(NamedDraftError) as marks:
        open_named_draft(press, "!!!")
    assert marks.value.code == "draft_name_required"
    assert press.commits == ("cafebabe",)
    assert press.published_url == PUBLISHED


def test_chatting_before_open_does_not_create_a_draft_or_a_commit():
    press = _press()
    with pytest.raises(NamedDraftError) as missing:
        post_draft_chat(press, "evening-copy", "Move the price under the title")
    assert missing.value.code == "draft_not_open"
    assert str(missing.value) == "Open the draft before chatting."
    assert press.commits == ("cafebabe",)
    opened = open_named_draft(press, "evening-copy")
    assert opened.messages == ()
    with pytest.raises(NamedDraftError) as empty:
        post_draft_chat(press, "evening-copy", "  ")
    assert empty.value.code == "draft_line_required"
    assert opened.messages == ()
    assert press.published_url == PUBLISHED


def test_a_draft_preview_is_not_the_published_url_even_when_they_would_match():
    colliding = "https://draft.boss.test/helpdesk/evening-copy"
    press = AppPress("helpdesk", colliding, ["cafebabe"])
    opened = open_named_draft(press, "evening-copy")
    assert press.published_url == colliding
    assert opened.preview_url == "https://draft.boss.test/helpdesk/evening-copy/proof"
    assert opened.preview_url != press.published_url
    assert press.commits == ("cafebabe",)


def test_an_unpublished_app_stays_unpublished_when_a_draft_opens():
    press = AppPress("helpdesk", "", ["cafebabe"])
    opened = open_named_draft(press, "evening-copy")
    assert press.published_url == ""
    assert opened.published_url == ""
    assert opened.preview_url == PREVIEW
    assert opened.commit_created is False
    assert press.commits == ("cafebabe",)


def test_a_later_registration_cannot_rewrite_the_url_or_add_a_commit():
    clear_named_drafts()
    press = press_for("helpdesk", PUBLISHED, ["cafebabe"])
    open_named_draft(press, "evening-copy")
    again = press_for("helpdesk", "https://evil.example/hijack", ["cafebabe", "deadbeef"])
    assert again is press
    assert press.published_url == PUBLISHED
    assert press.commits == ("cafebabe",)
    open_named_draft(again, "evening-copy")
    assert press.commits == ("cafebabe",)
    assert press.published_url == PUBLISHED


def test_opening_over_http_does_not_commit_or_change_the_published_url():
    clear_named_drafts()
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    body = {
        "app_id": "helpdesk",
        "name": "evening-copy",
        "published_url": PUBLISHED,
        "commits": ["cafebabe"],
    }
    first = client.post("/v1/named-drafts/open", json=body)
    assert first.status_code == 200
    data = first.json()
    assert data["commit_created"] is False
    assert data["git_sync"] is False
    assert data["published_url"] == PUBLISHED
    assert data["commits"] == ["cafebabe"]
    assert data["preview_url"] == PREVIEW
    assert data["chat_id"] == CHAT
    assert data["messages"] == []

    hijack = client.post("/v1/named-drafts/open", json={
        **body,
        "published_url": "https://evil.example/hijack",
        "commits": ["cafebabe", "deadbeef"],
    })
    assert hijack.status_code == 200
    again = hijack.json()
    assert again["published_url"] == PUBLISHED
    assert again["commits"] == ["cafebabe"]
    assert again["chat_id"] == CHAT
    assert again["commit_created"] is False

    blank = client.post("/v1/named-drafts/open", json={**body, "name": "   "})
    assert blank.status_code == 422
    assert blank.json()["message"] == "Name the draft before opening it."

    chat = client.post("/v1/named-drafts/chat", json={
        "app_id": "helpdesk",
        "name": "evening-copy",
        "text": "Move the price under the title",
    })
    assert chat.status_code == 200
    spoken = chat.json()
    assert spoken["messages"] == [
        {"role": "operator", "text": "Move the price under the title"},
    ]
    assert spoken["commits"] == ["cafebabe"]
    assert spoken["published_url"] == PUBLISHED
    assert spoken["commit_created"] is False
    assert spoken["git_sync"] is False

    morning = client.post("/v1/named-drafts/open", json={**body, "name": "morning-copy"})
    assert morning.status_code == 200
    other = morning.json()
    assert other["messages"] == []
    assert other["chat_id"] == "chat_helpdesk_morning-copy"
    assert other["preview_url"] == "https://draft.boss.test/helpdesk/morning-copy"
    assert other["commits"] == ["cafebabe"]
    assert other["published_url"] == PUBLISHED

    early = client.post("/v1/named-drafts/chat", json={
        "app_id": "other-app",
        "name": "evening-copy",
        "text": "Should not land",
    })
    assert early.status_code == 422
    assert early.json()["message"] == "Open the draft before chatting."
