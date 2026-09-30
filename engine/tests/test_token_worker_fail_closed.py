"""Fail-closed empty payloads when token/worker tables are missing.

Live dogfood 2026-08-13 returned 500 UndefinedTable on
token_budget_policies and worker_autoscaling_*. Reads must return
honest empty-200 shapes and must never invent consumption numbers.
"""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy.exc import ProgrammingError

from boss_engine.services import token_efficiency as te
from boss_engine.services.worker_autoscaling import WorkerAutoscalingService


class _UndefinedTable(Exception):
    """Stand-in for psycopg.errors.UndefinedTable (name is what we match)."""


_UndefinedTable.__name__ = "UndefinedTable"


class _Nested:
    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def __enter__(self) -> "_Nested":
        return self

    def __exit__(self, *args) -> bool:
        return False


class _MissingRelationConnection:
    """Every execute() raises UndefinedTable — the live 500 shape."""

    def __init__(self, relation: str) -> None:
        self.relation = relation

    def begin_nested(self):
        return _Nested()

    @contextmanager
    def begin(self):
        yield self

    def execute(self, *args, **kwargs):
        orig = _UndefinedTable(f'relation "{self.relation}" does not exist')
        raise ProgrammingError("SELECT 1", {}, orig)


class _Settings:
    infra_real_provision_allowed = False


def test_list_policies_missing_table_returns_defaults_not_raise():
    conn = _MissingRelationConnection("token_budget_policies")
    out = te.list_policies(conn, "boss-dogfood")
    assert out["project_id"] == "boss-dogfood"
    assert out["schema_ready"] is False
    assert out["configured"] is False
    assert out["count"] >= 1
    assert all(p["source"] == "default" for p in out["policies"])
    assert all(p["id"] is None for p in out["policies"])


def test_consumption_missing_tables_zeros_never_invented():
    conn = _MissingRelationConnection("token_budget_policies")
    out = te.build_consumption(conn, "boss-dogfood", window_hours=24)
    assert out["calls"] == 0
    assert out["total_tokens"] == 0
    assert out["prompt_tokens"] == 0
    assert out["completion_tokens"] == 0
    assert out["cost_usd_micros"] == 0
    assert out["failed_burn_usd_micros"] == 0
    assert out["cache_ratio"] is None
    assert out["series"] == []
    assert out["by_stage"] == []
    assert out["alerts"] == []
    assert out["schema_ready"] is False
    # Policy caps may be documented defaults; utilization stays unknown.
    assert out["policy"]["token_utilization"] is None
    assert out["policy"]["cost_utilization"] is None


def test_scorecard_missing_llm_calls_is_empty_n_a():
    conn = _MissingRelationConnection("llm_calls")
    out = te.build_scorecard(conn, "boss-dogfood")
    assert out["calls"] == 0
    assert out["total_tokens"] == 0
    assert out["cost_usd_micros"] == 0
    assert out["cache_ratio"] is None
    assert out["grades"]["cache_ratio"] == "n/a"
    assert out["grades"]["failed_burn"] == "n/a"
    assert out["by_stage"] == []


def test_ensure_tables_returns_false_when_ddl_cannot_run():
    conn = _MissingRelationConnection("token_budget_policies")
    assert te.ensure_tables(conn) is False


def test_autoscaling_reads_missing_tables_empty_not_raise():
    svc = WorkerAutoscalingService(_Settings())
    conn = _MissingRelationConnection("worker_autoscaling_policies")
    policies = svc.list_policies(conn, "boss-dogfood")
    assert policies == []
    history = svc.history(conn, "boss-dogfood")
    assert history == []
    status = svc.status(conn, "boss-dogfood")
    assert status["project_id"] == "boss-dogfood"
    assert status["schema_ready"] is False
    assert status["configured"] is False
    assert status["pools"] == []
    assert status["queue_depth"] == 0
    assert status["leased_jobs"] == 0
    assert status["running_jobs"] == 0
    # Never invent a live worker count when unconfigured.
    assert status.get("instance_counts") in (None, {}, {"active": 0})


def test_is_missing_relation_matches_undefined_table():
    orig = _UndefinedTable('relation "token_budget_policies" does not exist')
    exc = ProgrammingError("SELECT 1", {}, orig)
    assert te.is_missing_relation(exc) is True
    assert te.is_missing_relation(ValueError("nope")) is False
