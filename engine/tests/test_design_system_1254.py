"""A workspace design system is what a new app reads (BOSSAPP-1254).

The system is a name, colors, type and spacing saved on the workspace. A new app in that
workspace reads those values. Another org does not see them, and does not learn that they exist.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services.design_system import (
    DesignSystemError,
    create_app,
    read_system,
    save_system,
)

ORG, OTHER = "org_ds_1254", "org_ds_1254_b"
WS, OTHER_WS = "ws_ds_1254", "ws_ds_1254_b"

COLORS = {"ground": "#123044", "metal": "#C9D0D4", "signal": "#E3A008", "ink": "#1C1915"}
TYPE = {"display": "Barlow Condensed", "body": "Public Sans", "utility": "IBM Plex Mono"}
SPACING = {"tight": 4, "stack": 12, "band": 28}
NIGHT = {"ground": "#102833", "metal": "#C9D0D4", "signal": "#C4532A", "ink": "#1C1915"}


def _database_url() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("BOSS_TEST_DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or "test" not in name:
        pytest.skip("DATABASE_URL must name a test database")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            for org, ws in ((ORG, WS), (OTHER, OTHER_WS)):
                conn.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o)"), {"o": org})
                conn.execute(
                    text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'W')"),
                    {"w": ws, "o": org},
                )
            yield conn
        finally:
            transaction.rollback()


def test_a_new_app_reads_the_workspace_system_and_another_org_does_not(connection):
    saved = save_system(
        connection, org_id=ORG, workspace_id=WS,
        name="Harbor enamel", colors=COLORS, type=TYPE, spacing=SPACING,
    )
    assert saved["name"] == "Harbor enamel"
    assert saved["colors"] == COLORS
    assert saved["type"] == TYPE
    assert saved["spacing"] == SPACING
    again = save_system(
        connection, org_id=ORG, workspace_id=WS,
        name="Harbor enamel", colors=COLORS, type=TYPE, spacing=SPACING,
    )
    assert again["id"] == saved["id"]
    assert connection.execute(text(
        "SELECT count(*) FROM workspace_design_systems WHERE workspace_id = :w"),
        {"w": WS}).scalar() == 1

    app = create_app(connection, org_id=ORG, workspace_id=WS, name="Pantry")
    read = app["design_system"]
    assert app["name"] == "Pantry"
    assert app["workspace_id"] == WS
    assert app["org_id"] == ORG
    assert read["name"] == "Harbor enamel"
    assert read["colors"] == COLORS
    assert read["type"] == TYPE
    assert read["spacing"] == SPACING

    save_system(
        connection, org_id=ORG, workspace_id=WS,
        name="Harbor night", colors=NIGHT, type=TYPE, spacing={"tight": 8, "band": 32},
    )
    later = create_app(connection, org_id=ORG, workspace_id=WS, name="Ledger")
    assert later["design_system"]["name"] == "Harbor night"
    assert later["design_system"]["colors"]["signal"] == "#C4532A"
    assert later["design_system"]["spacing"] == {"tight": 8, "band": 32}
    # The first app keeps the spec it read. A later save does not repaint it.
    assert app["design_system"]["colors"]["signal"] == "#E3A008"

    assert read_system(connection, org_id=OTHER, workspace_id=WS) is None
    with pytest.raises(DesignSystemError) as other_save:
        save_system(
            connection, org_id=OTHER, workspace_id=WS,
            name="Stolen", colors={"signal": "#000000"}, type=TYPE, spacing=SPACING,
        )
    assert other_save.value.code == "not_found"
    assert "Harbor" not in other_save.value.message
    assert read_system(connection, org_id=ORG, workspace_id=WS)["name"] == "Harbor night"
    with pytest.raises(DesignSystemError) as other_app:
        create_app(connection, org_id=OTHER, workspace_id=WS, name="Sneak")
    assert other_app.value.code == "not_found"
    assert "Harbor" not in other_app.value.message
    assert "#C4532A" not in other_app.value.message
    own = read_system(connection, org_id=OTHER, workspace_id=OTHER_WS)
    assert own is None


def test_a_missing_or_invalid_system_is_a_visible_error(connection):
    with pytest.raises(DesignSystemError) as missing:
        create_app(connection, org_id=ORG, workspace_id=WS, name="Bare")
    assert missing.value.code == "design_system_missing"
    assert missing.value.status == 409
    assert missing.value.message == "Save a design system before a new app can read one."
    with pytest.raises(DesignSystemError) as bad:
        save_system(
            connection, org_id=ORG, workspace_id=WS,
            name="Harbor enamel", colors={"signal": "amber"}, type=TYPE, spacing=SPACING,
        )
    assert bad.value.code == "invalid_color"
    assert "signal" in bad.value.message
    assert read_system(connection, org_id=ORG, workspace_id=WS) is None


def test_http_save_is_what_a_new_app_reads_and_another_org_cannot_see_it(pg_engine):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routers.gap_1254 import router
    from boss_engine.config import get_settings

    org = get_settings().default_org_id
    ws, other_ws = "ws_ds_http_1254", "ws_ds_http_other_1254"
    other = "org_ds_http_other_1254"
    with pg_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO orgs (id, name) VALUES (:o, :o) ON CONFLICT (id) DO NOTHING"),
            {"o": org})
        conn.execute(text(
            "INSERT INTO orgs (id, name) VALUES (:o, :o) ON CONFLICT (id) DO NOTHING"),
            {"o": other})
        for workspace, owner in ((ws, org), (other_ws, other)):
            conn.execute(text(
                "INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'H') "
                "ON CONFLICT (id) DO NOTHING"), {"w": workspace, "o": owner})
        conn.execute(text(
            """
            INSERT INTO workspace_design_systems
              (id, org_id, workspace_id, name, colors, type_roles, spacing)
            VALUES
              ('wds_private_1254', :o, :w, 'Private kiln',
               CAST(:colors AS JSONB), CAST(:type AS JSONB), CAST(:spacing AS JSONB))
            ON CONFLICT (workspace_id) DO UPDATE
              SET name = 'Private kiln', org_id = EXCLUDED.org_id,
                  colors = EXCLUDED.colors
            """), {
                "o": other, "w": other_ws,
                "colors": '{"glaze":"#ABCDEF"}',
                "type": '{"display":"Barlow Condensed","body":"Public Sans"}',
                "spacing": '{"tight":4}',
            })
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    try:
        saved = client.put(f"/v1/workspaces/{ws}/design-system", json={
            "name": "Harbor enamel", "colors": COLORS, "type": TYPE, "spacing": SPACING,
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["name"] == "Harbor enamel"
        assert saved.json()["colors"]["signal"] == "#E3A008"
        created = client.post(f"/v1/workspaces/{ws}/apps", json={"name": "Pantry"})
        assert created.status_code == 201, created.text
        assert created.json()["design_system"]["name"] == "Harbor enamel"
        assert created.json()["design_system"]["colors"] == COLORS
        hidden = client.get(f"/v1/workspaces/{other_ws}/design-system")
        assert hidden.status_code == 404
        assert "Private kiln" not in hidden.text
        assert "ABCDEF" not in hidden.text
        stolen = client.put(f"/v1/workspaces/{other_ws}/design-system", json={
            "name": "Stolen", "colors": COLORS, "type": TYPE, "spacing": SPACING,
        })
        assert stolen.status_code == 404
        assert "Private kiln" not in stolen.text
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM workspace_design_apps WHERE workspace_id IN (:a, :b)"),
                {"a": ws, "b": other_ws})
            conn.execute(text(
                "DELETE FROM workspace_design_systems WHERE workspace_id IN (:a, :b)"),
                {"a": ws, "b": other_ws})
            conn.execute(text(
                "DELETE FROM workspaces WHERE id IN (:a, :b)"),
                {"a": ws, "b": other_ws})
