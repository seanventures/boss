"""Pasting a second GitHub token with no connection_id must ADD an account to the project, not
silently repoint the one connection it already has (MR Part 2, change 1)."""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import setup_actions
from boss_engine.services.setup_checks import HttpAnswer

from ._multirepo_support import make_engine, make_world


def _fake_http(login: str):
    def http(method, url, *, headers=None, json_body=None, timeout=15.0):
        if url.endswith("/user"):
            return HttpAnswer(status=200, headers={}, body={"login": login})
        if "/user/repos" in url:
            return HttpAnswer(status=200, headers={}, body=[])
        return HttpAnswer(status=404, headers={}, body={})
    return http


def _connection_labels(engine, project_id: str) -> list[str]:
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT connection_label FROM github_connections WHERE project_id = :p "
                 "ORDER BY created_at"), {"p": project_id}).scalars().all()
    return list(rows)


def test_p0_pasting_a_token_without_connection_id_adds_a_second_account():
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    settings = get_settings()
    before = _connection_labels(engine, pid)
    assert len(before) == 1  # make_world seeds one connection, labelled "token"

    with engine.begin() as conn:
        result = setup_actions.repository_token(
            conn, settings, pid, "a-fresh-token", accept_database_fallback=True, actor="test",
            http=_fake_http("acme"))

    after = _connection_labels(engine, pid)
    assert len(after) == 2, "a second row must be created, not a repoint of the first"
    assert "acme" in after
    assert result["connection_id"] not in (None, "")

    # Pasting again for the SAME account login, still with no connection_id, adds a THIRD
    # connection whose label is deduped against the project's existing labels.
    with engine.begin() as conn:
        setup_actions.repository_token(
            conn, settings, pid, "another-fresh-token", accept_database_fallback=True,
            actor="test", http=_fake_http("acme"))
    dedup = _connection_labels(engine, pid)
    assert len(dedup) == 3
    assert dedup.count("acme") == 1
    assert "acme 2" in dedup


def test_p1_pasting_a_token_with_connection_id_still_repoints_in_place():
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    settings = get_settings()
    existing_id = world["connections"][pid]
    before = _connection_labels(engine, pid)
    assert len(before) == 1

    with engine.begin() as conn:
        result = setup_actions.repository_token(
            conn, settings, pid, "replacement-token", connection_id=existing_id,
            accept_database_fallback=True, actor="test", http=_fake_http("acme"))

    after = _connection_labels(engine, pid)
    assert len(after) == 1, "a connection_id must repoint the existing row, never add one"
    assert result["connection_id"] == existing_id
