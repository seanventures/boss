"""BOSSAPP-1266: each built app has tables, filters, import, and a 30-day trash.

Soft-delete sets deleted_at. A row younger than 30 days can be restored. A row older than
30 days is purged when the test calls purge_expired with a fake clock. Live rows and other
tables (real user data) stay.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import app_data
from boss_engine.services.app_data import AppDataError

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
CSV = "name,city\nAda Lovelace,London\nGrace Hopper,Paris\n"


def _engine():
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.fail("DATABASE_URL must be the disposable Postgres for this ticket")
    return create_engine(url, future=True)


def _seed(connection) -> dict[str, str]:
    org = f"org_{uuid4().hex[:10]}"
    workspace = f"ws_{uuid4().hex[:10]}"
    project = f"prj_{uuid4().hex[:10]}"
    member = f"mem_{uuid4().hex[:10]}"
    connection.execute(text(
        "INSERT INTO orgs (id, name) VALUES (:id, 'Acme')"), {"id": org})
    connection.execute(text(
        "INSERT INTO workspaces (id, org_id, name) VALUES (:id, :org, 'Shop')"),
        {"id": workspace, "org": org})
    connection.execute(text(
        "INSERT INTO projects (id, workspace_id, name, slug) "
        "VALUES (:id, :ws, 'Shop', :slug)"),
        {"id": project, "ws": workspace, "slug": f"shop-{project}"})
    connection.execute(text(
        "INSERT INTO org_members (id, org_id, member_id, display_name) "
        "VALUES (:id, :org, 'usr_ada', 'Ada Lovelace')"),
        {"id": member, "org": org})
    return {"org": org, "project": project, "member": member}


@pytest.fixture()
def db():
    engine = _engine()
    with engine.begin() as connection:
        ids = _seed(connection)
    yield engine, ids
    engine.dispose()


def _member_exists(engine, member_id: str) -> bool:
    with engine.begin() as connection:
        return connection.execute(
            text("SELECT 1 FROM org_members WHERE id = :id"),
            {"id": member_id}).first() is not None


def _row(engine, row_id: str):
    with engine.begin() as connection:
        found = connection.execute(
            text("SELECT id, deleted_at FROM app_data_rows WHERE id = :id"),
            {"id": row_id}).mappings().first()
    return dict(found) if found else None


def test_import_adds_rows_and_filter_returns_only_matches(db):
    engine, ids = db
    project = ids["project"]
    with engine.begin() as connection:
        first = app_data.import_csv(
            connection, project_id=project, app_key="Recipe Box", table="Customers",
            csv_text=CSV)
        again = app_data.import_csv(
            connection, project_id=project, app_key="recipe-box", table="Customers",
            csv_text="name,city\n100% Pure,100%\n")
        exact = app_data.filter_rows(
            connection, project_id=project, app_key="recipe box", table="Customers",
            field="city", value="London", mode="is")
        contains = app_data.filter_rows(
            connection, project_id=project, app_key="recipe box", table="Customers",
            field="city", value="%", mode="contains")
        other = app_data.filter_rows(
            connection, project_id=project, app_key="Invoice Desk", table="Customers",
            field="city", value="London", mode="is")
    assert first["imported"] == 2
    assert again["imported"] == 1
    assert len(exact) == 1 and exact[0]["cells"]["name"] == "Ada Lovelace"
    assert [row["cells"]["city"] for row in contains] == ["100%"]
    assert other == []
    assert _member_exists(engine, ids["member"])


def test_soft_delete_sets_deleted_at_and_a_young_row_restores(db):
    engine, ids = db
    project = ids["project"]
    with engine.begin() as connection:
        imported = app_data.import_csv(
            connection, project_id=project, app_key="Recipe Box", table="Customers",
            csv_text=CSV)
        ada = next(row for row in imported["rows"] if row["cells"]["name"] == "Ada Lovelace")
        trashed = app_data.soft_delete(
            connection, project_id=project, app_key="recipe box", row_id=ada["id"], now=T0)
        hidden = app_data.filter_rows(
            connection, project_id=project, app_key="recipe box", table="Customers",
            field="name", value="Ada Lovelace", mode="is")
        again = app_data.soft_delete(
            connection, project_id=project, app_key="recipe box", row_id=ada["id"],
            now=T0 + timedelta(days=10))
        held = app_data.page(
            connection, project_id=project, app_key="recipe box", now=T0 + timedelta(days=10))
        restored = app_data.restore(
            connection, project_id=project, app_key="recipe box", row_id=ada["id"],
            now=T0 + timedelta(days=10))
        back = app_data.filter_rows(
            connection, project_id=project, app_key="recipe box", table="Customers",
            field="name", value="Ada Lovelace", mode="is")
    assert trashed["deleted_at"] is not None
    assert hidden == []
    assert again["deleted_at"] == trashed["deleted_at"]
    assert held["trash_days"] == 30
    assert held["trash"][0]["id"] == ada["id"]
    assert held["trash"][0]["days_left"] == 20
    assert held["trash"][0]["restorable"] is True
    assert restored["deleted_at"] is None
    assert len(back) == 1


def test_purge_with_a_fake_clock_drops_only_trash_older_than_30_days(db):
    engine, ids = db
    project = ids["project"]
    poison = "Robert'); DROP TABLE org_members;--"
    with engine.begin() as connection:
        live = app_data.import_csv(
            connection, project_id=project, app_key="Recipe Box", table="Customers",
            csv_text=f"name,city\n{poison},London\n")
        young = app_data.import_csv(
            connection, project_id=project, app_key="Recipe Box", table="Customers",
            csv_text="name,city\nYoung,Paris\n")
        boundary = app_data.import_csv(
            connection, project_id=project, app_key="Recipe Box", table="Customers",
            csv_text="name,city\nBoundary,Oslo\n")
        old = app_data.import_csv(
            connection, project_id=project, app_key="Recipe Box", table="Customers",
            csv_text="name,city\nOld,Rome\n")
        other_live = app_data.import_csv(
            connection, project_id=project, app_key="Invoice Desk", table="Customers",
            csv_text="name,city\nKept,London\n")
        other_old = app_data.import_csv(
            connection, project_id=project, app_key="Invoice Desk", table="Customers",
            csv_text="name,city\nGone,Rome\n")
        live_id = live["rows"][0]["id"]
        young_id = young["rows"][0]["id"]
        boundary_id = boundary["rows"][0]["id"]
        old_id = old["rows"][0]["id"]
        other_live_id = other_live["rows"][0]["id"]
        other_old_id = other_old["rows"][0]["id"]
        app_data.soft_delete(
            connection, project_id=project, app_key="recipe box", row_id=young_id,
            now=T0 + timedelta(days=20))
        app_data.soft_delete(
            connection, project_id=project, app_key="recipe box", row_id=boundary_id, now=T0)
        app_data.soft_delete(
            connection, project_id=project, app_key="recipe box", row_id=old_id,
            now=T0 - timedelta(seconds=1))
        app_data.soft_delete(
            connection, project_id=project, app_key="invoice desk", row_id=other_old_id,
            now=T0 - timedelta(days=1))
        connection.execute(text(
            "UPDATE app_data_rows SET created_at = :at WHERE id = :id"),
            {"id": live_id, "at": datetime(2010, 1, 1, tzinfo=timezone.utc)})
        result = app_data.purge_expired(
            connection, now=T0 + timedelta(days=30), project_id=project)
    purged = set(result["purged"])
    assert old_id in purged and other_old_id in purged
    assert live_id not in purged and young_id not in purged and boundary_id not in purged
    assert _row(engine, live_id)["deleted_at"] is None
    assert _row(engine, young_id)["deleted_at"] is not None
    assert _row(engine, boundary_id)["deleted_at"] is not None
    assert _row(engine, old_id) is None
    assert _row(engine, other_live_id)["deleted_at"] is None
    assert _row(engine, other_old_id) is None
    assert _member_exists(engine, ids["member"])
    with engine.begin() as connection:
        kept = app_data.filter_rows(
            connection, project_id=project, app_key="recipe box", table="Customers",
            field="name", value=poison, mode="is")
        restored = app_data.restore(
            connection, project_id=project, app_key="recipe box", row_id=boundary_id,
            now=T0 + timedelta(days=30))
    assert kept[0]["cells"]["name"] == poison
    assert restored["deleted_at"] is None


def test_restore_refuses_a_row_older_than_30_days(db):
    engine, ids = db
    with engine.begin() as connection:
        imported = app_data.import_csv(
            connection, project_id=ids["project"], app_key="Recipe Box", table="Customers",
            csv_text="name,city\nOld,Rome\n")
        row_id = imported["rows"][0]["id"]
        app_data.soft_delete(
            connection, project_id=ids["project"], app_key="recipe box", row_id=row_id,
            now=T0)
        with pytest.raises(AppDataError, match="30"):
            app_data.restore(
                connection, project_id=ids["project"], app_key="recipe box", row_id=row_id,
                now=T0 + timedelta(days=30, seconds=1))
    assert _row(engine, row_id)["deleted_at"] is not None
    assert _member_exists(engine, ids["member"])


def test_a_bad_import_is_a_visible_error_and_changes_nothing(db):
    engine, ids = db
    with engine.begin() as connection:
        with pytest.raises(AppDataError, match="no rows"):
            app_data.import_csv(
                connection, project_id=ids["project"], app_key="Recipe Box",
                table="Customers", csv_text="name,city\n")
        count = connection.execute(text(
            "SELECT count(*) FROM app_data_rows WHERE project_id = :p"),
            {"p": ids["project"]}).scalar()
    assert count == 0
    assert _member_exists(engine, ids["member"])
