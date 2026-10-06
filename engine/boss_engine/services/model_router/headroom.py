"""Subscription headroom — how much of a subscription's window is left, and what that means.

Three signals, best first (docs/product/MODEL_ROUTER.md, "(c) Source selection"):

  1. what the CLI reports — Claude Code's stream-json `rate_limit_event`: per window
     (`five_hour`, `seven_day`, `seven_day_opus`, ...) a utilisation 0..1, a status and a reset;
  2. limit messages — "You've hit your weekly limit · resets Mon 12:00am", the session, Opus and
     Sonnet limits, the monthly spend limit — with their reset times;
  3. an estimate — the API-equivalent value of included calls in the window over an allowance
     the operator set or BOSS learned the first time the limit was hit.

Everything here is pure: observations arrive as dicts (stored on the entitlement source's
`observed_capacity["windows"]`), and `assess` turns them into a state. The writes live in
`store.py`, fed by telemetry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

PLENTY, TIGHT, NEAR_LIMIT, EXHAUSTED = "plenty", "tight", "near_limit", "exhausted"
STATE_LABELS = {PLENTY: "Plenty", TIGHT: "Tight", NEAR_LIMIT: "Near limit",
                EXHAUSTED: "Exhausted"}

WEEKLY, SESSION, MONTHLY = "seven_day", "five_hour", "monthly"
#: Model-family windows: an Opus limit leaves Sonnet and Haiku running.
MODEL_WINDOWS = {"seven_day_opus": "opus", "seven_day_sonnet": "sonnet"}
WINDOW_SPAN = {WEEKLY: timedelta(days=7), SESSION: timedelta(hours=5)}


def _utc(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        seconds = float(value) / (1000.0 if float(value) > 10_000_000_000 else 1.0)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Window:
    kind: str
    used: float
    resets_at: Optional[datetime] = None
    status: str = "allowed"
    source: str = "cli"

    @property
    def spent(self) -> bool:
        return self.used >= 0.999 or self.status == "rejected"

    def to_dict(self) -> dict[str, Any]:
        return {"used": round(self.used, 4),
                "resets_at": self.resets_at.isoformat() if self.resets_at else None,
                "status": self.status, "source": self.source}


@dataclass(frozen=True)
class Headroom:
    engine: str
    state: str
    #: Fraction of the weekly window left, 0..1; None when nothing is known.
    left: Optional[float] = None
    resets_at: Optional[datetime] = None
    basis: str = "unknown"            # cli | limit | estimate | unknown
    exhausted_models: frozenset[str] = frozenset()
    detail: str = ""
    windows: Mapping[str, Window] = field(default_factory=dict)

    def model_exhausted(self, model: Optional[str]) -> bool:
        name = str(model or "").lower()
        return any(family in name for family in self.exhausted_models)

    def describe(self) -> str:
        if self.state == EXHAUSTED:
            when = f" · resets {self.resets_at:%a %H:%M} UTC" if self.resets_at else ""
            return f"exhausted{when}"
        if self.left is None:
            return "headroom unknown"
        return f"{round(self.left * 100)}% left ({STATE_LABELS[self.state].lower()})"

    def to_dict(self) -> dict[str, Any]:
        return {"engine": self.engine, "state": self.state,
                "left": None if self.left is None else round(self.left, 4),
                "resets_at": self.resets_at.isoformat() if self.resets_at else None,
                "basis": self.basis, "exhausted_models": sorted(self.exhausted_models),
                "detail": self.detail, "label": STATE_LABELS[self.state]}


UNKNOWN = Headroom(engine="", state=PLENTY)


# ── Reading what the CLI reports ───────────────────────────────────────────────────────────────

def windows_from_rate_limit_event(event: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """`{"type": "rate_limit_event", "rate_limit_info": {...}}` → window patches.

    Claude Code's shape has moved between releases, so this reads every form seen: a single
    window (`rateLimitType`, `utilization`, `resetsAt`, `status`) and the per-window map
    (`unifiedWindows` / `windows`). Unknown keys are ignored, never guessed at."""
    info = event.get("rate_limit_info") if isinstance(event.get("rate_limit_info"),
                                                      Mapping) else event
    out: dict[str, dict[str, Any]] = {}

    def one(kind: Any, body: Mapping[str, Any], status: Any = None) -> None:
        kind = str(kind or "").strip()
        if not kind:
            return
        used = body.get("utilization", body.get("used_percentage", body.get("used")))
        try:
            used_f = float(used) if used is not None else None
        except (TypeError, ValueError):
            used_f = None
        if used_f is not None and used_f > 1.0:
            used_f = used_f / 100.0      # a percentage
        status_s = str(body.get("status") or status or "allowed")
        if used_f is None:
            used_f = 1.0 if status_s == "rejected" else None
        if used_f is None and status_s != "allowed_warning":
            return
        resets = _utc(body.get("resetsAt", body.get("resets_at")))
        out[kind] = {"used": max(0.0, min(1.0, used_f if used_f is not None else 0.8)),
                     "resets_at": resets.isoformat() if resets else None,
                     "status": status_s, "source": "cli"}

    if not isinstance(info, Mapping):
        return out
    for key in ("unifiedWindows", "windows"):
        windows = info.get(key)
        if isinstance(windows, Mapping):
            for kind, body in windows.items():
                if isinstance(body, Mapping):
                    one(kind, body, info.get("status"))
    if info.get("rateLimitType"):
        one(info["rateLimitType"], info)
    return out


_LIMIT = re.compile(
    r"hit your (?P<what>session|weekly|opus|sonnet|5-hour|five-hour)\s+limit"
    r"(?:[^\n]*?resets?\s+(?P<when>[^\n(·]+?)\s*(?:\((?P<tz>[A-Za-z_/+\-0-9]+)\))?\s*(?:$|[\n·.]))?",
    re.IGNORECASE)
_MONTHLY = re.compile(r"hit your (?:org's |team's |individual |channel's )?(?:monthly )?spend limit",
                      re.IGNORECASE)
_DAYS = {d: i for i, d in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}
_TIME = re.compile(r"(?:(?P<day>mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+)?"
                   r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?", re.IGNORECASE)


def _next_reset(when: str, tz_name: Optional[str], now: datetime) -> Optional[datetime]:
    """"3:45pm", "Mon 12:00am", "3pm" → the next such moment after `now`."""
    match = _TIME.search(when or "")
    if not match:
        return None
    tz = timezone.utc
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(tz_name)  # type: ignore[assignment]
        except Exception:  # noqa: BLE001 — an unknown zone reads as UTC
            tz = timezone.utc
    hour = int(match.group("h")) % 12 if match.group("ap") else int(match.group("h"))
    if (match.group("ap") or "").lower() == "pm":
        hour += 12
    if hour > 23:
        return None
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=int(match.group("m") or 0), second=0,
                                  microsecond=0)
    if match.group("day"):
        ahead = (_DAYS[match.group("day")[:3].lower()] - local_now.weekday()) % 7
        candidate = candidate + timedelta(days=ahead)
        if candidate <= local_now:
            candidate += timedelta(days=7)
    elif candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def windows_from_limit_message(text: Optional[str], now: Optional[datetime] = None
                               ) -> dict[str, dict[str, Any]]:
    """The CLI's own limit sentence → the window it closed, with its reset when it said one."""
    now = now or datetime.now(timezone.utc)
    out: dict[str, dict[str, Any]] = {}
    for match in _LIMIT.finditer(text or ""):
        what = match.group("what").lower()
        kind = {"session": SESSION, "5-hour": SESSION, "five-hour": SESSION, "weekly": WEEKLY,
                "opus": "seven_day_opus", "sonnet": "seven_day_sonnet"}[what]
        resets = _next_reset(match.group("when") or "", match.group("tz"), now)
        if resets is None:
            resets = now + WINDOW_SPAN.get(kind, timedelta(days=7))
        out[kind] = {"used": 1.0, "resets_at": resets.isoformat(), "status": "rejected",
                     "source": "limit"}
    if _MONTHLY.search(text or ""):
        first = (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                 + timedelta(days=32)).replace(day=1)
        out[MONTHLY] = {"used": 1.0, "resets_at": first.isoformat(), "status": "rejected",
                        "source": "limit"}
    return out


# ── The verdict ────────────────────────────────────────────────────────────────────────────────

def _windows(capacity: Mapping[str, Any], now: datetime) -> dict[str, Window]:
    out: dict[str, Window] = {}
    for kind, body in ((capacity or {}).get("windows") or {}).items():
        if not isinstance(body, Mapping):
            continue
        resets = _utc(body.get("resets_at"))
        if resets is not None and resets <= now:
            continue                     # the window rolled over: what it said no longer holds
        try:
            used = float(body.get("used") or 0.0)
        except (TypeError, ValueError):
            continue
        out[str(kind)] = Window(str(kind), max(0.0, min(1.0, used)), resets,
                                str(body.get("status") or "allowed"),
                                str(body.get("source") or "cli"))
    return out


def state_for(left: Optional[float], thresholds: Mapping[str, float]) -> str:
    if left is None:
        return PLENTY
    if left <= 0.0:
        return EXHAUSTED
    if left < float(thresholds.get("near_limit", 0.15)):
        return NEAR_LIMIT
    if left < float(thresholds.get("tight", 0.40)):
        return TIGHT
    return PLENTY


def assess(engine: str, capacity: Optional[Mapping[str, Any]], *,
           thresholds: Mapping[str, float], used_usd: Optional[float] = None,
           allowance_usd: Optional[float] = None, now: Optional[datetime] = None) -> Headroom:
    """A source's headroom from its stored observations (and the estimate, when no CLI number
    is fresh). Never raises; nothing known reads as `plenty`, because refusing a source on no
    evidence would strand work the source could do."""
    now = now or datetime.now(timezone.utc)
    capacity = capacity or {}
    windows = _windows(capacity, now)
    exhausted_models = frozenset(family for kind, family in MODEL_WINDOWS.items()
                                 if kind in windows and windows[kind].spent)
    # A provider-limit exhaustion (provider_limits.record_call_outcome) still in force.
    try:
        from ..provider_limits import is_available

        limited = not is_available(capacity, now=now)
    except Exception:  # noqa: BLE001
        limited = False
    for kind in (SESSION, WEEKLY, MONTHLY):
        w = windows.get(kind)
        if w is not None and w.spent:
            return Headroom(engine, EXHAUSTED, 0.0, w.resets_at, w.source, exhausted_models,
                            f"{kind.replace('_', ' ')} limit", windows)
    if limited:
        return Headroom(engine, EXHAUSTED, 0.0, _utc(capacity.get("retry_at")), "limit",
                        exhausted_models, str(capacity.get("detail") or "")[:200], windows)
    weekly = windows.get(WEEKLY)
    if weekly is not None:
        left = 1.0 - weekly.used
        state = state_for(left, thresholds)
        if weekly.status == "allowed_warning" and state == PLENTY:
            state = TIGHT
        return Headroom(engine, state, left, weekly.resets_at, weekly.source, exhausted_models,
                        "", windows)
    allowance = allowance_usd or _learned(capacity)
    if used_usd is not None and allowance:
        left = max(0.0, 1.0 - float(used_usd) / float(allowance))
        return Headroom(engine, state_for(left, thresholds), left, None, "estimate",
                        exhausted_models, f"${used_usd:,.0f} of ~${allowance:,.0f} this week",
                        windows)
    session = windows.get(SESSION)
    if session is not None and session.status == "allowed_warning":
        return Headroom(engine, TIGHT, None, session.resets_at, "cli", exhausted_models,
                        "session window warning", windows)
    return Headroom(engine, PLENTY, None, None, "unknown", exhausted_models, "", windows)


def _learned(capacity: Mapping[str, Any]) -> Optional[float]:
    try:
        value = float(capacity.get("learned_weekly_allowance_usd") or 0)
    except (TypeError, ValueError):
        return None
    return value or None
