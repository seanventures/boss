"""Decide from Slack and phone (BOSSAPP-834): signed requests, single-use buttons, identity links.

What must hold, end to end through the real routes and the real DecisionService:

  * a request is believed only when the CHANNEL signed it (Slack's documented signature; a wrong,
    stale or missing one is 401 and recorded; an unknown endpoint is 404);
  * a new decision becomes a card in the outbox, posted with Approve/Reject buttons that each
    carry a signed single-use token, and the posted message is bound to the decision;
  * an unlinked presser is DMed a one-time link, and is linked only when the person signs in and
    confirms; the press decides nothing;
  * a linked press lands the REAL transition as that person (approved_by), with the press audited;
  * the same button pressed again refuses (compare-and-swap), and a forged token refuses;
  * a tier-3 decision is never executed from chat: the answer is the console link;
  * a reason-required action opens the Slack modal and the submission decides with that note;
  * the outbox retries with backoff and dead-letters after MAX_CHAT_ATTEMPTS;
  * Telegram: the webhook secret header and an inline-keyboard press take the same path;
  * no secret is ever returned.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import timedelta
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services import chat_adapters, chat_channels as chat, notify
from boss_engine.services.core import create_blocking_decision, now_utc

SIGNING = "slack-signing-secret-for-tests-0001"
BOT = "xoxb-test-bot-token"
TG_BOT = "123456:telegram-test-token"
TG_SECRET = "tg-webhook-secret-for-tests"


# ---- fixtures ------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("chat-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("chat-viewer")
    w.bind(w.viewer, "viewer")
    w.claimed()
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


class Calls:
    """The fake channel: records every Web API call and answers like Slack/Telegram do."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail = False

    def __call__(self, method, url, headers, body, purpose):
        name = url.rsplit("/", 1)[-1]
        payload = json.loads(body or b"{}")
        self.calls.append((name, payload))
        if self.fail:
            return 503, None
        if purpose == "chat.telegram":
            if name == "sendMessage":
                return 200, {"ok": True, "result": {"message_id": 77,
                                                     "chat": {"id": payload.get("chat_id")}}}
            return 200, {"ok": True, "result": True}
        if name == "chat.postMessage":
            return 200, {"ok": True, "channel": payload["channel"], "ts": "1700000000.000100"}
        if name == "conversations.open":
            return 200, {"ok": True, "channel": {"id": "D0DM"}}
        return 200, {"ok": True}

    def named(self, name):
        return [p for n, p in self.calls if n == name]


@pytest.fixture
def channel(monkeypatch):
    fake = Calls()
    monkeypatch.setattr(chat_adapters, "TRANSPORT", fake)
    settings = get_settings()
    monkeypatch.setattr(settings, "public_url", "https://boss.example.test")
    return fake


def _endpoint(engine, project_id, kind="slack", config=None):
    secrets = ({"signing_secret": SIGNING, "bot_token": BOT} if kind == "slack"
               else {"bot_token": TG_BOT, "webhook_secret": TG_SECRET})
    with engine.begin() as c:
        # one live endpoint per test: an earlier test's endpoint must not take the cards
        c.execute(text("UPDATE chat_endpoints SET status = 'disabled' WHERE project_id = :p"),
                  {"p": project_id})
        return chat.create_endpoint(c, get_settings(), project_id=project_id, kind=kind,
                                    name=f"{kind} test", secrets=secrets,
                                    config={"channel": "C0DECIDE", **(config or {})},
                                    actor="test")


def _decision(engine, project_id, tier=2, question="Ship the SLA timer?"):
    with engine.begin() as c:
        return create_blocking_decision(c, project_id, None, question,
                                        {"summary": "Recommended: ship it"}, {}, "chat test",
                                        risk_tier=tier)


def _deliver(engine):
    return notify.deliver_chat_due(engine, get_settings(), now=now_utc() + timedelta(seconds=1))


def _card_tokens(fake):
    post = fake.named("chat.postMessage")[-1]
    actions = [b for b in post["blocks"] if b["type"] == "actions"]
    return {e["action_id"].replace("boss_decide_", ""): e["value"]
            for e in (actions[0]["elements"] if actions else []) if "value" in e}


def _slack_post(client, endpoint_id, body: bytes, content_type, *, secret=SIGNING, ts=None,
                sig=None):
    ts = str(int(time.time()) if ts is None else ts)
    headers = {"Content-Type": content_type, "X-Slack-Request-Timestamp": ts,
               "X-Slack-Signature": sig or chat_adapters.slack_signature(secret, ts, body)}
    return client.post(f"/v1/webhooks/inbound/chat/{endpoint_id}", content=body, headers=headers)


def _press_body(token, action, user="U0PRESSER", trigger="13345224609.738474920.8088930838d88f008e0"):
    """A block_actions payload, as Slack records it (api.slack.com/reference/interaction-payloads)."""
    payload = {
        "type": "block_actions", "team": {"id": "T9TK3CUKW", "domain": "example"},
        "user": {"id": user, "username": "presser", "name": "presser", "team_id": "T9TK3CUKW"},
        "api_app_id": "AABA1ABCD", "token": "9s8d9as89d8as9d8as989",
        "container": {"type": "message", "message_ts": "1700000000.000100",
                      "channel_id": "C0DECIDE", "is_ephemeral": False},
        "trigger_id": trigger,
        "channel": {"id": "C0DECIDE", "name": "decisions"},
        "message": {"type": "message", "ts": "1700000000.000100", "text": "BOSS needs a decision"},
        "response_url": "https://hooks.slack.com/actions/T9TK3CUKW/1/abc",
        "actions": [{"action_id": f"boss_decide_{action}", "block_id": "boss_decision",
                     "text": {"type": "plain_text", "text": action.title()}, "value": token,
                     "type": "button", "action_ts": "1700000001.000200"}],
    }
    return urlencode({"payload": json.dumps(payload)}).encode()


def _link(engine, endpoint, principal, external="U0PRESSER"):
    with engine.begin() as c:
        code = chat.start_link(c, endpoint, external, "presser")
        return chat.confirm_link(c, code, principal)


def _status(engine, decision_id):
    with engine.connect() as c:
        return c.execute(text("SELECT status, approved_by FROM decisions WHERE id = :d"),
                         {"d": decision_id}).mappings().first()


def _events(engine, entity_id, event_type):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT * FROM events WHERE entity_id = :e AND event_type = :t"),
            {"e": entity_id, "t": event_type}).mappings()]


# ---- signatures ---------------------------------------------------------------------------------

def test_the_slack_signature_matches_slacks_documented_example():
    # api.slack.com/authentication/verifying-requests-from-slack — the recorded example request.
    body = (b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&"
            b"channel_id=G8PSS9T3V&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&"
            b"command=%2Fwebhook-collect&text=&response_url=https%3A%2F%2Fhooks.slack.com%2F"
            b"commands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN&trigger_id="
            b"398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c")
    sig = chat_adapters.slack_signature("8f742231b10e8888abcd99yyyzzz85a5", "1531420618", body)
    assert sig == "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"
    slack = chat_adapters.SlackChat()
    slack.verify("8f742231b10e8888abcd99yyyzzz85a5",
                 {"X-Slack-Request-Timestamp": "1531420618", "X-Slack-Signature": sig}, body,
                 now=1531420618 + 10)
    with pytest.raises(chat_adapters.ChatVerifyRefused) as stale:
        slack.verify("8f742231b10e8888abcd99yyyzzz85a5",
                     {"X-Slack-Request-Timestamp": "1531420618", "X-Slack-Signature": sig}, body,
                     now=1531420618 + 301)
    assert stale.value.reason == "stale_timestamp"


def test_an_unsigned_wrong_or_stale_request_is_refused_and_recorded(engine, world, client,
                                                                    channel):
    ep = _endpoint(engine, world.project_a)
    body = json.dumps({"type": "url_verification", "challenge": "c-123"}).encode()
    ok = _slack_post(client, ep["id"], body, "application/json")
    assert ok.status_code == 200 and ok.json() == {"challenge": "c-123"}
    wrong = _slack_post(client, ep["id"], body, "application/json", secret="not-the-secret")
    stale = _slack_post(client, ep["id"], body, "application/json", ts=int(time.time()) - 400)
    missing = client.post(f"/v1/webhooks/inbound/chat/{ep['id']}", content=body,
                          headers={"Content-Type": "application/json"})
    unknown = _slack_post(client, "che_nope", body, "application/json")
    assert (wrong.status_code, wrong.json()["error"]) == (401, "bad_signature")
    assert (stale.status_code, stale.json()["error"]) == (401, "stale_timestamp")
    assert (missing.status_code, missing.json()["error"]) == (401, "missing_signature")
    assert unknown.status_code == 404
    reasons = sorted(e["data"]["reason"] for e in _events(engine, ep["id"], "chat.request_refused"))
    assert reasons == ["bad_signature", "missing_signature", "stale_timestamp"]


def test_no_secret_is_ever_returned(engine, world, client, channel):
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/chat/endpoints", json={
            "project_id": world.project_a, "kind": "slack", "name": "Decisions",
            "config": {"channel": "C0DECIDE"},
            "secrets": {"signing_secret": SIGNING, "bot_token": BOT}})
        listed = client.get("/v1/chat/endpoints", params={"project_id": world.project_a})
    assert made.status_code == 201, made.text
    assert made.json()["urls"]["interactivity_request_url"].endswith(
        f"/v1/webhooks/inbound/chat/{made.json()['id']}")
    for text_ in (made.text, listed.text):
        assert SIGNING not in text_ and BOT not in text_
    assert made.json()["secrets_set"] == ["bot_token", "signing_secret"]
    with world.acting_as(world.ctx(world.viewer)):
        refused = client.post("/v1/chat/endpoints", json={
            "project_id": world.project_a, "kind": "slack", "config": {"channel": "C1"},
            "secrets": {"signing_secret": "x", "bot_token": "y"}})
    assert refused.status_code == 403


# ---- the decision flow ---------------------------------------------------------------------------

def test_a_new_decision_is_posted_with_signed_single_use_buttons(engine, world, channel):
    ep = _endpoint(engine, world.project_a)
    decision_id = _decision(engine, world.project_a)
    sent = _deliver(engine)
    assert sent["sent"] >= 1
    tokens = _card_tokens(channel)
    assert set(tokens) == {"approve", "reject"}
    post = channel.named("chat.postMessage")[-1]
    assert post["channel"] == "C0DECIDE"
    assert any(e.get("url", "").startswith("https://boss.example.test/?area=decisions")
               for b in post["blocks"] if b["type"] == "actions" for e in b["elements"])
    with engine.connect() as c:
        thread = c.execute(text("SELECT * FROM chat_threads WHERE decision_id = :d AND "
                                "endpoint_id = :e"), {"d": decision_id, "e": ep["id"]}
                           ).mappings().first()
        rows = c.execute(text("SELECT status FROM chat_actions WHERE decision_id = :d AND "
                              "endpoint_id = :e"), {"d": decision_id, "e": ep["id"]}).scalars().all()
    assert thread["external_thread_id"] == "1700000000.000100"
    assert sorted(rows) == ["pending", "pending"]
    # The card is one per endpoint per decision: a second sweep sends nothing new.
    assert _deliver(engine)["sent"] == 0


def test_an_unlinked_press_sends_a_link_and_decides_nothing_until_the_person_confirms(
        engine, world, client, channel):
    ep = _endpoint(engine, world.project_a)
    decision_id = _decision(engine, world.project_a)
    _deliver(engine)
    tokens = _card_tokens(channel)
    r = _slack_post(client, ep["id"], _press_body(tokens["approve"], "approve", user="U0NEW"),
                    "application/x-www-form-urlencoded")
    assert r.status_code == 200 and r.json()["status"] == "link_required"
    assert _status(engine, decision_id)["status"] == "pending"
    dm = channel.named("chat.postMessage")[-1]
    assert dm["channel"] == "D0DM" and "https://boss.example.test/chat/link?code=" in dm["text"]
    code = dm["text"].split("code=", 1)[1].split()[0]
    # the code alone links nobody: a signed-in PERSON confirms it
    with world.acting_as(world.ctx(world.owner)):
        peek = client.post("/v1/chat/links/peek", json={"code": code})
        done = client.post("/v1/chat/links/confirm", json={"code": code})
        again = client.post("/v1/chat/links/confirm", json={"code": code})
        mine = client.get("/v1/chat/links")
    assert peek.status_code == 200 and peek.json()["external_user_id"] == "U0NEW"
    assert done.status_code == 200, done.text
    assert done.json()["principal_id"] == world.owner.id
    assert again.status_code == 404
    assert [x["external_user_id"] for x in mine.json()["links"] if x["endpoint_id"] == ep["id"]] \
        == ["U0NEW"]
    # the button pressed before the link is still good (it was never spent)
    r2 = _slack_post(client, ep["id"], _press_body(tokens["approve"], "approve", user="U0NEW"),
                     "application/x-www-form-urlencoded")
    assert r2.json()["status"] == "done", r2.text


def test_a_linked_press_approves_as_the_person_with_audit_and_a_replay_refuses(
        engine, world, client, channel):
    ep = _endpoint(engine, world.project_a)
    _link(engine, ep, world.owner)
    decision_id = _decision(engine, world.project_a)
    _deliver(engine)
    tokens = _card_tokens(channel)
    r = _slack_post(client, ep["id"], _press_body(tokens["approve"], "approve"),
                    "application/x-www-form-urlencoded")
    assert r.status_code == 200 and r.json()["status"] == "done", r.text
    row = _status(engine, decision_id)
    assert row["status"] == "approved"
    assert row["approved_by"] == chat.person_actor_for(world.owner)
    answered = _events(engine, decision_id, "chat.decision_answered")
    assert len(answered) == 1
    assert answered[0]["data"]["principal_id"] == world.owner.id
    assert answered[0]["data"]["external_user_id"] == "U0PRESSER"
    assert answered[0]["data"]["outcome"] == "done"
    assert _events(engine, decision_id, "decision.approved")
    # the card is updated in place, through the outbox
    assert channel.named("chat.update")[-1]["ts"] == "1700000000.000100"
    # the same button again: compare-and-swap refuses; the other button is retired too
    replay = _slack_post(client, ep["id"], _press_body(tokens["approve"], "approve"),
                         "application/x-www-form-urlencoded")
    other = _slack_post(client, ep["id"], _press_body(tokens["reject"], "reject"),
                        "application/x-www-form-urlencoded")
    assert replay.json()["status"] == "refused" and "already used" in \
        channel.named("chat.postEphemeral")[-2]["text"]
    assert other.json()["status"] == "refused"
    assert _status(engine, decision_id)["status"] == "approved"


def test_a_forged_or_mismatched_token_refuses(engine, world, client, channel):
    ep = _endpoint(engine, world.project_a)
    _link(engine, ep, world.owner)
    decision_id = _decision(engine, world.project_a)
    _deliver(engine)
    tokens = _card_tokens(channel)
    forged = tokens["approve"].split(".")[0] + ".AAAAAAAAAAAAAAAAAAAAAA"
    r1 = _slack_post(client, ep["id"], _press_body(forged, "approve"),
                     "application/x-www-form-urlencoded")
    # a reject token sent as an approve button
    r2 = _slack_post(client, ep["id"], _press_body(tokens["reject"], "approve"),
                     "application/x-www-form-urlencoded")
    assert r1.json()["status"] == "refused" and r2.json()["status"] == "refused"
    assert _status(engine, decision_id)["status"] == "pending"
    with engine.connect() as c:
        spent = c.execute(text("SELECT count(*) FROM chat_actions WHERE decision_id = :d AND "
                               "status = 'used'"), {"d": decision_id}).scalar()
    assert spent == 0


def test_a_tier_three_decision_is_answered_in_the_console_never_from_chat(
        engine, world, client, channel):
    ep = _endpoint(engine, world.project_a)
    _link(engine, ep, world.owner)
    decision_id = _decision(engine, world.project_a, tier=3, question="Rotate production keys?")
    _deliver(engine)
    assert _card_tokens(channel) == {}            # no buttons on the card
    post = channel.named("chat.postMessage")[-1]
    assert "answered in BOSS" in json.dumps(post["blocks"])
    # even a token minted for it (a stale card, a forged path) only answers with the link
    with engine.begin() as c:
        token = chat.mint_action(c, chat.get_endpoint(c, ep["id"]),
                                 {"id": decision_id, "project_id": world.project_a}, "approve")
    r = _slack_post(client, ep["id"], _press_body(token, "approve"),
                    "application/x-www-form-urlencoded")
    assert r.json()["status"] == "console_required"
    assert "https://boss.example.test/?area=decisions" in channel.named(
        "chat.postEphemeral")[-1]["text"]
    assert _status(engine, decision_id)["status"] == "pending"


def test_a_reason_required_answer_opens_the_modal_and_the_submission_decides(
        engine, world, client, channel):
    ep = _endpoint(engine, world.project_a, config={"reject_needs_note": True})
    _link(engine, ep, world.owner)
    decision_id = _decision(engine, world.project_a)
    _deliver(engine)
    tokens = _card_tokens(channel)
    r = _slack_post(client, ep["id"], _press_body(tokens["reject"], "reject"),
                    "application/x-www-form-urlencoded")
    assert r.json()["status"] == "note_required"
    view = channel.named("views.open")[-1]
    assert view["trigger_id"].startswith("13345224609")
    meta = json.loads(view["view"]["private_metadata"])
    assert meta["token"] == tokens["reject"]
    assert _status(engine, decision_id)["status"] == "pending"
    submission = {
        "type": "view_submission", "team": {"id": "T9TK3CUKW"},
        "user": {"id": "U0PRESSER", "username": "presser"},
        "view": {"id": "V0MODAL", "callback_id": "boss_decide_note",
                 "private_metadata": view["view"]["private_metadata"],
                 "state": {"values": {"note": {"note_input": {
                     "type": "plain_text_input", "value": "Not this sprint: legal review first"}}}}},
    }
    body = urlencode({"payload": json.dumps(submission)}).encode()
    done = _slack_post(client, ep["id"], body, "application/x-www-form-urlencoded")
    assert done.status_code == 200 and done.json() == {"response_action": "clear"}, done.text
    with engine.connect() as c:
        row = c.execute(text("SELECT status, resolution_note FROM decisions WHERE id = :d"),
                        {"d": decision_id}).mappings().first()
    assert row["status"] == "rejected"
    assert "legal review first" in (row["resolution_note"] or "")


def test_a_viewer_linked_in_chat_still_may_not_decide(engine, world, client, channel):
    ep = _endpoint(engine, world.project_a)
    _link(engine, ep, world.viewer, external="U0VIEWER")
    decision_id = _decision(engine, world.project_a)
    _deliver(engine)
    tokens = _card_tokens(channel)
    r = _slack_post(client, ep["id"], _press_body(tokens["approve"], "approve", user="U0VIEWER"),
                    "application/x-www-form-urlencoded")
    assert r.json()["status"] == "refused" and r.json()["code"] == "not_permitted"
    assert _status(engine, decision_id)["status"] == "pending"


def test_before_the_claim_member_rules_decide_for_the_legacy_actor(engine, client, channel):
    w = IdentityWorld.create(engine)          # never claimed: today's member rules
    person = w.person("legacy-approver")
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO org_members (id, org_id, member_id, display_name, member_type, role_keys, "
            "permissions, status, created_by) VALUES (:i, :o, :m, 'Legacy approver', 'human', "
            "'[\"ceo\", \"owner\"]'::jsonb, '[\"decision_resolve\"]'::jsonb, 'active', 'test')"),
            {"i": f"om_{uuid.uuid4().hex[:8]}", "o": w.org, "m": person.id})
    ep = _endpoint(engine, w.project_a)
    _link(engine, ep, person, external="U0LEGACY")
    decision_id = _decision(engine, w.project_a)
    _deliver(engine)
    tokens = _card_tokens(channel)
    r = _slack_post(client, ep["id"], _press_body(tokens["approve"], "approve", user="U0LEGACY"),
                    "application/x-www-form-urlencoded")
    assert r.json()["status"] == "done", r.text
    assert _status(engine, decision_id)["approved_by"] == person.id


# ---- the outbox ----------------------------------------------------------------------------------

def test_the_outbox_retries_with_backoff_then_dead_letters(engine, world, channel):
    ep = _endpoint(engine, world.project_a)
    with engine.begin() as c:
        row_id = notify.enqueue_chat(c, endpoint_id=ep["id"], project_id=world.project_a,
                                     kind="message", payload={"op": "dm", "user": "U0X",
                                                              "text": "hello"},
                                     dedupe_key=f"t:{uuid.uuid4().hex}")
    channel.fail = True
    now = now_utc()
    seen = []
    for _ in range(notify.MAX_CHAT_ATTEMPTS):
        now += timedelta(hours=2)          # past any backoff
        notify.deliver_chat_due(engine, get_settings(), now=now, endpoint_id=ep["id"])
        with engine.connect() as c:
            r = c.execute(text("SELECT status, attempts, next_attempt_at, last_error FROM "
                               "chat_outbox WHERE id = :i"), {"i": row_id}).mappings().first()
        seen.append((r["status"], r["attempts"], r["next_attempt_at"] - now))
    assert [s[0] for s in seen] == ["failed"] * (notify.MAX_CHAT_ATTEMPTS - 1) + ["dead"]
    assert [s[1] for s in seen] == list(range(1, notify.MAX_CHAT_ATTEMPTS + 1))
    gaps = [s[2] for s in seen[:-1]]
    assert gaps == sorted(gaps) and gaps[0] < gaps[-1]           # backoff grows
    assert "Slack answered 503" in r["last_error"] and BOT not in r["last_error"]
    dead = _events(engine, ep["id"], "chat.delivery_dead")
    assert [e["data"]["outbox_id"] for e in dead] == [row_id]
    # dead is dead: no further attempts
    channel.fail = False
    notify.deliver_chat_due(engine, get_settings(), now=now + timedelta(days=1),
                            endpoint_id=ep["id"])
    with engine.connect() as c:
        assert c.execute(text("SELECT status FROM chat_outbox WHERE id = :i"),
                         {"i": row_id}).scalar() == "dead"


def test_a_failed_send_keeps_the_card_and_its_tokens_unminted_until_it_lands(engine, world,
                                                                             channel):
    ep = _endpoint(engine, world.project_a)
    decision_id = _decision(engine, world.project_a)
    channel.fail = True
    _deliver(engine)
    with engine.connect() as c:
        minted = c.execute(text("SELECT count(*) FROM chat_actions WHERE decision_id = :d AND "
                                "endpoint_id = :e"), {"d": decision_id, "e": ep["id"]}).scalar()
    assert minted == 0                     # the send failed: its tokens rolled back with it
    channel.fail = False
    notify.deliver_chat_due(engine, get_settings(), now=now_utc() + timedelta(hours=1),
                            endpoint_id=ep["id"])
    assert set(_card_tokens(channel)) == {"approve", "reject"}


# ---- Telegram --------------------------------------------------------------------------------------

def test_telegram_verifies_its_secret_header_and_decides_from_an_inline_button(
        engine, world, client, channel):
    ep = _endpoint(engine, world.project_a, kind="telegram")
    _link(engine, ep, world.owner, external="4242")
    decision_id = _decision(engine, world.project_a)
    _deliver(engine)
    sent = channel.named("sendMessage")[-1]
    keyboard = sent["reply_markup"]["inline_keyboard"]
    tokens = {b["text"]: b["callback_data"] for b in keyboard[0]}
    assert all(len(t.encode()) <= 64 for t in tokens.values())   # Telegram's callback_data cap
    update = {"update_id": 10000, "callback_query": {
        "id": "4382bfdwdsb323b2d9", "from": {"id": 4242, "is_bot": False, "username": "presser"},
        "message": {"message_id": 77, "chat": {"id": -100123, "type": "supergroup"}},
        "chat_instance": "-1", "data": tokens["Approve"]}}
    body = json.dumps(update).encode()
    bad = client.post(f"/v1/webhooks/inbound/chat/{ep['id']}", content=body,
                      headers={"Content-Type": "application/json",
                               "X-Telegram-Bot-Api-Secret-Token": "wrong"})
    assert bad.status_code == 401
    ok = client.post(f"/v1/webhooks/inbound/chat/{ep['id']}", content=body,
                     headers={"Content-Type": "application/json",
                              "X-Telegram-Bot-Api-Secret-Token": TG_SECRET})
    assert ok.status_code == 200 and ok.json()["status"] == "done", ok.text
    assert _status(engine, decision_id)["status"] == "approved"
    assert channel.named("answerCallbackQuery")[-1]["callback_query_id"] == "4382bfdwdsb323b2d9"
    assert channel.named("editMessageText")[-1]["message_id"] == 77


def test_discord_is_a_declared_stub_that_refuses_rather_than_pretends(engine, world, client):
    with engine.begin() as c:
        ep = chat.create_endpoint(c, get_settings(), project_id=world.project_a, kind="discord",
                                  name="discord", secrets={}, config={}, actor="test")
    assert ep["stub"] is True
    r = client.post(f"/v1/webhooks/inbound/chat/{ep['id']}", content=b"{}",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 401 and r.json()["error"] == "discord_not_implemented"
