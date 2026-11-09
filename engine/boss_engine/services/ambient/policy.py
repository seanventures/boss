"""Autonomy per task class, earned; one ambient envelope; one stop (BOSSAPP-905; design §4 L5, §9).

WHAT A GATE ANSWERS
-------------------
`gate(connection, project_id, task_class, action)` is the ONE question an ambient agent asks
before it acts: "may I take this action on this class of problem in this project, now, without a
person?" It answers with the level the class runs at, the level the action needs, whether a
person must decide, and HOW agents must agree first (single agent, maker-checker, squad vote,
recommend only — design §11.4). It never acts. A caller that ignores `allowed=False` is a bug the
ledger exposes: every ambient decision row records the level and action it was taken under.

ALWAYS A PERSON
---------------
Merge, deploy to production, an external send and a payment are never a level's call (the
operator's rule; autonomy.IRREVERSIBLE; the rc10.8 release rule). No matrix row, no promotion,
no standing level makes them `allowed`. They can only be the recommendation on a Needs-you
decision a person answers.

EARNED, NOT SET BY A GLOBAL SWITCH (design §2.2 pattern 6, §4 L5)
-----------------------------------------------------------------
* PROMOTION is suggested, never applied: after PROMOTE_MIN_DECIDED decided proposals in a class
  with at least PROMOTE_MIN_ACCEPTED accepted and none reverted, BOSS records a
  `promotion_suggested` change with that track record; a person raises the level (set_level).
* DEMOTION is automatic: one revert, or one incident the class caused, drops the class one level
  at once and records why. A drop never waits for a person; a rise always does.

ONE ENVELOPE, ONE STOP (design §9)
----------------------------------
Every ambient run reserves money through BudgetGovernor under category 'ambient' — so the
project's own daily/monthly caps still bind — and first through the project's ambient envelope
(ambient_settings.daily_cap_usd_micros), so self-started work can never spend the money a person's
work needs. A STOP (per project in ambient_settings, install-wide in system_state['ambient'], or
BOSS_AMBIENT_DISABLED=1 in the environment) refuses every new ambient item, reservation and
claim, and cancels every agent lease so running work stops at its next lease check.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import task_classes as tc
from ._common import emit, iso, new_id, now, serialize_row

SYSTEM_STATE_KEY = "ambient"
ENV_DISABLED = "BOSS_AMBIENT_DISABLED"
BUDGET_CATEGORY = "ambient"

#: The envelope and the loop guards' limits a project starts with (ambient_settings defaults).
DEFAULT_SETTINGS: dict[str, Any] = {
    "daily_cap_usd_micros": 5_000_000,   # $5/day of self-started work
    "max_concurrent": 2,                 # ambient items in progress at once (design §8 Phase 0)
    "max_spawn_depth": 2,                # causal depth (design §6 rule 2)
    "daily_attempt_cap": 10,             # new items per class per day (design §6 rule 4)
    "cooldown_hours": 24,                # after a dismissed/failed item; doubles each time
    "breaker_failure_ratio": 0.5,        # of ambient items failing in the last hour ...
    "breaker_min_runs": 4,               # ... once at least this many ended
}
SETTING_LIMITS: dict[str, tuple[float, float]] = {
    "daily_cap_usd_micros": (0, 10_000_000_000), "max_concurrent": (0, 50),
    "max_spawn_depth": (0, 5), "daily_attempt_cap": (0, 500), "cooldown_hours": (0, 24 * 90),
    "breaker_failure_ratio": (0.05, 1.0), "breaker_min_runs": (1, 100),
}

#: Promotion thresholds (design §4 L5: "after 20 proposals ... at least 85% accepted and no
#: reverts, BOSS suggests raising the class one level and a person confirms").
PROMOTE_MIN_DECIDED = 20
PROMOTE_MIN_ACCEPTED = 0.85
ACCEPTED_OUTCOMES = frozenset({"accepted", "edited", "merged", "succeeded"})
REJECTED_OUTCOMES = frozenset({"dismissed", "ignored", "overridden", "failed"})
DEMOTING_OUTCOMES = frozenset({"reverted", "incident_caused"})


# ── what runs by default (v1.0-rc10.13: the always-on agents are ON; rc10.11–12 shipped them off) ──

#: BOSS_AMBIENT_COS when it is unset and no person chose a mode in the console: the Chief of
#: Staff triages every signal and routes it to its owning squad, which acts within its task
#: class's autonomy level. Decisions on the Where people decide list still wait for a person.
#: A mode chosen in Govern or pinned in .env always wins (operator, 2026-11-09: "make the Agent
#: always on mode by default").
DEFAULT_COS_MODE = "route"
MODES = ("off", "propose", "route")
#: system_state key of the mode a person chose in the console (Govern › Autonomy › Always-on).
#: Its own key: the install-wide stop rewrites system_state['ambient'] whole.
MODE_STATE_KEY = "ambient_mode"


def _normal_mode(value: Any) -> str:
    text_value = str(value or "").strip().lower()
    if not text_value:
        return DEFAULT_COS_MODE
    return text_value if text_value in MODES else "propose"


def cos_mode(settings: Any, mode: Optional[str] = None) -> str:
    """off | propose | route. `mode` (the effective mode, see mode_state) wins when given; else
    BOSS_AMBIENT_COS. Unset → DEFAULT_COS_MODE (route); an unknown value → propose."""
    if mode is not None:
        return _normal_mode(mode)
    return _normal_mode(getattr(settings, "ambient_cos_mode", None))


def env_pins_mode(settings: Any) -> bool:
    """BOSS_AMBIENT_COS set (non-blank) in the environment pins the mode; the console shows it
    and cannot change it (the operator's .env is the stronger statement)."""
    return bool(str(getattr(settings, "ambient_cos_mode", None) or "").strip())


def mode_state(connection: Connection, settings: Any) -> dict[str, Any]:
    """The always-on mode in force and where it comes from: env (BOSS_AMBIENT_COS, pinned),
    console (a person chose it in Govern), or default (route: on). One answer for the tick, the
    learning tick, Discovery's dossier and the console."""
    if env_pins_mode(settings):
        return {"mode": cos_mode(settings), "source": "env", "pinned": True, "by": None,
                "at": None}
    value = connection.execute(text("SELECT value FROM system_state WHERE key = :k"),
                               {"k": MODE_STATE_KEY}).scalar()
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict) and value.get("mode"):
        return {"mode": _normal_mode(value["mode"]), "source": "console", "pinned": False,
                "by": value.get("by"), "at": value.get("at")}
    return {"mode": DEFAULT_COS_MODE, "source": "default", "pinned": False, "by": None,
            "at": None}


def effective_mode(connection: Connection, settings: Any) -> str:
    return mode_state(connection, settings)["mode"]


def set_mode(connection: Connection, settings: Any, *, mode: str, actor: str,
             reason: str = "") -> dict[str, Any]:
    """A person turns the always-on agents on (propose | route) or off for the install. Refused
    while BOSS_AMBIENT_COS pins it. Audited: who, when, why, from what. Turning it on gives no
    class more room: each class still acts only within its own level (the matrix)."""
    wanted = str(mode or "").strip().lower()
    if wanted not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if env_pins_mode(settings):
        raise ValueError("BOSS_AMBIENT_COS is set on this install, so the mode is pinned there; "
                         "clear it in .env to choose here")
    before = mode_state(connection, settings)
    connection.execute(text(
        "INSERT INTO system_state (key, value) VALUES (:k, CAST(:v AS JSONB)) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
        {"k": MODE_STATE_KEY, "v": json.dumps({"mode": wanted, "by": actor, "at": iso(now()),
                                                "reason": (reason or "")[:500],
                                                "from": before["mode"]})})
    # Audit on every project's stream (an install-wide row has none of its own).
    for pid in connection.execute(text("SELECT id FROM projects")).scalars():
        emit(connection, str(pid), "ambient.mode_changed", actor, "install", MODE_STATE_KEY,
             {"from": before["mode"], "to": wanted, "reason": (reason or "")[:500]})
    return mode_state(connection, settings)


def _explicit(value: Any) -> Optional[bool]:
    """1/0 (or true/false, yes/no, on/off) as set by a person; None when unset or blank."""
    if value is None or isinstance(value, bool):
        return value
    text_value = str(value).strip().lower()
    if not text_value:
        return None
    if text_value in ("1", "true", "yes", "on"):
        return True
    if text_value in ("0", "false", "no", "off"):
        return False
    try:
        return bool(int(text_value))
    except ValueError:
        return None


def signals_on(settings: Any, mode: Optional[str] = None) -> bool:
    """The signal sweep and Discovery's automatic dossier (BOSS_AMBIENT_SIGNALS): an explicit
    1/0 wins; unset, they run only while the Chief of Staff is on (`mode`: the effective mode)."""
    explicit = _explicit(getattr(settings, "ambient_signals", None))
    return explicit if explicit is not None else cos_mode(settings, mode) != "off"


def learning_on(settings: Any, mode: Optional[str] = None) -> bool:
    """The Recommended-roadmap tick (BOSS_AMBIENT_LEARNING): an explicit 1/0 wins; unset, it
    runs only while the Chief of Staff is on (`mode`: the effective mode)."""
    explicit = _explicit(getattr(settings, "ambient_learning", None))
    return explicit if explicit is not None else cos_mode(settings, mode) != "off"


# ── the rate guard on asking a person ────────────────────────────────────────────────────────

#: New person-facing ambient asks (Needs-you cards) per project per rolling hour. A mode switch,
#: a first scan of a big repository or a burst of signals must never become a burst of cards:
#: past this, the Chief of Staff holds its suggestions for the next card and owners do not seat
#: new squads (whose proposals would ask again) until the hour has room. Incidents are exempt.
ASKS_PER_PROJECT_HOUR = 3


def asks_per_hour(settings: Any = None) -> int:
    value = getattr(settings, "ambient_asks_per_hour", None) if settings is not None else None
    try:
        return max(0, int(ASKS_PER_PROJECT_HOUR if value is None else value))
    except (TypeError, ValueError):
        return ASKS_PER_PROJECT_HOUR


def asks_last_hour(connection: Connection, project_id: str) -> int:
    """Ambient cards raised here in the last hour (a withdrawn one asked nobody: not counted)."""
    return int(connection.execute(text(
        "SELECT count(*) FROM decisions WHERE project_id = :p "
        "AND recommendation->>'asked_by' IS NOT NULL "
        "AND recommendation->>'work_item_id' IS NOT NULL "
        "AND status <> 'withdrawn' AND created_at > now() - interval '1 hour'"),
        {"p": project_id}).scalar() or 0)


def ask_room(connection: Connection, project_id: str, settings: Any = None) -> int:
    """How many more ambient cards this project may raise this hour."""
    return max(0, asks_per_hour(settings) - asks_last_hour(connection, project_id))


# ── a new project's grace ────────────────────────────────────────────────────────────────────

#: A NEW PROJECT'S GRACE. Onboarding proposes a backlog (Add CI, tests for hot files ...) for the
#: person to decide on. Until they have looked at it — accepted or rejected any one item — and
#: for at most this many hours after onboarding started, always-on agents do not ACT here: every
#: A2+ action (start a run, open a draft PR, a staging change) needs a person, on the project's
#: one card; squads are not seated for the kinds of work onboarding proposes; and nothing that
#: duplicates a backlog item is worked at all (asks.backlog_twin). Incidents are exempt: a
#: failing app does not wait for a backlog review. After the window the grace lifts on its own,
#: so a project nobody reviews never stalls.
NEW_PROJECT_GRACE_HOURS = 24
#: The work onboarding's backlog proposes: squads leave these to the person during the grace.
ONBOARDING_CLASSES = frozenset({"ci_setup", "missing_tests", "doc_drift", "todo_triage"})
_GRACE_EXEMPT = frozenset({"incident_response", "incident_staging_mitigation"})


def grace_hours(settings: Any = None) -> int:
    value = getattr(settings, "ambient_new_project_grace_hours", None) if settings is not None \
        else None
    try:
        return max(0, int(NEW_PROJECT_GRACE_HOURS if value is None else value))
    except (TypeError, ValueError):
        return NEW_PROJECT_GRACE_HOURS


def grace(connection: Connection, project_id: str, settings: Any = None
          ) -> Optional[dict[str, Any]]:
    """The project's grace, or None. In grace: onboarding ran here less than
    NEW_PROJECT_GRACE_HOURS ago and no person has decided on any item of its backlog yet.
    A project that was never onboarded has no backlog to review and no grace."""
    hours = grace_hours(settings)
    if hours <= 0:
        return None
    try:
        with connection.begin_nested():
            row = connection.execute(text(
                "SELECT r.started_at, EXISTS (SELECT 1 FROM ambient_recommendations a "
                "  WHERE a.project_id = r.project_id AND a.detector = 'onboarding' "
                "  AND a.decided_at IS NOT NULL) AS reviewed, "
                "(SELECT count(*) FROM ambient_recommendations a WHERE a.project_id = "
                "  r.project_id AND a.detector = 'onboarding' AND a.status = 'open') AS open "
                "FROM project_onboarding_runs r WHERE r.project_id = :p "
                "AND r.started_at > now() - make_interval(hours => :h) "
                "ORDER BY r.started_at LIMIT 1"), {"p": project_id, "h": hours}).mappings().first()
    except Exception:  # noqa: BLE001 — an install without onboarding has no grace
        return None
    if row is None or row["reviewed"]:
        return None
    return {"since": iso(row["started_at"]), "hours": hours, "open": int(row["open"] or 0),
            "reason": "New project: agents act after you have looked at the onboarding backlog"}


# ── settings and the stop ────────────────────────────────────────────────────────────────────

def settings_for(connection: Connection, project_id: str) -> dict[str, Any]:
    row = connection.execute(text("SELECT * FROM ambient_settings WHERE project_id = :p"),
                             {"p": project_id}).mappings().first()
    out = {**DEFAULT_SETTINGS, "project_id": project_id, "stopped": False, "stopped_by": None,
           "stopped_at": None, "stop_reason": None, "updated_by": None, "updated_at": None}
    if row is not None:
        out.update(serialize_row(row))
        out["breaker_failure_ratio"] = float(out["breaker_failure_ratio"])
    return out


def update_settings(connection: Connection, project_id: str, *, actor: str,
                    **changes: Any) -> dict[str, Any]:
    """Change the envelope or a guard limit. Unknown keys and out-of-range values refuse."""
    unknown = set(changes) - set(SETTING_LIMITS)
    if unknown:
        raise ValueError(f"unknown ambient settings {sorted(unknown)}")
    clean: dict[str, Any] = {}
    for key, value in changes.items():
        if value is None:
            continue
        low, high = SETTING_LIMITS[key]
        number = float(value)
        if not low <= number <= high:
            raise ValueError(f"{key} must be between {low:g} and {high:g}")
        clean[key] = number if key == "breaker_failure_ratio" else int(number)
    _ensure_settings_row(connection, project_id, actor)
    if clean:
        sets = ", ".join(f"{k} = :{k}" for k in clean)
        connection.execute(text(f"UPDATE ambient_settings SET {sets}, updated_by = :a, "
                                "updated_at = now() WHERE project_id = :p"),
                           {**clean, "a": actor, "p": project_id})
        emit(connection, project_id, "ambient.settings_changed", actor, "project", project_id,
             {"changes": clean})
    return settings_for(connection, project_id)


def _ensure_settings_row(connection: Connection, project_id: str, actor: str) -> None:
    connection.execute(text(
        "INSERT INTO ambient_settings (project_id, updated_by) VALUES (:p, :a) "
        "ON CONFLICT (project_id) DO NOTHING"), {"p": project_id, "a": actor})


def _global_state(connection: Connection) -> dict[str, Any]:
    value = connection.execute(text("SELECT value FROM system_state WHERE key = :k"),
                               {"k": SYSTEM_STATE_KEY}).scalar()
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def stop_state(connection: Connection, project_id: Optional[str] = None) -> dict[str, Any]:
    """Is ambient work stopped here, by what, and since when. The env switch wins, then the
    install-wide stop, then the project's own."""
    if str(os.environ.get(ENV_DISABLED, "")).strip().lower() in {"1", "true", "yes", "on"}:
        return {"stopped": True, "scope": "env", "by": ENV_DISABLED, "at": None,
                "reason": f"{ENV_DISABLED} is set on this install"}
    state = _global_state(connection)
    if state.get("stopped"):
        return {"stopped": True, "scope": "global", "by": state.get("by"), "at": state.get("at"),
                "reason": state.get("reason") or ""}
    if project_id:
        row = connection.execute(text(
            "SELECT stopped, stopped_by, stopped_at, stop_reason FROM ambient_settings "
            "WHERE project_id = :p"), {"p": project_id}).mappings().first()
        if row is not None and row["stopped"]:
            return {"stopped": True, "scope": "project", "by": row["stopped_by"],
                    "at": iso(row["stopped_at"]), "reason": row["stop_reason"] or ""}
    return {"stopped": False, "scope": None, "by": None, "at": None, "reason": ""}


def is_stopped(connection: Connection, project_id: Optional[str]) -> bool:
    return bool(stop_state(connection, project_id)["stopped"])


def stop(connection: Connection, *, project_id: Optional[str], actor: str,
         reason: str) -> dict[str, Any]:
    """Stop ambient work in one project (or, with project_id=None, the whole install), and cancel
    every agent lease there. Returns the work items whose leases were cancelled."""
    from . import board

    reason = (reason or "").strip() or "stopped by a person"
    if project_id is None:
        connection.execute(text(
            "INSERT INTO system_state (key, value) VALUES (:k, CAST(:v AS JSONB)) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
            {"k": SYSTEM_STATE_KEY, "v": json.dumps({"stopped": True, "by": actor,
                                                      "at": iso(now()), "reason": reason})})
    else:
        _ensure_settings_row(connection, project_id, actor)
        connection.execute(text(
            "UPDATE ambient_settings SET stopped = true, stopped_by = :a, stopped_at = now(), "
            "stop_reason = :r, updated_by = :a, updated_at = now() WHERE project_id = :p"),
            {"a": actor, "r": reason, "p": project_id})
    released = board.cancel_agent_leases(connection, project_id=project_id, actor=actor,
                                         reason=f"ambient stopped: {reason}")
    touched = {r["project_id"] for r in released} | ({project_id} if project_id else set())
    for pid in sorted(touched):
        emit(connection, pid, "ambient.stopped", actor, "project", pid,
             {"scope": "project" if project_id else "global", "reason": reason,
              "released": [r["id"] for r in released if r["project_id"] == pid]})
    return {"stopped": True, "scope": "project" if project_id else "global",
            "released": [r["id"] for r in released]}


def resume(connection: Connection, *, project_id: Optional[str], actor: str,
           reason: str = "") -> dict[str, Any]:
    """Lift a stop. Leases cancelled by the stop are NOT given back: the next sweep re-admits."""
    if project_id is None:
        connection.execute(text(
            "INSERT INTO system_state (key, value) VALUES (:k, CAST(:v AS JSONB)) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
            {"k": SYSTEM_STATE_KEY, "v": json.dumps({"stopped": False, "by": actor,
                                                      "at": iso(now()), "reason": reason})})
    else:
        _ensure_settings_row(connection, project_id, actor)
        connection.execute(text(
            "UPDATE ambient_settings SET stopped = false, stopped_by = NULL, stopped_at = NULL, "
            "stop_reason = NULL, resumed_at = now(), updated_by = :a, updated_at = now() "
            "WHERE project_id = :p"),
            {"a": actor, "p": project_id})
        emit(connection, project_id, "ambient.resumed", actor, "project", project_id,
             {"reason": reason})
    return stop_state(connection, project_id)


# ── the matrix ───────────────────────────────────────────────────────────────────────────────

def level_for(connection: Connection, project_id: str, task_class: str) -> dict[str, Any]:
    klass = tc.task_class(task_class)
    row = connection.execute(text(
        "SELECT * FROM ambient_autonomy WHERE project_id = :p AND task_class = :c"),
        {"p": project_id, "c": klass.key}).mappings().first()
    if row is None:
        return {"task_class": klass.key, "level": klass.default_level, "ceiling": klass.ceiling,
                "default_level": klass.default_level, "max_ceiling": klass.ceiling,
                "source": "default", "set_by": None, "reason": "", "updated_at": None,
                "review_builds": None}
    ceiling = tc.clamp_level(row["ceiling"], klass.ceiling)
    return {"task_class": klass.key, "level": tc.clamp_level(row["level"], ceiling),
            "ceiling": ceiling, "default_level": klass.default_level,
            "max_ceiling": klass.ceiling, "source": "project", "set_by": row["set_by"],
            "reason": row["reason"], "updated_at": iso(row["updated_at"]),
            "review_builds": row.get("review_builds")}


def matrix(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Every class with its level here, its title, risk and the last suggestion BOSS made."""
    suggestions = {r["task_class"]: serialize_row(r) for r in connection.execute(text(
        "SELECT DISTINCT ON (task_class) task_class, kind, to_level, reason, evidence, created_at "
        "FROM ambient_autonomy_changes WHERE project_id = :p "
        "ORDER BY task_class, created_at DESC"), {"p": project_id}).mappings()}
    out = []
    for key, klass in tc.TASK_CLASSES.items():
        entry = level_for(connection, project_id, key)
        last = suggestions.get(key)
        entry.update({"title": klass.title, "risk": klass.risk, "owner_role": klass.owner_role,
                      "level_name": tc.LEVEL_NAMES[entry["level"]],
                      "suggested_level": (last or {}).get("to_level")
                      if (last or {}).get("kind") == "promotion_suggested" else None})
        out.append(entry)
    return out


def set_level(connection: Connection, project_id: str, task_class: str, *, actor: str,
              level: Optional[str] = None, ceiling: Optional[str] = None,
              reason: str = "", kind: str = "set") -> dict[str, Any]:
    """A person sets a class's level and/or ceiling here (or BOSS demotes it, kind='demoted').

    The level never exceeds the ceiling, and the ceiling never exceeds the catalogue's."""
    klass = tc.task_class(task_class)
    current = level_for(connection, project_id, klass.key)
    for name, value in (("level", level), ("ceiling", ceiling)):
        if value is not None and value not in tc.LEVELS:
            raise ValueError(f"{name} must be one of {', '.join(tc.LEVELS)}")
    new_ceiling = ceiling or current["ceiling"]
    if tc.level_rank(new_ceiling) > tc.level_rank(klass.ceiling):
        raise ValueError(f"{klass.title} can go no higher than {klass.ceiling} in any project")
    new_level = level or current["level"]
    if tc.level_rank(new_level) > tc.level_rank(new_ceiling):
        raise ValueError(f"level {new_level} is above this project's ceiling {new_ceiling} "
                         f"for {klass.title}")
    connection.execute(text(
        "INSERT INTO ambient_autonomy (project_id, task_class, level, ceiling, set_by, reason) "
        "VALUES (:p, :c, :l, :ce, :a, :r) ON CONFLICT (project_id, task_class) DO UPDATE SET "
        "level = EXCLUDED.level, ceiling = EXCLUDED.ceiling, set_by = EXCLUDED.set_by, "
        "reason = EXCLUDED.reason, updated_at = now()"),
        {"p": project_id, "c": klass.key, "l": new_level, "ce": new_ceiling, "a": actor,
         "r": reason or ""})
    _record_change(connection, project_id, klass.key, kind="ceiling" if level is None and
                   ceiling is not None else kind, from_level=current["level"],
                   to_level=new_level, actor=actor, reason=reason)
    return level_for(connection, project_id, klass.key)


def _record_change(connection: Connection, project_id: str, task_class: str, *, kind: str,
                   from_level: Optional[str], to_level: Optional[str], actor: str,
                   reason: str, evidence: Optional[dict[str, Any]] = None) -> str:
    change_id = new_id("aac")
    connection.execute(text(
        "INSERT INTO ambient_autonomy_changes (id, project_id, task_class, kind, from_level, "
        "to_level, actor, reason, evidence) VALUES (:id, :p, :c, :k, :f, :t, :a, :r, "
        "CAST(:e AS JSONB))"),
        {"id": change_id, "p": project_id, "c": task_class, "k": kind, "f": from_level,
         "t": to_level, "a": actor, "r": reason or "", "e": json.dumps(evidence or {})})
    event = {"set": "ambient.autonomy_changed", "ceiling": "ambient.autonomy_changed",
             "promotion_suggested": "ambient.promotion_suggested",
             "promoted": "ambient.autonomy_changed", "demoted": "ambient.autonomy_demoted"}[kind]
    emit(connection, project_id, event, actor, "task_class", task_class,
         {"change_id": change_id, "from": from_level, "to": to_level, "reason": reason,
          "evidence": evidence or {}})
    return change_id


def changes(connection: Connection, project_id: str, *, task_class: Optional[str] = None,
            limit: int = 50) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT * FROM ambient_autonomy_changes WHERE project_id = :p "
        "AND (CAST(:c AS TEXT) IS NULL OR task_class = :c) ORDER BY created_at DESC LIMIT :n"),
        {"p": project_id, "c": task_class, "n": max(1, min(int(limit), 500))}).mappings()
    return [serialize_row(r) for r in rows]


# ── review always-on builds before they start (identity/captain_authority.py) ─────────────────
#
# Always-on builds start on the project's Captain authority with no person needed (the operator's
# default, 2026-11-07). A person who wants to look first turns this on: install-wide in Govern ›
# Autonomy (the default every project follows), per project in Project › Settings › Autonomy &
# budget, and per class in the autonomy table. The most specific answer wins; unset follows the
# next one up; nothing set anywhere is OFF. With it on, the build waits on one card per builder.

REVIEW_STATE_KEY = "ambient_review_builds"


def review_builds_install(connection: Connection) -> bool:
    value = connection.execute(text("SELECT value FROM system_state WHERE key = :k"),
                               {"k": REVIEW_STATE_KEY}).scalar()
    if isinstance(value, str):
        value = json.loads(value)
    return bool(isinstance(value, dict) and value.get("review") is True)


def set_review_builds_install(connection: Connection, *, review: bool, actor: str) -> dict[str, Any]:
    connection.execute(text(
        "INSERT INTO system_state (key, value) VALUES (:k, CAST(:v AS JSONB)) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
        {"k": REVIEW_STATE_KEY, "v": json.dumps({"review": bool(review), "by": actor,
                                                  "at": iso(now())})})
    return {"install": review_builds_install(connection)}


def review_builds_state(connection: Connection, project_id: str) -> dict[str, Any]:
    """{install, project (None: follows the install), classes {class: bool}, effective, source}."""
    install = review_builds_install(connection)
    project = connection.execute(text(
        "SELECT review_squad_builds FROM ambient_settings WHERE project_id = :p"),
        {"p": project_id}).scalar()
    classes = {r[0]: bool(r[1]) for r in connection.execute(text(
        "SELECT task_class, review_builds FROM ambient_autonomy WHERE project_id = :p "
        "AND review_builds IS NOT NULL"), {"p": project_id})}
    effective = install if project is None else bool(project)
    return {"install": install, "project": project, "classes": classes, "effective": effective,
            "source": "install" if project is None else "project"}


def review_builds_for(connection: Connection, project_id: str,
                      task_class: Optional[str]) -> tuple[bool, str]:
    """Must a person review this class's always-on builds before they start? -> (answer, where
    it was set: class | project | install)."""
    state = review_builds_state(connection, project_id)
    if task_class and task_class in state["classes"]:
        return state["classes"][task_class], "class"
    return state["effective"], state["source"]


def set_review_builds(connection: Connection, project_id: str, *, actor: str,
                      review: Optional[bool], task_class: Optional[str] = None) -> dict[str, Any]:
    """Set (True/False) or clear (None: follow the next level up) the project's answer, or one
    class's. Turning it OFF lets always-on builds start on the Captain authority; ON makes them
    wait on the card."""
    if task_class:
        klass = tc.task_class(task_class)
        current = level_for(connection, project_id, klass.key)
        connection.execute(text(
            "INSERT INTO ambient_autonomy (project_id, task_class, level, ceiling, set_by, reason, "
            "review_builds) VALUES (:p, :c, :l, :ce, :a, '', :r) ON CONFLICT (project_id, task_class) "
            "DO UPDATE SET review_builds = EXCLUDED.review_builds, updated_at = now()"),
            {"p": project_id, "c": klass.key, "l": current["level"], "ce": current["ceiling"],
             "a": actor, "r": review})
    else:
        _ensure_settings_row(connection, project_id, actor)
        connection.execute(text(
            "UPDATE ambient_settings SET review_squad_builds = :r, updated_by = :a, updated_at = now() "
            "WHERE project_id = :p"), {"r": review, "a": actor, "p": project_id})
    emit(connection, project_id, "ambient.review_builds_changed", actor, "project", project_id,
         {"task_class": task_class, "review": review})
    return review_builds_state(connection, project_id)


# ── the gate ─────────────────────────────────────────────────────────────────────────────────

def gate(connection: Connection, project_id: str, task_class: str, action: str) -> dict[str, Any]:
    """May an agent take `action` on a `task_class` item in this project, now, without a person?

    Returns {allowed, needs_person, level, required_level, action, task_class, risk,
    reversibility, consensus_mode, reason}. `needs_person` with allowed=False means: propose it
    and put it in front of a person (a Needs-you decision); allowed=False without needs_person
    means the class may only observe (A0) — record it for the digest, ask nobody.
    """
    from .collab import consensus_mode

    klass = tc.task_class(task_class)
    act = tc.action(action)
    current = level_for(connection, project_id, klass.key)
    level = current["level"]
    # The class's risk weighs on ACTING (A2+), not on watching or asking: proposing an incident
    # card is as safe as proposing a doc fix; rolling back during one is not.
    risk = act.risk if tc.level_rank(act.min_level) <= 1 else _max_risk(klass.risk, act.risk)
    mode = consensus_mode(risk=risk, reversibility=act.reversibility,
                          always_a_person=act.always_a_person)
    base = {"action": act.key, "task_class": klass.key, "level": level,
            "required_level": act.min_level, "risk": risk, "reversibility": act.reversibility,
            "consensus_mode": mode}
    stopped = stop_state(connection, project_id)
    if stopped["stopped"]:
        return {**base, "allowed": False, "needs_person": False,
                "reason": f"ambient work is stopped ({stopped['scope']}): {stopped['reason']}"}
    if act.always_a_person:
        return {**base, "allowed": False, "needs_person": True,
                "reason": f"{act.title} always needs a person, at every autonomy level"}
    if tc.level_rank(level) < tc.level_rank(act.min_level):
        can_ask = tc.level_rank(level) >= tc.level_rank("A1")
        return {**base, "allowed": False, "needs_person": can_ask,
                "reason": (f"{klass.title} runs at {level} here; {act.title} needs "
                           f"{act.min_level}" + ("" if can_ask else " — observe only"))}
    if tc.level_rank(act.min_level) >= 2 and klass.key not in _GRACE_EXEMPT:
        held = grace(connection, project_id)
        if held:
            # A new project's grace: acting waits for a person, on the project's one card.
            return {**base, "allowed": False, "needs_person": True, "grace": held,
                    "reason": held["reason"]}
    return {**base, "allowed": True, "needs_person": mode in ("squad_vote_and_person",
                                                              "recommend_only"),
            "reason": f"{klass.title} runs at {level}; {act.title} needs {act.min_level}"}


def _max_risk(*risks: str) -> str:
    return max(risks, key=lambda r: tc.RISKS.index(r) if r in tc.RISKS else 2)


# ── the track record ─────────────────────────────────────────────────────────────────────────

def track_record(connection: Connection, project_id: str, task_class: str, *,
                 since: Any = None) -> dict[str, Any]:
    """Outcomes of this class's ambient decisions since the class's last level change."""
    if since is None:
        since = connection.execute(text(
            "SELECT max(created_at) FROM ambient_autonomy_changes WHERE project_id = :p AND "
            "task_class = :c AND kind IN ('set', 'promoted', 'demoted')"),
            {"p": project_id, "c": task_class}).scalar()
    rows = connection.execute(text(
        "SELECT outcome, count(*) AS n FROM agent_act_decisions WHERE project_id = :p "
        "AND task_class = :c AND overrides_act_id IS NULL AND decided_by NOT LIKE 'person:%' "
        "AND (CAST(:s AS TIMESTAMPTZ) IS NULL OR created_at > :s) GROUP BY outcome"),
        {"p": project_id, "c": task_class, "s": since}).mappings().all()
    counts = {str(r["outcome"]): int(r["n"]) for r in rows}
    accepted = sum(n for o, n in counts.items() if o in ACCEPTED_OUTCOMES)
    rejected = sum(n for o, n in counts.items() if o in REJECTED_OUTCOMES)
    demoting = sum(n for o, n in counts.items() if o in DEMOTING_OUTCOMES)
    decided = accepted + rejected + demoting
    return {"task_class": task_class, "since": iso(since), "decided": decided,
            "accepted": accepted, "rejected": rejected, "reverted": demoting,
            "undecided": counts.get("None", 0),
            "accepted_ratio": round(accepted / decided, 4) if decided else None,
            "outcomes": counts}


def review_track_record(connection: Connection, project_id: str, task_class: str, *,
                        actor: str = "boss") -> dict[str, Any]:
    """Demote at once on a revert/incident; suggest a promotion when the record earns one.

    Called by ledger.record_outcome after every outcome. Returns {"action": "demoted" |
    "promotion_suggested" | None, ...}."""
    klass = tc.task_class(task_class)
    current = level_for(connection, project_id, klass.key)
    record = track_record(connection, project_id, klass.key)
    rank = tc.level_rank(current["level"])
    if record["reverted"] > 0 and rank > 0:
        lower = tc.LEVELS[rank - 1]
        set_level(connection, project_id, klass.key, actor=actor, level=lower,
                  reason=(f"{record['reverted']} ambient change(s) in {klass.title} were reverted "
                          "or caused an incident"), kind="demoted")
        return {"action": "demoted", "from": current["level"], "to": lower, "record": record}
    if (record["decided"] >= PROMOTE_MIN_DECIDED and record["reverted"] == 0
            and (record["accepted_ratio"] or 0) >= PROMOTE_MIN_ACCEPTED
            and tc.level_rank(current["level"]) < tc.level_rank(current["ceiling"])):
        higher = tc.LEVELS[rank + 1]
        already = connection.execute(text(
            "SELECT 1 FROM ambient_autonomy_changes WHERE project_id = :p AND task_class = :c "
            "AND kind = 'promotion_suggested' AND to_level = :t AND created_at > "
            "COALESCE(CAST(:s AS TIMESTAMPTZ), '-infinity')"),
            {"p": project_id, "c": klass.key, "t": higher, "s": record["since"]}).first()
        if already is None:
            _record_change(connection, project_id, klass.key, kind="promotion_suggested",
                           from_level=current["level"], to_level=higher, actor=actor,
                           reason=(f"{record['accepted']} of {record['decided']} accepted, "
                                   "0 reverted"), evidence=record)
        return {"action": "promotion_suggested", "from": current["level"], "to": higher,
                "record": record}
    return {"action": None, "record": record}


# ── the envelope ─────────────────────────────────────────────────────────────────────────────

def envelope(connection: Connection, project_id: str) -> dict[str, Any]:
    """Today's (UTC) ambient money: the cap, what was spent, what is held, what is left."""
    from ..included_spend import MONEY_SQL

    cap = int(settings_for(connection, project_id)["daily_cap_usd_micros"])
    spent = int(connection.execute(text(
        f"SELECT COALESCE(sum({MONEY_SQL}), 0) FROM cost_receipts WHERE project_id = :p AND "
        "category = :c AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') "
        "AT TIME ZONE 'UTC'"), {"p": project_id, "c": BUDGET_CATEGORY}).scalar() or 0)
    held = int(connection.execute(text(
        "SELECT COALESCE(sum(amount_usd_micros), 0) FROM budget_reservations WHERE "
        "project_id = :p AND category = :c AND status = 'active' AND expires_at > now()"),
        {"p": project_id, "c": BUDGET_CATEGORY}).scalar() or 0)
    return {"cap_usd_micros": cap, "spent_usd_micros": spent, "held_usd_micros": held,
            "available_usd_micros": max(0, cap - spent - held), "resets": "00:00 UTC"}


def reserve(connection: Connection, settings: Any, *, project_id: str, work_item_id: str,
            amount_usd_micros: int, reason: str, expires_in_minutes: int = 120,
            risk_tier: Optional[int] = None) -> dict[str, Any]:
    """Reserve ambient money for one work item: the stop, then the envelope, then the project's
    own budget (BudgetGovernor.reserve, category 'ambient'). Returns BudgetGovernor's result
    ({status: 'reserved', reservation_id, ...}) or a refusal {status: 'denied', refusal, message}.
    """
    from ..budget import BudgetGovernor

    stopped = stop_state(connection, project_id)
    if stopped["stopped"]:
        return {"status": "denied", "refusal": "ambient_stopped", "transient": False,
                "message": f"Ambient work is stopped ({stopped['scope']}): {stopped['reason']}"}
    amount = max(0, int(amount_usd_micros))
    room = envelope(connection, project_id)
    if amount > room["available_usd_micros"]:
        refused = {"status": "denied", "refusal": "ambient_envelope", "transient": True,
                   "envelope": room,
                   "message": ("Today's ambient budget has "
                               f"${room['available_usd_micros'] / 1e6:.2f} left of "
                               f"${room['cap_usd_micros'] / 1e6:.2f}; this needs "
                               f"${amount / 1e6:.2f}. It resets at 00:00 UTC.")}
        emit(connection, project_id, "ambient.envelope_refused", "boss", "work_item",
             work_item_id, {"requested_usd_micros": amount, "envelope": room})
        return refused
    result = BudgetGovernor(settings).reserve(
        connection, project_id, BUDGET_CATEGORY, amount, reason, expires_in_minutes,
        risk_tier=risk_tier)
    reservation_id = result.get("reservation_id") or (result.get("reservation") or {}).get("id")
    if reservation_id:
        from .board import link

        link(connection, work_item_id, "reservation", str(reservation_id), actor="boss")
    return result
