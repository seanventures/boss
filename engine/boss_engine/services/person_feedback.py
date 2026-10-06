"""What the person said: one-tap ratings, reason chips, edit amounts and the monthly check-in.

docs/product/RUN_SCORECARDS.md ("Feedback the person gives", "Implementation"). Stored locally
ALWAYS — whether or not "Improve BOSS" is on — because it feeds this install's own scorecards
and experiments. Quiet by design: one tap, never blocking, and `prompts()` tells the console how
loudly it may ask (an open prompt at most 3 times a day per person; the check-in at most once in
30 days, a dismissal counting as that month's ask).
"""
from __future__ import annotations

import difflib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

from .improve_allowlist import CHIPS, DECISION_KINDS, EDIT_DOCS, PMF_ANSWERS, RATINGS

RATED = ("build", "research", "panel", "answer", "deploy")
KINDS = RATED + ("reject", "steer", "edit", "checkin")
ASKS_PER_DAY = 3
CHECKIN_EVERY_DAYS = 30
#: A new install is not asked how disappointed it would be before it has done anything.
CHECKIN_AFTER_DAYS = 7
LINE_MAX = 140

#: Which scorecard each rated subject feeds (its `rating` column).
_SCORECARD_KINDS = {"build": ("build", "quick_change", "workflow_run"),
                    "research": ("research",), "panel": ("panel",)}


class FeedbackError(ValueError):
    pass


class CheckinTooSoon(FeedbackError):
    """The check-in was answered less than 30 days ago (409)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def subject_unit(connection, subject_kind: str, subject_id: str) -> str:
    """The id a rating is filed under: the scored UNIT's own id. A panel is rated from its room,
    which may carry the meeting's id (mtg_…); the scorecard is the meeting run's (mrun_…)."""
    sid = str(subject_id or "").strip()
    if subject_kind == "panel" and sid and not sid.startswith("mrun_"):
        got = connection.execute(text(
            "SELECT id FROM meeting_runs WHERE meeting_id = :m ORDER BY created_at DESC LIMIT 1"),
            {"m": sid}).scalar()
        if got:
            return str(got)
    return sid


def record(connection, *, project_id: str, actor: str, subject_kind: str, subject_id: str,
           rating: Optional[str] = None, chips: Optional[list[str]] = None,
           decision_kind: Optional[str] = None) -> dict[str, Any]:
    """A rating (one per person and subject; a second tap changes it), or chips on a reject or
    a steer (appended). Unknown ratings and chips are refused, not stored."""
    from .core import make_id

    if subject_kind == "edit":
        raise FeedbackError("an edit amount is recorded with record_edit or record_edit_amount")
    if subject_kind not in RATED + ("reject", "steer"):
        raise FeedbackError(f"subject_kind must be one of {', '.join(RATED + ('reject', 'steer'))}")
    if not str(subject_id or "").strip():
        raise FeedbackError("subject_id is required")
    subject_id = subject_unit(connection, subject_kind, subject_id)
    allowed_chips = CHIPS.get(subject_kind, ())
    chips = list(dict.fromkeys(chips or []))
    bad = [c for c in chips if c not in allowed_chips]
    if bad:
        raise FeedbackError(f"unknown reason: {bad[0]}")
    if subject_kind in RATED:
        if rating not in RATINGS[subject_kind]:
            raise FeedbackError(f"rating must be one of {', '.join(RATINGS[subject_kind])}")
        row = connection.execute(text("""
            INSERT INTO person_feedback (id, project_id, subject_kind, subject_id, actor, rating, chips)
            VALUES (:id, :p, :k, :s, :a, :r, :c)
            ON CONFLICT (subject_kind, subject_id, actor)
              WHERE subject_kind IN ('build','research','panel','answer','deploy')
            DO UPDATE SET rating = EXCLUDED.rating, chips = EXCLUDED.chips, updated_at = now()
            RETURNING id, rating, chips"""),
            {"id": make_id("pfb"), "p": project_id, "k": subject_kind, "s": subject_id,
             "a": actor, "r": rating, "c": chips}).mappings().one()
        _answered(connection, actor, "rate")
        _mirror(connection, subject_kind, subject_id)
        return {"id": row["id"], "rating": row["rating"], "chips": list(row["chips"] or [])}
    if decision_kind is not None and decision_kind not in DECISION_KINDS:
        raise FeedbackError(f"decision_kind must be one of {', '.join(DECISION_KINDS)}")
    if not chips:
        return {"id": None, "chips": []}  # the chips are optional; nothing said, nothing kept
    fid = make_id("pfb")
    connection.execute(text("""
        INSERT INTO person_feedback (id, project_id, subject_kind, subject_id, actor, chips,
                                     decision_kind)
        VALUES (:id, :p, :k, :s, :a, :c, :d)"""),
        {"id": fid, "p": project_id, "k": subject_kind, "s": subject_id, "a": actor,
         "c": chips, "d": decision_kind})
    return {"id": fid, "chips": chips}


def _mirror(connection, subject_kind: str, subject_id: str) -> None:
    """A rating also lands on the unit's scorecard (re-recorded, so the row stays one source)."""
    kinds = _SCORECARD_KINDS.get(subject_kind)
    if not kinds:
        return
    from .scorecards import record_scorecard

    hit = connection.execute(text(
        "SELECT unit_kind FROM run_scorecards WHERE unit_kind = ANY(:k) AND unit_id = :id"),
        {"k": list(kinds), "id": subject_id}).scalar()
    if hit:
        with connection.begin_nested():
            record_scorecard(connection, hit, subject_id)


def edit_ratio(before: str, after: str) -> float:
    """How much a document changed, 0..1 (1 − difflib's similarity). The text is not kept."""
    if (before or "") == (after or ""):
        return 0.0
    return round(1.0 - difflib.SequenceMatcher(None, before or "", after or "",
                                               autojunk=False).ratio(), 3)


def record_edit(connection, *, project_id: str, actor: str, doc_kind: str, subject_id: str,
                before: str, after: str) -> Optional[float]:
    """The edit amount of a brief, PRD or plan before approval — a number, never the text."""
    from .core import make_id

    if doc_kind not in EDIT_DOCS:
        raise FeedbackError(f"doc_kind must be one of {', '.join(EDIT_DOCS)}")
    ratio = edit_ratio(before, after)
    connection.execute(text("""
        INSERT INTO person_feedback (id, project_id, subject_kind, subject_id, actor,
                                     decision_kind, edit_ratio)
        VALUES (:id, :p, 'edit', :s, :a, :d, :r)"""),
        {"id": make_id("pfb"), "p": project_id, "s": subject_id, "a": actor, "d": doc_kind,
         "r": ratio})
    return ratio


def record_edit_amount(connection, *, project_id: str, actor: str, doc_kind: str, subject_id: str,
                       ratio: Any) -> float:
    """The same edit amount, measured where the editing happened (the console compares its own
    before and after) and sent as the number alone."""
    from .core import make_id

    if doc_kind not in EDIT_DOCS:
        raise FeedbackError(f"doc_kind must be one of {', '.join(EDIT_DOCS)}")
    try:
        value = round(min(1.0, max(0.0, float(ratio))), 3)
    except (TypeError, ValueError) as error:
        raise FeedbackError("edit_ratio has to be a number from 0 to 1") from error
    connection.execute(text("""
        INSERT INTO person_feedback (id, project_id, subject_kind, subject_id, actor,
                                     decision_kind, edit_ratio)
        VALUES (:id, :p, 'edit', :s, :a, :d, :r)"""),
        {"id": make_id("pfb"), "p": project_id, "s": subject_id, "a": actor, "d": doc_kind,
         "r": value})
    return value


def _state(connection, actor: str, prompt: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT * FROM person_prompt_state WHERE actor = :a AND prompt = :p"),
        {"a": actor, "p": prompt}).mappings().first()
    return dict(row) if row else {}


def _answered(connection, actor: str, prompt: str) -> None:
    connection.execute(text("""
        INSERT INTO person_prompt_state (actor, prompt, last_answered_at) VALUES (:a, :p, now())
        ON CONFLICT (actor, prompt) DO UPDATE SET last_answered_at = now()"""),
        {"a": actor, "p": prompt})


def mark_shown(connection, actor: str, prompt: str) -> None:
    """The console showed an open ask: counted per person per day."""
    connection.execute(text("""
        INSERT INTO person_prompt_state (actor, prompt, last_shown_at, shown_today, shown_day)
        VALUES (:a, :p, now(), 1, CURRENT_DATE)
        ON CONFLICT (actor, prompt) DO UPDATE SET last_shown_at = now(),
          shown_today = CASE WHEN person_prompt_state.shown_day = CURRENT_DATE
                             THEN person_prompt_state.shown_today + 1 ELSE 1 END,
          shown_day = CURRENT_DATE"""), {"a": actor, "p": prompt})


def _install_age_days(connection) -> int:
    first = connection.execute(text("SELECT MIN(created_at) FROM projects")).scalar()
    return 0 if first is None else max(0, (_now() - first).days)


def prompts(connection, *, actor: str, subjects: Optional[list[tuple[str, str]]] = None
            ) -> dict[str, Any]:
    """How loudly the console may ask this person right now, and what they already said."""
    rate = _state(connection, actor, "rate")
    today = _now().date()
    shown_today = int(rate.get("shown_today") or 0) if rate.get("shown_day") == today else 0
    check = _state(connection, actor, "checkin")
    acted = [t for t in (check.get("last_answered_at"), check.get("last_dismissed_at")) if t]
    last_acted = max(acted) if acted else None
    shown = check.get("last_shown_at")
    month = timedelta(days=CHECKIN_EVERY_DAYS)
    # Showing it IS the month's ask: it stays for the rest of that day (so a reload does not
    # lose it), then not again for 30 days whether or not it was answered.
    shown_open = bool(shown and shown.date() == today and (last_acted is None or last_acted < shown))
    quiet = any(t and _now() - t < month for t in (last_acted, shown))
    due = _install_age_days(connection) >= CHECKIN_AFTER_DAYS and (shown_open or not quiet)
    mine: dict[str, dict[str, Any]] = {}
    for kind, asked in subjects or []:
        sid = subject_unit(connection, kind, asked)
        row = connection.execute(text(
            "SELECT rating, chips FROM person_feedback WHERE subject_kind = :k AND "
            "subject_id = :s AND actor = :a"), {"k": kind, "s": sid, "a": actor}).mappings().first()
        if row:
            mine[f"{kind}:{asked}"] = {"rating": row["rating"], "chips": list(row["chips"] or [])}
    return {"ask_open": shown_today < ASKS_PER_DAY, "asks_left_today": max(0, ASKS_PER_DAY - shown_today),
            "checkin_due": bool(due), "mine": mine,
            "chips": {k: list(v) for k, v in CHIPS.items()},
            "ratings": {k: list(v) for k, v in RATINGS.items()}}


def checkin(connection, *, project_id: str, actor: str, answer: Optional[str],
            line: Optional[str] = None, share_line: bool = False,
            dismiss: bool = False) -> dict[str, Any]:
    """The monthly check-in: the PMF answer and an optional one-liner, or a dismissal."""
    from .core import make_id

    if dismiss:
        connection.execute(text("""
            INSERT INTO person_prompt_state (actor, prompt, last_dismissed_at)
            VALUES (:a, 'checkin', now())
            ON CONFLICT (actor, prompt) DO UPDATE SET last_dismissed_at = now()"""), {"a": actor})
        return {"dismissed": True}
    if answer not in PMF_ANSWERS:
        raise FeedbackError(f"answer must be one of {', '.join(PMF_ANSWERS)}")
    # ONCE IN 30 DAYS, enforced here and not only by the console (defect 17).
    check = _state(connection, actor, "checkin")
    last = check.get("last_answered_at")
    if last and _now() - last < timedelta(days=CHECKIN_EVERY_DAYS):
        raise CheckinTooSoon("Already answered this month.")
    one = (line or "").strip()[:LINE_MAX] or None
    # A line is sendable only if it was ticked AND saved while Improve BOSS was on (P3): turning
    # the switch on later never sends what was written while it was off.
    from .improve_boss import switches

    sharing_on = bool(switches(connection)["improve"]["on"])
    share_line = bool(share_line and one and sharing_on)
    connection.execute(text("""
        INSERT INTO person_feedback (id, project_id, subject_kind, subject_id, actor, rating,
                                     one_liner, share_line)
        VALUES (:id, :p, 'checkin', :s, :a, :r, :l, :sh)"""),
        {"id": make_id("pfb"), "p": project_id, "s": _now().strftime("%Y-%m"), "a": actor,
         "r": answer, "l": one, "sh": share_line})
    _answered(connection, actor, "checkin")
    return {"answer": answer, "line_kept": bool(one), "line_shared": share_line}
