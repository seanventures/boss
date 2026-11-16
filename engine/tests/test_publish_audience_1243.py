"""Visitor snapshots are published copies, not the in-progress preview (BOSSAPP-1243).

Seam: ``publish_visitor_snapshot``, ``approve_visitor_snapshot``, ``current_visitor``,
and the gap router that calls them. A snapshot stores a frozen page for one audience.
Publishing without approval stays pending and has no visitor URL. An approved URL is
never the live preview URL, and publishing does not rewrite that preview.
"""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text

from apps.api.app.routers import gap_1243
from boss_engine.db import ensure_schema
from boss_engine.services.publish_audience import (
    PublishAudienceError,
    approve_visitor_snapshot,
    current_visitor,
    publish_visitor_snapshot,
)

PREVIEW_URL = "http://127.0.0.1:4311/web/"
PAGE = {"title": "Harbor menu", "revision": 3}


@pytest.fixture(scope="module")
def pg():
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("needs DATABASE_URL")
    engine = create_engine(url, future=True)
    ensure_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def project(pg):
    pid = "p_1243_" + uuid.uuid4().hex[:8]
    org = "org_" + pid
    ws = "ws_" + pid
    with pg.begin() as connection:
        connection.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, 'Audience')"), {"id": org})
        connection.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :org, 'Audience')"),
            {"id": ws, "org": org})
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :ws, 'Harbor menu', :slug)"),
            {"id": pid, "ws": ws, "slug": pid})
    yield pid
    with pg.begin() as connection:
        if connection.execute(text("SELECT to_regclass('public.visitor_snapshots')")).scalar():
            connection.execute(
                text("DELETE FROM visitor_snapshots WHERE project_id = :id"), {"id": pid})
        connection.execute(text("DELETE FROM app_previews WHERE project_id = :id"), {"id": pid})
        connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": pid})
        connection.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": ws})
        connection.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org})


def _publish(connection, project, *, approved=False, audience="customers", page=None,
             dedupe_key=None, preview_id="prev_bench", preview_url=PREVIEW_URL):
    return publish_visitor_snapshot(
        connection,
        project_id=project,
        audience=audience,
        actor="ada",
        snapshot=PAGE if page is None else page,
        approved=approved,
        source_preview_id=preview_id,
        preview_url=preview_url,
        dedupe_key=dedupe_key,
    )


def test_publishing_without_approval_stays_pending_and_is_not_the_visitor_url(pg, project):
    with pg.begin() as connection:
        plate = _publish(connection, project, approved=False)
        door = current_visitor(connection, project_id=project, audience="customers")
    assert plate["approval_state"] == "pending"
    assert plate["visitor_url"] is None
    assert plate["is_live_preview"] is False
    assert plate["kind"] == "visitor_snapshot"
    assert plate["audience"] == "customers"
    assert plate["snapshot"] == PAGE
    assert plate["preview_url"] == PREVIEW_URL
    assert plate["id"] != "prev_bench"
    assert door is None


def test_an_approved_snapshot_is_the_visitor_url_and_not_the_live_preview(pg, project):
    with pg.begin() as connection:
        plate = _publish(connection, project, approved=True)
        door = current_visitor(connection, project_id=project, audience="customers")
    assert plate["approval_state"] == "approved"
    assert plate["visitor_url"] == f"/v1/visitors/{plate['id']}"
    assert plate["visitor_url"] != PREVIEW_URL
    assert plate["is_live_preview"] is False
    assert door["id"] == plate["id"]
    assert door["visitor_url"] == plate["visitor_url"]


def test_a_later_pending_publish_does_not_take_the_visitor_url(pg, project):
    with pg.begin() as connection:
        approved = _publish(
            connection, project, approved=True, page={"title": "Harbor menu", "revision": 3})
        pending = _publish(
            connection, project, approved=False, page={"title": "Harbor menu", "revision": 4})
        door = current_visitor(connection, project_id=project, audience="customers")
        still = connection.execute(
            text("SELECT approval_state, visitor_url FROM visitor_snapshots WHERE id = :id"),
            {"id": pending["id"]}).mappings().one()
    assert pending["approval_state"] == "pending"
    assert pending["visitor_url"] is None
    assert still["visitor_url"] is None
    assert door["id"] == approved["id"]
    assert door["visitor_url"] != PREVIEW_URL


def test_publishing_does_not_rewrite_the_in_progress_preview(pg, project):
    preview_id = "prev_" + uuid.uuid4().hex[:8]
    with pg.begin() as connection:
        connection.execute(
            text("INSERT INTO app_previews (id, project_id, url, status) "
                 "VALUES (:id, :project, :url, 'running')"),
            {"id": preview_id, "project": project, "url": PREVIEW_URL})
        plate = _publish(connection, project, approved=True, preview_id=preview_id)
        preview = connection.execute(
            text("SELECT url, status FROM app_previews WHERE id = :id"),
            {"id": preview_id}).mappings().one()
    assert preview["url"] == PREVIEW_URL
    assert preview["status"] == "running"
    assert plate["source_preview_id"] == preview_id
    assert plate["id"] != preview_id
    assert plate["visitor_url"] != preview["url"]


def test_the_snapshot_stays_frozen_when_the_caller_mutates_the_page(pg, project):
    page = {"title": "Harbor menu", "revision": 3}
    with pg.begin() as connection:
        plate = _publish(connection, project, page=page)
        page["revision"] = 9
        stored = connection.execute(
            text("SELECT snapshot FROM visitor_snapshots WHERE id = :id"),
            {"id": plate["id"]}).scalar()
    assert stored["revision"] == 3
    assert plate["snapshot"]["revision"] == 3


def test_repeating_the_same_publish_does_not_create_a_second_snapshot(pg, project):
    with pg.begin() as connection:
        first = _publish(connection, project, dedupe_key="menu-r3")
        second = _publish(connection, project, approved=True, dedupe_key="menu-r3")
        count = connection.execute(
            text("SELECT count(*) FROM visitor_snapshots WHERE project_id = :id"),
            {"id": project}).scalar()
    assert second["id"] == first["id"]
    assert second["approval_state"] == "pending"
    assert second["visitor_url"] is None
    assert count == 1


def test_one_audience_does_not_open_another_audiences_door(pg, project):
    with pg.begin() as connection:
        _publish(connection, project, approved=True, audience="public")
        door = current_visitor(connection, project_id=project, audience="customers")
    assert door is None


def test_approving_a_pending_snapshot_opens_its_door_without_touching_the_preview(pg, project):
    with pg.begin() as connection:
        pending = _publish(connection, project, approved=False)
        opened = approve_visitor_snapshot(
            connection, project_id=project, snapshot_id=pending["id"], actor="ada")
        again = approve_visitor_snapshot(
            connection, project_id=project, snapshot_id=pending["id"], actor="ada")
        door = current_visitor(connection, project_id=project, audience="customers")
    assert opened["approval_state"] == "approved"
    assert opened["visitor_url"] == f"/v1/visitors/{pending['id']}"
    assert opened["visitor_url"] != PREVIEW_URL
    assert opened["snapshot"] == PAGE
    assert again["visitor_url"] == opened["visitor_url"]
    assert door["id"] == pending["id"]


def test_a_bad_audience_or_empty_page_is_a_visible_error(pg, project):
    with pg.begin() as connection:
        with pytest.raises(PublishAudienceError) as audience:
            _publish(connection, project, audience="vip-list")
        with pytest.raises(PublishAudienceError) as empty:
            _publish(connection, project, page={})
    assert audience.value.status == 422
    assert "audience" in str(audience.value)
    assert empty.value.status == 422
    assert "frozen" in str(empty.value)


def test_a_missing_snapshot_cannot_be_approved_quietly(pg, project):
    with pg.begin() as connection:
        with pytest.raises(PublishAudienceError) as missing:
            approve_visitor_snapshot(
                connection, project_id=project, snapshot_id="vsnap_missing", actor="ada")
    assert missing.value.status == 404


def test_the_router_keeps_a_pending_publish_off_the_visitor_url(pg, project, monkeypatch):
    monkeypatch.setattr(gap_1243, "get_engine", lambda: pg)
    published = gap_1243.publish_snapshot(project, gap_1243.PublishVisitorSnapshotRequest(
        audience="staff", actor="ada", snapshot={"title": "Shift card"}, approved=False,
        preview_url=PREVIEW_URL, source_preview_id="prev_shift"))
    door = gap_1243.read_visitor_url(project, audience="staff")
    assert published["approval_state"] == "pending"
    assert published["visitor_url"] is None
    assert published["visitor_url"] != PREVIEW_URL
    assert door["visitor_url"] is None
    assert door["snapshot_id"] is None


def test_the_router_names_a_bad_audience_instead_of_swallowing_it(pg, project, monkeypatch):
    monkeypatch.setattr(gap_1243, "get_engine", lambda: pg)
    with pytest.raises(HTTPException) as caught:
        gap_1243.publish_snapshot(project, gap_1243.PublishVisitorSnapshotRequest(
            audience="", actor="ada", snapshot={"title": "Shift card"}))
    assert caught.value.status_code == 422
    assert "audience" in str(caught.value.detail)


def test_the_gap_router_is_mountable():
    paths = {route.path for route in gap_1243.router.routes}
    assert "/v1/projects/{project_id}/visitor-snapshots" in paths
    assert "/v1/projects/{project_id}/visitor-snapshots/{snapshot_id}/approve" in paths
    assert "/v1/projects/{project_id}/visitor-url" in paths
    assert gap_1243.router.routes
