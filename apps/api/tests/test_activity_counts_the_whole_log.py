"""The Activity log counts the log, not the page, and every row is reachable.

The incident (boss-1 console, 2026-10-02): the level and stream chips counted the newest 200 rows
the page had fetched and read as totals — ERROR 0, Deadletters 0, Jobs 0, Model calls 0 — while
Operations reported "14 failed · 14 dead-letters". And there was no paging: anything past the
newest 200 rows could not be reached without guessing a filter.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

PROJECT_ID = "boss-dogfood"


def _events(engine, prefix: str, kinds: list[str]) -> list[str]:
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    ids = []
    with engine.begin() as c:
        for n, kind in enumerate(kinds):
            event_id = f"evt_{prefix}_{n:04d}"
            ids.append(event_id)
            c.execute(
                text(
                    """
                    INSERT INTO events (id, project_id, event_type, actor, entity_type,
                                        entity_id, data, created_at)
                    VALUES (:id, :p, :k, 'worker', 'job', :e, CAST(:d AS jsonb), :at)
                    """
                ),
                # Two rows share each timestamp, so the cursor has to break ties by id.
                {"id": event_id, "p": PROJECT_ID, "k": kind, "e": f"job_{prefix}_{n}",
                 "d": json.dumps({}), "at": base + timedelta(seconds=n // 2)},
            )
    return ids


def _event_count(engine) -> int:
    with engine.begin() as c:
        return int(c.execute(text("SELECT count(*) FROM events WHERE project_id = :p"),
                             {"p": PROJECT_ID}).scalar_one())


def test_the_chips_count_the_whole_log_not_the_page(client, engine):
    _events(engine, "fail", ["job.failed"] * 3)
    _events(engine, "fine", ["job.succeeded"] * 230)

    page = client.get("/v1/activity", params={"project_id": PROJECT_ID, "limit": 200})
    assert page.status_code == 200, page.text
    assert len(page.json()["items"]) == 200

    facets = client.get("/v1/activity/facets", params={"project_id": PROJECT_ID})
    assert facets.status_code == 200, facets.text
    body = facets.json()
    # The three failures are older than the newest 200 rows, and are still counted.
    assert body["levels"]["error"] >= 3
    assert body["sources"]["event"] == _event_count(engine)
    assert body["levels_exact"] is True
    assert set(body["sources"]) >= {"event", "job", "deadletter", "llm_call", "app_log"}


def test_load_older_reaches_every_row_once(client, engine):
    _events(engine, "page", ["job.succeeded"] * 250)
    seen: list[str] = []
    cursor = None
    for _ in range(20):
        params = {"project_id": PROJECT_ID, "source": "event", "limit": 60}
        if cursor:
            params["cursor"] = cursor
        response = client.get("/v1/activity", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body.get("next_cursor")
        if not cursor:
            break
    assert len(seen) == len(set(seen)), "a row appeared on two pages"
    assert len(seen) == _event_count(engine)


def test_a_cursor_with_a_non_recency_sort_is_refused_with_a_reason(client):
    response = client.get("/v1/activity", params={
        "project_id": PROJECT_ID, "cursor": "2026-09-01T00:00:00+00:00|evt_x", "sort": "kind"})
    assert response.status_code == 422
    assert "newest-first" in response.text
