"""A run's steps in the order its workflow declares them.

A run's step rows are created together, in one statement, so `created_at` ties for all of them
and `ORDER BY created_at` returns them in whatever order Postgres likes. D2: the incident card
listed Incident Response as "Investigate, Build, CI, Run report, Deploy, Verify, Write-up,
approve" although the person approves before the deploy. Read a run's steps through here."""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from sqlalchemy import text as sql


def declared_order(connection, workflow_run_id: str) -> dict[str, int]:
    """{step_id: position} from the run's pinned workflow version; {} when it has none."""
    ids = connection.execute(sql(
        "SELECT jsonb_path_query_array(v.spec, '$.steps[*].id') FROM workflow_runs r "
        "JOIN workflow_versions v ON v.id = r.workflow_version_id WHERE r.id = :r"),
        {"r": workflow_run_id}).scalar()
    return {str(s): i for i, s in enumerate(ids or [])}


def in_declared_order(connection, workflow_run_id: str,
                      rows: Iterable[Mapping[str, Any]]) -> list[Any]:
    """`rows` (each with a step_id) sorted by the declared order; undeclared steps keep their
    relative order after the declared ones. Stable, so iterations of one step stay in order."""
    rows = list(rows)
    order = declared_order(connection, workflow_run_id)
    last = len(order)
    return [r for _, r in sorted(enumerate(rows),
                                 key=lambda p: (order.get(str(p[1]["step_id"]), last), p[0]))]
