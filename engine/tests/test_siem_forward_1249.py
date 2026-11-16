"""Stream the security audit log to a customer SIEM (BOSSAPP-1249).

A saved http(s) destination gets one POST per new audit row. No destination
means no outbound call. A failed POST leaves the audit row in place and
records a retry, then a dead letter. Forwarding never rewrites the hash chain.
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import text

from boss_engine.identity import audit
from boss_engine.identity.audit import Outcome, SecurityEvent
from boss_engine.identity.declarations import Permit, access_of
from boss_engine.identity.siem_forward import (
    DestinationRefused,
    failures,
    forward_new,
    get_destination,
    save_destination,
)
from engine.tests.identity_s5_support import (  # noqa: F401, F811  (db, s5_engine: fixtures)
    NOW,
    ORG,
    community_install,
    db,
    fake_claim,
    s5_engine,
    settings,
)

MAX_TRIES_BEFORE_DEAD = 3


def ev(event_type="user.updated", **kwargs):
    kwargs.setdefault("actor_principal_id", "usr_alice")
    kwargs.setdefault("actor_kind", "user")
    return SecurityEvent(org_id=ORG, event_type=event_type, outcome=Outcome.SUCCESS, **kwargs)


def chain_rows(db):
    """The chain columns a rewrite would have to touch. Read with SQL, not the forwarder."""
    rows = db.execute(text(
        "SELECT id, seq, event_type, hash, prev_hash, hash_alg, details::text "
        "FROM identity_security_events WHERE org_id = :o ORDER BY seq"), {"o": ORG}).all()
    return [(row[0], row[1], row[2], bytes(row[3]), bytes(row[4]), row[5], row[6]) for row in rows]


def serve(status_holder):
    """A local HTTP server. ``status_holder['code']`` is what each POST answers with."""
    posts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's name
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length)
            posts.append({
                "path": self.path,
                "body": body,
                "content_type": self.headers.get("Content-Type"),
            })
            code = int(status_holder["code"])
            payload = b"" if code < 400 else b"siem refused"
            self.send_response(code)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/siem"
    return server, url, posts


def test_a_saved_destination_gets_one_post_per_new_audit_row(db):
    """Break: a saved URL is skipped, events are batched, or a sent row is posted again."""
    community_install(db)
    server, url, posts = serve({"code": 200})
    try:
        save_destination(ORG, url, db=db)
        first = audit.record(ev(), db=db)
        second = audit.record(ev("user.disabled", target_id="usr_bob"), db=db)
        forward_new(ORG, db=db)
        assert len(posts) == 2
        bodies = [json.loads(item["body"]) for item in posts]
        assert [item["content_type"] for item in posts] == ["application/json", "application/json"]
        assert [item["path"] for item in posts] == ["/siem", "/siem"]
        assert [(body["seq"], body["event_type"], body["id"]) for body in bodies] == [
            (first.seq, "user.updated", first.id),
            (second.seq, "user.disabled", second.id),
        ]
        stored = {row[0]: row[3].hex() for row in chain_rows(db)}
        assert bodies[0]["hash"] == stored[first.id]
        assert bodies[1]["hash"] == stored[second.id]
        assert bodies[0]["org_id"] == ORG
        forward_new(ORG, db=db)
        assert len(posts) == 2
    finally:
        server.shutdown()
        server.server_close()


def test_forwarding_does_not_rewrite_the_hash_chain(db):
    """Break: a successful POST updates hash, prev_hash, hash_alg, or details."""
    community_install(db)
    server, url, _posts = serve({"code": 200})
    try:
        save_destination(ORG, url, db=db)
        recorded = audit.record(ev(details={"reason": "left as written"}), db=db)
        before = chain_rows(db)
        forward_new(ORG, db=db)
        after = chain_rows(db)
        assert after == before
        assert audit.verify_chain(ORG, db=db).ok
        raw = db.execute(text(
            "SELECT hash FROM identity_security_events WHERE id = :id"),
            {"id": recorded.id}).scalar()
        assert bytes(raw) == recorded.hash
    finally:
        server.shutdown()
        server.server_close()


def test_no_destination_makes_no_outbound_call(db, monkeypatch):
    """Break: forward opens a socket when the org has no saved destination."""
    community_install(db)
    audit.record(ev(), db=db)
    opened = []
    real = socket.create_connection

    def trap(*args, **kwargs):
        opened.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", trap)
    result = forward_new(ORG, db=db)
    assert opened == []
    assert result["posted"] == 0
    assert get_destination(ORG, db=db) is None


def test_a_failed_post_keeps_the_audit_row_and_records_a_retry(db):
    """Break: a 500 drops the audit row, or the failure is not stored."""
    community_install(db)
    server, url, posts = serve({"code": 500})
    try:
        save_destination(ORG, url, db=db)
        recorded = audit.record(ev(), db=db)
        before = chain_rows(db)
        result = forward_new(ORG, db=db)
        assert len(posts) == 1
        assert result["posted"] == 0
        assert chain_rows(db) == before
        assert db.execute(text(
            "SELECT 1 FROM identity_security_events WHERE id = :id"),
            {"id": recorded.id}).first() is not None
        visible = failures(ORG, db=db)
        assert len(visible) == 1
        assert visible[0]["event_id"] == recorded.id
        assert visible[0]["status"] == "retry"
        assert visible[0]["attempts"] == 1
        assert "500" in visible[0]["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_three_failures_dead_letter_the_row_and_a_fourth_does_not_post(db):
    """Break: a permanent failure is retried forever, or the audit row is deleted."""
    community_install(db)
    server, url, posts = serve({"code": 500})
    try:
        save_destination(ORG, url, db=db)
        recorded = audit.record(ev(), db=db)
        before = chain_rows(db)
        for _ in range(MAX_TRIES_BEFORE_DEAD):
            forward_new(ORG, db=db)
        assert len(posts) == MAX_TRIES_BEFORE_DEAD
        visible = failures(ORG, db=db)
        assert len(visible) == 1
        assert visible[0]["status"] == "dead"
        assert visible[0]["attempts"] == MAX_TRIES_BEFORE_DEAD
        assert visible[0]["event_id"] == recorded.id
        forward_new(ORG, db=db)
        assert len(posts) == MAX_TRIES_BEFORE_DEAD
        assert chain_rows(db) == before
    finally:
        server.shutdown()
        server.server_close()


def test_a_retry_posts_again_until_the_siem_accepts_it(db):
    """Break: a retry is never sent again, or a later 200 leaves the failure visible."""
    community_install(db)
    status = {"code": 500}
    server, url, posts = serve(status)
    try:
        save_destination(ORG, url, db=db)
        recorded = audit.record(ev(), db=db)
        forward_new(ORG, db=db)
        assert failures(ORG, db=db)[0]["status"] == "retry"
        status["code"] = 200
        forward_new(ORG, db=db)
        assert len(posts) == 2
        assert json.loads(posts[1]["body"])["id"] == recorded.id
        assert failures(ORG, db=db) == []
        assert chain_rows(db)[0][3] == recorded.hash
    finally:
        server.shutdown()
        server.server_close()


def test_an_unreachable_destination_keeps_the_audit_row(db):
    """Break: a connection error raises and the caller's audit row is lost with it."""
    community_install(db)
    recorded = audit.record(ev(), db=db)
    before = chain_rows(db)
    save_destination(ORG, "http://127.0.0.1:1/siem", db=db)
    result = forward_new(ORG, db=db)
    assert result["posted"] == 0
    assert chain_rows(db) == before
    visible = failures(ORG, db=db)
    assert visible[0]["event_id"] == recorded.id
    assert visible[0]["status"] == "retry"
    assert visible[0]["error"]


def test_a_bad_url_is_refused_and_saves_nothing(db):
    """Break: a non-http URL is stored and later fetched."""
    community_install(db)
    for url in ("", "not a url", "ftp://logs.example/siem", "javascript:alert(1)", "http://"):
        with pytest.raises(DestinationRefused):
            save_destination(ORG, url, db=db)
    assert get_destination(ORG, db=db) is None


def test_a_new_url_replaces_the_old_one_and_only_the_new_one_is_called(db):
    """Break: events are posted to both URLs, or the replaced URL keeps receiving them."""
    community_install(db)
    first_server, first_url, first_posts = serve({"code": 200})
    second_server, second_url, second_posts = serve({"code": 200})
    try:
        save_destination(ORG, first_url, db=db)
        save_destination(ORG, second_url, db=db)
        assert get_destination(ORG, db=db)["url"] == second_url
        audit.record(ev(), db=db)
        forward_new(ORG, db=db)
        assert first_posts == []
        assert len(second_posts) == 1
    finally:
        first_server.shutdown()
        first_server.server_close()
        second_server.shutdown()
        second_server.server_close()


def test_one_org_does_not_receive_another_orgs_rows(db):
    """Break: forwarding one org posts the other org's audit rows."""
    community_install(db)
    db.execute(text(
        "INSERT INTO orgs (id, name) VALUES ('org_other', 'Other') ON CONFLICT DO NOTHING"))
    ours, our_url, our_posts = serve({"code": 200})
    theirs, their_url, their_posts = serve({"code": 200})
    try:
        save_destination(ORG, our_url, db=db)
        save_destination("org_other", their_url, db=db)
        audit.record(ev(), db=db)
        forward_new(ORG, db=db)
        forward_new("org_other", db=db)
        assert len(our_posts) == 1
        assert json.loads(our_posts[0]["body"])["org_id"] == ORG
        assert their_posts == []
    finally:
        ours.shutdown()
        ours.server_close()
        theirs.shutdown()
        theirs.server_close()


def _client(db, monkeypatch, ctx):
    from apps.api.app.routers import gap_1249
    from contextlib import contextmanager

    @contextmanager
    def same_transaction():
        with db.begin_nested():
            yield db

    monkeypatch.setattr(gap_1249, "begin", same_transaction)
    monkeypatch.setattr(gap_1249, "_context", lambda request: ctx)
    app = FastAPI()
    app.include_router(gap_1249.router)
    return TestClient(app), gap_1249


def test_the_route_saves_a_destination_and_forwards_one_new_row(db, monkeypatch):
    """Break: the HTTP surface saves a URL the forwarder does not use."""
    community_install(db)
    claim = fake_claim(db, name="Param", email="param@example.com", via="setup_token", now=NOW,
                       cfg=settings())
    client, _gap = _client(db, monkeypatch, claim.ctx)
    server, url, posts = serve({"code": 200})
    try:
        saved = client.put("/v1/security/siem", json={"url": url})
        assert saved.status_code == 200
        assert saved.json()["url"] == url
        client.post("/v1/security/siem/forward")
        already = len(posts)
        recorded = audit.record(ev(), db=db)
        again = client.post("/v1/security/siem/forward")
        assert again.status_code == 200
        assert again.json()["posted"] == 1
        new_posts = posts[already:]
        assert len(new_posts) == 1
        body = json.loads(new_posts[0]["body"])
        assert body["id"] == recorded.id
        assert body["event_type"] == "user.updated"
    finally:
        server.shutdown()
        server.server_close()


def test_the_route_shows_a_failure_and_refuses_a_bad_or_anonymous_save(db, monkeypatch):
    """Break: a 500 is hidden, a bad URL is stored, or an anonymous caller sets the destination."""
    community_install(db)
    claim = fake_claim(db, name="Param", email="param@example.com", via="setup_token", now=NOW,
                       cfg=settings())
    client, gap = _client(db, monkeypatch, claim.ctx)
    server, url, _posts = serve({"code": 500})
    try:
        assert client.put("/v1/security/siem", json={"url": "ftp://logs.example/x"}).status_code == 422
        assert get_destination(ORG, db=db) is None
        assert client.put("/v1/security/siem", json={"url": url}).status_code == 200
        audit.record(ev(), db=db)
        assert client.post("/v1/security/siem/forward").json()["posted"] == 0
        shown = client.get("/v1/security/siem")
        assert shown.status_code == 200
        body = shown.json()
        assert body["destination"]["url"] == url
        assert body["failures"][0]["status"] == "retry"
        assert "500" in body["failures"][0]["error"]
        cleared = client.delete("/v1/security/siem")
        assert cleared.status_code == 200
        assert get_destination(ORG, db=db) is None
        monkeypatch.setattr(gap, "_context", lambda request: None)
        assert client.put("/v1/security/siem", json={"url": url}).status_code == 401
        assert get_destination(ORG, db=db) is None
    finally:
        server.shutdown()
        server.server_close()


def test_siem_routes_declare_an_audit_permission():
    """Break: a mounted route has no declaration, so enforce mode refuses to boot."""
    from apps.api.app.routers.gap_1249 import router

    routes = [route for route in router.routes if isinstance(route, APIRoute)]
    # HEAD is added by the framework on GET; it is not a second operation.
    declared = {
        (route.path, tuple(sorted(method for method in route.methods if method != "HEAD")))
        for route in routes
    }
    assert declared == {
        ("/v1/security/siem", ("GET",)),
        ("/v1/security/siem", ("PUT",)),
        ("/v1/security/siem", ("DELETE",)),
        ("/v1/security/siem/forward", ("POST",)),
    }
    for route in routes:
        access = access_of(route.endpoint)
        assert isinstance(access, Permit)
        if route.methods <= {"GET", "HEAD"}:
            assert access.permission == "audit:read"
        else:
            assert access.permission == "audit:export"
