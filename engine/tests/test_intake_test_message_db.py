"""BOSS posts its own connection test message and confirms a person's reply (BOSSAPP-1179).

The post goes through the intake adapter (`SlackIntakeAdapter.send` / `OutboxOp`). These tests
fake that send. `chat_adapters.TRANSPORT` raises if anything reaches Slack or Teams for real.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from boss_engine.connectors.intake.contract import Receipt
from boss_engine.connectors.intake.slack.adapter import SlackIntakeAdapter
from boss_engine.services import chat_adapters, event_intake

from ._intake_support import (
    SLACK_BOT_TOKEN,
    SLACK_BOT_USER,
    make_connection,
    make_project,
    make_slack,
    pg_engine,
    purge_slack,
)


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


def _live_transport(*_a, **_k):
    raise AssertionError("live slack")


def _fake_send(sent: list):
    def send(_self, conn, op, secrets):
        blob = json.dumps({"op": op.op, "conversation": op.conversation, "message": op.message,
                           "dedupe_key": op.dedupe_key}, default=str)
        assert SLACK_BOT_TOKEN not in blob
        assert secrets.get("bot_token")  # the adapter boundary receives the secret view
        sent.append(op)
        return Receipt(ok=True, ref={"channel": op.conversation.get("channel"), "ts": "1700000001.000100",
                                     "bot_token": SLACK_BOT_TOKEN})

    return send


def _stamp(engine, event_id: str, posted_at: str, *, after: bool) -> None:
    shift = "interval '2 seconds'" if after else "- interval '2 minutes'"
    with engine.begin() as c:
        c.execute(text(
            f"UPDATE intake_events SET received_at = CAST(:p AS timestamptz) + {shift} "
            "WHERE id = :i"), {"p": posted_at, "i": event_id})


def _hear(engine, connection_id: str, project_id: str, *, user: str, channel: str,
          etype: str = "app_mention", text_: str = "hi", bot_id: str = "") -> str:
    """One stored inbound, as the front door leaves it (raw body, no envelope yet)."""
    from boss_engine.services.core import make_id

    event_id = make_id("iev")
    body = {"type": "event_callback", "event": {
        "type": etype, "user": user, "channel": channel, "text": text_, "ts": "1.0",
        **({"bot_id": bot_id} if bot_id else {}),
        **({"channel_type": "im"} if etype == "message" and channel.startswith("D") else {}),
    }}
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO intake_events (id, connection_id, project_id, dedupe_key, status, raw,
                   received_at)
            VALUES (:i, :c, :p, :d, 'received', :raw, clock_timestamp())"""),
            {"i": event_id, "c": connection_id, "p": project_id,
             "d": f"test:{event_id}", "raw": json.dumps(body).encode()})
    return event_id


@pytest.fixture()
def world(monkeypatch):
    engine = pg_engine()
    pid = make_project(engine)
    monkeypatch.setattr(chat_adapters, "TRANSPORT", _live_transport)
    try:
        yield engine, pid
    finally:
        purge_slack(engine, pid)


def test_boss_posts_a_test_message_and_a_later_mention_confirms_the_round_trip(world, monkeypatch):
    engine, pid = world
    slack = make_slack(engine, pid)
    cid = slack["connection"]["id"]
    sent: list = []
    monkeypatch.setattr(SlackIntakeAdapter, "send", _fake_send(sent))
    earlier = _hear(engine, cid, pid, user="U0PRIYA", channel="C0SUPPORT",
                    text_=f"<@{SLACK_BOT_USER}> before")
    with engine.begin() as c:
        posted = event_intake.post_test_message(c, _settings(), project_id=pid, connection_id=cid)
    assert posted["ok"] is True
    assert posted["posted"] is True
    assert posted["round_trip"] is False
    assert posted["channel"] == "C0SUPPORT"
    assert posted["probe_id"]
    assert posted["checks"]["secret:signing_secret"] == "ok"
    assert posted["checks"]["secret:bot_token"] == "ok"
    assert posted["receipt"] == {"channel": "C0SUPPORT", "ts": "1700000001.000100"}
    assert SLACK_BOT_TOKEN not in json.dumps(posted)
    assert len(sent) == 1
    assert sent[0].op == "post"
    assert sent[0].conversation["channel"] == "C0SUPPORT"
    text_ = sent[0].message.get("text") or sent[0].message.get("message", {}).get("text") or ""
    assert "@BOSS" in text_
    assert posted["probe_id"] in text_
    with engine.begin() as c:
        health = c.execute(text("SELECT health FROM intake_connections WHERE id = :i"),
                           {"i": cid}).scalar()
    health = health if isinstance(health, dict) else json.loads(health)
    assert health["last_test_at"]
    assert health["test_probe"]["id"] == posted["probe_id"]
    assert SLACK_BOT_TOKEN not in json.dumps(health)
    _stamp(engine, earlier, health["test_probe"]["posted_at"], after=False)
    echo = _hear(engine, cid, pid, user=SLACK_BOT_USER, channel="C0SUPPORT",
                 text_="BOSS test", bot_id="B0BOSS")
    other = _hear(engine, cid, pid, user="U0PRIYA", channel="C0OTHER")
    _stamp(engine, echo, health["test_probe"]["posted_at"], after=True)
    _stamp(engine, other, health["test_probe"]["posted_at"], after=True)
    with engine.begin() as c:
        still = event_intake.confirm_test_message(c, project_id=pid, connection_id=cid)
    assert still["posted"] is True and still["round_trip"] is False and still["event_id"] is None
    assert sent == sent[:1]
    reply = _hear(engine, cid, pid, user="U0PRIYA", channel="C0SUPPORT",
                  text_=f"<@{SLACK_BOT_USER}> after")
    _stamp(engine, reply, health["test_probe"]["posted_at"], after=True)
    with engine.begin() as c:
        done = event_intake.confirm_test_message(c, project_id=pid, connection_id=cid)
    assert done["round_trip"] is True
    assert done["event_id"] == reply
    assert done["event_status"] == "received"
    assert len(sent) == 1


def test_a_source_that_cannot_reply_does_not_post(world, monkeypatch):
    engine, pid = world
    sent: list = []
    monkeypatch.setattr(SlackIntakeAdapter, "send", _fake_send(sent))
    hook = make_connection(engine, pid, "webhook")
    with engine.begin() as c:
        with pytest.raises(event_intake.IntakeServiceError) as raised:
            event_intake.post_test_message(c, _settings(), project_id=pid,
                                           connection_id=hook["id"])
    assert raised.value.status == 422
    assert raised.value.code == "reply_unsupported"
    assert sent == []


def test_a_slack_connection_without_a_channel_does_not_post(world, monkeypatch):
    engine, pid = world
    slack = make_slack(engine, pid)
    sent: list = []
    monkeypatch.setattr(SlackIntakeAdapter, "send", _fake_send(sent))
    with engine.begin() as c:
        c.execute(text(
            "UPDATE chat_endpoints SET config = config - 'channel' WHERE id = :e"),
            {"e": slack["endpoint"]["id"]})
        with pytest.raises(event_intake.IntakeServiceError) as raised:
            event_intake.post_test_message(c, _settings(), project_id=pid,
                                           connection_id=slack["connection"]["id"])
    assert raised.value.status == 422
    assert raised.value.code == "channel_required"
    assert sent == []


def test_unresolvable_secrets_do_not_post(world, monkeypatch):
    engine, pid = world
    slack = make_slack(engine, pid)
    sent: list = []
    monkeypatch.setattr(SlackIntakeAdapter, "send", _fake_send(sent))
    with engine.begin() as c:
        c.execute(text("""
            UPDATE chat_endpoints
               SET secret_refs = secret_refs || CAST('{"bot_token": "sref_missing"}' AS JSONB)
             WHERE id = :e"""), {"e": slack["endpoint"]["id"]})
        got = event_intake.post_test_message(c, _settings(), project_id=pid,
                                             connection_id=slack["connection"]["id"])
    assert got["ok"] is False
    assert got["posted"] is False
    assert got["round_trip"] is False
    assert str(got["checks"].get("secret:bot_token", "")).startswith(("empty", "unresolvable"))
    assert sent == []
    assert SLACK_BOT_TOKEN not in json.dumps(got)
