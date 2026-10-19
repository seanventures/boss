"""A rule's preview in the editor: "would have fired N times in the last 24 h" (R1.13).

The expression is replayed over BOSS's own records for the subject (its scrapes and probes, and
the project's Postgres floor) through THE lifecycle the evaluator uses (app_alerts.lifecycle),
one evaluation a minute. A result over the 1,000-series cap is refused in words, as the
evaluator refuses it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import app_alerts as aa
from . import promql_lite as pq

WINDOW = timedelta(hours=24)


def _replay(present_at, now: datetime, hold_ms: int, step_s: int) -> tuple[int, Optional[str]]:
    """Fires and the final state, through THE lifecycle the evaluator uses."""
    state, active, fired = None, None, 0
    t = now - WINDOW
    while t <= now:
        ms = int(t.timestamp() * 1000)
        nxt = aa.lifecycle(state, active, present_at(ms), ms, hold_ms)
        if nxt == "pending" and state != "pending":
            active = ms
        if nxt == "firing" and state != "firing":
            fired += 1
        state = None if nxt in (None, "resolved") else nxt
        if state is None:
            active = None
        t += timedelta(seconds=step_s)
    return fired, state


def _words(fired: int, state: Optional[str], by: str, peak: int, now_series: int,
           note: str = "") -> dict[str, Any]:
    return {"fired": fired, "series_peak": peak, "series_now": now_series,
            "word": (f"Would have fired {fired} time{'s' if fired != 1 else ''} in the last 24 h"
                     + ("; firing now" if state == "firing" else
                        "; pending now" if state == "pending" else "") + note),
            "evaluated_by": by}


def preview(connection, subject: dict, expr: str, for_: Any = 0, *,
            now: Optional[datetime] = None, step_s: int = 60, prometheus: Any = None,
            scope: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """`prometheus` (the subject's datasource, as the evaluator picks it): the expression is
    replayed over Prometheus' range query, scoped like every query BOSS sends it. Otherwise over
    BOSS's own records. A construct BOSS's evaluator does not know is named in words."""
    now = now or datetime.now(timezone.utc)
    spec = aa.validate_custom({"alert": "Preview", "expr": expr, "for": for_})
    hold_ms = spec["for_seconds"] * 1000
    unsupported = None
    try:
        node = pq.parse(spec["expr"])
    except pq.PromQLUnsupported as exc:
        node, unsupported = None, str(exc)
    if prometheus is not None:
        q = pq.inject_scope(spec["expr"], scope) if scope and node is not None else spec["expr"]
        if scope and node is None:
            raise ValueError(f"BOSS can't scope {unsupported} to this project on a shared "
                             "Prometheus, so it won't send it")
        try:
            rows = prometheus.query_range(q, now - WINDOW, now, step_s)
        except pq.PromQLError as exc:
            raise ValueError(f"Prometheus refused it: {exc}") from None
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Prometheus did not answer ({type(exc).__name__})") from None
        counts: dict[int, int] = {}
        for _labels, values in rows:
            for t, _v in values:
                counts[t // 1000] = counts.get(t // 1000, 0) + 1
        peak = max(counts.values() or [0])
        if peak > aa.MAX_SERIES:
            raise ValueError(f"Returns {peak:,} series; aggregate by fewer labels "
                             f"(at most {aa.MAX_SERIES:,})")
        fired, state = _replay(lambda ms: counts.get(ms // 1000, 0) > 0, now, hold_ms, step_s)
        return _words(fired, state, "Prometheus", peak,
                      counts.get(int(now.timestamp()) // step_s * step_s, 0) if state else 0)
    if node is None:
        return {"fired": None, "series_now": None, "evaluated_by": "BOSS's own records",
                "word": f"BOSS's own evaluator does not do {unsupported}; only Prometheus "
                        "evaluates it, and none answers here, so there is no preview"}
    rng = aa._max_range_ms(spec["expr"])
    store = aa.local_store(connection, subject, now, int(WINDOW.total_seconds() * 1000) + rng)
    peak = 0
    results: dict[int, int] = {}

    def present(ms: int) -> bool:
        nonlocal peak
        try:
            got = pq.instant_vector(node, store, ms)
        except pq.PromQLError as exc:
            raise ValueError(str(exc)) from None
        peak = max(peak, len(got))
        if peak > aa.MAX_SERIES:
            raise ValueError(f"Returns {peak:,} series; aggregate by fewer labels "
                             f"(at most {aa.MAX_SERIES:,})")
        results[ms] = len(got)
        return bool(got)

    fired, state = _replay(present, now, hold_ms, step_s)
    wanted = set()
    try:
        wanted = pq.selected_metrics(spec["expr"]) - store.names()
    except pq.PromQLError:
        pass
    note = (f"; BOSS's own records hold no {', '.join(sorted(wanted))}" if wanted else "")
    return _words(fired, state, "BOSS's own records", peak,
                  results.get(max(results), 0) if results and state else 0, note)


__all__ = ["preview"]
