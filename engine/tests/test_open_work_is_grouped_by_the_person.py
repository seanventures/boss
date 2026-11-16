"""Open work for one project, grouped by the person on the row.

A person is the row's assignee (or owner_actor, when that is the column the table has).
An agent — `agent:…`, or a role key this project has — is not a person. A row with no
person is the group `none`, not dropped. Another project's rows are not in the count.
The age is how old the oldest still-open item in that group is.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from boss_engine.services import work_items
from boss_engine.services.work_by_person import open_work_by_person

from ._ambient_support import eng, make_project, project, purge  # noqa: F401

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def _stamp(connection, item_id: str, person, when: datetime) -> None:
    connection.execute(text(
        "UPDATE work_items SET assignee = :person, owner_actor = :person, created_at = :t "
        "WHERE id = :id"),
        {"person": person, "t": when, "id": item_id})


def _item(connection, project_id: str, title: str, person, when: datetime, *,
          status: str = "todo"):
    made = work_items.create_item(connection, project_id=project_id, title=title, status="todo")
    _stamp(connection, made["id"], person, when)
    if status != "todo":
        connection.execute(text("UPDATE work_items SET status = :s WHERE id = :id"),
                           {"s": status, "id": made["id"]})
    return made["id"]


def test_open_work_groups_by_the_person_and_the_oldest_open_item(eng, project):
    other = make_project(eng)
    try:
        with eng.begin() as c:
            c.execute(text(
                "INSERT INTO roles (id, project_id, role_key, display_name) "
                "VALUES (:id, :p, 'frontend', 'Frontend')"),
                {"id": f"role_{project}_frontend", "p": project})
            _item(c, project, "Ada's old bug", "person:ada", NOW - timedelta(days=3))
            _item(c, project, "Ada's fresh bug", "person:ada", NOW - timedelta(hours=1),
                  status="active")
            _item(c, project, "Ada already finished", "person:ada", NOW - timedelta(days=30),
                  status="done")
            _item(c, project, "Ada cancelled", "person:ada", NOW - timedelta(days=20),
                  status="cancelled")
            _item(c, project, "Bob is blocked", "person:bob", NOW - timedelta(days=2),
                  status="blocked")
            _item(c, project, "Nobody owns this", None, NOW - timedelta(minutes=10))
            _item(c, project, "Blank owner", "   ", NOW - timedelta(minutes=1))
            _item(c, project, "An agent is working it", "agent:qa", NOW - timedelta(days=4))
            _item(c, project, "The frontend role owns it", "frontend", NOW - timedelta(days=10))
            _item(c, other, "Ada on another project", "person:ada", NOW - timedelta(days=40))
            _item(c, other, "Another project's agent", "agent:qa", NOW - timedelta(days=40))

            rows = open_work_by_person(c, project, now=NOW)
    finally:
        purge(eng, other)

    assert rows == [
        {"person": "person:ada", "count": 2, "oldest_age_seconds": 3 * 86400},
        {"person": "person:bob", "count": 1, "oldest_age_seconds": 2 * 86400},
        {"person": "none", "count": 4, "oldest_age_seconds": 10 * 86400},
    ]
