"""A named link shares an in-progress app (BOSSAPP-1251).

The link has a password and an expiry. An admin can stop one link, and can
stop new links. An expired link and a stopped link are refused. The password
is never logged, stored in clear, or returned. This is not a visitor publish
snapshot.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

PG_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://boss:boss@127.0.0.1:55768/boss_test_1251",
)
PASSWORD = "cedar-harbor-91"


pytestmark = pytest.mark.skipif(
    not PG_URL.startswith("postgres"), reason="needs postgres")


@pytest.fixture(scope="module")
def engine():
    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def project(engine):
    pid = f"p1251_{uuid.uuid4().hex[:10]}"
    org = f"org_{pid}"
    ws = f"ws_{pid}"
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'Harbor')"), {"i": org})
        c.execute(text(
            "INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'Yard')"),
            {"i": ws, "o": org})
        c.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) "
            "VALUES (:i, :w, 'Harbor notes', :s)"),
            {"i": pid, "w": ws, "s": pid})
        for member, roles in (("ada", ["admin"]), ("bea", ["operator"])):
            c.execute(text(
                "INSERT INTO project_members (id, project_id, member_id, display_name, "
                "member_type, role_keys) VALUES (:i, :p, :m, :m, 'human', CAST(:r AS JSONB))"
            ), {"i": f"pm_{pid}_{member}", "p": pid, "m": member, "r": json.dumps(roles)})
    yield {"id": pid, "admin": "ada", "builder": "bea"}
    with engine.begin() as c:
        c.execute(text("DELETE FROM preview_shares WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM preview_share_policy WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM project_members WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
        c.execute(text("DELETE FROM workspaces WHERE id = :i"), {"i": ws})
        c.execute(text("DELETE FROM orgs WHERE id = :i"), {"i": org})


def _soon():
    return datetime.now(timezone.utc) + timedelta(hours=4)


def _issue(engine, project, *, name="Friday review", actor=None, password=PASSWORD):
    from boss_engine.services import preview_share

    return preview_share.create_share(
        engine,
        project_id=project["id"],
        app_key="harbor-notes",
        name=name,
        password=password,
        expires_at=_soon(),
        actor=actor or project["builder"],
    )


def test_a_named_link_opens_with_its_password(engine, project):
    from boss_engine.services import preview_share

    share = _issue(engine, project)
    assert share["name"] == "Friday review"
    assert share["slug"] == "friday-review"
    assert share["href"] == "/preview-share/friday-review"
    assert share["app_key"] == "harbor-notes"
    assert "password" not in share
    assert PASSWORD not in json.dumps(share)
    opened = preview_share.open_share(engine, slug=share["slug"], password=PASSWORD,
                                       project_id=project["id"])
    assert opened["app_key"] == "harbor-notes"
    assert opened["name"] == "Friday review"
    assert PASSWORD not in json.dumps(opened)


def test_a_repeated_name_gets_its_own_link(engine, project):
    first = _issue(engine, project)
    second = _issue(engine, project)
    assert first["slug"] == "friday-review"
    assert second["slug"] == "friday-review-2"
    assert second["href"] == "/preview-share/friday-review-2"


def test_a_wrong_password_is_refused(engine, project):
    from boss_engine.services import preview_share

    share = _issue(engine, project, name="Wrong gate")
    with pytest.raises(preview_share.ShareRefused) as caught:
        preview_share.open_share(engine, slug=share["slug"], password="not-the-pass",
                                  project_id=project["id"])
    assert caught.value.code == "password"
    assert PASSWORD not in str(caught.value)
    assert "not-the-pass" not in str(caught.value)


def test_an_expired_link_is_refused(engine, project):
    from boss_engine.services import preview_share

    share = _issue(engine, project, name="Old tide")
    with engine.begin() as c:
        c.execute(text(
            "UPDATE preview_shares SET expires_at = now() - interval '1 minute' WHERE id = :id"
        ), {"id": share["id"]})
    with pytest.raises(preview_share.ShareRefused) as caught:
        preview_share.open_share(engine, slug=share["slug"], password=PASSWORD,
                                  project_id=project["id"])
    assert caught.value.code == "expired"


def test_a_stopped_link_is_refused(engine, project):
    from boss_engine.services import preview_share

    share = _issue(engine, project, name="Hold the gate")
    stopped = preview_share.stop_share(
        engine, project_id=project["id"], share_id=share["id"], actor=project["admin"])
    assert stopped["stopped"] is True
    with pytest.raises(preview_share.ShareRefused) as caught:
        preview_share.open_share(engine, slug=share["slug"], password=PASSWORD,
                                  project_id=project["id"])
    assert caught.value.code == "stopped"


def test_an_admin_can_stop_new_links_and_an_old_one_still_opens(engine, project):
    from boss_engine.services import preview_share

    share = _issue(engine, project, name="Still open")
    policy = preview_share.stop_new_links(
        engine, project_id=project["id"], actor=project["admin"])
    assert policy["new_links_stopped"] is True
    with pytest.raises(preview_share.ShareRefused) as caught:
        _issue(engine, project, name="Too late")
    assert caught.value.code == "new_links_stopped"
    opened = preview_share.open_share(engine, slug=share["slug"], password=PASSWORD,
                                       project_id=project["id"])
    assert opened["slug"] == share["slug"]


def test_a_builder_cannot_stop_links(engine, project):
    from boss_engine.services import preview_share

    share = _issue(engine, project, name="Builder gate")
    with pytest.raises(preview_share.ShareRefused) as stopped_new:
        preview_share.stop_new_links(
            engine, project_id=project["id"], actor=project["builder"])
    assert stopped_new.value.code == "not_admin"
    with pytest.raises(preview_share.ShareRefused) as stopped_one:
        preview_share.stop_share(
            engine, project_id=project["id"], share_id=share["id"], actor=project["builder"])
    assert stopped_one.value.code == "not_admin"
    opened = preview_share.open_share(engine, slug=share["slug"], password=PASSWORD,
                                       project_id=project["id"])
    assert opened["slug"] == share["slug"]


def test_a_stranger_cannot_issue_a_link(engine, project):
    from boss_engine.services import preview_share

    with pytest.raises(preview_share.ShareRefused) as caught:
        _issue(engine, project, actor="stranger", name="No membership")
    assert caught.value.code == "not_member"


def test_the_password_is_not_logged_or_stored(engine, project, caplog):
    from boss_engine.services import preview_share

    caplog.set_level(logging.DEBUG)
    share = _issue(engine, project, name="Quiet gate")
    preview_share.open_share(engine, slug=share["slug"], password=PASSWORD,
                              project_id=project["id"])
    try:
        preview_share.open_share(engine, slug=share["slug"], password="wrong-guess-xx",
                                  project_id=project["id"])
    except preview_share.ShareRefused:
        pass
    blob = caplog.text
    assert PASSWORD not in blob
    assert "wrong-guess-xx" not in blob
    with engine.begin() as c:
        row = c.execute(text(
            "SELECT password_hash FROM preview_shares WHERE id = :id"),
            {"id": share["id"]}).one()
        columns = c.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'preview_shares'")).scalars().all()
    assert row[0] != PASSWORD
    assert PASSWORD not in row[0]
    assert "password" not in columns
    assert "password_hash" in columns
    listed = preview_share.list_shares(engine, project_id=project["id"])
    assert PASSWORD not in json.dumps(listed)
    assert all("password_hash" not in item for item in listed)


def test_http_share_omits_the_password(engine, project):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routers import gap_1251

    gap_1251.bind_engine(engine)
    try:
        app = FastAPI()
        app.include_router(gap_1251.router)
        client = TestClient(app)
        body = {
            "project_id": project["id"],
            "app_key": "harbor-notes",
            "name": "Wire pass",
            "password": PASSWORD,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "actor": project["builder"],
        }
        created = client.post("/v1/preview-shares", json=body)
        assert created.status_code == 201, created.text
        assert PASSWORD not in created.text
        assert "password_hash" not in created.text
        payload = created.json()
        assert payload["href"].startswith("/preview-share/")
        opened = client.post(
            f"/v1/preview-shares/{payload['slug']}/open",
            json={"project_id": project["id"], "password": "wrong-guess-xx"})
        assert opened.status_code == 403
        assert opened.json()["detail"] == "password"
        assert PASSWORD not in opened.text
        assert "wrong-guess-xx" not in opened.text
        stopped = client.post(
            "/v1/preview-shares/stop-new",
            json={"project_id": project["id"], "actor": project["admin"]})
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["new_links_stopped"] is True
    finally:
        gap_1251.bind_engine(None)
