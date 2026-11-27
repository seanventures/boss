"""BOSSAPP-1197 backend polish: People uncapped last_seen + total paging, and thread+record limits."""
from __future__ import annotations

import inspect

from boss_engine.services import event_intake, intake_guards


def test_connection_people_query_has_no_5000_cap_and_returns_total():
    src = inspect.getsource(event_intake.connection_people)
    code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    assert "PEOPLE_PAGE_MAX * 5" not in code
    assert "LIMIT :n" not in code
    assert 'out["total"] = len(people)' in code


def test_route_limits_supports_thread_and_record_limits_alongside_per_entity():
    chat_limits = intake_guards.route_limits(
        {"limits": {"per_thread": 18, "per_thread_window_seconds": 3600, "per_record": 7, "per_record_window_seconds": 86400}},
        chat=True,
    )
    assert chat_limits["per_thread"] == 18
    assert chat_limits["per_record"] == 7
    assert chat_limits["per_entity"] == 18
    assert chat_limits["per_entity_window_seconds"] == 3600

    record_limits = intake_guards.route_limits(
        {"limits": {"per_thread": 18, "per_thread_window_seconds": 3600, "per_record": 7, "per_record_window_seconds": 86400}},
        chat=False,
    )
    assert record_limits["per_thread"] == 18
    assert record_limits["per_record"] == 7
    assert record_limits["per_entity"] == 7
    assert record_limits["per_entity_window_seconds"] == 86400
