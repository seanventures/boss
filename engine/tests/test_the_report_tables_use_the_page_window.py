"""The Reports toggle is today, the last 7 days, or the last 30 days.

Headline counts already use that window. Job kind, model cost, the model-spend
ledger, and decision latency did not: they counted every row the project had
ever written. A finished failure is a job the worker has finished failing,
`failed_final` or `deadlettered`. `failed_retryable` is still scheduled.
`cancelled` is not a failure. `failed_terminal` is not a status the worker writes.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not URL or "test" not in URL.lower(),
    reason="needs BOSS_TEST_DATABASE_URL naming a disposable database",
)

PROJECT = "p_rc14_report_window"


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
    """Rows at midnight today, 3 days, 8 days, and 40 days ago.

    Midnight is inside today, the last 7 days, and the last 30 days. Three days
    is inside the week and the month only. Eight days is inside the month only.
    Forty days is outside every window the page offers.
    """
    connection.execute(text(
        """
        INSERT INTO jobs (id, project_id, kind, status, created_at, updated_at) VALUES
          ('j_ok', :p, 'probe', 'succeeded', date_trunc('day', now()), date_trunc('day', now())),
          ('j_final', :p, 'probe', 'failed_final', date_trunc('day', now()), date_trunc('day', now())),
          ('j_dlq', :p, 'probe', 'deadlettered', date_trunc('day', now()), date_trunc('day', now())),
          ('j_retry', :p, 'probe', 'failed_retryable', date_trunc('day', now()), date_trunc('day', now())),
          ('j_cancel', :p, 'probe', 'cancelled', date_trunc('day', now()), date_trunc('day', now())),
          ('j_invented', :p, 'probe', 'failed_terminal', date_trunc('day', now()), date_trunc('day', now())),
          ('j_3d', :p, 'probe', 'deadlettered', now() - interval '3 days', now() - interval '3 days'),
          ('j_8d', :p, 'probe', 'deadlettered', now() - interval '8 days', now() - interval '8 days'),
          ('j_40d', :p, 'probe', 'deadlettered', now() - interval '40 days', now() - interval '40 days')
        """
    ), {"p": PROJECT})
    connection.execute(text(
        """
        INSERT INTO cost_receipts (
          id, project_id, skill_or_model_id, estimated_usd_micros, actual_usd_micros, created_at
        ) VALUES
          ('cr_today', :p, 'today-model', 1000, 1000, date_trunc('day', now())),
          ('cr_3d', :p, 'week-model', 700, 700, now() - interval '3 days'),
          ('cr_8d', :p, 'month-model', 2500, 2500, now() - interval '8 days'),
          ('cr_40d', :p, 'old-model', 5000, 5000, now() - interval '40 days')
        """
    ), {"p": PROJECT})
    connection.execute(text(
        """
        INSERT INTO llm_calls (
          id, project_id, role, model, prompt_tokens, cache_read_tokens,
          cost_usd_micros, created_at
        ) VALUES
          ('ll_today', :p, 'qa', 'today-model', 4, 6, 100, date_trunc('day', now())),
          ('ll_3d', :p, 'engineer', 'week-model', 2, 2, 50, now() - interval '3 days'),
          ('ll_8d', :p, 'engineer', 'month-model', 10, 0, 300, now() - interval '8 days'),
          ('ll_40d', :p, 'qa', 'old-model', 40, 60, 900, now() - interval '40 days')
        """
    ), {"p": PROJECT})
    connection.execute(text(
        """
        INSERT INTO decisions (id, project_id, question, status, created_at, approved_at) VALUES
          ('d_today', :p, 'ship today', 'approved',
            date_trunc('day', now()), date_trunc('day', now()) + interval '600 seconds'),
          ('d_open', :p, 'still open', 'pending', date_trunc('day', now()), NULL),
          ('d_3d', :p, 'ship this week', 'approved',
            now() - interval '3 days', now() - interval '3 days' + interval '1 hour'),
          ('d_8d', :p, 'ship this month', 'approved',
            now() - interval '8 days', now() - interval '8 days' + interval '2 hours'),
          ('d_40d', :p, 'ship long ago', 'approved',
            now() - interval '40 days', now() - interval '40 days' + interval '1 hour')
        """
    ), {"p": PROJECT})


@pytest.fixture()
def summary():
    from boss_engine.config import Settings
    from boss_engine.services.reports import ReportService

    engine = create_engine(URL, future=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        _schema(connection)
        _seed(connection)
        yield ReportService(Settings()).build_summary(connection, PROJECT)
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def _kind(window: dict) -> dict:
    rows = window["by_kind"]
    assert [row["kind"] for row in rows] == ["probe"]
    return rows[0]


def _models(window: dict) -> dict[str, int]:
    return {row["model"]: row["spend_usd_micros"] for row in window["by_model"]}


def _roles(window: dict) -> dict[str, int]:
    return {row["role"]: row["spend_usd_micros"] for row in window["llm_spend"]["by_role"]}


def test_the_tables_use_the_headline_window_and_only_finished_failures(summary):
    today, week, month = (summary["windows"][name] for name in ("today", "week", "month"))

    assert "by_kind" in today, "job kinds must sit on the same window as the headline counts"
    assert "by_model" in today
    assert "llm_spend" in today
    assert "decision_latency" in today

    # Six jobs land today. Two are finished failures. A retry is still scheduled,
    # a cancel is not a failure, and failed_terminal is not a status the worker writes.
    assert (today["jobs"], today["jobs_succeeded"], today["jobs_failed"]) == (6, 1, 2)
    assert (week["jobs"], week["jobs_failed"]) == (7, 3)
    assert (month["jobs"], month["jobs_failed"]) == (8, 4)
    assert (_kind(today)["total"], _kind(today)["succeeded"], _kind(today)["failed"]) == (6, 1, 2)
    assert (_kind(week)["failed"], _kind(month)["failed"]) == (3, 4)
    assert summary["daily"][-1]["failed"] == 2

    assert _models(today) == {"today-model": 1000}
    assert _models(week) == {"today-model": 1000, "week-model": 700}
    assert _models(month) == {"today-model": 1000, "week-model": 700, "month-model": 2500}
    assert today["spend_usd_micros"] == 1000
    assert week["spend_usd_micros"] == 1700
    assert month["spend_usd_micros"] == 4200

    assert _roles(today) == {"qa": 100}
    assert _roles(week) == {"qa": 100, "engineer": 50}
    assert _roles(month) == {"engineer": 350, "qa": 100}
    assert today["llm_spend"]["cache"]["cache_read_tokens"] == 6
    assert today["llm_spend"]["cache"]["cache_hit_ratio"] == 0.6
    assert week["llm_spend"]["cache"]["cache_read_tokens"] == 8
    assert {row["model"] for row in today["llm_spend"]["by_model"]} == {"today-model"}

    # Latency follows created_at, the same column the headline decision count uses.
    # An unapproved decision counts in the headline and not in the latency.
    assert (today["decisions"], today["decisions_approved"]) == (2, 1)
    assert today["decision_latency"] == {"count": 1, "avg_seconds": 600.0}
    assert week["decision_latency"] == {"count": 2, "avg_seconds": 2100.0}
    assert month["decision_latency"] == {"count": 3, "avg_seconds": 3800.0}

    # The flat keys are what an older reader still loads. They are the week window,
    # which is the tab the page opens on, not an all-time total.
    assert summary["by_kind"] == week["by_kind"]
    assert summary["by_model"] == week["by_model"]
    assert summary["llm_spend"] == week["llm_spend"]
    assert summary["decision_latency"] == week["decision_latency"]

    ui = Path(__file__).resolve().parents[2] / "apps" / "web" / "app" / "reports-ui.tsx"
    assert "all time" not in ui.read_text(encoding="utf-8")
