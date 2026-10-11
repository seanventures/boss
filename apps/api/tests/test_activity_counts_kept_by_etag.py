"""The Activity histogram and facets are answered from their ETag when it has not changed.

Console latency, 2026-10-10: both aggregations took 210-290 ms on the laptop install (p95 over
300 ms) and every open Activity view asked for both every 5 s. Their ETag is already the promise
that nothing changed (filters, minute, newest row of every stream), so a second reader under the
same ETag is answered from memory — exact by construction, bounded in size and age.
"""
from __future__ import annotations

import inspect

from apps.api.app.routers import activity


def setup_function() -> None:
    activity._COUNTS_KEPT.clear()


def test_an_answer_is_kept_under_its_etag_and_only_that_etag() -> None:
    activity._keep_counts('"a"', {"total": 3})
    assert activity._kept_counts('"a"') == {"total": 3}
    assert activity._kept_counts('"b"') is None


def test_a_kept_answer_expires(monkeypatch) -> None:
    import time

    activity._keep_counts('"a"', {"total": 3})
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + activity._COUNTS_TTL_S + 1)
    assert activity._kept_counts('"a"') is None


def test_the_store_is_bounded() -> None:
    for i in range(activity._COUNTS_MAX * 2):
        activity._keep_counts(f'"{i}"', {"i": i})
    assert len(activity._COUNTS_KEPT) <= activity._COUNTS_MAX
    assert activity._kept_counts(f'"{activity._COUNTS_MAX * 2 - 1}"') == {"i": activity._COUNTS_MAX * 2 - 1}


def test_both_routes_read_the_store_before_counting() -> None:
    for fn, count in ((activity.activity_histogram, "service.histogram("),
                      (activity.activity_facets, "service.facets(")):
        src = inspect.getsource(fn)
        assert src.index("_not_modified(") < src.index("_kept_counts(etag)") < src.index(count)
        assert "_keep_counts(etag, body)" in src
