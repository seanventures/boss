"""Send and resend chat link prompts (BOSSAPP-1180).

The DMs go through the chat outbox and chat_adapters.TRANSPORT. These tests install FakeSlack
there, so nothing calls Slack or Teams.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta, timezone

import pytest
from sqlalchemy import text

from boss_engine.services import chat_adapters, chat_reply, event_intake, notify

from ._intake_support import (
    FakeSlack,
    link_chat_user,
    make_person,
    make_project,
    make_slack,
    pg_engine,
    purge_slack,
)

_CODE = re.compile(r"[A-Z0-9]{5}-[A-Z0-9]{5}")


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


def _seen(engine, connection_id: str, project_id: str, user: str, *, guest: bool = False,
          display: str = "") -> None:
    from boss_engine.services.core import make_id

    data = {"user": user, "text": "hello", "channel": "C0SUPPORT", "channel_type": "channel"}
    if guest:
        data["is_restricted"] = True
    envelope = {"type": "com.slack.message", "bossactor": {"kind": "user", "external_id": user,
                "display": display or user}, "data": data}
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO intake_events (id, connection_id, project_id, dedupe_key, status,
                   actor_kind, actor_external_id, envelope, received_at)
            VALUES (:i, :c, :p, :d, 'ignored', 'user', :u, CAST(:e AS JSONB), clock_timestamp())"""),
            {"i": make_id("iev"), "c": connection_id, "p": project_id, "d": f"seen:{user}",
             "u": user, "e": json.dumps(envelope)})


def _flush(engine, endpoint_id: str) -> None:
    for _ in range(4):
        chat_reply.BUCKETS.reset()
        with engine.begin() as c:
            stamped = c.execute(text("SELECT clock_timestamp()")).scalar()
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=timezone.utc)
        got = notify.deliver_chat_due(engine, _settings(), now=stamped + timedelta(seconds=3),
                                      endpoint_id=endpoint_id)
        if not (got["sent"] or got["waiting"] or got["skipped"]):
            return


def _dm_users(fake: FakeSlack) -> list[str]:
    return [str(body.get("users") or "") for body in fake.named("conversations.open")]


@pytest.fixture()
def world(monkeypatch):
    engine = pg_engine()
    pid = make_project(engine)
    fake = FakeSlack()
    monkeypatch.setattr(chat_adapters, "TRANSPORT", fake)
    try:
        yield engine, pid, fake
    finally:
        purge_slack(engine, pid)


def test_link_prompts_go_to_unlinked_people_once_and_resend_sends_again(world):
    engine, pid, fake = world
    slack = make_slack(engine, pid)
    cid = slack["connection"]["id"]
    endpoint = slack["endpoint"]["id"]
    linked = make_person(engine, f"linked-{pid}@example.com", name="Linked")
    link_chat_user(engine, endpoint, "U0LINKED", linked)
    revoked = make_person(engine, f"revoked-{pid}@example.com", name="Revoked")
    revoked_link = link_chat_user(engine, endpoint, "U0REVOKED", revoked)
    with engine.begin() as c:
        c.execute(text("UPDATE chat_identity_links SET revoked_at = now() WHERE id = :i"),
                  {"i": revoked_link})
    _seen(engine, cid, pid, "U0DEV", display="Dev")
    _seen(engine, cid, pid, "U0GUEST", guest=True, display="Guest")
    _seen(engine, cid, pid, "U0LINKED", display="Linked")
    _seen(engine, cid, pid, "U0REVOKED", display="Revoked")

    with engine.begin() as c:
        first = event_intake.prompt_people(c, _settings(), project_id=pid, connection_id=cid)
    assert first["sent"] == ["U0DEV"]
    assert first["already_sent"] == []
    assert "U0GUEST" not in json.dumps(first["sent"] + first["already_sent"] + first.get("skipped", []))
    assert "U0LINKED" not in first["sent"]
    assert "U0REVOKED" not in first["sent"]
    assert _CODE.search(json.dumps(first)) is None
    _flush(engine, endpoint)
    assert _dm_users(fake) == ["U0DEV"]
    assert len(fake.named("chat.postMessage")) == 1

    with engine.begin() as c:
        second = event_intake.prompt_people(c, _settings(), project_id=pid, connection_id=cid)
    assert second["sent"] == []
    assert second["already_sent"] == ["U0DEV"]
    _flush(engine, endpoint)
    assert _dm_users(fake) == ["U0DEV"]

    with engine.begin() as c:
        resent = event_intake.prompt_people(c, _settings(), project_id=pid, connection_id=cid,
                                            external_user_id="U0DEV", resend=True)
    assert resent["sent"] == ["U0DEV"]
    assert _CODE.search(json.dumps(resent)) is None
    _flush(engine, endpoint)
    assert _dm_users(fake) == ["U0DEV", "U0DEV"]
    assert len(fake.named("chat.postMessage")) == 2


def test_a_guest_a_linked_person_and_an_unknown_user_are_not_prompted(world):
    engine, pid, fake = world
    slack = make_slack(engine, pid)
    cid = slack["connection"]["id"]
    endpoint = slack["endpoint"]["id"]
    linked = make_person(engine, f"one-{pid}@example.com")
    link_chat_user(engine, endpoint, "U0LINKED", linked)
    _seen(engine, cid, pid, "U0GUEST", guest=True)
    _seen(engine, cid, pid, "U0LINKED")
    with engine.begin() as c:
        with pytest.raises(event_intake.IntakeServiceError) as guest:
            event_intake.prompt_people(c, _settings(), project_id=pid, connection_id=cid,
                                      external_user_id="U0GUEST", resend=True)
        with pytest.raises(event_intake.IntakeServiceError) as known:
            event_intake.prompt_people(c, _settings(), project_id=pid, connection_id=cid,
                                      external_user_id="U0LINKED", resend=True)
        with pytest.raises(event_intake.IntakeServiceError) as missing:
            event_intake.prompt_people(c, _settings(), project_id=pid, connection_id=cid,
                                      external_user_id="U0NOBODY")
    assert guest.value.status == 422 and "guest" in guest.value.code
    assert known.value.status == 422
    assert missing.value.status == 404
    assert fake.calls == []
