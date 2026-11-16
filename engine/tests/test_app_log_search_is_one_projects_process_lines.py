"""Search app_logs for one project: a text fragment inside a time window.

These rows are lines BOSS wrote while it worked (the api and the worker). They are not a
customer's deployed app. An empty match is an empty list — it does not say the app was quiet.
No project id is not a search of every project. Another project's rows stay out.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

URL = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    "postgresql+psycopg://boss:boss@127.0.0.1:55924/boss_test_logs",
)
pytestmark = pytest.mark.skipif(
    not URL or "test" not in URL.lower(),
    reason="needs a disposable database whose name contains test",
)

# A fixed clock so the window is a fact of the fixture, not "now".
NOON = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
WINDOW = (NOON, NOON + timedelta(hours=1))


def _said(result) -> str:
    return json.dumps(result, default=str).lower()


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(URL, future=True)
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS app_logs ("
            " id TEXT PRIMARY KEY,"
            " project_id TEXT,"
            " run_id TEXT,"
            " service TEXT,"
            " level TEXT NOT NULL,"
            " logger TEXT NOT NULL,"
            " message TEXT NOT NULL,"
            " created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        ))
    yield eng
    eng.dispose()


@pytest.fixture()
def logs(engine):
    """Insert rows for this test only, and delete those ids afterwards. Never the table."""
    ids: list[str] = []

    def insert(*, project_id, message, at, service="api", level="info",
               logger="boss_engine.services.code_agent", run_id="run_1"):
        row_id = f"alog_{uuid4().hex}"
        ids.append(row_id)
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO app_logs "
                "(id, project_id, run_id, service, level, logger, message, created_at) "
                "VALUES (:id, :p, :r, :s, :lv, :lg, :m, :at)"
            ), {"id": row_id, "p": project_id, "r": run_id, "s": service,
                "lv": level, "lg": logger, "m": message, "at": at})
        return row_id

    yield insert
    if ids:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM app_logs WHERE id = ANY(:ids)"), {"ids": ids})


def _search(engine, **kwargs):
    from boss_engine.services.app_log_search import search

    with engine.begin() as conn:
        return search(conn, **kwargs)


def test_a_fragment_inside_the_window_is_only_that_projects_lines(engine, logs):
    """A later edit that drops the project predicate, the window, or the fragment would
    return the other project's line, the line from yesterday, or the unrelated line."""
    project = f"proj_{uuid4().hex}"
    other = f"proj_{uuid4().hex}"
    inside = "lane Queue Waited 900s for the model"
    logs(project_id=project, message=inside, at=NOON + timedelta(minutes=10))
    logs(project_id=project, message="lane Queue Waited 40s for the model",
         at=NOON + timedelta(minutes=40))
    logs(project_id=project, message="preview refused the port",
         at=NOON + timedelta(minutes=20))
    logs(project_id=project, message="lane Queue Waited 900s yesterday",
         at=NOON - timedelta(minutes=1))
    logs(project_id=other, message="lane Queue Waited 900s for the model",
         at=NOON + timedelta(minutes=10))
    logs(project_id=None, message="lane Queue Waited 900s with no project",
         at=NOON + timedelta(minutes=10))

    result = _search(engine, project_id=project, fragment="queue waited",
                     since=WINDOW[0], until=WINDOW[1])

    messages = [line["message"] for line in result["lines"]]
    assert messages == [
        "lane Queue Waited 900s for the model",
        "lane Queue Waited 40s for the model",
    ]
    assert all(line["project_id"] == project for line in result["lines"])
    said = _said(result)
    assert "boss" in said and "process" in said
    assert "not" in said and "deployed app" in said
    assert "quiet" not in said


def test_an_empty_match_is_an_empty_list_and_does_not_say_the_app_was_quiet(engine, logs):
    """Deleting the empty-list branch and substituting 'the app was quiet' is the bug."""
    project = f"proj_{uuid4().hex}"
    logs(project_id=project, message="lane Queue Waited 900s for the model",
         at=NOON + timedelta(minutes=10))

    result = _search(engine, project_id=project, fragment="no such token",
                     since=WINDOW[0], until=WINDOW[1])

    assert result["lines"] == []
    assert isinstance(result["lines"], list)
    said = _said(result)
    assert "quiet" not in said
    assert "boss" in said and "process" in said
    assert "deployed app" in said


def test_a_query_without_a_project_id_returns_nothing(engine, logs):
    """A missing project id must not become a scan of every project's lines."""
    project = f"proj_{uuid4().hex}"
    secret = f"only-this-project-{uuid4().hex}"
    logs(project_id=project, message=secret, at=NOON + timedelta(minutes=5))

    for missing in (None, "", "   "):
        result = _search(engine, project_id=missing, fragment=secret,
                         since=WINDOW[0], until=WINDOW[1])
        assert result["lines"] == []
        assert secret not in _said(result)


def test_a_blank_fragment_does_not_return_every_line(engine, logs):
    project = f"proj_{uuid4().hex}"
    logs(project_id=project, message="lane Queue Waited 900s for the model",
         at=NOON + timedelta(minutes=10))

    result = _search(engine, project_id=project, fragment="   ",
                     since=WINDOW[0], until=WINDOW[1])

    assert result["lines"] == []


def test_a_percent_sign_is_a_character_not_a_wildcard(engine, logs):
    project = f"proj_{uuid4().hex}"
    logs(project_id=project, message="retained 100% of the batch",
         at=NOON + timedelta(minutes=10))
    logs(project_id=project, message="retained 100X of the batch",
         at=NOON + timedelta(minutes=11))

    result = _search(engine, project_id=project, fragment="100%",
                     since=WINDOW[0], until=WINDOW[1])

    assert [line["message"] for line in result["lines"]] == ["retained 100% of the batch"]


def test_the_window_includes_its_edges(engine, logs):
    project = f"proj_{uuid4().hex}"
    logs(project_id=project, message="at the start", at=WINDOW[0])
    logs(project_id=project, message="at the end", at=WINDOW[1])
    logs(project_id=project, message="just after", at=WINDOW[1] + timedelta(microseconds=1))

    result = _search(engine, project_id=project, fragment="at",
                     since=WINDOW[0], until=WINDOW[1])

    assert [line["message"] for line in result["lines"]] == ["at the start", "at the end"]
