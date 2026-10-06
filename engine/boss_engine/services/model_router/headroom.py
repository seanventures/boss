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

WEEKLY, SESSION, MONTHLY, MINUTE = "seven_day", "five_hour", "monthly", "minute"
#: Model-family windows: an Opus limit leaves Sonnet and Haiku running.
MODEL_WINDOWS = {"seven_day_opus": "opus", "seven_day_sonnet": "sonnet"}
WINDOW_SPAN = {WEEKLY: timedelta(days=7), SESSION: timedelta(hours=5),
               "seven_day_opus": timedelta(days=7), "seven_day_sonnet": timedelta(days=7)}


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

def _fraction(body: Mapping[str, Any]) -> Optional[float]:
    """How much of a window is used, as a fraction 0..1, by the field's own unit:
    `utilization` is a FRACTION (Claude Code's rate_limit_event: 0.86 means 86%), `used_percentage`
    and `used_percent` are PERCENT (the status line: 86 means 86%). A `utilization` above 1 is
    not a fraction and is read as a percent only when it is a plausible one (≤ 100)."""
    for key, scale in (("utilization", 1.0), ("used_percentage", 100.0), ("used_percent", 100.0),
                       ("used", 1.0)):
        if body.get(key) is None:
            continue
        try:
            value = float(body[key])
        except (TypeError, ValueError):
            continue
        if scale == 1.0 and value > 1.0:
            if value > 100.0:
                continue
            value = value / 100.0
        else:
            value = value / scale
        return max(0.0, min(1.0, value))
    return None


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
        used = _fraction(body)
        status_s = str(body.get("status") or status or "allowed")
        if used is None:
            if status_s == "rejected":
                used = 1.0
            elif status_s == "allowed_warning":
                # A warning with no number: past the CLI's warning threshold
                # (`surpassedThreshold`, else its usual 0.75).
                used = _fraction({"utilization": body.get("surpassedThreshold")}) or 0.75
            else:
                return
        resets = _utc(body.get("resetsAt", body.get("resets_at")))
        out[kind] = {"used": used, "resets_at": resets.isoformat() if resets else None,
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


# ── Limit messages: one parser, a corpus test (test_model_router_limits.py) ──────────────────
#
# Every phrasing seen from Claude Code, Codex and the OpenAI-compatible APIs. Each match names
# the WINDOW it closed and, when the message says one, WHEN it reopens.

_ZONE = r"(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+\-]+)*|UTC|GMT)\))?"
_RESET = (r"(?:[^\n]*?(?:resets?|reset at|will reset at|try again (?:at|after))\s+"
          r"(?P<when>[^\n(·∙|]+?)" + _ZONE + r"\s*(?:$|[\n·∙.|]))?")
_WINDOW_WORDS = {
    "session": SESSION, "5-hour": SESSION, "five-hour": SESSION, "5 hour": SESSION,
    "weekly": WEEKLY, "opus": "seven_day_opus", "opus weekly": "seven_day_opus",
    "sonnet": "seven_day_sonnet", "sonnet weekly": "seven_day_sonnet",
}
_LIMIT_PATTERNS = (
    # "You've hit your weekly limit · resets Mon 12:00am", "…your Opus limit…", "…your limit…"
    re.compile(r"hit your (?:(?P<what>session|5-hour|five-hour|weekly|opus|sonnet)\s+)?limit"
               + _RESET, re.IGNORECASE),
    # "Weekly limit reached ∙ resets Oct 13, 10am", "Opus weekly limit reached ∙ …"
    re.compile(r"(?P<what>opus weekly|sonnet weekly|session|5-hour|five-hour|5 hour|weekly|opus|"
               r"sonnet)\s+limit reached" + _RESET, re.IGNORECASE),
    # "Claude usage limit reached. Your limit will reset at 3pm (America/Chicago)."
    re.compile(r"usage limit reached(?!\|)" + _RESET, re.IGNORECASE),
    # Codex: "You've hit your usage limit … try again at Sep 22nd, 2026 10:06 PM."
    re.compile(r"hit your usage limit" + _RESET, re.IGNORECASE),
)
_EPOCH = re.compile(r"usage limit reached\|(?P<epoch>\d{9,13})", re.IGNORECASE)
_MONTHLY = re.compile(r"hit your (?:org's |team's |individual |channel's )?(?:monthly )?spend "
                      r"limit|(?:exceeded your current quota|insufficient_quota|credit balance "
                      r"is too low|out of credits)", re.IGNORECASE)
_RATE = re.compile(r"\b429\b|rate[_ ]limit(?:_error| reached| exceeded)|too many requests",
                   re.IGNORECASE)
_RETRY_SECONDS = re.compile(r"(?:retry[- ]after[:= ]\s*|try again in\s*)(?P<n>\d+(?:\.\d+)?)"
                            r"\s*(?P<unit>ms|s|sec|seconds|m|min|minutes)?", re.IGNORECASE)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_DAYS = {d: i for i, d in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}
_WHEN = re.compile(
    r"^(?:(?P<day>mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?"
    r"(?:(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<date>\d{1,2})(?:st|nd|rd|th)?,?\s*(?:(?P<year>\d{4}),?\s*)?(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?\b",
    re.IGNORECASE)


def _zone(tz_name: Optional[str]):
    if not tz_name or tz_name.upper() in ("UTC", "GMT"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — an unknown zone reads as UTC
        return timezone.utc


def _next_reset(when: str, tz_name: Optional[str], now: datetime) -> Optional[datetime]:
    """"3:45pm", "Mon 12:00am", "Oct 13 at 10am", "Oct 13, 10am", "Sep 22nd, 2026 10:06 PM",
    an ISO time → the moment it names, the next one after `now` when the date is implied."""
    text = (when or "").strip()
    iso = re.match(r"\d{4}-\d{2}-\d{2}[T ][\d:]{5,8}", text)
    if iso:
        found = _utc(iso.group(0).replace(" ", "T"))
        return found
    match = _WHEN.match(text)
    if not match:
        return None
    tz = _zone(tz_name)
    hour = int(match.group("h"))
    ap = (match.group("ap") or "").lower()
    if ap:
        hour = hour % 12 + (12 if ap == "pm" else 0)
    if hour > 23:
        return None
    minute = int(match.group("m") or 0)
    local_now = now.astimezone(tz)
    if match.group("month"):
        month = _MONTHS[match.group("month")[:3].lower()]
        year = int(match.group("year") or local_now.year)
        try:
            candidate = local_now.replace(year=year, month=month, day=int(match.group("date")),
                                          hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
        if not match.group("year") and candidate < local_now - timedelta(days=1):
            candidate = candidate.replace(year=year + 1)
        return candidate.astimezone(timezone.utc)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if match.group("day"):
        ahead = (_DAYS[match.group("day")[:3].lower()] - local_now.weekday()) % 7
        candidate = candidate + timedelta(days=ahead)
        if candidate <= local_now:
            candidate += timedelta(days=7)
    elif candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def retry_after_seconds(text: Optional[str]) -> Optional[float]:
    match = _RETRY_SECONDS.search(text or "")
    if not match:
        return None
    n = float(match.group("n"))
    unit = (match.group("unit") or "s").lower()
    return n / 1000.0 if unit == "ms" else n * 60 if unit.startswith("m") and unit != "ms" else n


def windows_from_limit_message(text: Optional[str], now: Optional[datetime] = None
                               ) -> dict[str, dict[str, Any]]:
    """The provider's own limit sentence → the window it closed, with its reset when it said
    one. Empty for anything that is not a capacity limit (a crash, a bad prompt)."""
    now = now or datetime.now(timezone.utc)
    body = str(text or "")
    out: dict[str, dict[str, Any]] = {}

    def close(kind: str, resets: Optional[datetime], source: str = "limit") -> None:
        if resets is None:
            resets = now + WINDOW_SPAN.get(kind, timedelta(hours=5))
        out[kind] = {"used": 1.0, "resets_at": resets.isoformat(), "status": "rejected",
                     "source": source}

    for pattern in _LIMIT_PATTERNS:
        for match in pattern.finditer(body):
            groups = match.groupdict()
            what = (groups.get("what") or "").lower()
            resets = _next_reset(groups.get("when") or "", groups.get("tz"), now) \
                if groups.get("when") else None
            if what:
                kind = _WINDOW_WORDS.get(what, WEEKLY)
            elif resets is not None and resets - now > timedelta(hours=5, minutes=5):
                kind = WEEKLY            # "your limit" that resets days away is the week
            else:
                kind = SESSION           # "your limit" / "usage limit reached": the 5-hour one
            close(kind, resets)
    for match in _EPOCH.finditer(body):
        close(SESSION, _utc(int(match.group("epoch"))))
    if _MONTHLY.search(body):
        first = (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                 + timedelta(days=32)).replace(day=1)
        close(MONTHLY, first)
    if not out and _RATE.search(body):
        # A per-minute rate limit (429, "Rate limit reached for gpt-4o … try again in 20s"): a
        # short window — the provider is fine, this minute is spent.
        seconds = retry_after_seconds(body) or 60.0
        close(MINUTE, now + timedelta(seconds=min(max(seconds, 1.0), 3600.0)), "rate")
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


#: How far back over a threshold headroom must come before its state relaxes (hysteresis):
#: near limit is entered under 15% left and left only at 20%, so a source sitting at 15% does
#: not flap between Claude and OpenRouter boundary by boundary.
HYSTERESIS = 0.05


def state_for(left: Optional[float], thresholds: Mapping[str, float],
              previous: Optional[str] = None) -> str:
    if left is None:
        return PLENTY
    if left <= 0.0:
        return EXHAUSTED
    near = float(thresholds.get("near_limit", 0.15))
    tight = float(thresholds.get("tight", 0.40))
    if left < near or (previous in (NEAR_LIMIT, EXHAUSTED) and left < near + HYSTERESIS):
        return NEAR_LIMIT
    if left < tight or (previous in (TIGHT, NEAR_LIMIT, EXHAUSTED)
                        and left < tight + HYSTERESIS):
        return TIGHT
    return PLENTY


def assess(engine: str, capacity: Optional[Mapping[str, Any]], *,
           thresholds: Mapping[str, float], used_usd: Optional[float] = None,
           allowance_usd: Optional[float] = None, now: Optional[datetime] = None,
           previous: Optional[str] = None) -> Headroom:
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
    for kind in (MINUTE, SESSION, WEEKLY, MONTHLY):
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
        state = state_for(left, thresholds, previous)
        if weekly.status == "allowed_warning" and state == PLENTY:
            state = TIGHT
        return Headroom(engine, state, left, weekly.resets_at, weekly.source, exhausted_models,
                        "", windows)
    allowance = allowance_usd or _learned(capacity)
    if used_usd is not None and allowance:
        left = max(0.0, 1.0 - float(used_usd) / float(allowance))
        return Headroom(engine, state_for(left, thresholds, previous), left, None, "estimate",
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
