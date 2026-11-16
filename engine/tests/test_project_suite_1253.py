"""A project keeps a website, a mobile app, slides, and a data view (BOSSAPP-1253).

Publishing one surface leaves the others on the project. Publishing together ships
every surface in one record. The same dedupe key does not write a second record.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.project_suite import (
    SURFACES,
    SuiteError,
    hold_surface,
    publish,
    read_suite,
)


def _engine():
    url = os.environ["DATABASE_URL"]
    engine = create_engine(url, future=True)
    from boss_engine.config import get_settings
    from boss_engine.db import ensure_schema

    ensure_schema(engine, get_settings())
    return engine


@pytest.fixture(scope="module")
def engine():
    eng = _engine()
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture()
def project_id(db):
    suffix = uuid.uuid4().hex[:12]
    org_id = f"org_suite_{suffix}"
    workspace_id = f"ws_suite_{suffix}"
    proj_id = f"proj_suite_{suffix}"
    db.execute(text("INSERT INTO orgs (id, name) VALUES (:id, :n)"),
               {"id": org_id, "n": "Suite test"})
    db.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :o, :n)"),
               {"id": workspace_id, "o": org_id, "n": "Suite test"})
    db.execute(text(
        "INSERT INTO projects (id, workspace_id, name, slug) VALUES (:id, :w, :n, :s)"),
        {"id": proj_id, "w": workspace_id, "n": "Harbor", "s": f"harbor-{suffix}"})
    return proj_id


def _hold_all(db, project_id: str) -> None:
    bodies = {
        "website": ("Harbor site", "A public page."),
        "mobile": ("Harbor phone", "The same work, in a pocket."),
        "slides": ("Harbor deck", "Twelve frames."),
        "data_view": ("Harbor numbers", "The figures behind the page."),
    }
    for kind in SURFACES:
        title, body = bodies[kind]
        hold_surface(db, project_id, kind=kind, title=title, body=body)


def _rows(db, project_id: str) -> list[dict]:
    found = db.execute(text(
        """
        SELECT kind, title, body, status, revision, published_revision
        FROM project_suite_surfaces
        WHERE project_id = :id
        ORDER BY kind
        """), {"id": project_id}).mappings().all()
    return [dict(row) for row in found]


def _publish_count(db, project_id: str) -> int:
    return int(db.execute(text(
        "SELECT count(*) FROM project_suite_publishes WHERE project_id = :id"),
        {"id": project_id}).scalar() or 0)


def test_a_project_can_hold_all_four_surfaces(db, project_id):
    _hold_all(db, project_id)
    view = read_suite(db, project_id)
    assert view["project_name"] == "Harbor"
    assert [item["kind"] for item in view["surfaces"]] == list(SURFACES)
    assert {item["status"] for item in view["surfaces"]} == {"draft"}
    again = hold_surface(db, project_id, kind="website", title="Harbor site revised",
                         body="A public page.")
    assert [item["kind"] for item in again["surfaces"]] == list(SURFACES)
    by_kind = {item["kind"]: item for item in again["surfaces"]}
    assert by_kind["website"]["title"] == "Harbor site revised"
    assert by_kind["mobile"]["title"] == "Harbor phone"
    assert len(_rows(db, project_id)) == 4


def test_publishing_one_surface_does_not_drop_the_others(db, project_id):
    _hold_all(db, project_id)
    before = {row["kind"]: row for row in _rows(db, project_id)}
    view = publish(db, project_id, mode="one", kind="website", dedupe_key="one-website")
    stored = {row["kind"]: row for row in _rows(db, project_id)}
    assert set(stored) == set(SURFACES)
    assert stored["website"]["status"] == "published"
    assert stored["website"]["published_revision"] == stored["website"]["revision"]
    for kind in ("mobile", "slides", "data_view"):
        assert stored[kind]["status"] == "draft"
        assert stored[kind]["title"] == before[kind]["title"]
        assert stored[kind]["body"] == before[kind]["body"]
        assert stored[kind]["revision"] == before[kind]["revision"]
        assert stored[kind]["published_revision"] is None
    assert [item["kind"] for item in view["surfaces"]] == list(SURFACES)
    assert _publish_count(db, project_id) == 1
    assert view["publish"]["mode"] == "one"
    assert view["publish"]["kinds"] == ["website"]
    assert view["publish"]["repeated"] is False


def test_publishing_one_after_the_sheet_does_not_unpublish_the_others(db, project_id):
    _hold_all(db, project_id)
    publish(db, project_id, mode="together", dedupe_key="sheet-1")
    publish(db, project_id, mode="one", kind="slides", dedupe_key="slides-again")
    stored = {row["kind"]: row for row in _rows(db, project_id)}
    assert set(stored) == set(SURFACES)
    assert {row["status"] for row in stored.values()} == {"published"}


def test_publishing_together_marks_every_surface_in_one_record(db, project_id):
    _hold_all(db, project_id)
    view = publish(db, project_id, mode="together", dedupe_key="sheet-together")
    stored = _rows(db, project_id)
    assert len(stored) == 4
    assert {row["status"] for row in stored} == {"published"}
    assert _publish_count(db, project_id) == 1
    assert view["publish"]["mode"] == "together"
    assert view["publish"]["kinds"] == list(SURFACES)
    assert view["publish"]["repeated"] is False


def test_a_repeated_publish_does_not_write_a_second_record(db, project_id):
    _hold_all(db, project_id)
    first = publish(db, project_id, mode="together", dedupe_key="sheet-same")
    second = publish(db, project_id, mode="together", dedupe_key="sheet-same")
    assert second["publish"]["id"] == first["publish"]["id"]
    assert second["publish"]["repeated"] is True
    assert _publish_count(db, project_id) == 1
    first_revs = {item["kind"]: item["revision"] for item in first["surfaces"]}
    second_revs = {item["kind"]: item["revision"] for item in second["surfaces"]}
    assert second_revs == first_revs


def test_the_same_key_cannot_publish_a_different_set(db, project_id):
    _hold_all(db, project_id)
    publish(db, project_id, mode="one", kind="website", dedupe_key="shared-key")
    before = _rows(db, project_id)
    with pytest.raises(SuiteError) as caught:
        publish(db, project_id, mode="together", dedupe_key="shared-key")
    assert caught.value.status == 409
    assert caught.value.code == "dedupe_conflict"
    assert _rows(db, project_id) == before
    assert _publish_count(db, project_id) == 1


def test_publishing_together_refuses_a_missing_surface_and_keeps_the_rest(db, project_id):
    hold_surface(db, project_id, kind="website", title="Harbor site", body="A public page.")
    hold_surface(db, project_id, kind="mobile", title="Harbor phone", body="")
    hold_surface(db, project_id, kind="data_view", title="Harbor numbers", body="")
    with pytest.raises(SuiteError) as caught:
        publish(db, project_id, mode="together", dedupe_key="incomplete")
    assert caught.value.status == 409
    assert caught.value.code == "sheet_incomplete"
    assert "Slides" in caught.value.message
    stored = _rows(db, project_id)
    assert {row["kind"] for row in stored} == {"website", "mobile", "data_view"}
    assert {row["status"] for row in stored} == {"draft"}
    assert _publish_count(db, project_id) == 0


def test_an_unknown_surface_is_refused(db, project_id):
    with pytest.raises(SuiteError) as caught:
        hold_surface(db, project_id, kind="poster", title="A poster", body="")
    assert caught.value.status == 422
    assert caught.value.code == "unknown_surface"
    assert _rows(db, project_id) == []


def test_publishing_a_surface_that_is_not_held_is_refused(db, project_id):
    hold_surface(db, project_id, kind="website", title="Harbor site", body="A public page.")
    with pytest.raises(SuiteError) as caught:
        publish(db, project_id, mode="one", kind="mobile", dedupe_key="missing-mobile")
    assert caught.value.status == 409
    assert caught.value.code == "surface_missing"
    assert "Mobile app" in caught.value.message
    stored = _rows(db, project_id)
    assert [row["kind"] for row in stored] == ["website"]
    assert stored[0]["status"] == "draft"
    assert _publish_count(db, project_id) == 0


def test_a_missing_project_is_an_error(db):
    with pytest.raises(SuiteError) as caught:
        read_suite(db, "proj_missing")
    assert caught.value.status == 404
    assert caught.value.code == "project_missing"


def test_the_gap_router_names_the_suite_routes():
    from apps.api.app.routers.gap_1253 import router

    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/v1/projects/{project_id}/suite" in paths
    assert "/v1/projects/{project_id}/suite/publish" in paths
    assert "/v1/projects/{project_id}/suite/surfaces/{kind}" in paths
