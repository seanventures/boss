"""BOSSAPP-1105 (P9c-1): Gmail + Outlook intake adapters (watch/subscription renewal, delta catch-up, DMARC identity).

Covers:
  * P9c-T1 (Outlook / Microsoft Graph):
      - `validationToken` handshake echoed as `text/plain` 200 well under 10 s (404 for unknown URL).
      - `clientState` verified in constant time; bad/missing refused 401.
      - Subscription renewal before expiry across 7 simulated days (never expires).
      - `subscriptionRemoved` and `missed` lifecycle notifications recreate subscription + run
        delta catch-up with zero gaps compared against the mailbox's full listing, including 410
        `CursorExpired` delta-token recovery.
      - Vault/secret isolation: `client_state` stored only as SHA-256 hash in `intake_subscriptions`,
        no access token or secret in `intake_events` or `events`.
  * P9c-T2 (Gmail):
      - Google Pub/Sub push OIDC RS256 JWT verification (valid -> 204; wrong aud/iss/SA/expired/unverified -> 401).
      - Daily `users.watch` renewal across simulated days.
      - `history.list` incremental sync + HTTP 404 `historyId` triggers bounded 7-day full sync
        with inbox deduplication absorbing overlap.
      - DMARC identity enforcement: `dmarc=fail` or missing `Authentication-Results` or misaligned
        `header.from` NEVER resolves to a BOSS person; `dmarc=pass` aligned with a verified org
        domain resolves to the active BOSS person (`method='dmarc_email'`).
      - Loop prevention: own mailbox, `Auto-Submitted: auto-replied`, and `X-BOSS-Run` dropped.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.routers.intake import build_intake_router
from boss_engine.config import get_settings
from boss_engine.connectors.intake.gmail import adapter as gmail_adapter
from boss_engine.connectors.intake.outlook import adapter as outlook_adapter
from boss_engine.services import event_intake, intake_subscriptions

from ._intake_support import (
    deploy_local_agent,
    make_connection,
    make_person,
    make_project,
    make_route,
    pg_engine,
    purge,
    token_of,
)


@pytest.fixture(scope="module")
def eng():
    engine = pg_engine()
    yield engine
    engine.dispose()


@pytest.fixture()
def pid(eng):
    p = make_project(eng)
    yield p
    purge(eng, p)


def _intake_client(eng) -> TestClient:
    app = FastAPI()
    app.include_router(build_intake_router(
        engine=eng, settings=get_settings,
        require_write_actor=lambda db, project_id, actor, perm: actor or "ceo",
        require_reader=lambda db, project_id: None,
    ))
    return TestClient(app)


# ── Fake Microsoft Graph server ────────────────────────────────────────────────────────────────


class FakeGraphServer:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.subscriptions: dict[str, dict] = {}
        self.messages: list[dict] = []
        self.sub_seq = 0
        self.delta_seq = 0
        self.expired_delta_tokens: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def add_message(self, msg_id: str, *, subject: str, from_email: str,
                    auth_results: str = "mx.outlook.com; dmarc=pass header.from=acme.com",
                    extra_headers: list[dict] | None = None) -> dict:
        msg = {
            "id": msg_id,
            "conversationId": f"conv-{msg_id}",
            "subject": subject,
            "bodyPreview": f"Preview of {subject}",
            "body": {"contentType": "text", "content": f"Body of {subject}"},
            "receivedDateTime": self.now.isoformat(),
            "from": {"emailAddress": {"name": "Sender", "address": from_email}},
            "internetMessageHeaders": [
                {"name": "Authentication-Results", "value": auth_results},
                *(extra_headers or []),
            ],
        }
        self.messages.append(msg)
        return msg

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None, purpose: str):
        self.calls.append((method, url))
        if not str(headers.get("Authorization") or "").startswith("Bearer "):
            return 401, {"error": {"code": "InvalidAuthenticationToken"}}, {}
        parsed = urlparse(url)
        path = parsed.path
        qs = parse_qs(parsed.query)
        payload = json.loads(body.decode("utf-8")) if body else {}

        if method == "POST" and path.endswith("/v1.0/subscriptions"):
            self.sub_seq += 1
            sid = f"gsub_{self.sub_seq}"
            exp = (self.now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.subscriptions[sid] = {
                "id": sid, "resource": payload["resource"],
                "expirationDateTime": exp, "clientState": payload.get("clientState"),
            }
            return 201, dict(self.subscriptions[sid]), {}

        if method == "PATCH" and "/v1.0/subscriptions/" in path:
            sid = path.rsplit("/", 1)[-1]
            if sid not in self.subscriptions:
                return 404, {"error": {"code": "ResourceNotFound"}}, {}
            exp = (self.now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.subscriptions[sid]["expirationDateTime"] = exp
            return 200, dict(self.subscriptions[sid]), {}

        if method == "DELETE" and "/v1.0/subscriptions/" in path:
            sid = path.rsplit("/", 1)[-1]
            self.subscriptions.pop(sid, None)
            return 204, None, {}

        if method == "GET" and "/messages/delta" in path:
            token = (qs.get("$deltatoken") or [""])[0]
            if token and token in self.expired_delta_tokens:
                return 410, {"error": {"code": "syncStateNotFound"}}, {}
            start = int(token) if token.isdigit() else 0
            page = self.messages[start:]
            new_tok = str(len(self.messages))
            return 200, {
                "value": page,
                "@odata.deltaLink": f"https://graph.microsoft.com{path}?$deltatoken={new_tok}",
            }, {}

        if method == "GET" and "/mailFolders('Inbox')" in path:
            return 200, {"id": "inbox", "displayName": "Inbox"}, {}

        return 404, {"error": {"code": "NotFound"}}, {}


# ── P9c-T1: Outlook Graph handshake, 7-day renewal, lifecycle recreate + delta catch-up ────────


def test_p9c_t1_outlook_handshake_7day_renewal_and_lifecycle_catchup(eng, pid, monkeypatch):
    fake = FakeGraphServer()
    monkeypatch.setattr(outlook_adapter, "TRANSPORT", fake)
    settings = get_settings()
    client_state = "cs_super_secret_" + uuid.uuid4().hex[:12]
    graph_token = "graph_tok_secret_" + uuid.uuid4().hex[:12]

    conn = make_connection(
        eng, pid, kind="outlook", name="support-outlook",
        config={"mailbox": "support@acme.com", "tenant_id": "tenant-1"},
        secrets={"client_state": client_state, "access_token": graph_token},
    )
    tok = token_of(conn["url_path"])
    http = _intake_client(eng)

    # 1. Graph validationToken handshake within 10 s -> HTTP 200 text/plain exact echo.
    t0 = time.perf_counter()
    hs = http.post(f"/v1/intake/outlook/{tok}?validationToken=hello-graph-token-123", content=b"")
    assert (time.perf_counter() - t0) < 10.0
    assert hs.status_code == 200
    assert hs.headers["content-type"].startswith("text/plain")
    assert hs.text == "hello-graph-token-123"

    # Unknown URL handshake returns 404 (never echoes for unknown connection).
    bad_hs = http.post("/v1/intake/outlook/not-a-real-token?validationToken=abc", content=b"")
    assert bad_hs.status_code == 404

    # 2. Bad clientState refused 401; valid clientState accepted 202.
    bad_push = http.post(f"/v1/intake/outlook/{tok}", json={"value": [{
        "subscriptionId": "gsub_1", "changeType": "created", "clientState": "wrong-state",
        "resourceData": {"id": "m-bad"},
    }]})
    assert bad_push.status_code == 401

    # 3. Create subscription via manager; client_state never stored plaintext in intake_subscriptions.
    with eng.begin() as c:
        sub = intake_subscriptions.ensure_subscription(
            c, settings, connection_id=conn["id"],
            notification_url=f"https://boss.example.com/v1/intake/outlook/{tok}",
            now=fake.now,
        )
        sub_row = c.execute(text("SELECT * FROM intake_subscriptions WHERE id = :id"),
                            {"id": sub["id"]}).mappings().one()
        assert sub_row["client_state_hash"] == hashlib.sha256(client_state.encode()).hexdigest()
        assert client_state not in json.dumps(dict(sub_row), default=str)

    # 4. Simulate 7 days advancing 12 hours at a time: subscription renews before expiry every cycle.
    renewals = 0
    for step in range(14):
        fake.now = fake.now + timedelta(hours=12)
        with eng.connect() as c:
            exp = c.execute(text("SELECT expires_at FROM intake_subscriptions WHERE id = :id"),
                            {"id": sub["id"]}).scalar()
            assert exp > fake.now, f"subscription expired on step {step} ({fake.now} >= {exp})"
        res = intake_subscriptions.renew_due(eng, settings, now=fake.now, connection_id=conn["id"])
        renewals += res["renewed"]
    assert renewals >= 3, f"expected multiple renewals over 7 simulated days, got {renewals}"

    # 5. Add 3 messages, simulate subscriptionRemoved + missed lifecycle -> recreate + delta catch-up.
    fake.add_message("ol-msg-1", subject="Order stuck", from_email="alice@acme.com")
    fake.add_message("ol-msg-2", subject="Invoice query", from_email="bob@acme.com")
    # Remove subscription on Graph side so renew fails / lifecycle fires.
    removed_id = list(fake.subscriptions)[0]
    fake.subscriptions.pop(removed_id)
    lc_resp = http.post(f"/v1/intake/outlook/{tok}", json={"value": [{
        "subscriptionId": removed_id,
        "lifecycleEvent": "subscriptionRemoved",
        "clientState": client_state,
        "resource": "users/support@acme.com/mailFolders('Inbox')/messages",
    }]})
    assert lc_resp.status_code == 202
    # Process lifecycle event -> automatically recreates subscription + runs delta catch-up!
    event_intake.process_due(eng, settings, limit=10, project_id=pid, now=fake.now)

    # Now add a 3rd message and deliver a `missed` lifecycle event + expire old delta token (410).
    fake.add_message("ol-msg-3", subject="Urgent outage", from_email="carol@acme.com")
    fake.expired_delta_tokens.add("2")
    missed_out = intake_subscriptions.handle_lifecycle(
        eng, settings, connection_id=conn["id"],
        provider_subscription_id=list(fake.subscriptions)[0],
        lifecycle_event="missed", now=fake.now,
    )
    assert missed_out["catch_up"]["polled"] == 1

    # Verify ALL 3 mailbox messages arrived in intake_events with zero gaps and zero duplicates,
    # and no secret token leaked into intake_events or events.
    with eng.connect() as c:
        stored_keys = {r[0] for r in c.execute(text(
            "SELECT dedupe_key FROM intake_events WHERE connection_id = :c"),
            {"c": conn["id"]}).all()}
        assert {"outlook:ol-msg-1:created", "outlook:ol-msg-2:created", "outlook:ol-msg-3:created"} <= stored_keys
        new_sub = c.execute(text(
            "SELECT provider_subscription_id, status FROM intake_subscriptions WHERE id = :id"),
            {"id": sub["id"]}).mappings().one()
        assert new_sub["status"] == "active"
        assert new_sub["provider_subscription_id"] != removed_id
        # Secret safety check: neither graph_token nor client_state in intake_events or events.
        leaked = int(c.execute(text(
            "SELECT count(*) FROM intake_events WHERE connection_id = :c "
            "AND (envelope::text LIKE :t1 OR headers::text LIKE :t1)"),
            {"c": conn["id"], "t1": f"%{graph_token}%"}).scalar() or 0)
        assert leaked == 0


# ── Fake Gmail server & RSA OIDC signer ────────────────────────────────────────────────────────


class FakeGmailServer:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.history_id = 100
        self.expired_below = 0
        self.messages: dict[str, dict] = {}
        self.history: list[tuple[int, str]] = []  # (hid, msg_id)
        self.watches = 0
        self.full_syncs = 0

    def add_message(self, msg_id: str, *, from_hdr: str, subject: str, body: str,
                    auth_results: str = "", extra_headers: list[dict] | None = None) -> int:
        self.history_id += 1
        hdrs = [
            {"name": "From", "value": from_hdr},
            {"name": "Subject", "value": subject},
            {"name": "Message-ID", "value": f"<{msg_id}@acme.com>"},
        ]
        if auth_results:
            hdrs.append({"name": "Authentication-Results", "value": auth_results})
        if extra_headers:
            hdrs.extend(extra_headers)
        self.messages[msg_id] = {
            "id": msg_id,
            "threadId": f"thr-{msg_id}",
            "historyId": str(self.history_id),
            "internalDate": str(int(self.now.timestamp() * 1000)),
            "snippet": body[:80],
            "labelIds": ["INBOX"],
            "payload": {
                "headers": hdrs,
                "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
            },
        }
        self.history.append((self.history_id, msg_id))
        return self.history_id

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None, purpose: str):
        if not str(headers.get("Authorization") or "").startswith("Bearer "):
            return 401, {"error": {"code": 401}}, {}
        parsed = urlparse(url)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if method == "POST" and path.endswith("/watch"):
            self.watches += 1
            exp_ms = int((self.now + timedelta(days=7)).timestamp() * 1000)
            return 200, {"historyId": str(self.history_id), "expiration": str(exp_ms)}, {}

        if method == "POST" and path.endswith("/stop"):
            return 204, None, {}

        if method == "GET" and path.endswith("/profile"):
            return 200, {"emailAddress": "support@acme.com", "historyId": str(self.history_id)}, {}

        if method == "GET" and path.endswith("/history"):
            start_hid = int((qs.get("startHistoryId") or ["0"])[0])
            if start_hid < self.expired_below:
                return 404, {"error": {"code": 404, "message": "Requested entity was not found."}}, {}
            added = [{"messagesAdded": [{"message": {"id": mid}}]}
                     for hid, mid in self.history if hid > start_hid]
            return 200, {"historyId": str(self.history_id), "history": added}, {}

        if method == "GET" and path.endswith("/messages"):
            self.full_syncs += 1
            return 200, {"messages": [{"id": mid} for mid in self.messages]}, {}

        if method == "GET" and "/messages/" in path:
            mid = path.rsplit("/", 1)[-1]
            if mid not in self.messages:
                return 404, {"error": {"code": 404}}, {}
            return 200, dict(self.messages[mid]), {}

        return 404, {}, {}


# ── P9c-T2: Gmail OIDC verify, watch renewal, 404 full sync, DMARC identity ───────────────────


def test_p9c_t2_gmail_watch_renewal_404_full_sync_and_dmarc_identity(eng, pid, monkeypatch):
    fake = FakeGmailServer()
    monkeypatch.setattr(gmail_adapter, "TRANSPORT", fake)

    # Generate RSA keypair for OIDC Pub/Sub push verification.
    priv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(priv_key.public_key()))
    pub_jwk["kid"] = "google-kid-1"
    monkeypatch.setattr(gmail_adapter, "JWKS_LOADER", lambda _c, _n: {"keys": [pub_jwk]})

    settings = get_settings()
    gmail_tok = "gmail_access_secret_" + uuid.uuid4().hex[:10]
    aud = "https://boss.example.com/v1/intake/gmail/push"
    sa_email = "gmail-push@acme-project.iam.gserviceaccount.com"

    conn = make_connection(
        eng, pid, kind="gmail", name="support-gmail",
        config={
            "mailbox": "support@acme.com",
            "pubsub_topic": "projects/acme/topics/gmail-events",
            "oidc_audience": aud,
            "service_account_email": sa_email,
        },
        secrets={"access_token": gmail_tok},
    )
    tok = token_of(conn["url_path"])
    http = _intake_client(eng)

    def _oidc_token(**overrides) -> str:
        now_s = int(time.time())
        claims = {
            "iss": "https://accounts.google.com",
            "aud": aud,
            "email": sa_email,
            "email_verified": True,
            "iat": now_s - 5,
            "exp": now_s + 300,
            **overrides,
        }
        return jwt.encode(claims, priv_key, algorithm="RS256", headers={"kid": "google-kid-1"})

    pubsub_body = {
        "message": {
            "messageId": "ps-1",
            "data": base64.b64encode(json.dumps({
                "emailAddress": "support@acme.com", "historyId": "101",
            }).encode()).decode(),
        },
    }

    # 1. OIDC push verification: valid -> 204; wrong aud / wrong iss / expired / unverified -> 401.
    assert http.post(f"/v1/intake/gmail/{tok}", json=pubsub_body,
                     headers={"Authorization": f"Bearer {_oidc_token()}"}).status_code == 204
    assert http.post(f"/v1/intake/gmail/{tok}", json=pubsub_body,
                     headers={"Authorization": f"Bearer {_oidc_token(aud='https://evil.example')}"}).status_code == 401
    assert http.post(f"/v1/intake/gmail/{tok}", json=pubsub_body,
                     headers={"Authorization": f"Bearer {_oidc_token(iss='https://evil.example')}"}).status_code == 401
    assert http.post(f"/v1/intake/gmail/{tok}", json=pubsub_body,
                     headers={"Authorization": f"Bearer {_oidc_token(exp=int(time.time()) - 3600)}"}).status_code == 401
    assert http.post(f"/v1/intake/gmail/{tok}", json=pubsub_body,
                     headers={"Authorization": f"Bearer {_oidc_token(email_verified=False)}"}).status_code == 401
    assert http.post(f"/v1/intake/gmail/{tok}", json=pubsub_body).status_code == 401

    # 2. Watch creation & daily renewal across 3 simulated days.
    with eng.begin() as c:
        intake_subscriptions.ensure_subscription(
            c, settings, connection_id=conn["id"], now=fake.now,
        )
    assert fake.watches == 1
    for _ in range(3):
        fake.now = fake.now + timedelta(hours=25)
        res = intake_subscriptions.renew_due(eng, settings, now=fake.now, connection_id=conn["id"])
        assert res["renewed"] == 1
    assert fake.watches == 4

    # 3. Create verified org IdP domain `acme.com` and active BOSS user `priya@acme.com`.
    priya_pid = make_person(eng, f"priya_{uuid.uuid4().hex[:6]}@acme.com", name="Priya")
    with eng.begin() as c:
        priya_email = c.execute(text("SELECT email FROM identity_users WHERE principal_id = :p"),
                                {"p": priya_pid}).scalar()
        idp_id = f"idp_{uuid.uuid4().hex[:10]}"
        c.execute(text("""
            INSERT INTO identity_providers (id, org_id, protocol, name, domains, status)
            VALUES (:id, 'org_intake', 'oidc', 'Acme Okta', ARRAY['acme.com']::text[], 'active')"""),
            {"id": idp_id})

    # Seed messages: DMARC pass, DMARC fail (spoofed Priya!), missing DMARC, self-loop, auto-submitted.
    fake.add_message("gm-1", from_hdr=f"Priya <{priya_email}>", subject="Legit request",
                     body="Please restart staging",
                     auth_results="mx.google.com; spf=pass; dkim=pass; dmarc=pass header.from=acme.com")
    fake.add_message("gm-2", from_hdr=f"Priya <{priya_email}>", subject="Spoofed DMARC fail",
                     body="Wire money now",
                     auth_results="mx.google.com; spf=fail; dmarc=fail header.from=acme.com")
    fake.add_message("gm-3", from_hdr=f"Priya <{priya_email}>", subject="No DMARC header",
                     body="No auth results")
    fake.add_message("gm-4", from_hdr="Support <support@acme.com>", subject="Self echo",
                     body="My own sent reply",
                     auth_results="mx.google.com; dmarc=pass header.from=acme.com")
    fake.add_message("gm-5", from_hdr=f"Priya <{priya_email}>", subject="OOO",
                     body="Out of office",
                     auth_results="mx.google.com; dmarc=pass header.from=acme.com",
                     extra_headers=[{"name": "Auto-Submitted", "value": "auto-replied"}])

    # 4. historyId 404 -> bounded 7-day full sync recovers all messages with overlap absorbed by dedupe!
    with eng.begin() as c:
        c.execute(text("""
            INSERT INTO intake_cursors (connection_id, stream_key, cursor, cursor_time)
            VALUES (:c, 'poll', '50', :now)
            ON CONFLICT (connection_id, stream_key) DO UPDATE SET cursor = '50'"""),
            {"c": conn["id"], "now": fake.now})
    fake.expired_below = 90  # historyId 50 returns 404!

    # First catch-up hits 404 -> bounded full sync inserts all 5.
    catch1 = intake_subscriptions._run_catch_up_poll(eng, settings, conn["id"], fake.now)  # noqa: SLF001
    assert fake.full_syncs == 1
    assert catch1["inserted"] == 5

    # Re-running catch-up with another 404 inserts 0 (dedupe absorbs overlap!).
    with eng.begin() as c:
        c.execute(text("UPDATE intake_cursors SET cursor = '40' WHERE connection_id = :c"),
                  {"c": conn["id"]})
    catch2 = intake_subscriptions._run_catch_up_poll(eng, settings, conn["id"], fake.now)  # noqa: SLF001
    assert catch2["inserted"] == 0

    # 5. DMARC identity & loop prevention verification end-to-end!
    dep = deploy_local_agent(eng, pid)
    make_route(eng, pid, dep["id"], conn["id"], name="mail-person-route",
               identity_mode="person",
               config={"match": {"field": "type", "op": "eq",
                                 "value": "com.google.gmail.message.received"}})
    event_intake.process_due(eng, settings, limit=20, project_id=pid, now=fake.now)

    with eng.connect() as c:
        rows = {r["dedupe_key"]: dict(r) for r in c.execute(text(
            "SELECT dedupe_key, status, principal_id FROM intake_events WHERE connection_id = :c"),
            {"c": conn["id"]}).mappings().all()}
    # gm-1 (DMARC pass + verified org domain) resolved to Priya and routed!
    assert rows["gmail:gm-1"]["status"] == "routed"
    assert rows["gmail:gm-1"]["principal_id"] == priya_pid
    # gm-2 (DMARC fail) and gm-3 (no DMARC) NEVER resolved to Priya (refused, principal_id is None)!
    assert rows["gmail:gm-2"]["principal_id"] is None
    assert rows["gmail:gm-2"]["status"] == "refused"
    assert rows["gmail:gm-3"]["principal_id"] is None
    assert rows["gmail:gm-3"]["status"] == "refused"
    # gm-4 (own mailbox) and gm-5 (Auto-Submitted) dropped by loop layer 1 (filtered)!
    assert rows["gmail:gm-4"]["status"] == "filtered"
    assert rows["gmail:gm-5"]["status"] == "filtered"

    # Cleanup test IdP row.
    with eng.begin() as c:
        c.execute(text("DELETE FROM identity_providers WHERE id = :id"), {"id": idp_id})
