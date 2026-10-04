"""What a provider's quota is doing, as a governed fact rather than a log line.

WHY THIS EXISTS. A codex subscription hit its weekly cap mid-run. Every role that touched it
failed in about two seconds, the panel stalled for fifteen minutes, and a build filled with red
crosses — and the sentence that explained all of it,

    You've hit your usage limit ... try again at Sep 22nd, 2026 10:06 PM.

reached no screen. It was in a container log, truncated out of the step's error by a slice that
kept the CLI's banner instead. The operator spent a day on symptoms and said, correctly, "I
never saw that error at all — I could have switched to a different model."

A platform that governs AI labour has to treat "this provider is out of capacity until
Monday" as a fact it records and shows, not an incident someone reconstructs afterwards. The
information is already in the failure; nothing was carrying it.

Deliberately pure and provider-agnostic: one classifier, fed from the single place every engine
already reports through (record_llm_call), rather than a copy per adapter.
"""

from __future__ import annotations

import json as _json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

#: Phrases that mean "you have capacity again later", not "this request was wrong".
#: Matched case-insensitively against the provider's own message.
_EXHAUSTED = (
    "usage limit",
    "rate limit exceeded",
    "quota exceeded",
    "insufficient_quota",
    "out of credits",
    "credit balance is too low",
    "exceeded your current quota",
    "too many requests",
)

#: "try again at Sep 22nd, 2026 10:06 PM" — codex's phrasing, and the most useful field in the
#: whole message: it is the answer to "when can I run this again".
_RETRY_AT = re.compile(
    r"try again (?:at|after)\s+"
    r"(?P<month>[A-Z][a-z]{2})[a-z]*\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?P<year>\d{4})\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[AP]M)",
    re.IGNORECASE,
)
#: An ISO timestamp or a seconds count, which most HTTP APIs use instead of prose.
_RETRY_ISO = re.compile(r"try again (?:at|after)\s+(?P<iso>\d{4}-\d{2}-\d{2}[T ][\d:]{5,8})",
                        re.IGNORECASE)
_RETRY_SECONDS = re.compile(r"retry[- ]after[:\s]+(?P<seconds>\d+)", re.IGNORECASE)

_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}


@dataclass(frozen=True)
class LimitSignal:
    """A provider saying it has no capacity for you right now."""

    exhausted: bool
    detail: str
    retry_at: Optional[datetime] = None
    retry_after_seconds: Optional[int] = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_capacity(self) -> dict[str, Any]:
        """The shape stored on the entitlement source's observed_capacity."""
        out: dict[str, Any] = {
            "exhausted": self.exhausted,
            "detail": self.detail[:400],
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.retry_at:
            out["retry_at"] = self.retry_at.isoformat()
        if self.retry_after_seconds is not None:
            out["retry_after_seconds"] = self.retry_after_seconds
        return out


def _parse_retry_at(message: str) -> Optional[datetime]:
    match = _RETRY_ISO.search(message)
    if match:
        try:
            return datetime.fromisoformat(match.group("iso").replace(" ", "T")).replace(
                tzinfo=timezone.utc)
        except ValueError:
            pass
    match = _RETRY_AT.search(message)
    if not match:
        return None
    month = _MONTHS.get(match.group("month").lower())
    if not month:
        return None
    hour = int(match.group("hour")) % 12
    if match.group("ampm").upper() == "PM":
        hour += 12
    try:
        return datetime(int(match.group("year")), month, int(match.group("day")),
                        hour, int(match.group("minute")), tzinfo=timezone.utc)
    except ValueError:
        return None


def classify_limit(error: Optional[str]) -> Optional[LimitSignal]:
    """A capacity exhaustion signal from a provider's own words, or None.

    None means "not a capacity problem" — a bad prompt, a missing binary, a crash. Only an
    answer that says capacity will return belongs here, because the action it implies is
    "wait, or use a different model", and offering that for an unrelated failure is worse
    than saying nothing.
    """
    if not error:
        return None
    text = str(error)
    lowered = text.lower()
    if not any(phrase in lowered for phrase in _EXHAUSTED):
        return None

    retry_at = _parse_retry_at(text)
    seconds_match = _RETRY_SECONDS.search(text)
    seconds = int(seconds_match.group("seconds")) if seconds_match else None

    # The provider's own sentence, not a paraphrase: it names the plan, the cap and the
    # remedy, and an operator deciding whether to wait or switch needs all three.
    sentence = text.strip()
    for line in text.splitlines():
        if any(phrase in line.lower() for phrase in _EXHAUSTED):
            sentence = line.strip()
            break

    return LimitSignal(
        exhausted=True,
        detail=sentence,
        retry_at=retry_at,
        retry_after_seconds=seconds,
        evidence={"matched": next(p for p in _EXHAUSTED if p in lowered)},
    )


def is_available(capacity: Optional[dict[str, Any]], *, now: Optional[datetime] = None) -> bool:
    """False only while a recorded exhaustion is still in force.

    A signal with no retry time is NOT permanent — it expires the moment a call succeeds, which
    clear_limit records. Treating it as forever would strand a provider that recovered.
    """
    if not isinstance(capacity, dict) or not capacity.get("exhausted"):
        return True
    retry_at = capacity.get("retry_at")
    if not retry_at:
        return False
    try:
        when = datetime.fromisoformat(str(retry_at))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) >= when


def source_ids_for_engine(settings, engine_name: str) -> tuple[str, ...]:
    """Entitlement source ids that spend on behalf of this engine.

    Asked of the harness specs and the API-key naming convention, the same two ways
    connected_engines resolves it, so a harness added later is covered without being listed
    here a second time.
    """
    ids: list[str] = []
    try:
        from .cli_harness import load_specs
        if settings is None:
            # Telemetry's context carries a db engine and ids, not settings. Building the
            # default here keeps ONE resolution path instead of a second hand-written map of
            # engine -> entitlement source, which is the shape of bug this file exists to end.
            from ..config import get_settings
            settings = get_settings()
        for spec in load_specs(settings).values():
            if spec.entitlement_source_id and spec.name == engine_name:
                ids.append(spec.entitlement_source_id)
    except Exception:  # noqa: BLE001 — telemetry must never break a call it is reporting on
        pass
    if engine_name == "claude_code":
        ids.append("anthropic_claude_code")
    ids.append(f"setup-{engine_name}")
    return tuple(dict.fromkeys(ids))


def record_call_outcome(connection, settings, project_id: str, engine_name: str,
                        *, ok: bool, error: Optional[str]) -> Optional[LimitSignal]:
    """Fold one call's outcome into what BOSS knows about this provider's capacity.

    A success clears any standing exhaustion — the provider answering IS the evidence that it
    recovered, and it is the only evidence that arrives on its own. A capacity failure records
    the provider's own sentence and, where it gave one, the time it will be available again.
    Any other failure is left alone: it says nothing about capacity.

    Never raises. This runs inside telemetry, and a bookkeeping error must not fail the work
    it is describing.
    """
    from sqlalchemy import text as _sql

    try:
        signal = None if ok else classify_limit(error)
        if ok:
            patch = {"exhausted": False,
                     "recovered_at": datetime.now(timezone.utc).isoformat()}
        elif signal:
            patch = signal.as_capacity()
        else:
            return None

        for source_id in source_ids_for_engine(settings, engine_name):
            connection.execute(
                _sql("UPDATE entitlement_sources "
                     "   SET observed_capacity = COALESCE(observed_capacity, '{}'::jsonb) "
                     "                           || CAST(:patch AS jsonb), "
                     "       updated_at = now() "
                     " WHERE project_id = :p AND (source_id = :s "
                     "      OR (source_id LIKE 'setup-%' AND source_id LIKE :s || '-%'))"),
                {"p": project_id, "s": source_id, "patch": _json.dumps(patch)},
            )
        return signal
    except Exception:  # noqa: BLE001
        return None
