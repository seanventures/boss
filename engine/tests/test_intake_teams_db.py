"""Microsoft Teams intake & outbox conformance tests on Postgres (P9b-1, BOSSAPP-1100).

Uses a real local fake Bot Framework + Entra HTTP server (ThreadingHTTPServer) and an
ephemeral RSA-2048 keypair to test RS256 OIDC JWKS verification, Activity normalization,
activity.id dedupe, zero secret/token persistence, loop prevention, conversation_ref refresh,
Entra aad_oid vs guest rejection, TeamsChat proactive delivery, multi-window rate buckets, and
403 MessageWritesBlocked permanent stop.
"""
from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from datetime import timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

from boss_engine.connectors.intake.contract import RawRequest
from boss_engine.connectors.intake.teams import adapter as teams_adapter
from boss_engine.services import (
    chat_adapters,
    chat_channels,
    chat_reply,
    event_intake,
    intake_guards,
    notify,
    scoped_budgets,
)

from ._intake_support import (
    deploy_local_agent,
    make_person,
    make_project,
    make_route,
    pg_engine,
    purge,
    runs_of,
)


def _b64u(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _rsa_pair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key().public_numbers()
    jwk = {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": "bf-key-1",
           "n": _b64u(pub.n), "e": _b64u(pub.e)}
    return priv, jwk


class FakeBotFrameworkServer:
    """In-process fake Bot Framework OIDC/JWKS + Entra Token + v3 Activities server."""

    def __init__(self, jwk: dict[str, Any]):
        self.jwk = jwk
        self.sent: list[dict[str, Any]] = []
        self.next_status: int | None = None
        self.next_body: dict[str, Any] | None = None
        self.retry_after: str | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _send(self, code: int, payload: dict[str, Any], headers: dict[str, str] | None = None):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path.endswith("/.well-known/openidconfiguration"):
                    self._send(200, {"jwks_uri": f"{outer.base_url}/jwks"})
                elif self.path.endswith("/jwks"):
                    self._send(200, {"keys": [outer.jwk]})
                else:
                    self._send(404, {"error": "not_found"})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                if self.path.endswith("/oauth2/v2.0/token"):
                    self._send(200, {"token_type": "Bearer", "expires_in": 3600,
                                     "access_token": "bf_access_tok_test"})
                    return
                parsed = json.loads(raw.decode("utf-8") or "{}")
                outer.sent.append({"method": "POST", "path": self.path, "body": parsed,
                                   "auth": self.headers.get("Authorization")})
                if outer.next_status is not None:
                    st = outer.next_status
                    bd = outer.next_body or {}
                    hdrs = {"Retry-After": outer.retry_after} if outer.retry_after else {}
                    outer.next_status = None
                    outer.next_body = None
                    outer.retry_after = None
                    self._send(st, bd, hdrs)
                    return
                self._send(201, {"id": f"bf_act_{len(outer.sent)}"})

            def do_PUT(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                parsed = json.loads(raw.decode("utf-8") or "{}")
                outer.sent.append({"method": "PUT", "path": self.path, "body": parsed})
                self._send(200, {"id": self.path.rsplit("/", 1)[-1]})

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()

    def transport(self, method: str, url: str, headers: dict[str, str],
                  body: bytes | None, purpose: str) -> tuple[int, Any]:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                data = {"raw": raw}
            if err.code == 429 and isinstance(data, dict):
                data["retry_after"] = err.headers.get("Retry-After")
            return err.code, data


def _sign_bf_jwt(priv, *, app_id: str, service_url: str, iss: str = "https://api.botframework.com",
                 exp_offset: int = 300, nbf_offset: int = -10, kid: str = "bf-key-1") -> str:
    now = int(time.time())
    return jwt.encode(
        {"iss": iss, "aud": app_id, "nbf": now + nbf_offset, "exp": now + exp_offset,
         "serviceUrl": service_url},
        priv,
        algorithm="RS256",
        headers={"kid": kid},
    )


def _activity(*, act_id: str, text_val: str, service_url: str, tenant_id: str,
              app_id: str, conv_id: str = "19:chan1@thread.tacv2",
              conv_type: str = "channel", reply_to: str = "",
              from_id: str = "29:user-1", aad_oid: str = "",
              user_type: str = "", is_guest: bool = False,
              mention_bot: bool = True, boss_marker: dict | None = None) -> dict[str, Any]:
    bot_id = f"28:{app_id}"
    entities = ([{"type": "mention", "mentioned": {"id": bot_id, "name": "BOSS"}}]
                if mention_bot else [])
    ch_data: dict[str, Any] = {"tenant": {"id": tenant_id}}
    if boss_marker:
        ch_data["boss"] = boss_marker
    sender: dict[str, Any] = {"id": from_id, "name": "Priya Nair"}
    if aad_oid:
        sender["aadObjectId"] = aad_oid
    if user_type:
        sender["userType"] = user_type
    if is_guest:
        sender["isGuest"] = True
    return {
        "type": "message",
        "id": act_id,
        "timestamp": "2026-11-25T10:00:00Z",
        "channelId": "msteams",
        "serviceUrl": service_url,
        "from": sender,
        "conversation": {"id": conv_id, "conversationType": conv_type, "tenantId": tenant_id},
        "recipient": {"id": bot_id, "name": "BOSS"},
        "text": f"<at>BOSS</at> {text_val}" if mention_bot else text_val,
        "entities": entities,
        "channelData": ch_data,
        **({"replyToId": reply_to} if reply_to else {}),
    }


def _make_teams(engine, pid: str, server: FakeBotFrameworkServer, *,
                app_id: str = "11111111-1111-1111-1111-111111111111",
                tenant_id: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                channel: str = "19:chan1@thread.tacv2") -> dict[str, Any]:
    from boss_engine.config import get_settings

    settings = get_settings()
    with engine.begin() as c:
        ep = chat_channels.create_endpoint(
            c, settings, project_id=pid, kind="teams", name="Teams Corp",
            config={
                "app_id": app_id,
                "tenant_id": tenant_id,
                "bot_id": f"28:{app_id}",
                "channel": channel,
                "service_url": server.base_url,
                "oidc_config_url": f"{server.base_url}/.well-known/openidconfiguration",
                "token_url": f"{server.base_url}/oauth2/v2.0/token",
            },
            secrets={"app_password": "teams_secret_never_stored_in_db"},
            actor="ceo",
        )
        conn = event_intake.create_connection(
            c, project_id=pid, kind="teams", actor="ceo", name="Teams Corp",
            config={"chat_endpoint_id": ep["id"]}, secrets={},
        )
    return {"endpoint": ep, "connection": conn, "token": conn["url_path"].rsplit("/", 1)[-1],
            "app_id": app_id, "tenant_id": tenant_id}


_SCOPE: dict[str, Any] = {"pid": None}


@pytest.fixture()
def world(monkeypatch):
    priv, jwk = _rsa_pair()
    server = FakeBotFrameworkServer(jwk)
    teams_adapter.reset_jwks_cache()
    event_intake.BUCKETS.reset()
    chat_reply.BUCKETS.reset()
    intake_guards.reset_for_tests()
    scoped_budgets.reset_for_tests()
    monkeypatch.setattr(chat_adapters, "TRANSPORT", server.transport)
    engine = pg_engine()
    pid = make_project(engine)
    _SCOPE["pid"] = pid
    try:
        yield engine, pid, server, priv
    finally:
        teams_adapter.reset_jwks_cache()
        with engine.begin() as c:
            c.execute(text(
                "DELETE FROM identity_user_identities WHERE principal_id IN "
                "(SELECT id FROM identity_principals WHERE org_id = 'org_intake' AND id <> 'ceo')"
            ))
            c.execute(text(
                "DELETE FROM identity_providers WHERE org_id = 'org_intake' AND preset = 'entra'"
            ))
        purge(engine, pid)
        server.close()


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


def _process(engine) -> dict:
    return event_intake.process_due(engine, _settings(), project_id=_SCOPE["pid"])


def _deliver(engine, *, at: float = 0.0) -> dict:
    with engine.begin() as c:
        stamped = c.execute(text("SELECT clock_timestamp()")).scalar()
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    return notify.deliver_chat_due(engine, _settings(), now=stamped + timedelta(seconds=at))


def test_teams_jwt_verification_dedupe_and_zero_secret_persistence(world):
    engine, pid, server, priv = world
    teams = _make_teams(engine, pid, server)
    act = _activity(act_id="act_001", text_val="check alerts", service_url=server.base_url,
                    tenant_id=teams["tenant_id"], app_id=teams["app_id"])
    raw = json.dumps(act).encode("utf-8")

    # Missing auth -> 401
    r0 = event_intake.receive(engine, "teams", teams["token"],
                              RawRequest(method="POST", headers={"content-type": "application/json"}, body=raw))
    assert r0["http_status"] == 401

    # Wrong audience -> 401
    bad_aud = _sign_bf_jwt(priv, app_id="wrong-app-id", service_url=server.base_url)
    r1 = event_intake.receive(engine, "teams", teams["token"], RawRequest(
        method="POST", headers={"authorization": f"Bearer {bad_aud}", "content-type": "application/json"}, body=raw))
    assert r1["http_status"] == 401

    # Mismatched serviceUrl claim -> 401
    bad_svc = _sign_bf_jwt(priv, app_id=teams["app_id"], service_url="https://evil.example")
    r2 = event_intake.receive(engine, "teams", teams["token"], RawRequest(
        method="POST", headers={"authorization": f"Bearer {bad_svc}", "content-type": "application/json"}, body=raw))
    assert r2["http_status"] == 401

    # Expired token beyond +/- 5m leeway -> 401
    expired = _sign_bf_jwt(priv, app_id=teams["app_id"], service_url=server.base_url, exp_offset=-400)
    r3 = event_intake.receive(engine, "teams", teams["token"], RawRequest(
        method="POST", headers={"authorization": f"Bearer {expired}", "content-type": "application/json"}, body=raw))
    assert r3["http_status"] == 401

    # Valid RS256 Bot Framework token -> 200 received + replay dedupes on activity.id
    good = _sign_bf_jwt(priv, app_id=teams["app_id"], service_url=server.base_url)
    r_ok = event_intake.receive(engine, "teams", teams["token"], RawRequest(
        method="POST", headers={"authorization": f"Bearer {good}", "content-type": "application/json"}, body=raw))
    assert r_ok["http_status"] == 200 and r_ok["outcome"] == "received"
    r_dup = event_intake.receive(engine, "teams", teams["token"], RawRequest(
        method="POST", headers={"authorization": f"Bearer {good}", "content-type": "application/json"}, body=raw))
    assert r_dup["http_status"] == 200 and r_dup["outcome"] == "duplicate"

    # Zero tokens/secrets in intake_events.headers or rows
    with engine.begin() as c:
        row = c.execute(text("SELECT headers, raw FROM intake_events WHERE id = :i"),
                        {"i": r_ok["event_id"]}).mappings().first()
    hdr_dump = json.dumps(row["headers"])
    assert "authorization" not in hdr_dump.lower()
    assert good not in hdr_dump
    assert "teams_secret_never_stored_in_db" not in hdr_dump


def test_teams_entra_aad_oid_linking_and_guest_rejection(world):
    engine, pid, server, priv = world
    teams = _make_teams(engine, pid, server)
    dep = deploy_local_agent(engine, pid, key="triage")
    make_route(engine, pid, dep["id"], teams["connection"]["id"],
               identity_mode="person",
               config={"match": {"field": "type", "op": "eq", "value": "com.microsoft.teams.message.mention"}})
    oid = f"oid-{uuid.uuid4().hex[:8]}"
    principal = make_person(engine, f"priya-{uuid.uuid4().hex[:6]}@corp.test")
    with engine.begin() as c:
        idp_id = f"idp_{uuid.uuid4().hex[:8]}"
        c.execute(text("""
            INSERT INTO identity_providers (id, org_id, protocol, name, preset, issuer, status)
            VALUES (:id, 'org_intake', 'oidc', 'Entra', 'entra', :iss, 'active')"""),
            {"id": idp_id, "iss": f"https://login.microsoftonline.com/{teams['tenant_id']}/v2.0"})
        c.execute(text("""
            INSERT INTO identity_user_identities (id, principal_id, provider_id, issuer, subject)
            VALUES (:id, :p, :idp, :iss, :sub)"""),
            {"id": f"iui_{uuid.uuid4().hex[:8]}", "p": principal, "idp": idp_id,
             "iss": f"https://login.microsoftonline.com/{teams['tenant_id']}/v2.0", "sub": oid})

    # Guest user with same aad_oid MUST NEVER auto-link
    guest_act = _activity(
        act_id="act_guest", text_val="hello", service_url=server.base_url,
        tenant_id=teams["tenant_id"], app_id=teams["app_id"],
        from_id="29:guest-1", aad_oid=oid, user_type="Guest", is_guest=True,
    )
    tok = _sign_bf_jwt(priv, app_id=teams["app_id"], service_url=server.base_url)
    event_intake.receive(engine, "teams", teams["token"], RawRequest(
        method="POST", headers={"authorization": f"Bearer {tok}", "content-type": "application/json"},
        body=json.dumps(guest_act).encode("utf-8"),
    ))
    _process(engine)
    assert len(runs_of(engine, pid)) == 0

    # Home-tenant non-guest member with aad_oid auto-links with method='aad_oid' and starts a run + proactive reply
    home_act = _activity(
        act_id="act_home", text_val="triage this alert", service_url=server.base_url,
        tenant_id=teams["tenant_id"], app_id=teams["app_id"],
        from_id="29:home-1", aad_oid=oid,
    )
    event_intake.receive(engine, "teams", teams["token"], RawRequest(
        method="POST", headers={"authorization": f"Bearer {tok}", "content-type": "application/json"},
        body=json.dumps(home_act).encode("utf-8"),
    ))
    _process(engine)
    with engine.begin() as c:
        link = c.execute(text(
            "SELECT method, idp_subject, principal_id FROM chat_identity_links "
            "WHERE endpoint_id = :e AND external_user_id = '29:home-1'"),
            {"e": teams["endpoint"]["id"]}).mappings().first()
        thread = c.execute(text(
            "SELECT conversation_ref, last_inbound_at FROM chat_threads WHERE endpoint_id = :e"),
            {"e": teams["endpoint"]["id"]}).mappings().first()
    assert link is not None and link["method"] == "aad_oid" and link["idp_subject"] == oid
    assert thread is not None and thread["conversation_ref"]["serviceUrl"] == server.base_url
    assert thread["last_inbound_at"] is not None
    assert len(runs_of(engine, pid)) == 1

    # Deliver status reply via fake Bot Framework server
    chat_reply.BUCKETS.reset()
    deliv = _deliver(engine, at=1)
    assert deliv["sent"] >= 1
    assert any("/v3/conversations/" in s["path"] for s in server.sent)

    # Loop prevention: bot echo & channelData.boss marker produce 0 runs
    echo_act = _activity(
        act_id="act_echo", text_val="Looking into it", service_url=server.base_url,
        tenant_id=teams["tenant_id"], app_id=teams["app_id"],
        from_id=f"28:{teams['app_id']}",
        boss_marker={"endpoint_id": teams["endpoint"]["id"], "run_id": "run_1", "hops": 1},
    )
    res_echo = event_intake.receive(engine, "teams", teams["token"], RawRequest(
        method="POST", headers={"authorization": f"Bearer {tok}", "content-type": "application/json"},
        body=json.dumps(echo_act).encode("utf-8"),
    ))
    _process(engine)
    with engine.begin() as c:
        ev_echo = c.execute(text("SELECT status FROM intake_events WHERE id = :i"),
                            {"i": res_echo["event_id"]}).scalar()
    assert ev_echo == "filtered"
    assert len(runs_of(engine, pid)) == 1


def test_teams_rate_buckets_and_403_message_writes_blocked(world):
    engine, pid, server, _priv = world
    teams = _make_teams(engine, pid, server)

    # Multi-window rate bucket: 7/1s allowed on one conversation, 8th waits
    chat_reply.BUCKETS.reset()
    base_t = 1000.0
    for idx in range(7):
        assert chat_reply.BUCKETS.wait_for_teams("ep:conv1", "teams_app:app1", now=base_t + idx * 0.01) == 0.0
    wait = chat_reply.BUCKETS.wait_for_teams("ep:conv1", "teams_app:app1", now=base_t + 0.1)
    assert wait > 0.0

    # Queue an outbox message and return 403 MessageWritesBlocked from fake Bot Framework server
    with engine.begin() as c:
        ct_id = f"cht_{uuid.uuid4().hex[:8]}"
        c.execute(text("""
            INSERT INTO chat_threads (id, endpoint_id, project_id, external_channel_id,
                   external_thread_id, conversation_ref)
            VALUES (:id, :ep, :p, '19:chan1@thread.tacv2', 'act_block', CAST(:ref AS JSONB))"""),
            {"id": ct_id, "ep": teams["endpoint"]["id"], "p": pid,
             "ref": json.dumps({"serviceUrl": server.base_url,
                                "conversation": {"id": "19:chan1@thread.tacv2"},
                                "tenantId": teams["tenant_id"]})})
        c.execute(text("""
            INSERT INTO chat_identity_links (id, endpoint_id, external_user_id, principal_id, verified_at)
            VALUES (:id, :ep, '29:blocked-user', 'ceo', now())"""),
            {"id": f"chl_{uuid.uuid4().hex[:8]}", "ep": teams["endpoint"]["id"]})
        notify.enqueue_chat(
            c, endpoint_id=teams["endpoint"]["id"], project_id=pid, kind="message",
            payload={"op": "dm", "channel": "19:chan1@thread.tacv2",
                     "user": "29:blocked-user", "text": "hello"},
            dedupe_key=f"blocked:{uuid.uuid4().hex}",
        )
        c.execute(text("UPDATE chat_outbox SET chat_thread_id = :ct WHERE project_id = :p"),
                  {"ct": ct_id, "p": pid})

    server.next_status = 403
    server.next_body = {"error": {"code": "MessageWritesBlocked", "message": "App blocked"}}
    chat_reply.BUCKETS.reset()
    res = _deliver(engine, at=1)
    assert res["dead"] == 1

    with engine.begin() as c:
        ob = c.execute(text("SELECT status, attempts, last_error FROM chat_outbox WHERE project_id = :p"),
                       {"p": pid}).mappings().first()
        th = c.execute(text("SELECT blocked_at, blocked_reason FROM chat_threads WHERE id = :id"),
                       {"id": ct_id}).mappings().first()
        lk = c.execute(text(
            "SELECT blocked_at, blocked_reason FROM chat_identity_links "
            "WHERE endpoint_id = :e AND external_user_id = '29:blocked-user'"),
            {"e": teams["endpoint"]["id"]}).mappings().first()
    assert ob["status"] == "dead" and ob["attempts"] == 1 and "MessageWritesBlocked" in ob["last_error"]
    assert th["blocked_at"] is not None and th["blocked_reason"] == "MessageWritesBlocked"
    assert lk["blocked_at"] is not None and lk["blocked_reason"] == "MessageWritesBlocked"

    # A subsequent outbox message to the already-blocked thread dies immediately without calling Teams.
    sent_before = len(server.sent)
    with engine.begin() as c:
        ob2_id = notify.enqueue_chat(
            c, endpoint_id=teams["endpoint"]["id"], project_id=pid, kind="message",
            payload={"op": "dm", "channel": "19:chan1@thread.tacv2",
                     "user": "29:blocked-user", "text": "should not send",
                     "teams_config": {"service_url": "https://evil.example"}},
            dedupe_key=f"blocked2:{uuid.uuid4().hex}",
        )
        c.execute(text("UPDATE chat_outbox SET chat_thread_id = :ct WHERE id = :id"),
                  {"ct": ct_id, "id": ob2_id})
    res2 = _deliver(engine, at=2)
    assert res2["dead"] == 1
    assert len(server.sent) == sent_before
