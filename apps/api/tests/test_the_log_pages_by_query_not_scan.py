"""The log's performance budget, enforced: queries, not scans.

On dogfood the log held 46,774 rows; `/v1/activity` once took 14.3s, a filtered page classified
up to 50,000 rows in Python to show 50, and the step panel repeated that every five seconds.

Budget (activity-govern spec §8.5), against 50,000 rows seeded inside the 24h window:
  * first page (24h, no count): p95 ≤ 400ms server time, one log statement;
  * filtered pages (level, agent, step): the same budget and statement bound;
  * an unchanged tail poll answers 304 in ≤ 30ms p95;
  * the step panel's poll is ONE bounded log statement (plus the step's own lookup).
Timings are measured in-process through the real router and Postgres (no HTTP hop), which is
the server time the budget names.
"""

from __future__ import annotations

import statistics
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from apps.api.tests import log_seed as seed
from apps.api.tests.log_seed import PROJECT

ROWS = 50_000
RUNS = 15


@contextmanager
def statements():
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", record)


def _p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20)[-1] if len(samples) >= 2 else samples[0]


def _timed(client, params: dict, headers=None, runs: int = RUNS):
    samples, response = [], None
    for _ in range(runs):
        started = time.perf_counter()
        response = client.get("/v1/activity", params=params, headers=headers or {})
        samples.append((time.perf_counter() - started) * 1000)
    return response, _p95(samples)


def _log_statements(seen: list[str]) -> list[str]:
    return [s for s in seen if "UNION ALL" in s and " LIMIT " in s]


def test_the_log_pages_by_query_not_scan(client, engine, capsys):
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        seed.wipe(c)
        written = seed.bulk(c, rows=ROWS, end=now - timedelta(seconds=5),
                            span=timedelta(hours=23))
        wfr = seed.workflow(c)
        seed.step(c, workflow_run_id=wfr, step_id="build", created_at=now - timedelta(hours=1),
                  completed_at=now - timedelta(minutes=30), output={"code_task_run_id":
                                                                      "crun_bulk_7"})
    assert sum(written.values()) == ROWS
    report: dict[str, float] = {}
    base = {"project_id": PROJECT, "range": "24h", "total": "0", "limit": 100,
            "hide_verbose": "1"}
    try:
        # Warm the plan cache and the buffer pool once, as a running console would be.
        client.get("/v1/activity", params=base)
        for name, extra in {
            "first page": {},
            "level=error": {"level": "error"},
            "agent": {"agent": "adversarial_reviewer"},
            "step scope": {"workflow_run_id": wfr, "step_id": "build"},
            "search": {"q": "tests"},
        }.items():
            with statements() as seen:
                response, p95 = _timed(client, {**base, **extra})
            assert response.status_code == 200, response.text
            assert response.json()["items"], name
            report[name] = p95
            per_request = len(seen) / RUNS
            assert len(_log_statements(seen)) == RUNS, (name, "one log statement per page")
            assert per_request <= 6, (name, per_request, seen[:8])
            assert p95 <= 400, (name, p95)

        # The live tail: nothing new → 304, cheaply.
        first = client.get("/v1/activity", params=base)
        etag = first.headers["etag"]
        tail = {**base, "after": first.json()["newest_cursor"]}
        tail_etag = client.get("/v1/activity", params=tail).headers["etag"]
        with statements() as seen:
            response, p95 = _timed(client, tail, headers={"If-None-Match": tail_etag})
        assert response.status_code == 304
        assert not _log_statements(seen), "a 304 never reads a page"
        report["tail 304"] = p95
        assert p95 <= 30, p95
        # And a new row changes the answer.
        with engine.begin() as c:
            seed.event(c, event_type="workflow.step_ready", at=datetime.now(timezone.utc))
        assert client.get("/v1/activity", params=tail,
                          headers={"If-None-Match": tail_etag}).status_code == 200
        assert client.get("/v1/activity", params=base,
                          headers={"If-None-Match": etag}).status_code == 200

        # The step panel: one bounded statement for rows and counts together.
        panel = {"project_id": PROJECT, "workflow_run_id": wfr, "step_id": "build",
                 "summary": "1", "limit": 5}
        with statements() as seen:
            response, p95 = _timed(client, panel)
        body = response.json()
        assert len(body["items"]) <= 5 and set(body["counts"]) == {"error", "warn"}
        assert len(_log_statements(seen)) == RUNS
        report["step panel"] = p95
        assert p95 <= 400, p95

        # The pulse and the facets, reported (spec: ≤ 300ms each, not gating).
        for name, path in (("histogram", "/v1/activity/histogram"),
                           ("facets", "/v1/activity/facets")):
            samples = []
            for _ in range(5):
                started = time.perf_counter()
                assert client.get(path, params={"project_id": PROJECT, "range": "24h"}
                                  ).status_code == 200
                samples.append((time.perf_counter() - started) * 1000)
            report[name] = _p95(samples)
    finally:
        with capsys.disabled():
            print("\nlog performance (p95 ms, 50k rows / 24h):",
                  {k: round(v, 1) for k, v in report.items()})
        with engine.begin() as c:
            seed.wipe(c)
            c.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": PROJECT})
