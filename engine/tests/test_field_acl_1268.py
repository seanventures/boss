"""A builder sets who can read or change each field, then opens the app as that user.

A user without read does not receive the field. A user without change can read it and
cannot write it. Opening as that user uses the same grants as that user, not the opener's.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]
URL = os.environ.get("DATABASE_URL", "")


def _schema_tail() -> str:
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    marker = "-- BOSSAPP-1268:"
    assert marker in sql, "schema.sql must append a BOSSAPP-1268 block"
    assert sql.index("BOSSAPP-1275") < sql.index(marker)
    return sql[sql.index(marker):]


def _statements(block: str) -> list[str]:
    parts: list[str] = []
    for raw in block.split(";"):
        lines = [ln for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("--")]
        if lines:
            parts.append(raw.strip())
    return parts


@pytest.fixture(scope="module")
def eng():
    assert "boss_test_1268" in URL, "DATABASE_URL must be the BOSSAPP-1268 database"
    engine = create_engine(URL, future=True)
    with engine.begin() as connection:
        for statement in _statements(_schema_tail()):
            connection.execute(text(statement))
    return engine


@pytest.fixture()
def db(eng):
    with eng.begin() as connection:
        yield connection


def test_schema_appends_the_field_acl_tables():
    tail = _schema_tail()
    for table in ("field_acl_apps", "field_acl_fields", "field_acl_grants",
                  "field_acl_members", "field_acl_records"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in tail


def _pid() -> str:
    return "p_" + uuid.uuid4().hex[:10]


def _card(db, project_id: str | None = None) -> str:
    from boss_engine.services import field_acl

    project_id = project_id or _pid()
    app = field_acl.create_app(
        db, project_id=project_id, name="Personnel card", created_by="builder",
        fields=[
            {"key": "salary", "label": "Salary"},
            {"key": "title", "label": "Title"},
            {"key": "notes", "label": "Notes"},
        ],
    )
    app_id = app["id"]
    field_acl.set_member(db, app_id, "ada", ["employee"])
    field_acl.set_member(db, app_id, "rio", ["payroll"])
    field_acl.set_member(db, app_id, "builder", [])
    field_acl.set_grants(db, app_id, [
        {"field_key": "salary", "subject_kind": "role", "subject_id": "payroll",
         "can_read": True, "can_change": True},
        {"field_key": "title", "subject_kind": "user", "subject_id": "ada",
         "can_read": True, "can_change": True},
        {"field_key": "title", "subject_kind": "role", "subject_id": "payroll",
         "can_read": True, "can_change": True},
        {"field_key": "notes", "subject_kind": "user", "subject_id": "ada",
         "can_read": True, "can_change": False},
        {"field_key": "notes", "subject_kind": "user", "subject_id": "builder",
         "can_read": True, "can_change": True},
        {"field_key": "notes", "subject_kind": "role", "subject_id": "payroll",
         "can_read": True, "can_change": True},
    ])
    field_acl.write_fields(db, app_id, as_user="rio", values={
        "salary": "180000", "title": "Engineer", "notes": "On the rota",
    })
    return app_id


def test_a_user_without_read_does_not_receive_the_field(db):
    import json

    from boss_engine.services import field_acl

    app_id = _card(db)
    view = field_acl.open_app(db, app_id, as_user="ada", actor="builder")
    assert [field["key"] for field in view["fields"]] == ["title", "notes"]
    assert view["values"] == {"title": "Engineer", "notes": "On the rota"}
    assert "salary" not in json.dumps(view)
    assert "grants" not in view and "members" not in view
    rio = field_acl.open_app(db, app_id, as_user="rio")
    assert rio["values"]["salary"] == "180000"
    assert next(field for field in rio["fields"] if field["key"] == "salary")["writable"] is True


def test_a_user_without_change_can_read_and_cannot_write(db):
    from boss_engine.services import field_acl
    from boss_engine.services.field_acl import FieldWriteRefused

    app_id = _card(db)
    view = field_acl.open_app(db, app_id, as_user="ada")
    notes = next(field for field in view["fields"] if field["key"] == "notes")
    title = next(field for field in view["fields"] if field["key"] == "title")
    assert notes["writable"] is False and view["values"]["notes"] == "On the rota"
    assert title["writable"] is True

    with pytest.raises(FieldWriteRefused) as refused:
        field_acl.write_fields(db, app_id, as_user="ada", actor="builder", values={"notes": "no"})
    assert refused.value.code == "not_writable"
    assert "notes" not in str(refused.value)
    assert field_acl.open_app(db, app_id, as_user="ada")["values"]["notes"] == "On the rota"

    with pytest.raises(FieldWriteRefused):
        field_acl.write_fields(db, app_id, as_user="ada", values={"title": "Lead", "notes": "no"})
    assert field_acl.open_app(db, app_id, as_user="ada")["values"]["title"] == "Engineer"

    wrote = field_acl.write_fields(db, app_id, as_user="ada", actor="builder", values={"title": "Lead"})
    assert wrote["values"]["title"] == "Lead"
    assert "salary" not in wrote["values"]
    assert wrote["values"]["notes"] == "On the rota"
    assert field_acl.open_app(db, app_id, as_user="rio")["values"]["salary"] == "180000"
    assert field_acl.open_app(db, app_id, as_user="rio")["values"]["title"] == "Lead"


def test_opening_as_a_user_does_not_add_the_openers_grants(db):
    from boss_engine.services import field_acl
    from boss_engine.services.field_acl import FieldWriteRefused

    app_id = _card(db)
    as_ada = field_acl.open_app(db, app_id, as_user="ada")
    opened = field_acl.open_app(db, app_id, as_user="ada", actor="builder")
    assert opened["fields"] == as_ada["fields"]
    assert opened["values"] == as_ada["values"]
    assert opened["as_user"] == "ada"
    notes = next(field for field in opened["fields"] if field["key"] == "notes")
    assert notes["writable"] is False
    with pytest.raises(FieldWriteRefused):
        field_acl.write_fields(db, app_id, as_user="ada", actor="builder", values={"notes": "widened"})
    builder = field_acl.open_app(db, app_id, as_user="builder")
    assert next(field for field in builder["fields"] if field["key"] == "notes")["writable"] is True
    field_acl.write_fields(db, app_id, as_user="builder", actor="ada", values={"notes": "From the desk"})
    assert field_acl.open_app(db, app_id, as_user="ada")["values"]["notes"] == "From the desk"


def test_change_implies_read_and_a_cleared_grant_hides_the_field(db):
    from boss_engine.services import field_acl

    app_id = _card(db)
    stored = field_acl.set_field_grants(db, app_id, "salary", [
        {"subject_kind": "user", "subject_id": "ada", "can_read": False, "can_change": True},
    ])
    assert stored == [{
        "field_key": "salary", "subject_kind": "user", "subject_id": "ada",
        "can_read": True, "can_change": True,
    }]
    view = field_acl.open_app(db, app_id, as_user="ada")
    assert next(field for field in view["fields"] if field["key"] == "salary")["writable"] is True
    assert view["values"]["salary"] == "180000"

    field_acl.set_field_grants(db, app_id, "salary", [])
    hidden = field_acl.open_app(db, app_id, as_user="ada")
    assert "salary" not in [field["key"] for field in hidden["fields"]]
    assert "salary" not in hidden["values"]
    # Payroll's role grant was on this field and the replace cleared it.
    assert "salary" not in [field["key"] for field in field_acl.open_app(db, app_id, as_user="rio")["fields"]]


def test_a_role_grant_reaches_members_of_that_role_only(db):
    import json

    from boss_engine.services import field_acl

    app_id = _card(db)
    field_acl.set_field_grants(db, app_id, "salary", [
        {"subject_kind": "role", "subject_id": "employee", "can_read": True, "can_change": False},
    ])
    ada = field_acl.open_app(db, app_id, as_user="ada")
    salary = next(field for field in ada["fields"] if field["key"] == "salary")
    assert salary["writable"] is False and ada["values"]["salary"] == "180000"
    field_acl.set_member(db, app_id, "ben", ["contractor"])
    ben = field_acl.open_app(db, app_id, as_user="ben")
    assert ben["fields"] == [] and ben["values"] == {}
    assert "salary" not in json.dumps(ben)


def test_a_readable_field_with_no_stored_value_is_still_received(db):
    from boss_engine.services import field_acl

    project_id = _pid()
    app = field_acl.create_app(
        db, project_id=project_id, name="Blank", created_by="builder",
        fields=[{"key": "title", "label": "Title"}, {"key": "salary", "label": "Salary"}],
    )
    field_acl.set_field_grants(db, app["id"], "title", [
        {"subject_kind": "user", "subject_id": "ada", "can_read": True, "can_change": False},
    ])
    view = field_acl.open_app(db, app["id"], as_user="ada")
    assert [field["key"] for field in view["fields"]] == ["title"]
    assert view["values"] == {"title": None}


def test_bad_grants_do_not_wipe_the_ones_already_stored(db):
    from boss_engine.services import field_acl
    from boss_engine.services.field_acl import FieldAclError

    app_id = _card(db)
    before = field_acl.builder_view(db, app_id)["grants"]
    with pytest.raises(FieldAclError) as refused:
        field_acl.set_grants(db, app_id, [
            {"field_key": "title", "subject_kind": "user", "subject_id": "ada",
             "can_read": True, "can_change": True},
            {"field_key": "title", "subject_kind": "user", "subject_id": "ada",
             "can_read": True, "can_change": False},
        ])
    assert refused.value.code == "duplicate_grant"
    assert field_acl.builder_view(db, app_id)["grants"] == before

    with pytest.raises(FieldAclError) as unknown:
        field_acl.set_grants(db, app_id, [
            {"field_key": "nope", "subject_kind": "user", "subject_id": "ada",
             "can_read": True, "can_change": False},
        ])
    assert unknown.value.code == "unknown_field"
    assert field_acl.builder_view(db, app_id)["grants"] == before


def test_a_hidden_or_unknown_write_is_the_same_refusal(db):
    from boss_engine.services import field_acl
    from boss_engine.services.field_acl import FieldWriteRefused

    app_id = _card(db)
    for values in ({"salary": "1"}, {"not_a_field": "x"}):
        with pytest.raises(FieldWriteRefused) as refused:
            field_acl.write_fields(db, app_id, as_user="ada", actor="builder", values=values)
        assert refused.value.code == "not_writable"
        assert str(refused.value) == "You can't change that field."
    assert field_acl.open_app(db, app_id, as_user="rio")["values"]["salary"] == "180000"


def test_create_rejects_an_empty_or_duplicate_field_list(db):
    from boss_engine.services import field_acl
    from boss_engine.services.field_acl import FieldAclError

    project_id = _pid()
    with pytest.raises(FieldAclError) as empty:
        field_acl.create_app(db, project_id=project_id, name="X", created_by="builder", fields=[])
    assert empty.value.code == "no_fields"
    with pytest.raises(FieldAclError) as dup:
        field_acl.create_app(
            db, project_id=project_id, name="X", created_by="builder",
            fields=[{"key": "title", "label": "A"}, {"key": "title", "label": "B"}],
        )
    assert dup.value.code == "duplicate_field"


def test_list_apps_stays_inside_the_project(db):
    from boss_engine.services import field_acl

    one = _pid()
    two = _pid()
    left = _card(db, one)
    right = _card(db, two)
    ids = [app["id"] for app in field_acl.list_apps(db, one)]
    assert left in ids and right not in ids


def test_open_of_another_project_is_not_found(db):
    from boss_engine.services import field_acl
    from boss_engine.services.field_acl import FieldAclError

    app_id = _card(db, _pid())
    with pytest.raises(FieldAclError) as missing:
        field_acl.open_app(db, app_id, as_user="ada", project_id=_pid())
    assert missing.value.status == 404
    with pytest.raises(FieldAclError) as gone:
        field_acl.open_app(db, "fa_missing", as_user="ada")
    assert gone.value.code == "not_found"


def test_a_nested_value_is_refused(db):
    from boss_engine.services import field_acl
    from boss_engine.services.field_acl import FieldAclError

    app_id = _card(db)
    with pytest.raises(FieldAclError) as refused:
        field_acl.write_fields(db, app_id, as_user="ada", values={"title": {"sneak": "salary"}})
    assert refused.value.code == "bad_value"
    assert field_acl.open_app(db, app_id, as_user="ada")["values"]["title"] == "Engineer"


def test_the_http_open_uses_the_same_rules(eng):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routers.gap_1268 import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    project_id = _pid()
    created = client.post("/v1/field-acl/apps", json={
        "project_id": project_id, "actor": "builder", "name": "Personnel card",
        "fields": [
            {"key": "salary", "label": "Salary"},
            {"key": "title", "label": "Title"},
            {"key": "notes", "label": "Notes"},
        ],
    })
    assert created.status_code == 200, created.text
    app_id = created.json()["id"]
    for user_id, roles in (("ada", ["employee"]), ("rio", ["payroll"]), ("builder", [])):
        member = client.put(f"/v1/field-acl/apps/{app_id}/members/{user_id}", json={
            "project_id": project_id, "roles": roles,
        })
        assert member.status_code == 200, member.text
    grants = client.put(f"/v1/field-acl/apps/{app_id}/grants", json={
        "project_id": project_id,
        "grants": [
            {"field_key": "salary", "subject_kind": "role", "subject_id": "payroll",
             "can_read": True, "can_change": True},
            {"field_key": "title", "subject_kind": "user", "subject_id": "ada",
             "can_read": True, "can_change": True},
            {"field_key": "title", "subject_kind": "role", "subject_id": "payroll",
             "can_read": True, "can_change": True},
            {"field_key": "notes", "subject_kind": "user", "subject_id": "ada",
             "can_read": True, "can_change": False},
            {"field_key": "notes", "subject_kind": "user", "subject_id": "builder",
             "can_read": True, "can_change": True},
            {"field_key": "notes", "subject_kind": "role", "subject_id": "payroll",
             "can_read": True, "can_change": True},
        ],
    })
    assert grants.status_code == 200, grants.text
    seeded = client.put(f"/v1/field-acl/apps/{app_id}/record", json={
        "project_id": project_id, "as_user": "rio", "actor": "builder",
        "values": {"salary": "180000", "title": "Engineer", "notes": "On the rota"},
    })
    assert seeded.status_code == 200, seeded.text
    opened = client.get(f"/v1/field-acl/apps/{app_id}/open", params={
        "project_id": project_id, "as_user": "ada", "actor": "builder",
    })
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert [field["key"] for field in body["fields"]] == ["title", "notes"]
    assert body["fields"][1]["writable"] is False
    assert "salary" not in opened.text
    refused = client.put(f"/v1/field-acl/apps/{app_id}/record", json={
        "project_id": project_id, "as_user": "ada", "actor": "builder",
        "values": {"notes": "no"},
    })
    assert refused.status_code == 403
    assert refused.json()["error"] == "not_writable"
    assert client.get(f"/v1/field-acl/apps/{app_id}/open", params={
        "project_id": project_id, "as_user": "rio",
    }).json()["values"]["salary"] == "180000"
    wrote = client.put(f"/v1/field-acl/apps/{app_id}/record", json={
        "project_id": project_id, "as_user": "ada", "actor": "builder",
        "values": {"title": "Lead"},
    })
    assert wrote.status_code == 200, wrote.text
    assert "salary" not in wrote.text
    assert wrote.json()["values"]["title"] == "Lead"
    rio = client.get(f"/v1/field-acl/apps/{app_id}/open", params={
        "project_id": project_id, "as_user": "rio",
    }).json()
    assert rio["values"]["title"] == "Lead" and rio["values"]["salary"] == "180000"
    # Touch the engine fixture so the schema is applied before the client runs.
    assert eng is not None


def test_routes_declare_a_permission():
    from boss_engine.identity import access_of

    from apps.api.app.routers.gap_1268 import router

    paths = set()
    for route in router.routes:
        assert access_of(route.endpoint) is not None, route.path
        for method in route.methods:
            paths.add((method, route.path))
    assert ("GET", "/v1/field-acl/apps/{app_id}/open") in paths
    assert ("PUT", "/v1/field-acl/apps/{app_id}/record") in paths
    assert ("PUT", "/v1/field-acl/apps/{app_id}/grants") in paths
