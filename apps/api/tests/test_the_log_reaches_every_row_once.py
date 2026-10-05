"""Paging the log reaches every row exactly once, in time order; the tail adds only newer rows;
an unchanged log answers 304 and a new row changes the answer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from apps.api.tests import log_seed as seed
from apps.api.tests.log_seed import PROJECT


@pytest.fixture
def mixed_log(engine):
    """137 rows across six streams, with timestamp ties between streams on purpose."""
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    ids: list[str] = []
    with engine.begin() as c:
        seed.wipe(c)
        wfr = seed.workflow(c)
        for i in range(40):
            at = base + timedelta(seconds=i // 2)  # pairs share a timestamp
            ids.append(seed.event(c, event_type="workflow.step_ready", at=at))
            ids.append(seed.turn(c, at=at, level="info"))
        for i in range(20):
            at = base + timedelta(seconds=i)
            ids.append(seed.llm_call(c, at=at))
            ids.append(seed.app_log(c, at=at, project=None if i % 3 == 0 else PROJECT))
        for i in range(17):
            at = base + timedelta(seconds=100 + i)
            ids.append(seed.step(c, workflow_run_id=wfr, step_id=f"s{i}", created_at=at,
                                 completed_at=at))
    yield set(ids)
    with engine.begin() as c:
        seed.wipe(c)


def _walk(client, **params) -> list[dict]:
    seen, cursor = [], None
    for _ in range(100):
        query = {"project_id": PROJECT, "limit": 13, "total": "0", **params}
        if cursor:
            query["cursor"] = cursor
        body = client.get("/v1/activity", params=query).json()
        seen.extend(body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    return seen


def test_a_cursor_walk_returns_every_row_exactly_once_in_time_order(client, mixed_log):
    walked = _walk(client)
    ids = [i["id"] for i in walked]
    assert len(ids) == len(set(ids)), "a row was returned twice"
    assert set(ids) == mixed_log, "a row was never reached"
    keys = [(i["created_at"], i["id"]) for i in walked]
    stamps = [k[0] for k in keys]
    assert stamps == sorted(stamps, reverse=True)
    # Filtered walks are complete too (a filter used to stop at 5,000 rows per source).
    walked_steps = _walk(client, source="workflow_step")
    assert len(walked_steps) == 17


def test_after_returns_only_newer_rows_and_the_etag_tracks_new_rows(client, engine, mixed_log):
    first = client.get("/v1/activity", params={"project_id": PROJECT, "limit": 5,
                                               "total": "0"})
    body = first.json()
    newest, etag = body["newest_cursor"], first.headers["etag"]
    assert newest and etag
    tail = {"project_id": PROJECT, "after": newest, "total": "0"}
    quiet = client.get("/v1/activity", params=tail)
    assert quiet.json()["items"] == [] and quiet.json()["gap"] is False
    assert client.get("/v1/activity", params=tail,
                      headers={"If-None-Match": quiet.headers["etag"]}).status_code == 304
    assert client.get("/v1/activity", params={"project_id": PROJECT, "limit": 5, "total": "0"},
                      headers={"If-None-Match": etag}).status_code == 304
    with engine.begin() as c:
        fresh = seed.event(c, event_type="deploy.started", at=datetime.now(timezone.utc))
    moved = client.get("/v1/activity", params=tail,
                       headers={"If-None-Match": quiet.headers["etag"]})
    assert moved.status_code == 200
    assert [i["id"] for i in moved.json()["items"]] == [fresh]


def test_the_count_is_exact_and_comes_from_the_query(client, mixed_log):
    body = client.get("/v1/activity", params={"project_id": PROJECT, "limit": 1}).json()
    assert body["total"] == len(mixed_log) and body["total_is_exact"] is True
    filtered = client.get("/v1/activity", params={"project_id": PROJECT, "limit": 1,
                                                  "source": "llm_call"}).json()
    assert filtered["total"] == 20 and filtered["total_is_exact"] is True
