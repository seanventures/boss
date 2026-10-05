"""The pulse, the facet counts and the rows paged through are three views of one number.

The old chips counted the 200 rows on screen and read as totals (boss-1: ERROR 0 beside 14
dead-letters). Now the sum of the histogram's buckets, the facets' total and the rows a cursor
walk reaches must be equal, for several filters — and each facet ignores its own selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from apps.api.tests import log_seed as seed
from apps.api.tests.log_seed import PROJECT


@pytest.fixture
def log(engine):
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        seed.wipe(c)
        for i in range(30):
            at = now - timedelta(minutes=10 * i + 1)
            seed.turn(c, at=at, level=["verbose", "info", "warn", "error"][i % 4],
                      role=["software_engineer", "qa_engineer"][i % 2])
            seed.llm_call(c, at=at, ok=i % 5 != 0, role="software_engineer")
            seed.app_log(c, at=at, level=["info", "warning", "error"][i % 3],
                         service=["api", "worker"][i % 2])
        seed.deadletter(c, at=now - timedelta(minutes=3))
    yield
    with engine.begin() as c:
        seed.wipe(c)


def _walk(client, params) -> int:
    count, cursor = 0, None
    while True:
        body = client.get("/v1/activity", params={**params, "limit": 50, "total": "0",
                                                  **({"cursor": cursor} if cursor else {})}
                          ).json()
        count += len(body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            return count


@pytest.mark.parametrize("filters", [
    {},
    {"level": "error"},
    {"agent": "software_engineer", "hide_verbose": "1"},
    {"source": "app_log,deadletter", "exclude_level": "info"},
])
def test_buckets_facets_and_rows_agree(client, log, filters):
    params = {"project_id": PROJECT, "range": "24h", **filters}
    histogram = client.get("/v1/activity/histogram", params=params).json()
    facets = client.get("/v1/activity/facets", params=params).json()
    bucket_sum = sum(b["error"] + b["warn"] + b["info"] + b["verbose"]
                     for b in histogram["buckets"])
    walked = _walk(client, params)
    assert bucket_sum == histogram["total"] == facets["total"] == walked, (
        bucket_sum, histogram["total"], facets["total"], walked)
    # Dense: every 15-minute bucket of the 24 hours, empty ones as zeros.
    assert 96 <= len(histogram["buckets"]) <= 98
    assert histogram["bucket_seconds"] == 900


def test_each_facet_ignores_its_own_selection(client, log):
    params = {"project_id": PROJECT, "range": "24h", "level": "error"}
    facets = client.get("/v1/activity/facets", params=params).json()
    # The level facet still counts every level: picking Warn next would show this many.
    assert facets["levels"]["warn"] > 0 and facets["levels"]["info"] > 0
    # Every other facet is narrowed by level=error.
    assert facets["sources"]["deadletter"] == 1
    assert sum(facets["sources"].values()) == facets["total"]
    unfiltered = client.get("/v1/activity/facets",
                            params={"project_id": PROJECT, "range": "24h"}).json()
    assert facets["levels"] == unfiltered["levels"]
    assert {a["role_key"] for a in unfiltered["agents"]} == {"software_engineer", "qa_engineer"}
    assert unfiltered["services"] == {"api": 15, "worker": 15}


def test_kinds_are_counted_for_autocomplete_and_ignore_their_own_selection(client, log):
    """D13: `kind:` suggests the window's real kinds, counted in SQL, most common first."""
    base = {"project_id": PROJECT, "range": "24h"}
    every = client.get("/v1/activity/facets", params=base).json()["kinds"]
    assert every and all(k["kind"] and k["n"] > 0 for k in every)
    assert [k["n"] for k in every] == sorted((k["n"] for k in every), reverse=True)
    picked = every[-1]["kind"]
    narrowed = client.get("/v1/activity/facets", params={**base, "kind": picked}).json()
    assert narrowed["kinds"] == every  # picking a kind never hides the others
    assert narrowed["total"] == every[-1]["n"]
