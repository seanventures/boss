"""When a connection is proved again — the one policy, consulted by every scheduler.

A connection proof is a full engine session (one trivial prompt), so when it is asked matters:
the hourly keep-alive used to re-prove every non-green subscription on a fixed hourly beat,
forever — 391 proof sessions in one month, 314 of them dying at the engine's 420 s review
timeout (audit BOSSAPP-997, finding 5; BOSSAPP-1008). The policy here replaces that beat:

- GREEN IS CACHED. A connection proven healthy is believed until its TTL expires
  (``connection_proof_ttl_hours``, default 6 h). Before that, nothing re-proves it on a timer.
- FAILURE BACKS OFF. A failing connection is retried at 1 h, then 2 h, 4 h … capped at 24 h,
  instead of hourly forever. When the provider itself names a sooner/later moment (a plan
  reset's ``retry_at``), that moment wins over the backoff.
- A PERSON ALWAYS CAN. The console's Re-check (`run_checks` with an explicit target) and a
  fresh connect never consult this schedule; they prove now, and the result resets it.
- LIVE TRAFFIC RESETS IT. A real call that finds the login rejected
  (`model_failures.mark_login(rejected=True)`) clears the schedule, so the next sweep
  re-proves immediately instead of waiting out a stale green TTL.

The schedule is stored on the source (``connection_metadata.reproof``) by
`setup_actions._record_model_check`, read by `subscription_keepalive._reprove` and
`setup_view.stale_targets`, and shown on the Models row (``next_check_at`` /
``next_check_why``), so the console says when the next attempt is and why.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .setup_checks import parse_time

#: A proven-healthy connection is believed this long before the keep-alive asks again.
#: Config: ``BOSS_CONNECTION_PROOF_TTL_HOURS``; 6 h keeps the pre-1008 cadence for green rows.
GREEN_TTL_HOURS = 6

#: A failing connection's first retry waits this long; each further failure doubles it …
BACKOFF_FIRST = timedelta(hours=1)
#: … up to this cap. The daily stale re-check and the console's Re-check still reach a row
#: that is waiting out the cap.
BACKOFF_CAP = timedelta(hours=24)

#: Results that are not the provider's answer — BOSS's own queue or budget said "not now" —
#: neither prove nor disprove the connection, so they leave the schedule untouched.
NOT_AN_ANSWER = ("paused_budget", "job_deferred", "legacy_hold")


def green_ttl(settings: Any) -> timedelta:
    hours = getattr(settings, "connection_proof_ttl_hours", GREEN_TTL_HOURS) or GREEN_TTL_HOURS
    return timedelta(hours=max(1, int(hours)))


def stored(metadata: Any) -> dict[str, Any]:
    """The schedule kept on a source's ``connection_metadata`` (empty when none yet)."""
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    if not isinstance(metadata, dict):
        return {}
    schedule = metadata.get("reproof")
    return schedule if isinstance(schedule, dict) else {}


def after_check(*, ok: bool, state: str, failures_before: int, now: datetime,
                settings: Any = None, retry_at: Any = None) -> Optional[dict[str, Any]]:
    """The schedule to store after one real check, or None to leave the stored one alone.

    ``failures_before`` is the stored consecutive-failure count; ``retry_at`` is the
    provider's own "try again at" when it gave one (a plan reset).
    """
    if state in NOT_AN_ANSWER:
        return None
    if ok:
        ttl = green_ttl(settings)
        return {"failures": 0, "next_at": (now + ttl).isoformat(),
                "why": f"Working. Checked again after {int(ttl.total_seconds() // 3600)} h, "
                       "on Re-check, or when a real call fails."}
    failures = max(0, int(failures_before or 0)) + 1
    wait = min(BACKOFF_FIRST * (2 ** (failures - 1)), BACKOFF_CAP)
    next_at = now + wait
    why = (f"Check failed ({state or 'failed'}) {failures} time{'s' if failures > 1 else ''}; "
           f"backing off. Re-check tries now.")
    said = parse_time(retry_at)
    if said is not None and said > now:
        # The provider named the moment itself (a plan reset): that is when to ask.
        next_at = said
        why = (f"Check failed ({state or 'failed'}); the provider said to try again at "
               f"{said.strftime('%H:%M')} UTC. Re-check tries now.")
    return {"failures": failures, "next_at": next_at.isoformat(), "why": why}


def waiting(entry: dict[str, Any], now: datetime) -> bool:
    """Is this Models row's next scheduled attempt still in the future?"""
    next_at = parse_time(entry.get("next_check_at"))
    return next_at is not None and now < next_at


def entry_due(entry: dict[str, Any], now: datetime, settings: Any = None) -> bool:
    """Should the keep-alive re-prove this Models row now?

    A row with a schedule follows it exactly. A row without one (written before this policy,
    or never checked) keeps the old rule once — non-green or past the green TTL — and the
    check it triggers writes the schedule.
    """
    next_at = parse_time(entry.get("next_check_at"))
    if next_at is not None:
        return now >= next_at
    tone = str((entry.get("pill") or {}).get("tone") or "")
    checked = parse_time(entry.get("checked_at")) if entry.get("checked_at") else None
    return tone != "green" or checked is None or now - checked > green_ttl(settings)
