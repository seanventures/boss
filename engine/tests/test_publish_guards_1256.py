"""An admin can tighten publish guards (BOSSAPP-1256). No row keeps today's allow."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

URL = os.environ.get("DATABASE_URL") or os.environ.get("BOSS_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not URL.startswith("postgres") or "test" not in URL.lower(),
    reason="needs DATABASE_URL naming a disposable test database",
)


@pytest.fixture(scope="module")
def eng():
    from boss_engine.db import ensure_schema

    engine = create_engine(URL, future=True)
    ensure_schema(engine)
    yield engine
    engine.dispose()


def _bind(c, *, org: str, principal: str, role: str, scope_type: str, scope_id: str,
          expires: str | None = None) -> None:
    c.execute(text(
        "INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
        "scope_type, scope_id, granted_by, expires_at) VALUES (:id, :o, 'principal', :s, :r, "
        ":st, :sid, 'test', CAST(:exp AS timestamptz))"),
        {"id": "bnd_" + uuid4().hex[:16], "o": org, "s": principal, "r": role,
         "st": scope_type, "sid": scope_id, "exp": expires})


@pytest.fixture()
def world(eng):
    sfx = uuid4().hex[:8]
    org = f"org_1256_{sfx}"
    people = {
        "admin": ("active", "arl_admin", "org", org, None),
        "operator": ("active", "arl_operator", "org", org, None),
        "expired": ("active", "arl_admin", "org", org, "2000-01-01"),
        "project_admin": ("active", "arl_admin", "project", f"prj_{sfx}", None),
        "disabled": ("disabled", "arl_admin", "org", org, None),
    }
    ids = {"org": org}
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'publish guards')"), {"o": org})
        for key, (status, role, scope_type, scope_id, expires) in people.items():
            pid = f"usr_{key[:3]}_{sfx}"
            ids[key] = pid
            c.execute(text(
                "INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                "VALUES (:i, :o, 'user', :s, :i)"),
                {"i": pid, "o": org, "s": status})
            _bind(c, org=org, principal=pid, role=role, scope_type=scope_type,
                  scope_id=scope_id, expires=expires)
    yield ids
    with eng.begin() as c:
        c.execute(text("DELETE FROM identity_role_bindings WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM publish_guards WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM identity_principals WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM orgs WHERE id = :o"), {"o": org})


def test_defaults_allow_public_publish_findings_export_and_chat(eng, world):
    """A missing policy is today's behavior: nothing is refused."""
    from boss_engine.services import publish_guards as guards

    with eng.connect() as c:
        policy = guards.policy(c, world["org"])
        guards.check_publish(c, world["org"], "public")
        guards.check_publish(c, world["org"], "private")
        guards.check_finding(c, world["org"], "critical")
        guards.check_finding(c, world["org"], "not-a-severity")
        guards.check_source_export(c, world["org"])
        guards.check_chat_upload(c, world["org"])
    assert policy["require_private"] is False
    assert policy["blocked_severity"] is None
    assert policy["ban_source_export"] is False
    assert policy["ban_chat_upload"] is False


def test_only_an_active_org_admin_can_require_private_publishes(eng, world):
    """Public is refused only after an org admin requires private. Others cannot set it."""
    from boss_engine.services import publish_guards as guards

    org = world["org"]
    for actor in (world["operator"], world["expired"], world["project_admin"], world["disabled"]):
        with pytest.raises(guards.PublishGuardError) as caught:
            with eng.begin() as c:
                guards.set_policy(c, org, actor=actor, require_private=True)
        assert caught.value.code == "not_admin"
    with eng.connect() as c:
        assert guards.policy(c, org)["require_private"] is False
        guards.check_publish(c, org, "public")

    with eng.begin() as c:
        saved = guards.set_policy(c, org, actor=world["admin"], require_private=True)
    assert saved["require_private"] is True
    with eng.connect() as c:
        assert guards.policy(c, org)["require_private"] is True
        guards.check_publish(c, org, "private")
        guards.check_publish(c, org, "PRIVATE")
        with pytest.raises(guards.PublishGuardRefused) as refused:
            guards.check_publish(c, org, "public")
        assert refused.value.code == "public_publish_refused"
        with pytest.raises(guards.PublishGuardRefused) as shouted:
            guards.check_publish(c, org, "PUBLIC")
        assert shouted.value.code == "public_publish_refused"
        with pytest.raises(guards.PublishGuardRefused):
            guards.check_publish(c, org, "")


def test_a_finding_at_or_above_the_blocked_severity_is_refused(eng, world):
    """high blocks critical and high, not medium. critical blocks only critical. info blocks all."""
    from boss_engine.services import publish_guards as guards

    org = world["org"]
    with eng.begin() as c:
        guards.set_policy(c, org, actor=world["admin"], blocked_severity="high")
    with eng.connect() as c:
        for severity in ("critical", "high", "HIGH"):
            with pytest.raises(guards.PublishGuardRefused) as refused:
                guards.check_finding(c, org, severity)
            assert refused.value.code == "severity_blocked"
        guards.check_finding(c, org, "medium")
        guards.check_finding(c, org, "low")
        guards.check_finding(c, org, "info")
        with pytest.raises(guards.PublishGuardRefused) as unknown:
            guards.check_finding(c, org, "severe")
        assert unknown.value.code == "severity_blocked"

    with eng.begin() as c:
        guards.set_policy(c, org, actor=world["admin"], blocked_severity="critical")
    with eng.connect() as c:
        with pytest.raises(guards.PublishGuardRefused):
            guards.check_finding(c, org, "critical")
        guards.check_finding(c, org, "high")

    with pytest.raises(guards.PublishGuardError) as bad:
        with eng.begin() as c:
            guards.set_policy(c, org, actor=world["admin"], blocked_severity="severe")
    assert bad.value.code == "bad_severity"
    with eng.connect() as c:
        assert guards.policy(c, org)["blocked_severity"] == "critical"

    with eng.begin() as c:
        guards.set_policy(c, org, actor=world["admin"], blocked_severity="info")
    with eng.connect() as c:
        with pytest.raises(guards.PublishGuardRefused):
            guards.check_finding(c, org, "info")
        with pytest.raises(guards.PublishGuardRefused):
            guards.check_finding(c, org, "medium")

    with eng.begin() as c:
        cleared = guards.set_policy(c, org, actor=world["admin"], blocked_severity=None)
    assert cleared["blocked_severity"] is None
    with eng.connect() as c:
        guards.check_finding(c, org, "critical")
        guards.check_finding(c, org, "severe")


def test_export_and_chat_bans_are_independent_and_a_partial_save_keeps_the_rest(eng, world):
    from boss_engine.services import publish_guards as guards

    org = world["org"]
    with eng.begin() as c:
        guards.set_policy(c, org, actor=world["admin"], require_private=True,
                          blocked_severity="high")
        guards.set_policy(c, org, actor=world["admin"], ban_source_export=True)
    with eng.connect() as c:
        policy = guards.policy(c, org)
        with pytest.raises(guards.PublishGuardRefused) as export:
            guards.check_source_export(c, org)
        assert export.value.code == "source_export_banned"
        guards.check_chat_upload(c, org)
        with pytest.raises(guards.PublishGuardRefused):
            guards.check_publish(c, org, "public")
        with pytest.raises(guards.PublishGuardRefused):
            guards.check_finding(c, org, "critical")
    assert policy["require_private"] is True
    assert policy["blocked_severity"] == "high"
    assert policy["ban_source_export"] is True
    assert policy["ban_chat_upload"] is False

    with eng.begin() as c:
        guards.set_policy(c, org, actor=world["admin"], ban_chat_upload=True,
                          ban_source_export=False)
    with eng.connect() as c:
        guards.check_source_export(c, org)
        with pytest.raises(guards.PublishGuardRefused) as upload:
            guards.check_chat_upload(c, org)
        assert upload.value.code == "chat_upload_banned"
        assert guards.policy(c, org)["require_private"] is True
        assert guards.policy(c, org)["blocked_severity"] == "high"


def test_http_refuses_the_same_actions_and_keeps_omitted_knobs(eng, world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routers.gap_1256 import router

    org = world["org"]
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    untouched = client.get("/v1/publish-guards", params={"org_id": org})
    assert untouched.status_code == 200
    body = untouched.json()
    assert body["require_private"] is False
    assert body["blocked_severity"] is None
    assert body["ban_source_export"] is False
    assert body["ban_chat_upload"] is False

    denied = client.put("/v1/publish-guards", json={
        "org_id": org, "actor": world["operator"], "require_private": True})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "not_admin"

    saved = client.put("/v1/publish-guards", json={
        "org_id": org, "actor": world["admin"], "require_private": True,
        "blocked_severity": "high"})
    assert saved.status_code == 200
    assert saved.json()["require_private"] is True
    assert saved.json()["blocked_severity"] == "high"

    banned = client.put("/v1/publish-guards", json={
        "org_id": org, "actor": world["admin"], "ban_source_export": True,
        "ban_chat_upload": True})
    assert banned.status_code == 200
    assert banned.json()["require_private"] is True
    assert banned.json()["blocked_severity"] == "high"
    assert banned.json()["ban_source_export"] is True
    assert banned.json()["ban_chat_upload"] is True

    public = client.post("/v1/publish-guards/publish",
                         json={"org_id": org, "visibility": "public"})
    assert public.status_code == 409
    assert public.json()["detail"]["code"] == "public_publish_refused"
    private = client.post("/v1/publish-guards/publish",
                          json={"org_id": org, "visibility": "private"})
    assert private.status_code == 200
    assert private.json()["allowed"] is True

    finding = client.post("/v1/publish-guards/finding",
                          json={"org_id": org, "severity": "critical"})
    assert finding.status_code == 409
    assert finding.json()["detail"]["code"] == "severity_blocked"
    medium = client.post("/v1/publish-guards/finding",
                         json={"org_id": org, "severity": "medium"})
    assert medium.status_code == 200
    assert medium.json()["allowed"] is True

    export = client.post("/v1/publish-guards/source-export", json={"org_id": org})
    assert export.status_code == 409
    assert export.json()["detail"]["code"] == "source_export_banned"
    upload = client.post("/v1/publish-guards/chat-upload", json={"org_id": org})
    assert upload.status_code == 409
    assert upload.json()["detail"]["code"] == "chat_upload_banned"

    bad = client.put("/v1/publish-guards", json={
        "org_id": org, "actor": world["admin"], "blocked_severity": "severe"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "bad_severity"
    still = client.get("/v1/publish-guards", params={"org_id": org})
    assert still.json()["blocked_severity"] == "high"
