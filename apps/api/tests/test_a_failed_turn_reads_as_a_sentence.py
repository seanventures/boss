"""A turn that fails says so in one sentence — never the database's diagnostics.

shared-server's dock showed the operator: "BOSS could not complete that turn: IntegrityError:
(psycopg.errors.UniqueViolation) duplicate key value violates unique constraint … [SQL: INSERT
INTO agent_threads (…". Every agent-turn surface now goes through one explainer
(model_failures.explain_turn_failure): the visible `detail` is a sentence, the technical line
travels separately for a collapsed Details, and the traceback goes to the server log.
"""
from __future__ import annotations

from sqlalchemy import create_engine, text

PROJECT = "boss-dogfood"
#: What must never reach the visible message.
RAW = ("[SQL", "INSERT INTO", "psycopg", "IntegrityError", "UniqueViolation", "Traceback")


def _a_real_database_error(engine):
    """Raise exactly what a stale unique index raises: SQLAlchemy's IntegrityError with
    psycopg's UniqueViolation underneath and the statement in its text."""
    with create_engine(engine.url).begin() as c:
        c.execute(text("CREATE TEMP TABLE turn_probe (k text PRIMARY KEY)"))
        c.execute(text("INSERT INTO turn_probe (k) VALUES ('role:Program Manager'), "
                       "('role:Program Manager')"))


def test_a_database_error_in_a_turn_is_a_short_sentence(client, engine, monkeypatch, caplog):
    from apps.api.app import main as api_main

    def route_turn(*_args, **_kwargs):
        _a_real_database_error(engine)

    monkeypatch.setattr(api_main.orchestration_agent.router, "route_turn", route_turn)
    said = client.post("/v1/engine/agent/chat", json={
        "project_id": PROJECT, "actor": "executive_console", "role": "Program Manager",
        "message": "What is on your plate?"})

    assert said.status_code == 500
    body = said.json()
    visible = body["detail"]
    assert visible.startswith("BOSS couldn't save this conversation."), visible
    assert "Try again" in visible
    assert not [raw for raw in RAW if raw in visible], visible
    assert len(visible) < 160
    # The technical line exists — for a collapsed Details — and carries no statement.
    technical = body["failure"]["technical"]
    assert "duplicate key value violates unique constraint" in technical
    assert "[SQL" not in technical and "INSERT INTO" not in technical
    assert body["failure"]["kind"] == "storage" and body["failure"]["retry"] is True
    # The whole story is in the server log.
    assert any("agent chat failed" in r.getMessage() for r in caplog.records)


def test_any_other_failure_is_a_sentence_too(client, monkeypatch):
    from apps.api.app import main as api_main

    def route_turn(*_args, **_kwargs):
        raise KeyError("pending_plan_summary")

    monkeypatch.setattr(api_main.orchestration_agent.router, "route_turn", route_turn)
    said = client.post("/v1/engine/agent/chat", json={
        "project_id": PROJECT, "actor": "executive_console", "message": "Hello?"})
    assert said.status_code == 500
    body = said.json()
    assert body["detail"].startswith("BOSS couldn't finish this turn."), body
    assert "KeyError" not in body["detail"] and "pending_plan_summary" not in body["detail"]
    assert body["failure"]["technical"] == "KeyError: 'pending_plan_summary'"
