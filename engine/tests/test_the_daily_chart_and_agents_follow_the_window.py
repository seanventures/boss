"""The daily chart and the agent list follow the same since as job kind.

An absent since keeps the 14-day chart and every role. Today, the last 7 days,
and the last 30 days each carry their own chart, starting on the day that
window starts, and only roles created inside that window. A row earlier that
same day, before the window's instant, stays out. A finished failure is
failed_final or deadlettered.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not URL or "test" not in URL.lower(),
    reason="needs BOSS_TEST_DATABASE_URL naming a disposable database",
)

PROJECT = "p_rc18_report_rest"


def _schema(connection) -> None:
    connection.execute(text(
        """
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          kind TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS decisions (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          question TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          approved_at TIMESTAMPTZ
        );
        CREATE TABLE IF NOT EXISTS cost_receipts (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          skill_or_model_id TEXT NOT NULL,
          estimated_usd_micros BIGINT NOT NULL,
          actual_usd_micros BIGINT,
          equivalent_usd_micros BIGINT,
          included_in_subscription BOOLEAN NOT NULL DEFAULT false,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS llm_calls (
          id TEXT PRIMARY KEY,
          project_id TEXT,
          role TEXT,
          model TEXT,
          prompt_tokens BIGINT NOT NULL DEFAULT 0,
          cache_read_tokens INTEGER NOT NULL DEFAULT 0,
          cache_write_tokens INTEGER NOT NULL DEFAULT 0,
          total_tokens BIGINT NOT NULL DEFAULT 0,
          cost_usd_micros BIGINT NOT NULL DEFAULT 0,
          price_estimated BOOLEAN NOT NULL DEFAULT false,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS roles (
          id TEXT PRIMARY KEY,
          project_id TEXT,
          role_key TEXT,
          display_name TEXT,
          trust_score INTEGER,
          autonomy_level INTEGER,
          successful_runs INTEGER,
          policy_violation_count INTEGER,
          scope_violation_count INTEGER,
          last_reviewed_at TIMESTAMPTZ,
          last_violation_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    ))


def _seed(connection) -> None:
    """One finished failure and one task on the dates the windows disagree about.

    Today is inside every window. Three days is inside the week, the month, and
    the 14-day chart. One second before the week instant is still on that
    week's first calendar day, but it is not inside the week. Eight days is
    inside the month and the 14-day chart only. Twenty days is inside the month
    only. Forty days is outside every window and outside the chart.
    """
    connection.execute(text(
        """
        INSERT INTO jobs (id, project_id, kind, status, created_at, updated_at) VALUES
          ('j_today', :p, 'probe', 'failed_final', date_trunc('day', now()), date_trunc('day', now())),
          ('j_3d', :p, 'probe', 'failed_final', now() - interval '3 days', now() - interval '3 days'),
          ('j_edge', :p, 'probe', 'failed_final',
            now() - interval '7 days' - interval '1 second',
            now() - interval '7 days' - interval '1 second'),
          ('j_8d', :p, 'probe', 'failed_final', now() - interval '8 days', now() - interval '8 days'),
          ('j_20d', :p, 'probe', 'failed_final', now() - interval '20 days', now() - interval '20 days'),
          ('j_40d', :p, 'probe', 'deadlettered', now() - interval '40 days', now() - interval '40 days')
        """
    ), {"p": PROJECT})
    connection.execute(text(
        """
        INSERT INTO tasks (id, project_id, created_at) VALUES
          ('t_today', :p, date_trunc('day', now())),
          ('t_8d', :p, now() - interval '8 days'),
          ('t_20d', :p, now() - interval '20 days'),
          ('t_40d', :p, now() - interval '40 days')
        """
    ), {"p": PROJECT})
    connection.execute(text(
        """
        INSERT INTO roles (
          id, project_id, role_key, display_name, trust_score, autonomy_level,
          successful_runs, policy_violation_count, scope_violation_count, created_at
        ) VALUES
          ('r_today', :p, 'a_today', 'Today', 50, 1, 0, 0, 0, date_trunc('day', now())),
          ('r_3d', :p, 'a_3d', 'Three', 50, 1, 0, 0, 0, now() - interval '3 days'),
          ('r_edge', :p, 'a_edge', 'Edge', 50, 1, 0, 0, 0,
            now() - interval '7 days' - interval '1 second'),
          ('r_8d', :p, 'a_8d', 'Eight', 50, 1, 0, 0, 0, now() - interval '8 days'),
          ('r_20d', :p, 'a_20d', 'Twenty', 50, 1, 0, 0, 0, now() - interval '20 days'),
          ('r_40d', :p, 'a_40d', 'Forty', 50, 1, 0, 0, 0, now() - interval '40 days')
        """
    ), {"p": PROJECT})


def _dates(connection) -> dict[str, str]:
    row = connection.execute(text(
        """
        SELECT
          to_char(date_trunc('day', now()), 'YYYY-MM-DD') AS today,
          to_char(date_trunc('day', now() - interval '3 days'), 'YYYY-MM-DD') AS d3,
          to_char(date_trunc('day', now() - interval '7 days'), 'YYYY-MM-DD') AS week_start,
          to_char(date_trunc('day', now()) - interval '13 days', 'YYYY-MM-DD') AS chart_start,
          to_char(date_trunc('day', now() - interval '8 days'), 'YYYY-MM-DD') AS d8,
          to_char(date_trunc('day', now() - interval '20 days'), 'YYYY-MM-DD') AS d20,
          to_char(date_trunc('day', now() - interval '30 days'), 'YYYY-MM-DD') AS month_start
        """
    )).mappings().one()
    return dict(row)


@pytest.fixture()
def report():
    from boss_engine.config import Settings
    from boss_engine.services.reports import ReportService

    engine = create_engine(URL, future=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        _schema(connection)
        _seed(connection)
        summary = ReportService(Settings()).build_summary(connection, PROJECT)
        yield summary, _dates(connection)
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def _on(chart: list[dict], day: str) -> dict:
    match = [row for row in chart if row["date"] == day]
    assert match, f"{day} missing from {[row['date'] for row in chart]}"
    return match[0]


def test_each_window_chart_starts_at_its_since_and_no_since_keeps_fourteen_days(report):
    summary, dates = report
    today = summary["windows"]["today"]["daily"]
    week = summary["windows"]["week"]["daily"]
    month = summary["windows"]["month"]["daily"]
    chart = summary["daily"]

    assert [row["date"] for row in today] == [dates["today"]]
    assert today[0]["failed"] == 1
    assert today[0]["tasks"] == 1

    assert week[0]["date"] == dates["week_start"]
    assert week[-1]["date"] == dates["today"]
    assert len(week) == 8
    assert _on(week, dates["today"])["failed"] == 1
    assert _on(week, dates["d3"])["failed"] == 1
    assert _on(week, dates["d3"])["tasks"] == 0
    assert sum(row["failed"] for row in week) == 2
    assert sum(row["tasks"] for row in week) == 1
    assert dates["d8"] not in {row["date"] for row in week}

    assert month[0]["date"] == dates["month_start"]
    assert month[-1]["date"] == dates["today"]
    assert len(month) == 31
    assert sum(row["failed"] for row in month) == 5
    assert sum(row["tasks"] for row in month) == 3
    assert _on(month, dates["d20"])["failed"] == 1
    assert _on(month, dates["d20"])["tasks"] == 1

    assert chart[0]["date"] == dates["chart_start"]
    assert chart[-1]["date"] == dates["today"]
    assert len(chart) == 14
    assert sum(row["failed"] for row in chart) == 4
    assert sum(row["tasks"] for row in chart) == 2
    assert dates["d20"] not in {row["date"] for row in chart}
    assert _on(chart, dates["d8"])["failed"] == 1
    assert _on(chart, dates["d8"])["tasks"] == 1


def _keys(rows: list[dict]) -> set[str]:
    return {row["role_key"] for row in rows}


def test_agents_follow_since_and_an_absent_since_lists_every_role(report):
    summary, _dates = report
    assert _keys(summary["windows"]["today"]["agents"]) == {"a_today"}
    assert _keys(summary["windows"]["week"]["agents"]) == {"a_today", "a_3d"}
    assert _keys(summary["windows"]["month"]["agents"]) == {
        "a_today", "a_3d", "a_edge", "a_8d", "a_20d",
    }
    assert _keys(summary["agents"]) == {
        "a_today", "a_3d", "a_edge", "a_8d", "a_20d", "a_40d",
    }
