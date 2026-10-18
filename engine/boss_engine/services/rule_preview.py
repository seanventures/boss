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


def preview(connection, subject: dict, expr: str, for_: Any = 0, *,
            now: Optional[datetime] = None, step_s: int = 60) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    spec = aa.validate_custom({"alert": "Preview", "expr": expr, "for": for_})
    try:
        node = pq.parse(spec["expr"])
    except pq.PromQLUnsupported as exc:
        return {"fired": None, "word": f"Only Prometheus evaluates {exc}; no preview here",
                "series_now": None}
    rng = aa._max_range_ms(spec["expr"])
    store = aa.local_store(connection, subject, now, int(WINDOW.total_seconds() * 1000) + rng)
    hold_ms = spec["for_seconds"] * 1000
    state, active, fired, peak = None, None, 0, 0
    t = now - WINDOW
    while t <= now:
        ms = int(t.timestamp() * 1000)
        try:
            got = pq.instant_vector(node, store, ms) if not isinstance(node, str) else []
        except pq.PromQLError as exc:
            raise ValueError(str(exc)) from None
        peak = max(peak, len(got))
        if peak > aa.MAX_SERIES:
            raise ValueError(f"Returns {peak:,} series; aggregate by fewer labels "
                             f"(at most {aa.MAX_SERIES:,})")
        nxt = aa.lifecycle(state, active, bool(got), ms, hold_ms)
        if nxt == "pending" and state != "pending":
            active = ms
        if nxt == "firing" and state != "firing":
            fired += 1
        state = None if nxt in (None, "resolved") else nxt
        if state is None:
            active = None
        t += timedelta(seconds=step_s)
    now_series = peak if state else 0
    return {"fired": fired, "series_peak": peak, "series_now": now_series,
            "word": (f"Would have fired {fired} time{'s' if fired != 1 else ''} in the last 24 h"
                     + ("; firing now" if state == "firing" else "")),
            "evaluated_by": "BOSS's own records"}


__all__ = ["preview"]
