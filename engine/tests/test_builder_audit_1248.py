"""BOSSAPP-1248: one audit row per builder prompt, and one for the commit a publish put
on the live URL. A publish with no commit says so and does not invent a SHA. Rows go
through the existing identity audit writer, so the hash chain still links."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from boss_engine.identity import audit
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, \
    PrincipalStatus
from boss_engine.services.builder_audit import (NO_COMMIT, BuilderAuditError, record_builder_prompt,
                                                record_publish, rows_for)
from engine.tests.identity_s5_support import ORG, base_world, db, events, s5_engine  # noqa: F401

SHA = "a" * 40
URL = "https://live.example/shop"
PROMPT = "Ship a refund button on the order page."


def _details(row: dict) -> dict:
    raw = row["details"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def _digest(url: str, kind: str) -> str:
    algo = hashlib.sha1 if kind == "sha1" else hashlib.sha256
    return algo(url.encode()).hexdigest()


def _ctx(org: str = ORG, principal_id: str = "usr_builder") -> AuthContext:
    principal = Principal(principal_id, PrincipalKind.USER, org, PrincipalStatus.ACTIVE, "Builder")
    return AuthContext(principal, AuthMethod.SESSION, request_id="req_1248")


def _banned(url: str) -> set[str]:
    return {
        _digest(url, "sha1"), _digest(url, "sha256"),
        hashlib.sha1(b"").hexdigest(), hashlib.sha256(b"").hexdigest(),
        "0" * 40, "0" * 64, "f" * 40, "f" * 64,
    }


def test_each_builder_prompt_is_one_audit_row(db):
    base_world(db)
    ctx = _ctx()
    first = record_builder_prompt(db, org_id=ORG, prompt=PROMPT, actor=ctx, project_id="proj_shop")
    second = record_builder_prompt(db, org_id=ORG, prompt="Then show the receipt.", actor=ctx,
                                   project_id="proj_shop")
    rows = events(db, "builder.prompt")
    assert [first.seq, second.seq] == [rows[0]["seq"], rows[1]["seq"]]
    assert [_details(row)["prompt"] for row in rows] == [PROMPT, "Then show the receipt."]
    assert all(row["actor_principal_id"] == "usr_builder" for row in rows)
    assert _details(rows[0])["project_id"] == "proj_shop"
    assert audit.verify_chain(ORG, db=db).ok


def test_a_blank_prompt_writes_nothing(db):
    base_world(db)
    with pytest.raises(BuilderAuditError):
        record_builder_prompt(db, org_id=ORG, prompt="   ")
    assert events(db, "builder.prompt") == []


def test_a_publish_records_the_commit_it_put_on_the_live_url(db):
    base_world(db)
    recorded = record_publish(db, org_id=ORG, live_url=URL, commit=SHA.upper(),
                              actor=_ctx(), project_id="proj_shop")
    rows = events(db, "publish.live")
    assert len(rows) == 1 and rows[0]["seq"] == recorded.seq
    details = _details(rows[0])
    assert details["live_url"] == URL
    assert details["commit"] == SHA
    assert details["commit_stated"] == SHA
    assert details["commit"] != _digest(URL, "sha1")
    assert bytes(rows[0]["prev_hash"]) == audit.GENESIS_HASH
    assert audit.verify_chain(ORG, db=db).ok


@pytest.mark.parametrize("commit", [None, "", "   ", "HEAD", "abc1234", "g" * 40, "a" * 39,
                                     "a" * 41, "a" * 63])
def test_a_publish_with_no_commit_says_so_and_does_not_invent_a_sha(db, commit):
    base_world(db)
    record_publish(db, org_id=ORG, live_url=URL, commit=commit)
    details = _details(events(db, "publish.live")[-1])
    blob = json.dumps(details)
    assert details["commit"] is None
    assert details["commit_stated"] == NO_COMMIT
    assert NO_COMMIT in blob
    for banned in _banned(URL):
        assert banned not in blob
    if isinstance(commit, str) and commit.strip():
        assert commit.strip().lower() not in blob


def test_an_actor_cannot_write_another_orgs_chain(db):
    base_world(db)
    with pytest.raises(BuilderAuditError):
        record_builder_prompt(db, org_id="org_other", prompt=PROMPT, actor=_ctx())
    assert events(db, "builder.prompt") == []


def test_prompt_then_publish_extends_one_chain(db):
    base_world(db)
    record_builder_prompt(db, org_id=ORG, prompt=PROMPT)
    record_publish(db, org_id=ORG, live_url=URL, commit=SHA)
    rows = [row for row in events(db) if row["event_type"] in ("builder.prompt", "publish.live")]
    assert [row["event_type"] for row in rows] == ["builder.prompt", "publish.live"]
    assert bytes(rows[1]["prev_hash"]) == bytes(rows[0]["hash"])
    listed = rows_for(db, ORG)
    assert [item["event_type"] for item in listed] == ["builder.prompt", "publish.live"]
    assert listed[1]["commit"] == SHA
    assert audit.verify_chain(ORG, db=db).ok


def _client(db, ctx: AuthContext | None):
    from apps.api.app.routers import gap_1248

    @contextmanager
    def _same(_engine=None):
        yield db

    gap_1248.begin = _same
    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        if ctx is not None:
            request.state.identity = ctx
            request.state.identity_checked = True
        return await call_next(request)

    app.include_router(gap_1248.router)
    return TestClient(app)


def test_the_prompt_route_writes_one_row_for_the_signed_in_org(db, monkeypatch):
    base_world(db)
    client = _client(db, _ctx())
    forged = client.post("/v1/gap/1248/builder-prompts", json={
        "prompt": PROMPT, "project_id": "proj_shop", "actor_principal_id": "usr_mallory",
    })
    assert forged.status_code == 200
    body = forged.json()
    assert body["event_type"] == "builder.prompt"
    assert body["prompt"] == PROMPT
    rows = events(db, "builder.prompt")
    assert len(rows) == 1
    assert rows[0]["actor_principal_id"] == "usr_builder"
    assert "usr_mallory" not in json.dumps(_details(rows[0]))


def test_the_prompt_route_refuses_a_stranger_and_a_foreign_org(db):
    base_world(db)
    stranger = _client(db, None)
    refused = stranger.post("/v1/gap/1248/builder-prompts", json={"prompt": PROMPT})
    assert refused.status_code == 401
    foreign = _client(db, _ctx()).post("/v1/gap/1248/builder-prompts", json={
        "prompt": PROMPT, "org_id": "org_other",
    })
    assert foreign.status_code == 403
    assert events(db, "builder.prompt") == []


def test_the_publish_route_says_when_there_is_no_commit(db):
    base_world(db)
    client = _client(db, _ctx())
    empty = client.post("/v1/gap/1248/publishes", json={"live_url": URL})
    assert empty.status_code == 200
    body = empty.json()
    assert body["commit"] is None
    assert body["commit_stated"] == NO_COMMIT
    assert body["live_url"] == URL
    landed = client.post("/v1/gap/1248/publishes", json={"live_url": URL, "commit": SHA})
    assert landed.status_code == 200
    assert landed.json()["commit"] == SHA
    listed = client.get("/v1/gap/1248/audit")
    assert listed.status_code == 200
    assert [item["commit_stated"] for item in listed.json()["rows"]] == [NO_COMMIT, SHA]


def test_a_failed_audit_write_is_not_swallowed(db, monkeypatch):
    base_world(db)
    from apps.api.app.routers import gap_1248

    def _boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(gap_1248.builder_audit, "record_builder_prompt", _boom)
    client = _client(db, _ctx())
    failed = client.post("/v1/gap/1248/builder-prompts", json={"prompt": PROMPT})
    assert failed.status_code == 500
    assert events(db, "builder.prompt") == []
