"""An admin can pin where a built app runs, including an EU geography.

The pin does not move the control plane, and it does not contact a cloud region.
A deploy whose target geography is not the pin is refused. An unset pin keeps
today's path.
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text

from boss_engine.db import ensure_schema
from boss_engine.services import residency_pin as pin

# This string is the control plane. Pinning an app must not rewrite it.
CONTROL_PLANE_HOST = "plane.example:8000"
TODAY_PATH = "ssh://deploy@198.51.100.10/ledger"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def engine():
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("needs DATABASE_URL")
    eng = create_engine(url, future=True)
    ensure_schema(eng)
    return eng


def _org(connection) -> str:
    org = f"org_{uuid4().hex[:8]}"
    connection.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o)"), {"o": org})
    connection.execute(
        text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :o)"),
        {"w": f"ws_{org}", "o": org},
    )
    return org


def _project(connection) -> str:
    org = _org(connection)
    project_id = f"p_{uuid4().hex[:8]}"
    connection.execute(
        text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
        {"p": project_id, "w": f"ws_{org}"},
    )
    return project_id


def _member(connection, project_id: str, member_id: str, roles: list[str]) -> None:
    connection.execute(
        text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, role_keys) "
            "VALUES (:i, :p, :m, :m, CAST(:r AS jsonb))"
        ),
        {
            "i": f"pm_{uuid4().hex[:10]}",
            "p": project_id,
            "m": member_id,
            "r": "[" + ",".join(f'"{role}"' for role in roles) + "]",
        },
    )


def test_schema_block_is_appended_for_this_ticket():
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    start = schema.rfind("-- BOSSAPP-1255:")
    assert start != -1
    block = schema[start:]
    assert "CREATE TABLE IF NOT EXISTS app_residency_pins" in block
    assert "ADD COLUMN IF NOT EXISTS" in block
    # The block is an append. Nothing earlier in the file is this ticket.
    assert schema[:start].count("-- BOSSAPP-1255:") == 0


def test_eu_is_a_geography_not_a_cloud_region():
    found = pin.geographies()
    eu = next(item for item in found if item["id"] == "eu")
    assert eu["kind"] == "geography"
    assert eu["label"] == "European Union"
    assert all(item["kind"] == "geography" for item in found)
    assert all("-" not in item["id"] for item in found)
    source = (ROOT / "engine" / "boss_engine" / "services" / "residency_pin.py").read_text(
        encoding="utf-8"
    )
    for banned in ("boto3", "botocore", "httpx", "urllib.request", "eu-west-1", "eu-central-1"):
        assert banned not in source


def test_an_unset_pin_keeps_todays_path(engine):
    with engine.begin() as connection:
        project_id = _project(connection)
        out = pin.admit_deploy(
            connection,
            project_id=project_id,
            app_key="ledger",
            target_geography="us",
            app_path=TODAY_PATH,
            control_plane_host=CONTROL_PLANE_HOST,
        )
        assert out["allowed"] is True
        assert out["path"] == "today"
        assert out["app_path"] == TODAY_PATH
        assert out["control_plane_host"] == CONTROL_PLANE_HOST
        assert out["cloud_contacted"] is False
        assert out["pinned"] is False
        stored = pin.read_pin(
            connection,
            project_id=project_id,
            app_key="ledger",
            control_plane_host=CONTROL_PLANE_HOST,
        )
        assert stored["pinned"] is False
        assert stored["geography"] is None
        assert stored["control_plane_host"] == CONTROL_PLANE_HOST


def test_admin_can_pin_eu_without_moving_the_control_plane(engine):
    with engine.begin() as connection:
        project_id = _project(connection)
        _member(connection, project_id, "ada", ["admin"])
        pinned = pin.set_pin(
            connection,
            project_id=project_id,
            app_key="ledger",
            geography="eu",
            actor="ada",
            control_plane_host=CONTROL_PLANE_HOST,
        )
        assert pinned["geography"] == "eu"
        assert pinned["pinned"] is True
        assert pinned["control_plane_host"] == CONTROL_PLANE_HOST
        assert pinned["cloud_contacted"] is False
        moved = pin.set_pin(
            connection,
            project_id=project_id,
            app_key="ledger",
            geography="uk",
            actor="ada",
            control_plane_host="other.example:9",
        )
        assert moved["geography"] == "uk"
        assert moved["control_plane_host"] == CONTROL_PLANE_HOST
        again = pin.set_pin(
            connection,
            project_id=project_id,
            app_key="ledger",
            geography="eu",
            actor="ada",
            control_plane_host="other.example:9",
        )
        assert again["geography"] == "eu"
        assert again["control_plane_host"] == CONTROL_PLANE_HOST
        rows = connection.execute(
            text(
                "SELECT count(*) FROM app_residency_pins "
                "WHERE project_id = :p AND app_key = 'ledger'"
            ),
            {"p": project_id},
        ).scalar()
        assert rows == 1


def test_a_non_admin_cannot_pin(engine):
    with engine.begin() as connection:
        project_id = _project(connection)
        _member(connection, project_id, "bea", ["viewer"])
        with pytest.raises(pin.ResidencyRefused) as raised:
            pin.set_pin(
                connection,
                project_id=project_id,
                app_key="ledger",
                geography="eu",
                actor="bea",
                control_plane_host=CONTROL_PLANE_HOST,
            )
        assert raised.value.code == "not_admin"


def test_a_cloud_region_is_not_contacted(engine):
    with engine.begin() as connection:
        project_id = _project(connection)
        _member(connection, project_id, "ada", ["owner"])
        with pytest.raises(pin.ResidencyRefused) as raised:
            pin.set_pin(
                connection,
                project_id=project_id,
                app_key="ledger",
                geography="eu-central-1",
                actor="ada",
                control_plane_host=CONTROL_PLANE_HOST,
            )
        assert raised.value.code == "cloud_region"
        assert "not contacted" in str(raised.value).lower()
        assert "eu-central-1" not in str(raised.value)


def test_a_deploy_outside_the_pin_is_refused(engine):
    with engine.begin() as connection:
        project_id = _project(connection)
        _member(connection, project_id, "ada", ["admin"])
        pin.set_pin(
            connection,
            project_id=project_id,
            app_key="ledger",
            geography="eu",
            actor="ada",
            control_plane_host=CONTROL_PLANE_HOST,
        )
        match = pin.admit_deploy(
            connection,
            project_id=project_id,
            app_key="ledger",
            target_geography="eu",
            app_path=TODAY_PATH,
            control_plane_host="moved.example:1",
        )
        assert match["allowed"] is True
        assert match["path"] == "pinned"
        assert match["app_path"] == TODAY_PATH
        assert match["control_plane_host"] == CONTROL_PLANE_HOST
        assert match["cloud_contacted"] is False
        refused = pin.admit_deploy(
            connection,
            project_id=project_id,
            app_key="ledger",
            target_geography="us",
            app_path=TODAY_PATH,
            control_plane_host="moved.example:1",
        )
        assert refused["allowed"] is False
        assert refused["path"] == "refused"
        assert refused["app_path"] == TODAY_PATH
        assert refused["control_plane_host"] == CONTROL_PLANE_HOST
        assert refused["cloud_contacted"] is False
        assert refused["reason"]
        assert "contacted" not in refused["reason"].lower()
        region = pin.admit_deploy(
            connection,
            project_id=project_id,
            app_key="ledger",
            target_geography="eu-west-1",
            app_path=TODAY_PATH,
            control_plane_host=CONTROL_PLANE_HOST,
        )
        assert region["allowed"] is False
        assert region["cloud_contacted"] is False
        assert "not contacted" in region["reason"].lower()


def test_clearing_the_pin_restores_todays_path(engine):
    with engine.begin() as connection:
        project_id = _project(connection)
        _member(connection, project_id, "ada", ["admin"])
        pin.set_pin(
            connection,
            project_id=project_id,
            app_key="ledger",
            geography="eu",
            actor="ada",
            control_plane_host=CONTROL_PLANE_HOST,
        )
        cleared = pin.clear_pin(
            connection, project_id=project_id, app_key="ledger", actor="ada"
        )
        assert cleared["pinned"] is False
        assert cleared["geography"] is None
        assert cleared["control_plane_host"] == CONTROL_PLANE_HOST
        out = pin.admit_deploy(
            connection,
            project_id=project_id,
            app_key="ledger",
            target_geography="us",
            app_path=TODAY_PATH,
            control_plane_host="moved.example:1",
        )
        assert out["allowed"] is True
        assert out["path"] == "today"
        assert out["app_path"] == TODAY_PATH
        assert out["control_plane_host"] == CONTROL_PLANE_HOST


def test_the_router_refuses_a_mismatch_and_keeps_the_host(engine):
    from apps.api.app.routers import gap_1255

    with engine.begin() as connection:
        project_id = _project(connection)
        _member(connection, project_id, "ada", ["admin"])
    body = gap_1255.PinBody(
        actor="ada", geography="eu", control_plane_host=CONTROL_PLANE_HOST
    )
    saved = gap_1255.put_residency_pin(project_id, "ledger", body)
    assert saved["control_plane_host"] == CONTROL_PLANE_HOST
    assert saved["cloud_contacted"] is False
    assert saved["geography"] == "eu"
    moved = gap_1255.put_residency_pin(
        project_id,
        "ledger",
        gap_1255.PinBody(actor="ada", geography="eu", control_plane_host="other.example:9"),
    )
    assert moved["control_plane_host"] == CONTROL_PLANE_HOST
    with pytest.raises(HTTPException) as raised:
        gap_1255.admit_residency_deploy(
            project_id,
            "ledger",
            gap_1255.AdmitBody(
                target_geography="us",
                app_path=TODAY_PATH,
                control_plane_host="other.example:9",
            ),
        )
    assert raised.value.status_code == 409
    detail = raised.value.detail
    assert detail["cloud_contacted"] is False
    assert detail["control_plane_host"] == CONTROL_PLANE_HOST
    assert detail["allowed"] is False
    read = gap_1255.read_residency_pin(
        project_id, "ledger", control_plane_host="other.example:9"
    )
    assert read["control_plane_host"] == CONTROL_PLANE_HOST
    assert gap_1255.router.routes
