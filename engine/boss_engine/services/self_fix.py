"""BOSS FIXES ITSELF: a BOSS-side failure becomes a tested, reviewed, approved, installed fix.

BOSSAPP-571. PRD docs/product/BOSS_FIXES_ITSELF_PRD.md (R1–R19), engineering spec
docs/product/BOSS_FIXES_ITSELF.md (its findings F1–F18 override the PRD where they say so).

THE CASE (HelpDesk AI, run wfr_f383649a8b024d97a80c, 2026-10-17). The deploy step's recovery
agent placed two failures on BOSS itself, correctly, recorded them as recommendations and parked
the step. A person's coding agent wrote each fix in four minutes; the second one took six hours
to reach the box. With this feature the same decision starts a SELF-FIX, and the person's only
job is to approve.

THIS MODULE is the self-fix's record and its rules — everything BOSS (never the model) decides:

  * WHETHER (Stage 0, §8.7): `BOSS_SELF_FIX=off` beats everything and is read on every call; the
    org switch (`system_state['self_fix']`) is next (default ON only for a one-person install
    that can change itself); the install must be able to install (a git install: a source sha
    and the self-source mirror); the step kind is one of `SELF_FIX_KINDS`; the month's budget has
    room; the cause has not had `attempts_per_signature` self-fixes already.
  * HOW SURE (Stage 1, §5.2): a CONFIDENCE BOSS computes from signals — the coded triage agrees,
    the model's evidence validated, the cited code exists, the cause recurred, an app-side
    action already failed for it; minus an instruction-shaped string in the evidence or the
    model and the triage disagreeing. The model's own claim is one input, never the answer.
    Below `self_fix_min_confidence` (default medium) today's behaviour runs: record and park.
  * ONE AT A TIME (Stage 2, R19, F14): the CLASS signature (install parameters stripped:
    `deploy:port_held`) is claimed install-wide; a second step that hits it waits on that fix.
  * THE STATE MACHINE (§5.1): every move is checked against `TRANSITIONS`, written with the
    actor, emitted as an event with a sentence for the run page (R14), and the security-relevant
    ones also go to the hash-chained identity audit (§8.5).
  * THE STEP'S HANDLE (§11.2): `{kind: self_fix, id, then: resume}` is due when the fix is
    installed AND the running build is the stack that holds it; a self-fix that ends without
    being installed turns the handle back into today's `boss_update` wait, so a person's own fix
    still resumes the step.
  * THE VERDICT (Stage 12, F18): the resumed step passing, or failing for a DIFFERENT cause,
    VERIFIES the fix; failing for the SAME cause means it did not hold — it is rolled back and
    its recommendation reopens with what was learned.

The work itself is elsewhere, each piece under the `never` rule of the policy (self_fix*.py):
the Mender build (self_fix_build), the gates (self_fix_policy, self_fix_integrity), the source
mirror (self_fix_source), the approval, tickets, install, rollback and upgrade plan
(self_fix_install), the privacy rule (self_fix_privacy).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id, now_utc

log = logging.getLogger(__name__)

ID_PREFIX = "sfx"
ACTOR_AGENT = "boss_maintainer"          # Mender's role key (its Passport, §8.5)
AGENT_NAME = "Mender"
#: Mender's agent principal: autonomous (BOSS started it), sponsored by the install's first
#: Owner, a READ-ONLY ceiling. It can never approve, waive a gate, read a secret, run on a host
#: or deploy, and it has no route to the self-installer: tickets come only from approved
#: decisions (§8.5, identity §7.13).
MENDER_AGENT_ID = "agt_boss_maintainer"
MENDER_CEILING = ("workflow_run:read", "code_task:read", "repository:read")
SYSTEM_ACTOR = "sys_worker"
INSTALLER_ACTOR = "sys_self_installer"

# ── statuses (§5.1) ──────────────────────────────────────────────────────────────────────────

PROPOSED, CLAIMED, WAITING_ON = "proposed", "claimed", "waiting_on"
DIAGNOSING, REPRODUCING, FIXING, CHECKING, REVIEWING = (
    "diagnosing", "reproducing", "fixing", "checking", "reviewing")
AWAITING_APPROVAL, APPROVED = "awaiting_approval", "approved"
INSTALLING, HEALTH_WINDOW, INSTALLED, VERIFYING, VERIFIED = (
    "installing", "health_window", "installed", "verifying", "verified")
REFUSED, REJECTED, INSTALL_FAILED, NOT_RESOLVED = (
    "refused", "rejected", "install_failed", "not_resolved")
ROLLING_BACK, ROLLED_BACK, CANCELLED, SUPERSEDED = (
    "rolling_back", "rolled_back", "cancelled", "superseded")

STATUSES = (PROPOSED, CLAIMED, WAITING_ON, DIAGNOSING, REPRODUCING, FIXING, CHECKING, REVIEWING,
            AWAITING_APPROVAL, APPROVED, INSTALLING, HEALTH_WINDOW, INSTALLED, VERIFYING,
            VERIFIED, REFUSED, REJECTED, INSTALL_FAILED, NOT_RESOLVED, ROLLING_BACK, ROLLED_BACK,
            CANCELLED, SUPERSEDED)
TERMINAL = frozenset({VERIFIED, REFUSED, REJECTED, INSTALL_FAILED, ROLLED_BACK, CANCELLED,
                      SUPERSEDED})
BUILDING = frozenset({PROPOSED, CLAIMED, DIAGNOSING, REPRODUCING, FIXING, CHECKING, REVIEWING})
ON_THE_BOX = frozenset({INSTALLING, HEALTH_WINDOW, INSTALLED, VERIFYING, ROLLING_BACK})
#: Ends that never reached the box: the step falls back to today's wait for a BOSS update.
FELL_BACK = frozenset({REFUSED, REJECTED, CANCELLED, INSTALL_FAILED, ROLLED_BACK, NOT_RESOLVED})

_ANY_LIVE = (BUILDING | {AWAITING_APPROVAL, APPROVED}) - {PROPOSED}
TRANSITIONS: dict[str, frozenset[str]] = {
    PROPOSED: frozenset({CLAIMED, REFUSED, CANCELLED}),
    CLAIMED: frozenset({DIAGNOSING, REFUSED, CANCELLED}),
    DIAGNOSING: frozenset({REPRODUCING, REFUSED, CANCELLED}),
    REPRODUCING: frozenset({FIXING, REFUSED, CANCELLED}),
    FIXING: frozenset({CHECKING, REFUSED, CANCELLED}),
    CHECKING: frozenset({REVIEWING, FIXING, REFUSED, CANCELLED}),
    REVIEWING: frozenset({AWAITING_APPROVAL, FIXING, REFUSED, CANCELLED}),
    AWAITING_APPROVAL: frozenset({APPROVED, REJECTED, FIXING, CANCELLED}),
    APPROVED: frozenset({INSTALLING, INSTALL_FAILED, CANCELLED}),
    INSTALLING: frozenset({HEALTH_WINDOW, INSTALL_FAILED, ROLLING_BACK, ROLLED_BACK}),
    HEALTH_WINDOW: frozenset({INSTALLED, ROLLING_BACK, ROLLED_BACK}),
    INSTALLED: frozenset({VERIFYING, VERIFIED, ROLLING_BACK, SUPERSEDED}),
    VERIFYING: frozenset({VERIFIED, NOT_RESOLVED, INSTALLED}),
    NOT_RESOLVED: frozenset({ROLLING_BACK, ROLLED_BACK}),
    ROLLING_BACK: frozenset({ROLLED_BACK, INSTALL_FAILED}),
    VERIFIED: frozenset({SUPERSEDED, ROLLING_BACK}),
    REFUSED: frozenset(), REJECTED: frozenset(), INSTALL_FAILED: frozenset(),
    ROLLED_BACK: frozenset(), CANCELLED: frozenset(), SUPERSEDED: frozenset(),
    WAITING_ON: frozenset(),
}

#: The PRD's status chips (§9), for the console.
CHIPS = {**{s: "Building" for s in BUILDING}, AWAITING_APPROVAL: "Needs you",
         APPROVED: "Installed", INSTALLING: "Installed", HEALTH_WINDOW: "Installed",
         INSTALLED: "Installed", VERIFYING: "Installed", VERIFIED: "Verified",
         NOT_RESOLVED: "Rolled back", ROLLING_BACK: "Rolled back", ROLLED_BACK: "Rolled back",
         REFUSED: "Refused", REJECTED: "Refused", INSTALL_FAILED: "Rolled back",
         CANCELLED: "Refused", SUPERSEDED: "In release"}

#: The parked step's strip (§10.1): Found, Reproduced, Fixing, Checks, Review, You, Install, Resume.
STAGES = ("found", "reproduced", "fixing", "checks", "review", "you", "install", "resume")
_STAGE_OF = {PROPOSED: "found", CLAIMED: "found", DIAGNOSING: "found", REPRODUCING: "reproduced",
             FIXING: "fixing", CHECKING: "checks", REVIEWING: "review", AWAITING_APPROVAL: "you",
             APPROVED: "install", INSTALLING: "install", HEALTH_WINDOW: "install",
             INSTALLED: "resume", VERIFYING: "resume", VERIFIED: "resume"}

#: Refusal classes (§5.3), each a plain reason on the step and the recommendation.
REFUSALS = ("protected_path", "no_reproduction", "tests", "tests_weakened", "scope",
            "dependencies", "too_big", "secrets", "review", "budget", "time", "engine",
            "no_source", "api_suite", "switched_off", "diagnosis", "policy")

#: Step kinds a self-fix answers (review_findings never: those are the app's). `platform_gap` is
#: no step: a gap an agent diagnosed and filed (tools: file_platform_gap), started by a person's
#: press (request_self_fix); its signature starts `platform_gap:`.
SELF_FIX_KINDS = ("deploy", "deploy_repair", "build", "platform_gap")
PLATFORM_GAP = "platform_gap"


def kind_of_signature(signature: str) -> str:
    """The self-fix kind a recommendation's cause signature names."""
    head = str(signature or "").split(":", 1)[0]
    if head == "deploy":
        return "deploy"
    return PLATFORM_GAP if head == PLATFORM_GAP else "build"
CONFIDENCE = ("low", "medium", "high")

EVENT_PREFIX = "self_fix."
#: Identity audit types (identity/audit.py EVENT_TYPES).
AUDIT_TYPES = ("self_fix.started", "self_fix.refused", "self_fix.approved", "self_fix.rejected",
               "self_fix.ticket_issued", "self_fix.installed", "self_fix.rolled_back",
               "self_fix.removed", "self_fix.upstream_sent", "self_fix.policy_changed",
               "self_fix.switch_changed")

SWITCH_KEY = "self_fix"
ENV_SWITCH = "BOSS_SELF_FIX"

DEFAULTS: dict[str, Any] = {
    "self_fix_min_confidence": "medium", "self_fix_budget_cents": 1000,
    "self_fix_month_cents": 5000, "self_fix_max_minutes": 60,
    "self_fix_attempts_per_signature": 2, "self_fix_max_diff_lines": 300,
    "self_fix_health_minutes": 10,
}
#: Auto-install (§8.3) is OFF by default and exists only as a recorded policy here: no code path
#: installs without a person's approval in v1.
AUTO_INSTALL_POLICY = "off"
AUTO_INSTALL_WHY = ("A person approves every install in this version. Installing without asking "
                    "may come later, only on a one-person developer box, for small tier-2 fixes "
                    "that passed every gate the first time; it turns itself off when a second "
                    "person joins.")


class SelfFixError(RuntimeError):
    """A self-fix move BOSS refuses (an illegal transition, an unknown id)."""


# ── the switch and the settings (§8.7, §11.5) ────────────────────────────────────────────────

def env_off() -> bool:
    """`BOSS_SELF_FIX=off` (any Improve-BOSS off word), read on every call: beats everything."""
    from .improve_boss import TELEMETRY_OFF_WORDS

    return (os.environ.get(ENV_SWITCH) or "").strip().lower() in TELEMETRY_OFF_WORDS


def _state(connection: Connection) -> Optional[dict[str, Any]]:
    try:
        with connection.begin_nested():
            value = connection.execute(text("SELECT value FROM system_state WHERE key = :k"),
                                       {"k": SWITCH_KEY}).scalar()
    except Exception:  # noqa: BLE001 — no system_state: no stored switch
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    return value if isinstance(value, dict) else None


def _active_people(connection: Connection) -> int:
    try:
        with connection.begin_nested():
            return int(connection.execute(text(
                "SELECT count(*) FROM identity_principals WHERE kind = 'user' "
                "AND status = 'active'")).scalar() or 0)
    except Exception:  # noqa: BLE001 — no identity: nobody known
        return 0


def switch(connection: Connection) -> dict[str, Any]:
    """{on, stored, set_by, at, why, settings}: the org switch as it decides now."""
    stored = _state(connection)
    if env_off():
        return {"on": False, "stored": bool(stored), "why": f"{ENV_SWITCH} is off",
                "settings": dict((stored or {}).get("settings") or {})}
    if stored is not None and "on" in stored:
        return {"on": bool(stored.get("on")), "stored": True, "set_by": stored.get("set_by"),
                "at": stored.get("at"), "why": "" if stored.get("on") else
                "self-fix is switched off in Govern",
                "settings": dict(stored.get("settings") or {})}
    # THE DEFAULT (§8.7): on for a single-person install that can change itself, off otherwise.
    people = _active_people(connection)
    on = people == 1
    return {"on": on, "stored": False, "settings": {},
            "why": "" if on else ("self-fix starts switched off on an install with more than "
                                  "one person; an Owner turns it on in Govern"
                                  if people > 1 else "nobody has claimed this install yet")}


def set_switch(connection: Connection, *, on: bool, actor: str, why: str = "",
               settings_patch: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Turn self-fix on or off (and change its settings). Audited. Off cancels every self-fix
    still building at its next stage boundary; nothing installed is removed."""
    stored = _state(connection) or {}
    merged = dict(stored.get("settings") or {})
    for key, value in dict(settings_patch or {}).items():
        if key not in DEFAULTS:
            raise SelfFixError(f"'{key}' is not a self-fix setting")
        merged[key] = value
    value = {"on": bool(on), "set_by": actor, "at": now_utc().isoformat(), "why": why[:300],
             "settings": merged, "auto_install": AUTO_INSTALL_POLICY}
    connection.execute(
        text("INSERT INTO system_state (key, value, updated_at) VALUES (:k, CAST(:v AS JSONB), "
             "now()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
        {"k": SWITCH_KEY, "v": json.dumps(value)})
    changed = bool(stored.get("on")) != bool(on) or "on" not in stored
    if changed:
        audit(connection, "self_fix.switch_changed", actor=actor, outcome="success",
              details={"on": bool(on), "why": why[:300]})
    if settings_patch:
        audit(connection, "self_fix.policy_changed", actor=actor, outcome="success",
              details={"settings": dict(settings_patch)})
    if not on:
        # OFF MEANS OFF (§8.7): every self-fix still building, waiting for a person, or approved
        # but not yet taken by the host is cancelled and its tickets revoked. An install the host
        # already took finishes its health window or rolls back; nothing installed is removed.
        for row in connection.execute(text(
                "SELECT id FROM self_fixes WHERE status = ANY(:s)"),
                {"s": sorted(BUILDING | {AWAITING_APPROVAL, APPROVED})}).all():
            cancel(connection, row[0], actor=actor, why="self-fix was switched off")
    return switch(connection)


def setting(connection: Optional[Connection], settings: Any, key: str) -> Any:
    """One self-fix setting: the switch's stored settings, then the install's Settings, then the
    default."""
    stored = (_state(connection) or {}).get("settings") or {} if connection is not None else {}
    if key in stored and stored[key] not in (None, ""):
        return stored[key]
    value = getattr(settings, key, None) if settings is not None else None
    return DEFAULTS.get(key) if value in (None, "") else value


def _int_setting(connection: Optional[Connection], settings: Any, key: str) -> int:
    try:
        return int(setting(connection, settings, key))
    except (TypeError, ValueError):
        return int(DEFAULTS[key])


def source_sha() -> str:
    """The commit this BOSS was built from (stamped into the images at install)."""
    return (os.environ.get("BOSS_SOURCE_SHA") or "").strip()


def running_stack_head() -> str:
    """The head of the local stack this BOSS runs (the release when nothing is installed)."""
    return (os.environ.get("BOSS_STACK_HEAD") or "").strip() or source_sha()


def mirror_path(settings: Any) -> Path:
    return Path(str(getattr(settings, "self_fix_source", "") or
                    "/var/lib/boss/selfsrc/boss.git"))


def install_capable(settings: Any) -> tuple[bool, str]:
    """Can this install change itself? A git install: its source sha is known and the
    self-source mirror holds it (seeded by the host self-installer). An archive install cannot
    (§6.2), and says so."""
    sha = source_sha()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return False, ("this install was not built from a git checkout, so it cannot change "
                       "itself; install from git to enable self-fix")
    mirror = mirror_path(settings)
    if not (mirror / "HEAD").exists() and not (mirror / ".git").exists():
        return False, ("BOSS's own source is not on this machine yet; run `boss self-fix seed` "
                       "once on the host")
    return True, ""


def availability(connection: Connection, settings: Any, *, kind: str = "deploy"
                 ) -> tuple[bool, str]:
    """Stage 0: (a self-fix may start, why not)."""
    if env_off():
        return False, f"self-fix is off on this install ({ENV_SWITCH})"
    state = switch(connection)
    if not state["on"]:
        return False, state.get("why") or "self-fix is switched off"
    if kind not in SELF_FIX_KINDS:
        return False, f"a {kind} step is not one BOSS fixes itself for"
    return install_capable(settings)


# ── the cause ────────────────────────────────────────────────────────────────────────────────

def class_signature(signature: str) -> str:
    """The cause without this install's parameters (§7.4, F13): `deploy:port_held:8000` →
    `deploy:port_held`; `deploy:unwired:res-db` keeps a BOSS-defined service name (`res-*`) and
    drops any other; `<family>:<step>:<digest>` keeps its digest (it already normalises ids and
    numbers)."""
    parts = [p for p in str(signature or "").split(":") if p != ""]
    if len(parts) >= 3 and parts[1] == "port_held":
        return ":".join(parts[:2])
    if len(parts) >= 3 and parts[1] == "unwired":
        return ":".join(parts[:3]) if parts[2].startswith("res-") else ":".join(parts[:2])
    if len(parts) >= 3 and parts[1] == "missing_tool":
        return ":".join(parts[:3])
    return ":".join(parts) or "unknown"


_INJECTION = re.compile(
    r"ignore (?:all |any |the |your |previous |prior )+(?:instructions|rules)|"
    r"disregard (?:the |your |all )?(?:instructions|rules|system)|you are now\b|"
    r"new instructions:|system prompt|<\|im_start\|>|\bassistant:\s|\bas an ai\b|"
    r"(?:edit|change|modify|patch|delete)\s+[\w/]*identity/|(?:disable|skip|waive|remove)\s+"
    r"(?:the |all |every )?(?:gates?|checks?|tests?|reviewers?|approvals?)|"
    r"(?:approve|merge|install)\s+(?:this|it|yourself)\b", re.I)


def injection_markers(value: Any) -> list[str]:
    """Instruction-shaped strings in evidence (§8.8): data that reads like orders. Never
    followed; they lower confidence and flag the card."""
    raw = value if isinstance(value, str) else json.dumps(value, default=str)
    return sorted({m.group(0).strip()[:80] for m in _INJECTION.finditer(raw or "")})


def _occurrences(connection: Connection, klass: str) -> int:
    try:
        with connection.begin_nested():
            rows = connection.execute(text(
                "SELECT signature, occurrences FROM boss_recommendations")).all()
    except Exception:  # noqa: BLE001
        return 0
    return sum(int(r[1] or 1) for r in rows if class_signature(str(r[0])) == klass)


def _code_exists(settings: Any, files: Iterable[str]) -> list[str]:
    """The files a recommendation cites that exist in BOSS's source at the running sha (the
    mirror; the running tree when the mirror cannot answer)."""
    from . import self_fix_source

    found = []
    for raw in files or []:
        path = str(raw or "").split("::")[0].split(":")[0].split("#")[0].strip()
        if not path or "/" not in path:
            continue
        if self_fix_source.file_exists(settings, source_sha(), path):
            found.append(path)
    return found


def score_confidence(*, coded_on_boss: bool, model_on_boss: bool, model_validated: bool,
                     code_found: Iterable[str], occurrences: int, app_action_failed: bool,
                     injection: Iterable[str], disagree: bool) -> dict[str, Any]:
    """§5.2 Stage 1, the table, as points and reasons. high ≥ 5, medium 3–4, low ≤ 2."""
    points, reasons = 0, []
    if coded_on_boss:
        points += 3
        reasons.append({"signal": "coded_triage_agrees", "points": 3,
                        "words": "BOSS's own triage places it on BOSS"})
    if model_on_boss and model_validated:
        points += 2
        reasons.append({"signal": "model_evidence_validated", "points": 2,
                        "words": "the agent chose a BOSS fix and its evidence checked out"})
    found = list(code_found)
    if found:
        points += 1
        reasons.append({"signal": "code_located", "points": 1,
                        "words": f"the code it names exists ({', '.join(found[:3])})"})
    if occurrences >= 2:
        points += 1
        reasons.append({"signal": "recurred", "points": 1,
                        "words": f"seen {occurrences} times"})
    if app_action_failed:
        points += 1
        reasons.append({"signal": "app_fix_did_not_hold", "points": 1,
                        "words": "an app-side fix was already tried for this cause"})
    marks = list(injection)
    if marks:
        points -= 3
        reasons.append({"signal": "instruction_shaped_evidence", "points": -3,
                        "words": "the evidence holds text that reads like instructions "
                                 f"({marks[0]})"})
    if disagree:
        points -= 3
        reasons.append({"signal": "model_and_triage_disagree", "points": -3,
                        "words": "the agent and BOSS's triage disagree on whose bug this is"})
    level = "high" if points >= 5 else "medium" if points >= 3 else "low"
    return {"level": level, "points": points, "reasons": reasons}


def confidence_for(connection: Connection, settings: Any, *, kind: str, entry: Mapping[str, Any],
                   evidence: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    """The confidence of the recovery decision `entry` that a failure is BOSS's own."""
    from . import step_recovery

    signature = str(entry.get("signature") or "")
    failure = evidence.get("failure") or {}
    pattern = {"pattern": entry.get("pattern")} if entry.get("pattern") else \
        step_recovery.cause_signature(kind, failure)[1]
    coded = step_recovery.coded_decision(kind, evidence, signature=signature, pattern=pattern,
                                         last={}, allowed=step_recovery.KINDS.get(kind, ()))
    coded_on_boss = coded.get("action") in step_recovery.BOSS_SIDE_ACTIONS
    by = entry.get("by") or {}
    by_model = str(by.get("engine") or "") not in ("", "coded", "none")
    model_on_boss = by_model and entry.get("action") in step_recovery.BOSS_SIDE_ACTIONS
    disagree = by_model and model_on_boss and coded.get("action") in (
        step_recovery.FIX_APP, step_recovery.CLEAN_AND_RETRY)
    files = list((entry.get("boss_fix") or {}).get("files") or [])
    app_failed = any(isinstance(d, Mapping) and d.get("signature") == signature
                     and d.get("action") in (step_recovery.FIX_APP, step_recovery.CLEAN_AND_RETRY)
                     for d in state.get("decisions") or [])
    return score_confidence(
        coded_on_boss=coded_on_boss, model_on_boss=model_on_boss,
        model_validated=model_on_boss, code_found=_code_exists(settings, files),
        occurrences=_occurrences(connection, class_signature(signature)),
        app_action_failed=app_failed, injection=injection_markers(evidence),
        disagree=disagree)


def meets(level: str, minimum: str) -> bool:
    try:
        return CONFIDENCE.index(level) >= CONFIDENCE.index(minimum)
    except ValueError:
        return False


def mender(connection: Connection) -> dict[str, Any]:
    """Mender's Passport, made once per install (idempotent). Returns {agent_id, passport}: the
    passport is absent on an install whose identity has no Owner yet; the self-fix still records
    Mender's role as its author, and its audit names the role."""
    try:
        from ..identity import passports, users
        from ..identity.migration import default_org

        with connection.begin_nested():
            if passports.get(MENDER_AGENT_ID, db=connection) is not None:
                return {"agent_id": MENDER_AGENT_ID, "passport": True}
            org = default_org(connection)
            owners = sorted(users.active_owner_ids(connection, org)) if org else []
            if not owners:
                return {"agent_id": ACTOR_AGENT, "passport": False}
            passports.ensure_passport(
                org_id=org, agent_id=MENDER_AGENT_ID, role_key=ACTOR_AGENT,
                display_name=f"{AGENT_NAME} (BOSS maintainer)", sponsor_principal_id=owners[0],
                db=connection, autonomous=True, can_spawn=False, max_depth=0,
                created_by=SYSTEM_ACTOR)
            for key in MENDER_CEILING:
                connection.execute(text(
                    "INSERT INTO identity_agent_ceilings (agent_id, permission_key) VALUES "
                    "(:a, :k) ON CONFLICT DO NOTHING"), {"a": MENDER_AGENT_ID, "k": key})
            return {"agent_id": MENDER_AGENT_ID, "passport": True}
    except Exception as error:  # noqa: BLE001 — no identity schema: the role still authors it
        log.info("Mender's passport not made: %s", error)
        return {"agent_id": ACTOR_AGENT, "passport": False}


# ── budget (§8.4) ────────────────────────────────────────────────────────────────────────────

def month_spent(connection: Connection) -> int:
    start = now_utc().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        with connection.begin_nested():
            return int(connection.execute(text(
                "SELECT COALESCE(SUM(GREATEST(cost_micros, budget_usd_micros)), 0) FROM "
                "self_fixes WHERE created_at >= :s AND status <> 'cancelled'"),
                {"s": start}).scalar() or 0)
    except Exception:  # noqa: BLE001
        return 0


def attempts_for(connection: Connection, klass: str) -> int:
    with connection.begin_nested():
        return int(connection.execute(text(
            "SELECT count(*) FROM self_fixes WHERE class_signature = :c AND status NOT IN "
            "('cancelled', 'superseded')"), {"c": klass}).scalar() or 0)


def _reserve(connection: Connection, settings: Any, project_id: str, sfx_id: str,
             micros: int, minutes: int) -> tuple[Optional[str], str]:
    """(reservation id, why not). `budget.reserve(category="self_fix")`: BOSS's spend on itself,
    never charged to the step's recovery budget."""
    from .budget import BudgetGovernor

    try:
        with connection.begin_nested():
            got = BudgetGovernor(settings).reserve(
                connection, project_id, "self_fix", micros,
                f"self-fix {sfx_id}: diagnosis, build and reviews", minutes + 30, risk_tier=2)
    except Exception as error:  # noqa: BLE001 — no budget answer: no self-fix (fail closed)
        return None, f"the self-fix budget could not be reserved ({type(error).__name__})"
    if str(got.get("status")) != "reserved":
        return None, str(got.get("message") or "the budget refused the self-fix")
    return str(got.get("reservation_id") or ""), ""


def release_budget(connection: Connection, settings: Any, sfx: Mapping[str, Any],
                   reason: str) -> None:
    if not sfx.get("budget_reservation_id"):
        return
    from .budget import BudgetGovernor

    try:
        with connection.begin_nested():
            BudgetGovernor(settings).release(connection, str(sfx["project_id"]),
                                             str(sfx["budget_reservation_id"]), reason)
    except Exception:  # noqa: BLE001 — the reservation expires by itself
        log.info("self-fix %s budget not released", sfx.get("id"))


# ── the record ───────────────────────────────────────────────────────────────────────────────

_JSON = ("confidence_reasons", "evidence", "diagnosis", "flags", "paths", "diff_stat",
         "red_green", "gates", "review", "pr", "progress", "verification")


def _row(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    # The fix commit as a git bundle is binary and only the host self-installer and the upstream
    # send read it (`bundle_of`): never in a record a page or a tool is handed.
    out["has_bundle"] = bool(out.pop("bundle", None))
    for key in _JSON:
        value = out.get(key)
        if isinstance(value, str):
            try:
                out[key] = json.loads(value)
            except ValueError:
                pass
    for key, value in list(out.items()):
        if isinstance(value, datetime):
            out[key] = value.isoformat()
    out["chip"] = chip_of(out)
    out["stage"] = _STAGE_OF.get(str(out.get("status")))
    return out


def chip_of(sfx: Mapping[str, Any]) -> str:
    """The PRD's status chip (§9). A verified fix the person sent is "Sent upstream"; one the
    release now holds (merged upstream, or dropped from the stack because the release has it) is
    "In release"; otherwise the status decides."""
    status = str(sfx.get("status") or "")
    upstream = str(sfx.get("upstream_state") or "")
    if status == SUPERSEDED or upstream == "merged" or sfx.get("stack_state") == \
            "dropped_upstream":
        return "In release"
    if status == VERIFIED and upstream == "sent":
        return "Sent upstream"
    if status in (INSTALLED, VERIFYING, VERIFIED) and sfx.get("stack_state") == "removed":
        return "Rolled back"  # a person removed it from the box
    return CHIPS.get(status, "Building")


def get(connection: Connection, sfx_id: str, *, lock: bool = False) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT * FROM self_fixes WHERE id = :i" + (" FOR UPDATE" if lock else "")),
        {"i": sfx_id}).mappings().first()
    return _row(row) if row else None


def bundle_of(connection: Connection, sfx_id: str) -> Optional[bytes]:
    value = connection.execute(text("SELECT bundle FROM self_fixes WHERE id = :i"),
                               {"i": sfx_id}).scalar()
    return bytes(value) if value else None


def list_self_fixes(connection: Connection, *, project_id: Optional[str] = None,
                    status: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT * FROM self_fixes WHERE (CAST(:p AS TEXT) IS NULL OR project_id = :p) "
        "AND (CAST(:s AS TEXT) IS NULL OR status = :s) ORDER BY created_at DESC LIMIT :n"),
        {"p": project_id, "s": status, "n": max(1, min(500, limit))}).mappings().all()
    out = []
    for r in rows:
        item = _row(r)
        item.get("pr", {}).pop("diff", None) if isinstance(item.get("pr"), dict) else None
        out.append(item)
    return out


def _org_id(connection: Connection) -> Optional[str]:
    try:
        with connection.begin_nested():
            return connection.execute(text("SELECT id FROM identity_orgs ORDER BY created_at "
                                           "LIMIT 1")).scalar()
    except Exception:  # noqa: BLE001
        return None


def audit(connection: Connection, event_type: str, *, actor: str, outcome: str = "success",
          sfx: Optional[Mapping[str, Any]] = None, details: Optional[Mapping[str, Any]] = None
          ) -> None:
    """The hash-chained identity audit (§8.5) for the security-relevant moves. Never raises:
    the product event (`emit`) is the record a run page reads; this is the security log's."""
    try:
        from ..identity import audit as identity_audit
    except Exception:  # noqa: BLE001
        return
    org = _org_id(connection)
    if not org:
        return
    kind = "agent" if actor == ACTOR_AGENT else "system" if actor.startswith("sys_") else "user"
    try:
        with connection.begin_nested():
            identity_audit.record(identity_audit.SecurityEvent(
                org_id=org, event_type=event_type,
                outcome=identity_audit.Outcome(outcome) if outcome in ("success", "failure",
                                                                       "denied")
                else identity_audit.Outcome.SUCCESS,
                actor_principal_id=actor, actor_kind=kind, target_type="self_fix",
                target_id=str((sfx or {}).get("id") or "") or None,
                details={**dict(details or {}),
                         **({"signature": sfx.get("class_signature")} if sfx else {})}),
                db=connection)
    except Exception as error:  # noqa: BLE001
        log.warning("self-fix audit %s not written: %s", event_type, error)


def emit(connection: Connection, sfx: Mapping[str, Any], event: str, *, actor: str,
         sentence: str, data: Optional[Mapping[str, Any]] = None) -> None:
    """`self_fix.<event>` with an actor and a sentence (R14): on the run when there is one, so
    the run page tells the story, and always naming the self-fix."""
    payload = {"self_fix_id": sfx.get("id"), "status": sfx.get("status"),
               "signature": sfx.get("signature"), "class_signature": sfx.get("class_signature"),
               "step_id": sfx.get("step_id"), "sentence": sentence[:600], **dict(data or {})}
    run_id = sfx.get("run_id")
    emit_event(connection, str(sfx["project_id"]), EVENT_PREFIX + event, actor,
               "workflow_run" if run_id else "self_fix", str(run_id or sfx["id"]),
               json.loads(json.dumps(payload, default=str)))


_EVENT_OF = {CLAIMED: "claimed", DIAGNOSING: "diagnosing", REPRODUCING: "reproduced",
             FIXING: "fixed", CHECKING: "checking", REVIEWING: "reviewed",
             AWAITING_APPROVAL: "awaiting_approval", APPROVED: "approved", REJECTED: "rejected",
             INSTALLING: "installing", HEALTH_WINDOW: "health_window", INSTALLED: "installed",
             VERIFYING: "verifying", VERIFIED: "verified", NOT_RESOLVED: "not_resolved",
             ROLLING_BACK: "rolling_back", ROLLED_BACK: "rolled_back", REFUSED: "refused",
             CANCELLED: "cancelled", INSTALL_FAILED: "install_failed",
             SUPERSEDED: "superseded"}
_AUDIT_OF = {APPROVED: "self_fix.approved", REJECTED: "self_fix.rejected",
             INSTALLED: "self_fix.installed", ROLLED_BACK: "self_fix.rolled_back",
             REFUSED: "self_fix.refused"}

_UPDATABLE = {"location", "area", "refusal", "refusal_detail", "diagnosis", "base_sha",
              "fix_commit_sha", "fix_tree_sha", "branch", "tier", "flags", "paths", "diff_stat",
              "red_green", "gates", "review", "pr", "progress", "decision_id", "proposal_id",
              "engine", "harness", "reviewer_engine", "cost_micros", "repair_rounds",
              "installed_stack_head", "installed_at", "resumed_at", "verified_at",
              "rolled_back_at", "verification", "stack_state", "started_at", "evidence"}


def update(connection: Connection, sfx_id: str, **fields: Any) -> None:
    """Write fields of a self-fix (never its status: `transition` moves that)."""
    bad = set(fields) - _UPDATABLE
    if bad:
        raise SelfFixError(f"not updatable: {sorted(bad)}")
    if not fields:
        return
    sets, params = [], {"i": sfx_id}
    for key, value in fields.items():
        if key in _JSON:
            sets.append(f"{key} = CAST(:{key} AS JSONB)")
            params[key] = json.dumps(value, default=str)
        else:
            sets.append(f"{key} = :{key}")
            params[key] = value
    connection.execute(text(f"UPDATE self_fixes SET {', '.join(sets)}, updated_at = now() "  # noqa: S608
                            "WHERE id = :i"), params)


def transition(connection: Connection, sfx_id: str, to: str, *, actor: str, sentence: str,
               data: Optional[Mapping[str, Any]] = None, **fields: Any) -> dict[str, Any]:
    """Move a self-fix to `to` — only along `TRANSITIONS` — with the actor, the event, the
    audit, and what the end of a self-fix means for its claim, waiters and recommendation."""
    sfx = get(connection, sfx_id, lock=True)
    if sfx is None:
        raise SelfFixError(f"no self-fix {sfx_id}")
    was = str(sfx["status"])
    if to not in TRANSITIONS.get(was, frozenset()):
        raise SelfFixError(f"a self-fix cannot go from {was} to {to}")
    connection.execute(text("UPDATE self_fixes SET status = :s, updated_at = now() WHERE id = :i"),
                       {"s": to, "i": sfx_id})
    if fields:
        update(connection, sfx_id, **fields)
    progress = {"stage": _STAGE_OF.get(to), "sentence": sentence[:600],
                "at": now_utc().isoformat(), "status": to}
    update(connection, sfx_id, progress=progress)
    sfx = get(connection, sfx_id)
    emit(connection, sfx, _EVENT_OF.get(to, to), actor=actor, sentence=sentence,
         data={"from": was, **dict(data or {})})
    if to in _AUDIT_OF:
        audit(connection, _AUDIT_OF[to], actor=actor, sfx=sfx,
              outcome="failure" if to in (REFUSED, ROLLED_BACK) else "success",
              details={"from": was, **{k: v for k, v in dict(data or {}).items()
                                       if isinstance(v, (str, int, bool, float))}})
    if to in TERMINAL or to == NOT_RESOLVED:
        _ended(connection, sfx, to, actor=actor, sentence=sentence)
    refresh_step_views(connection, sfx)
    return sfx


def refresh_step_views(connection: Connection, sfx: Mapping[str, Any]) -> None:
    """Every step that shows this self-fix (its own, and steps waiting on it) shows it as it is
    NOW: one place, on every move, so a strip can never say "Install" over a verified fix."""
    view = {k: v for k, v in step_view(sfx).items() if k != "waiting_on"}
    try:
        with connection.begin_nested():
            connection.execute(text(
                "UPDATE workflow_step_runs SET output = jsonb_set(output, '{self_fix}', "
                "COALESCE(output->'self_fix', '{}'::jsonb) || CAST(:v AS JSONB)), "
                "updated_at = now() WHERE output->'self_fix'->>'id' = :i"),
                {"i": str(sfx["id"]), "v": json.dumps(view, default=str)})
    except Exception as error:  # noqa: BLE001 — no workflow tables (an engine-only test)
        log.debug("self-fix %s step views not refreshed: %s", sfx.get("id"), error)


def say(connection: Connection, sfx_id: str, sentence: str, *, stage: Optional[str] = None
        ) -> None:
    """The parked step's live line, without a move ("Mender: the new test fails on BOSS as it
    is, as expected. Making it pass.")."""
    sfx = get(connection, sfx_id)
    if not sfx:
        return
    progress = {"stage": stage or _STAGE_OF.get(str(sfx["status"])), "sentence": sentence[:600],
                "at": now_utc().isoformat(), "status": sfx["status"]}
    update(connection, sfx_id, progress=progress)
    emit(connection, sfx, "progress", actor=ACTOR_AGENT, sentence=sentence,
         data={"stage": progress["stage"]})
    refresh_step_views(connection, {**sfx, "progress": progress})


def refuse(connection: Connection, sfx_id: str, refusal: str, detail: str, *,
           actor: str = ACTOR_AGENT, settings: Any = None) -> dict[str, Any]:
    """A self-fix BOSS will not carry on with (§5.3): the reason on the step and on the
    recommendation, the claim released, the budget released."""
    if refusal not in REFUSALS:
        refusal = "policy"
    sfx = get(connection, sfx_id)
    if sfx is None or sfx["status"] in TERMINAL:
        return sfx or {}
    sentence = f"{AGENT_NAME}: BOSS did not fix itself — {refusal_words(refusal, detail)}"
    out = transition(connection, sfx_id, REFUSED, actor=actor, sentence=sentence,
                     data={"refusal": refusal}, refusal=refusal, refusal_detail=detail[:2000])
    release_budget(connection, settings, out, f"self-fix refused: {refusal}")
    return out


def refusal_words(refusal: str, detail: str) -> str:
    lead = {
        "protected_path": "this needs a change to a part of BOSS it never changes by itself",
        "no_reproduction": "BOSS could not reproduce this in a test",
        "tests": "its fix did not make the tests pass",
        "tests_weakened": "the change would have weakened a test",
        "scope": "the change went outside what the diagnosis is about",
        "dependencies": "the fix needs a new dependency or image",
        "too_big": "the fix is too big for a self-fix",
        "secrets": "the change held a secret-shaped value",
        "review": "BOSS's reviewers left findings open",
        "budget": "BOSS spent its self-fix budget without a passing fix",
        "time": "BOSS ran out of its self-fix time without a passing fix",
        "engine": "no engine could build the fix",
        "no_source": "BOSS's own source is not available here",
        "api_suite": "the fix touches apps/api, which no self-fix test suite covers yet",
        "switched_off": "self-fix was switched off",
        "diagnosis": "BOSS could not locate the bug in its own code",
    }.get(refusal, "a self-fix rule refused it")
    return f"{lead}: {detail}".strip().rstrip(":")[:600] if detail else lead


def cancel(connection: Connection, sfx_id: str, *, actor: str, why: str) -> dict[str, Any]:
    """Stop a self-fix that has not reached the box (Stop, the kill switch, a person). An install
    in progress is never stopped half-way: it finishes or rolls back (§8.7)."""
    sfx = get(connection, sfx_id)
    if sfx is None or sfx["status"] in TERMINAL or sfx["status"] in ON_THE_BOX:
        return sfx or {}
    if CANCELLED not in TRANSITIONS.get(str(sfx["status"]), frozenset()):
        return sfx
    from . import self_fix_install

    self_fix_install.revoke_tickets(connection, sfx_id, why=why)
    return transition(connection, sfx_id, CANCELLED, actor=actor,
                      sentence=f"Self-fix stopped: {why}. Nothing was installed.",
                      data={"why": why})


def _ended(connection: Connection, sfx: Mapping[str, Any], to: str, *, actor: str,
           sentence: str) -> None:
    """The claim, the waiters and the recommendation, when a self-fix ends."""
    from . import boss_recommendations

    if to in (VERIFIED, REFUSED, REJECTED, CANCELLED, ROLLED_BACK, INSTALL_FAILED, SUPERSEDED,
              NOT_RESOLVED):
        connection.execute(text("DELETE FROM self_fix_claims WHERE self_fix_id = :i"),
                           {"i": sfx["id"]})
    rec_id = sfx.get("recommendation_id")
    if not rec_id:
        return
    if to == VERIFIED:
        boss_recommendations.set_status(connection, str(sfx["project_id"]), str(rec_id),
                                        "implemented")
    elif to == SUPERSEDED:
        boss_recommendations.set_status(connection, str(sfx["project_id"]), str(rec_id), "done")
    elif to in (REFUSED, REJECTED, CANCELLED, ROLLED_BACK, INSTALL_FAILED, NOT_RESOLVED):
        boss_recommendations.record_attempt(connection, str(sfx["project_id"]), str(rec_id), {
            "self_fix_id": sfx["id"], "outcome": to, "refusal": sfx.get("refusal"),
            "why": sentence[:600], "diagnosis": (sfx.get("diagnosis") or {}).get("hypothesis"),
            "location": sfx.get("location"), "at": now_utc().isoformat()})


# ── Stage 1–2: proposing a self-fix ──────────────────────────────────────────────────────────

def _live_claim(connection: Connection, klass: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT c.self_fix_id, f.status FROM self_fix_claims c JOIN self_fixes f "
        "ON f.id = c.self_fix_id WHERE c.class_signature = :c FOR UPDATE OF c"),
        {"c": klass}).mappings().first()
    if row is None:
        return None
    if row["status"] in TERMINAL:
        connection.execute(text("DELETE FROM self_fix_claims WHERE class_signature = :c"),
                           {"c": klass})
        return None
    return get(connection, str(row["self_fix_id"]))


def propose(connection: Connection, settings: Any, *, project_id: str, run_id: str,
            step_id: str, kind: str, entry: Mapping[str, Any], evidence: Mapping[str, Any],
            state: Mapping[str, Any], recommendation_id: Optional[str],
            jobs: Any = None, started_by: Optional[str] = None) -> dict[str, Any]:
    """A BOSS-side recovery decision, considered for a self-fix (R1). Returns one of
    {"outcome": "started", self_fix} — a self-fix claimed and its build queued;
    {"outcome": "waiting_on", self_fix} — another self-fix holds this cause (R19);
    {"outcome": "skipped", why, confidence?} — today's behaviour runs, and says why."""
    ok, why = availability(connection, settings, kind=kind)
    if not ok:
        return {"outcome": "skipped", "why": why}
    signature = str(entry.get("signature") or "")
    klass = class_signature(signature)
    # A cause already being fixed: wait on that fix, whatever the confidence of this decision.
    holder = _live_claim(connection, klass)
    if holder is not None:
        connection.execute(text(
            "INSERT INTO self_fix_waiters (self_fix_id, run_id, step_id, project_id) VALUES "
            "(:f, :r, :s, :p) ON CONFLICT DO NOTHING"),
            {"f": holder["id"], "r": run_id, "s": step_id, "p": project_id})
        emit(connection, holder, "waiting_on", actor=SYSTEM_ACTOR,
             sentence=f"BOSS is already fixing this ({holder['id']}); this step waits for it.",
             data={"waiting_run_id": run_id, "waiting_step_id": step_id})
        return {"outcome": "waiting_on", "self_fix": holder}
    confidence = confidence_for(connection, settings, kind=kind, entry=entry,
                                evidence=evidence, state=state)
    minimum = str(setting(connection, settings, "self_fix_min_confidence") or "medium")
    if started_by:
        # A PERSON chose to start it from the recommendation: their choice is the threshold.
        # The confidence BOSS computed is still recorded and shown.
        confidence = {**confidence, "reasons": [*confidence["reasons"], {
            "signal": "started_by_a_person", "points": 0,
            "words": f"{started_by} started this self-fix from the recommendation"}]}
    elif not meets(confidence["level"], minimum):
        return {"outcome": "skipped", "confidence": confidence,
                "why": ("BOSS was not sure enough this is its own bug to change itself "
                        f"(confidence {confidence['level']}; it needs {minimum})")}
    cap = _int_setting(connection, settings, "self_fix_attempts_per_signature")
    if attempts_for(connection, klass) >= cap:
        return {"outcome": "skipped", "confidence": confidence,
                "why": (f"BOSS already tried to fix {klass} by itself {cap} time(s); it stays "
                        "a recommendation for a person")}
    budget_micros = _int_setting(connection, settings, "self_fix_budget_cents") * 10_000
    month_micros = _int_setting(connection, settings, "self_fix_month_cents") * 10_000
    if month_spent(connection) + budget_micros > month_micros:
        return {"outcome": "skipped", "confidence": confidence,
                "why": (f"BOSS's self-fix budget for this month (${month_micros / 1e6:.2f}) "
                        "has no room for another fix")}
    from . import boss_recommendations, self_fix_privacy

    sfx_id = make_id(ID_PREFIX)
    author = mender(connection)["agent_id"]
    known = self_fix_privacy.known_hosts(connection)
    fix = dict(entry.get("boss_fix") or {})
    failure = dict(evidence.get("failure") or {})
    masked_evidence = self_fix_privacy.mask_value({
        "symptom": str(failure.get("headline") or failure.get("why") or failure.get("error")
                       or "")[:2000],
        "cause": str(entry.get("cause") or "")[:600],
        "evidence": [str(e)[:600] for e in entry.get("evidence") or []][:8],
        "log_tail": str(failure.get("log_tail") or "")[-3000:],
        "hypothesis": {"title": str(fix.get("title") or "")[:300],
                       "root_cause": str(fix.get("root_cause") or "")[:1500],
                       "proposed_change": str(fix.get("proposed_change") or "")[:3000],
                       "files": [str(f)[:200] for f in fix.get("files") or []][:12],
                       # "Done when" for a gap an agent filed: what the fix must make pass.
                       "acceptance_tests": [str(t)[:600] for t in
                                            fix.get("acceptance_tests") or []][:12]},
        "host_state": {k: v for k, v in (evidence.get("host_state") or {}).items()
                       if k in ("port", "port_holders", "own_projects", "keep_project",
                                "target_dir")},
        "deployment_id": failure.get("deployment_id"),
        "build_run_id": failure.get("build_run_id"),
        "injection": injection_markers(evidence),
    }, known)
    from .self_fix_policy import area_of

    connection.execute(text(
        "INSERT INTO self_fixes (id, org_id, project_id, recommendation_id, run_id, step_id, "
        "step_kind, signature, class_signature, area, status, confidence, confidence_reasons, "
        "evidence, base_sha, created_by, budget_usd_micros) VALUES (:i, :o, :p, :r, :run, :st, "
        ":k, :sig, :c, :a, :status, :conf, CAST(:reasons AS JSONB), CAST(:ev AS JSONB), :base, "
        ":by, :b)"),
        {"i": sfx_id, "o": _org_id(connection), "p": project_id, "r": recommendation_id,
         "run": run_id, "st": step_id, "k": kind, "sig": signature[:300], "c": klass[:300],
         "a": area_of(fix.get("files") or []) if fix.get("files") else
         ("deploy" if kind.startswith("deploy") else
          "engine" if kind == PLATFORM_GAP else "build"),
         "status": PROPOSED, "conf": confidence["level"],
         "reasons": json.dumps(confidence["reasons"]), "ev": json.dumps(masked_evidence),
         "base": running_stack_head(), "by": author, "b": budget_micros})
    sfx = get(connection, sfx_id)
    emit(connection, sfx, "proposed", actor=ACTOR_AGENT,
         sentence=(f"{AGENT_NAME}: this is a bug in BOSS, not in your app, so BOSS is fixing "
                   f"itself (confidence {confidence['level']})."),
         data={"confidence": confidence["level"], "points": confidence["points"],
               "recommendation_id": recommendation_id})
    inserted = connection.execute(text(
        "INSERT INTO self_fix_claims (class_signature, self_fix_id) VALUES (:c, :i) "
        "ON CONFLICT DO NOTHING"), {"c": klass, "i": sfx_id}).rowcount
    if not inserted:  # a race lost to another step this very moment: wait on the winner
        refuse(connection, sfx_id, "policy", "another self-fix claimed this cause first",
               settings=settings)
        winner = _live_claim(connection, klass)
        if winner is not None:
            connection.execute(text(
                "INSERT INTO self_fix_waiters (self_fix_id, run_id, step_id, project_id) VALUES "
                "(:f, :r, :s, :p) ON CONFLICT DO NOTHING"),
                {"f": winner["id"], "r": run_id, "s": step_id, "p": project_id})
            return {"outcome": "waiting_on", "self_fix": winner}
        return {"outcome": "skipped", "why": "another self-fix claimed this cause first"}
    reservation, why_not = _reserve(connection, settings, project_id, sfx_id, budget_micros,
                                    _int_setting(connection, settings, "self_fix_max_minutes"))
    if reservation is None:
        refuse(connection, sfx_id, "budget", why_not, settings=settings)
        return {"outcome": "skipped", "why": why_not, "confidence": confidence}
    connection.execute(text("UPDATE self_fixes SET budget_reservation_id = :r WHERE id = :i"),
                       {"r": reservation, "i": sfx_id})
    if recommendation_id:
        boss_recommendations.link_self_fix(connection, project_id, recommendation_id, sfx_id,
                                           klass)
    sfx = transition(connection, sfx_id, CLAIMED, actor=ACTOR_AGENT,
                     sentence=(f"{AGENT_NAME}: this is a bug in BOSS, not in your app, so BOSS "
                               f"is fixing itself ({klass}); reading BOSS's own code first."),
                     data={"recommendation_id": recommendation_id},
                     started_at=now_utc())
    audit(connection, "self_fix.started", actor=ACTOR_AGENT, sfx=sfx,
          details={"run_id": run_id, "step_id": step_id, "confidence": confidence["level"]})
    enqueue_build(connection, sfx, jobs=jobs)
    return {"outcome": "started", "self_fix": sfx, "confidence": confidence}


def start_from_recommendation(connection: Connection, settings: Any, *, project_id: str,
                              recommendation_id: str, actor: str, jobs: Any = None
                              ) -> dict[str, Any]:
    """POST /v1/self-fixes: a person starts a self-fix from an open recommendation."""
    from . import boss_recommendations

    rec = next((r for r in boss_recommendations.list_recommendations(connection, project_id)
                if r["id"] == recommendation_id), None)
    if rec is None:
        raise SelfFixError("no such recommendation in this project")
    if rec["status"] not in ("open",):
        raise SelfFixError(f"this recommendation is {rec['status']}; only an open one can be "
                           "fixed")
    ref = next((r for r in reversed(rec.get("refs") or []) if isinstance(r, Mapping)), {})
    signature = str(rec["signature"])
    kind = kind_of_signature(signature)
    entry = {"signature": signature, "action": "fix_boss", "by": {"engine": "person"},
             "cause": rec.get("title"), "evidence": rec.get("evidence") or [],
             "boss_fix": {"title": rec.get("title"), "root_cause": rec.get("root_cause"),
                          "proposed_change": rec.get("proposed_change"),
                          "files": rec.get("files") or [],
                          "acceptance_tests": rec.get("acceptance_tests") or []}}
    evidence = {"failure": {"headline": rec.get("symptom") or rec.get("title"),
                            "deployment_id": ref.get("deployment_id"),
                            "build_run_id": ref.get("build_id")}}
    return propose(connection, settings, project_id=project_id,
                   run_id=str(ref.get("run_id") or ""), step_id=str(ref.get("step_id") or ""),
                   kind=kind, entry=entry, evidence=evidence, state={},
                   recommendation_id=recommendation_id, jobs=jobs, started_by=actor)


BUILD_JOB = "self_fix.build"


def enqueue_build(connection: Connection, sfx: Mapping[str, Any], *, jobs: Any = None,
                  round_note: str = "") -> None:
    """Queue the Mender build (a worker job; the API runs it under the job's lease)."""
    from .jobs import enqueue_job

    enqueue_job(connection, str(sfx["project_id"]), BUILD_JOB,
                {"self_fix_id": sfx["id"], **({"note": round_note[:3000]} if round_note else {})},
                priority=60, max_attempts=1,
                dedupe_key=f"{BUILD_JOB}:{sfx['id']}:{int(sfx.get('repair_rounds') or 0)}"
                           f"{':' + str(abs(hash(round_note)) % 10**8) if round_note else ''}")


# ── the step's handle (§11.2) ────────────────────────────────────────────────────────────────

AWAIT_KIND = "self_fix"


def handle_for(sfx: Mapping[str, Any], *, step_kind: str, waiting_on: bool = False,
               decision: Any = None, build_run_id: Optional[str] = None) -> dict[str, Any]:
    return {"kind": AWAIT_KIND, "id": sfx["id"], "then": "resume", "step_kind": step_kind,
            "signature": sfx.get("signature"), "decision": decision,
            **({"waiting_on": True} if waiting_on else {}),
            **({"build_run_id": build_run_id} if build_run_id else {})}


def handle_verdict(connection: Connection, handle: Mapping[str, Any]
                   ) -> tuple[str, str, Optional[dict[str, Any]]]:
    """("wait" | "resume" | "fallback", why, the self-fix). Resume only when the fix is
    installed AND the running build is the stack that holds it (F5): a restarted API that is
    still the old build keeps waiting; a self-fix that ended without holding falls back to
    today's wait for a BOSS update."""
    sfx = get(connection, str(handle.get("id") or ""))
    if sfx is None:
        return "fallback", "the self-fix is gone", None
    status = str(sfx["status"])
    if status in FELL_BACK:
        return "fallback", refusal_words(str(sfx.get("refusal") or ""), str(
            sfx.get("refusal_detail") or "")) if status == REFUSED else f"the self-fix ended {status}", sfx
    if status in (INSTALLED, VERIFIED) or (status == VERIFYING and handle.get("waiting_on")):
        head = str(sfx.get("installed_stack_head") or "")
        if head and running_stack_head() and running_stack_head() != head \
                and not _stack_contains(connection, sfx):
            return "wait", "the fix is installed; BOSS is restarting into it", sfx
        return "resume", "the fix is installed and running", sfx
    return "wait", str((sfx.get("progress") or {}).get("sentence") or status), sfx


def _stack_contains(connection: Connection, sfx: Mapping[str, Any]) -> bool:
    """A later self-fix on top of this one moved the stack head on: this fix is still in it."""
    running = running_stack_head()
    if not running:
        return False
    row = connection.execute(text(
        "SELECT 1 FROM self_fixes WHERE installed_stack_head = :h AND status IN "
        "('installed', 'verifying', 'verified') AND installed_at >= :at LIMIT 1"),
        {"h": running, "at": sfx.get("installed_at") or now_utc()}).first()
    return row is not None


def mark_resumed(connection: Connection, sfx_id: str, *, run_id: str, step_id: str,
                 own: bool) -> None:
    """The step this self-fix was for resumed: the next outcome of that step is the verdict."""
    sfx = get(connection, sfx_id)
    if not sfx or not own or sfx["status"] != INSTALLED:
        return
    transition(connection, sfx_id, VERIFYING, actor=SYSTEM_ACTOR,
               sentence=f"BOSS fixed itself ({sfx_id}) — resuming the {step_id} step to prove it.",
               resumed_at=now_utc(),
               verification={"run_id": run_id, "step_id": step_id,
                             "resumed_at": now_utc().isoformat()})


def waiters(connection: Connection, sfx_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(text(
        "SELECT run_id, step_id, project_id FROM self_fix_waiters WHERE self_fix_id = :i"),
        {"i": sfx_id}).mappings().all()]


# ── Stage 12: the verdict ────────────────────────────────────────────────────────────────────

def _after(entry: Mapping[str, Any], since: Optional[str]) -> bool:
    if not since:
        return True
    try:
        return datetime.fromisoformat(str(entry.get("at"))) >= datetime.fromisoformat(since)
    except (TypeError, ValueError):
        return False


def verdict_of_step(connection: Connection, sfx: Mapping[str, Any]
                    ) -> Optional[dict[str, Any]]:
    """What the resumed step says about the fix, or None while it is still deciding:
    {outcome: passed|new_cause|same_cause|inconclusive, next_signature?}."""
    from . import step_recovery

    verification = sfx.get("verification") or {}
    since = str(verification.get("resumed_at") or sfx.get("resumed_at") or "")
    row = connection.execute(text(
        "SELECT s.status, s.output, s.last_error, r.status AS run_status FROM workflow_step_runs s "
        "JOIN workflow_runs r ON r.id = s.workflow_run_id WHERE s.workflow_run_id = :r "
        "AND s.step_id = :s ORDER BY s.updated_at DESC LIMIT 1"),
        {"r": sfx.get("run_id"), "s": sfx.get("step_id")}).mappings().first()
    if row is None:
        return {"outcome": "inconclusive", "why": "the step is gone"}
    output = row["output"] or {}
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            output = {}
    if str(row["status"]) == "succeeded":
        return {"outcome": "passed"}
    if str(row["status"]) == "skipped":
        return {"outcome": "inconclusive", "why": "a person skipped the step"}
    decisions = [d for d in (step_recovery.state_of(output).get("decisions") or [])
                 if isinstance(d, Mapping) and d.get("signature") and _after(d, since)
                 and d.get("action") in step_recovery.ACTIONS]
    if decisions:
        first = decisions[0]
        nxt = str(first.get("signature"))
        same = class_signature(nxt) == str(sfx.get("class_signature"))
        return {"outcome": "same_cause" if same else "new_cause", "next_signature": nxt,
                "decision": first.get("n")}
    if str(row["status"]) in ("failed", "deadlettered"):
        nxt, _ = step_recovery.cause_signature(str(sfx.get("step_kind") or "deploy"),
                                               {"headline": str(row["last_error"] or "")})
        same = class_signature(nxt) == str(sfx.get("class_signature"))
        return {"outcome": "same_cause" if same else "new_cause", "next_signature": nxt}
    if str(row["run_status"]) in ("cancelled", "failed", "completed") \
            and str(row["status"]) not in ("running", "queued", "waiting"):
        return {"outcome": "inconclusive", "why": f"the run ended ({row['run_status']})"}
    return None


def settle_verification(connection: Connection, settings: Any, sfx_id: str
                        ) -> Optional[dict[str, Any]]:
    """Stage 12 for one verifying self-fix. Passed or a new cause → verified (F18); the same
    cause → not resolved, rolled back (R7, decision 4); a run that ended → inconclusive."""
    sfx = get(connection, sfx_id)
    if not sfx or sfx["status"] != VERIFYING:
        return None
    verdict = verdict_of_step(connection, sfx)
    if verdict is None:
        return None
    verification = {**(sfx.get("verification") or {}), **verdict, "at": now_utc().isoformat()}
    outcome = verdict["outcome"]
    if outcome == "passed":
        out = transition(connection, sfx_id, VERIFIED, actor=SYSTEM_ACTOR,
                         sentence=f"Verified: the {sfx['step_id']} step passed with BOSS's own "
                                  "fix installed.",
                         data={"outcome": "passed"}, verification=verification,
                         verified_at=now_utc())
    elif outcome == "new_cause":
        out = transition(connection, sfx_id, VERIFIED, actor=SYSTEM_ACTOR,
                         sentence=(f"Verified: the {sfx['step_id']} step got past "
                                   f"{sfx['class_signature']}; it now fails for a different "
                                   f"reason ({verdict.get('next_signature')}), which is a new "
                                   "problem."),
                         data={"outcome": "new_cause",
                               "next_signature": verdict.get("next_signature")},
                         verification=verification, verified_at=now_utc())
    elif outcome == "same_cause":
        transition(connection, sfx_id, NOT_RESOLVED, actor=SYSTEM_ACTOR,
                   sentence="The fix did not hold: the step failed the same way. BOSS is "
                            "putting the previous build back.",
                   data={"outcome": "same_cause"}, verification=verification)
        from . import self_fix_install

        self_fix_install.issue_rollback(connection, settings, sfx_id,
                                        trigger="the resumed step failed for the same cause")
        out = get(connection, sfx_id)
    else:
        out = transition(connection, sfx_id, INSTALLED, actor=SYSTEM_ACTOR,
                         sentence=f"Verification inconclusive: {verdict.get('why')}. The fix "
                                  "stays installed and is checked on the next run.",
                         data={"outcome": "inconclusive"},
                         verification={**verification, "outcome": "inconclusive"})
    if out and out.get("status") == VERIFIED:
        add_label(connection, sfx_id, VERIFIED_LABEL)
        out = get(connection, sfx_id) or out
    release_budget(connection, settings, out, f"self-fix {outcome}")
    return out


VERIFIED_LABEL = "verified:local"


def add_label(connection: Connection, sfx_id: str, label: str) -> None:
    """A label on the local PR record (R10): `verified:local` once the parked step proved it."""
    sfx = get(connection, sfx_id)
    if not sfx:
        return
    pr = dict(sfx.get("pr") or {})
    labels = [str(x) for x in pr.get("labels") or []]
    if label not in labels:
        pr["labels"] = [*labels, label]
        update(connection, sfx_id, pr=pr)


def settle_run(connection: Connection, settings: Any, *, run_id: str) -> list[dict[str, Any]]:
    """Every verifying self-fix of one run (the runner's sweep calls this each tick)."""
    rows = connection.execute(text(
        "SELECT id FROM self_fixes WHERE run_id = :r AND status = 'verifying'"),
        {"r": run_id}).all()
    out = []
    for (sfx_id,) in rows:
        try:
            with connection.begin_nested():
                settled = settle_verification(connection, settings, sfx_id)
        except Exception as error:  # noqa: BLE001 — one fix's verdict never stops the sweep
            log.warning("self-fix %s verdict not settled: %s", sfx_id, error)
            continue
        if settled:
            out.append({"self_fix_id": sfx_id, "status": settled.get("status")})
    return out


# ── lineage (§10.3) ──────────────────────────────────────────────────────────────────────────

def lineage(connection: Connection, sfx_id: str) -> Optional[dict[str, Any]]:
    """Problem → recommendation → diagnosis → fix → tests → review → approval → install →
    resume → verdict, from the record and its events."""
    sfx = get(connection, sfx_id)
    if sfx is None:
        return None
    events = [dict(r) for r in connection.execute(text(
        "SELECT event_type, actor, data, created_at FROM events WHERE event_type LIKE "
        "'self_fix.%' AND data->>'self_fix_id' = :i ORDER BY created_at"),
        {"i": sfx_id}).mappings().all()]
    for e in events:
        if isinstance(e.get("created_at"), datetime):
            e["created_at"] = e["created_at"].isoformat()
        if isinstance(e.get("data"), str):
            try:
                e["data"] = json.loads(e["data"])
            except ValueError:
                pass
    tickets = [dict(r) for r in connection.execute(text(
        "SELECT id, kind, status, phases, created_at, finished_at, expires_at FROM "
        "self_fix_tickets WHERE self_fix_id = :i ORDER BY created_at"),
        {"i": sfx_id}).mappings().all()]
    for t in tickets:
        for k in ("created_at", "finished_at", "expires_at"):
            if isinstance(t.get(k), datetime):
                t[k] = t[k].isoformat()
    pr = dict(sfx.get("pr") or {})
    pr.pop("diff", None)
    return {"self_fix": {**sfx, "pr": pr}, "events": events, "tickets": tickets,
            "waiters": waiters(connection, sfx_id),
            "stages": [{"stage": s, "done": STAGES.index(s) < STAGES.index(sfx["stage"])
                        if sfx.get("stage") in STAGES else False} for s in STAGES],
            "lineage": lineage_steps(sfx, events)}


LINEAGE = (("problem", "Problem"), ("diagnosis", "Diagnosis"), ("fix", "Fix"),
           ("tests", "Tests"), ("review", "Review"), ("approved", "Approved"),
           ("installed", "Installed"), ("resumed", "Resumed"), ("upstream", "Upstream"))
#: How far along the lineage each status is: the index of the step it is AT (§10.3).
_LINEAGE_AT = {PROPOSED: 1, CLAIMED: 1, DIAGNOSING: 1, REPRODUCING: 2, FIXING: 2, CHECKING: 3,
               REVIEWING: 4, AWAITING_APPROVAL: 5, APPROVED: 6, INSTALLING: 6,
               HEALTH_WINDOW: 6, INSTALLED: 7, VERIFYING: 7, VERIFIED: 8, SUPERSEDED: 9}
#: Where an ending that did not hold stopped the lineage.
_LINEAGE_FAILED = {REJECTED: 5, INSTALL_FAILED: 6, ROLLING_BACK: 7, ROLLED_BACK: 7,
                   NOT_RESOLVED: 7}
_REFUSED_AT = {"protected_path": 1, "diagnosis": 1, "api_suite": 1, "no_source": 1,
               "no_reproduction": 2, "tests": 3, "tests_weakened": 3, "scope": 3,
               "dependencies": 3, "too_big": 3, "secrets": 3, "review": 4}
_UPSTREAM_WORDS = {"local_only": "not sent", "previewed": "previewed, not sent",
                   "sent": "sent", "merged": "merged upstream", "closed": "closed upstream",
                   "duplicate_of": "a duplicate of an upstream fix"}


def lineage_steps(sfx: Mapping[str, Any], events: Iterable[Mapping[str, Any]] = ()
                  ) -> list[dict[str, Any]]:
    """Problem → diagnosis → fix → tests → review → approved → installed → resumed → upstream,
    each {key, label, state: done|now|next|failed|skipped, at, detail} (§10.3). BOSS decides
    the words; the page only draws them."""
    status = str(sfx.get("status") or "")
    at_of: dict[str, str] = {}
    for e in events or []:
        name = str(e.get("event_type") or "").removeprefix(EVENT_PREFIX)
        at_of.setdefault(name, str(e.get("created_at") or ""))
    diagnosis = sfx.get("diagnosis") or {}
    stat = sfx.get("diff_stat") or {}
    red_green = sfx.get("red_green") or {}
    review = (sfx.get("review") or {}).get("seats") or {}
    verification = sfx.get("verification") or {}
    files = len(stat.get("files") or [])
    detail = {
        "problem": f"{sfx.get('signature')} · run {sfx.get('run_id') or '—'} step "
                   f"{sfx.get('step_id') or '—'}",
        "diagnosis": " · ".join(x for x in (
            str(diagnosis.get("hypothesis") or "")[:200], f"confidence {sfx.get('confidence')}",
            "first guess rejected" if diagnosis.get("rejected") else "") if x),
        "fix": " · ".join(x for x in (
            str(sfx.get("branch") or ""), f"{files} file(s)" if files else "",
            f"+{stat.get('added', 0)} −{stat.get('removed', 0)}" if files else "") if x),
        "tests": (f"✗→✓ {len(red_green.get('tests') or [])} new regression test(s)"
                  if red_green.get("tests") else ""),
        "review": " ".join(f"{seat.replace('_check', '').replace('_', ' ')} "
                           f"{'✓' if (v or {}).get('ok') else '✗'}"
                           for seat, v in review.items() if isinstance(v, Mapping)),
        "approved": f"decision {sfx.get('decision_id')}" if sfx.get("decision_id") else "",
        "installed": (f"stack head {str(sfx.get('installed_stack_head'))[:12]}"
                      if sfx.get("installed_stack_head") else ""),
        "resumed": {"passed": "the step passed", "new_cause":
                    f"got past it; new cause {verification.get('next_signature')}",
                    "same_cause": "failed the same way: rolled back",
                    "inconclusive": f"inconclusive: {verification.get('why') or ''}"}.get(
                        str(verification.get("outcome") or ""), ""),
        "upstream": _UPSTREAM_WORDS.get(str(sfx.get("upstream_state") or "local_only"), ""),
    }
    when = {"problem": sfx.get("created_at"), "diagnosis": at_of.get("reproduced"),
            "fix": at_of.get("checking"), "tests": at_of.get("reviewed"),
            "review": at_of.get("awaiting_approval"), "approved": at_of.get("approved"),
            "installed": sfx.get("installed_at"), "resumed": sfx.get("resumed_at"),
            "upstream": at_of.get("upstream_sent")}
    if status in _LINEAGE_FAILED:
        reached, failed = _LINEAGE_FAILED[status], True
    elif status in (REFUSED, CANCELLED):
        # Where it stopped: the stage its refusal class belongs to (a stop by a person or the
        # switch, at the diagnosis unless the record says it had a fix).
        failed = True
        reached = _REFUSED_AT.get(str(sfx.get("refusal") or ""),
                                  4 if sfx.get("fix_commit_sha") else 1)
    else:
        reached, failed = _LINEAGE_AT.get(status, 1), False
    upstream = str(sfx.get("upstream_state") or "local_only")
    out = []
    for i, (key, label) in enumerate(LINEAGE):
        if i < reached:
            state = "done"
        elif i == reached:
            state = "failed" if failed else "now"
        else:
            state = "skipped" if failed else "next"
        if key == "upstream" and status in (VERIFIED, SUPERSEDED):
            state = "done" if upstream in ("sent", "merged", "duplicate_of") or \
                status == SUPERSEDED else "now"
        if key == "problem":
            state = "done"
        out.append({"key": key, "label": label, "state": state,
                    "at": str(when.get(key)) if when.get(key) and state == "done" else None,
                    "detail": detail.get(key) or ""})
    if status in (REFUSED, CANCELLED) and failed:
        stop = next((o for o in out if o["state"] == "failed"), None)
        if stop is not None:
            stop["detail"] = refusal_words(str(sfx.get("refusal") or ""),
                                           str(sfx.get("refusal_detail") or "")) \
                if status == REFUSED else "stopped by a person or the switch"
    return out


def step_view(sfx: Mapping[str, Any], *, waiting_on: bool = False) -> dict[str, Any]:
    """What the parked step shows (§10.1): "Fixing BOSS", the strip of stages with where it is,
    the live line, the facts under it (files, tests added, cost of its budget, minutes), and the
    way to the self-fix. The run page reads it from the step's output."""
    stage = _STAGE_OF.get(str(sfx.get("status") or ""))
    at = STAGES.index(stage) if stage in STAGES else -1
    if str(sfx.get("status")) == VERIFIED:
        at = len(STAGES)  # every stage done
    return {"id": sfx.get("id"), "status": sfx.get("status"), "chip": chip_of(sfx),
            "label": "Fixing BOSS", "stage": stage,
            "stages": [{"stage": s, "state": "done" if i < at else "now" if i == at else "next"}
                       for i, s in enumerate(STAGES)],
            "line": step_line(sfx), "needs_you": sfx.get("status") == AWAITING_APPROVAL,
            "decision_id": sfx.get("decision_id"), "facts": step_facts(sfx),
            **({"waiting_on": True} if waiting_on else {})}


def _minutes_since(value: Any) -> int:
    if not value:
        return 0
    try:
        start = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return max(0, int((now_utc() - start).total_seconds() // 60))
    except (TypeError, ValueError):
        return 0


def step_facts(sfx: Mapping[str, Any]) -> dict[str, Any]:
    """The strip's facts line: `stack_deploy.py · 1 test added · $1.84 of $10 · 6 min`."""
    stat = sfx.get("diff_stat") or {}
    red_green = sfx.get("red_green") or {}
    files = []
    for f in stat.get("files") or []:
        path = f.get("path") if isinstance(f, Mapping) else f
        if path and not str(path).startswith(("engine/tests/", "apps/api/tests/",
                                              "apps/web/tests/")):
            files.append(Path(str(path)).name)
    if not files:
        loc = str(sfx.get("location") or "").split("::")[0]
        files = [Path(loc).name] if loc else []
    end = sfx.get("verified_at") or sfx.get("rolled_back_at") or None
    start = sfx.get("started_at") or sfx.get("created_at")
    minutes = _minutes_since(start)
    if end and start:
        try:
            minutes = max(0, int((datetime.fromisoformat(str(end)) - datetime.fromisoformat(
                str(start))).total_seconds() // 60))
        except (TypeError, ValueError):
            pass
    return {"files": files[:4], "tests_added": len(red_green.get("tests") or []),
            "cost_usd": round(int(sfx.get("cost_micros") or 0) / 1e6, 2),
            "budget_usd": round(int(sfx.get("budget_usd_micros") or 0) / 1e6, 2),
            "minutes": minutes}


def step_line(sfx: Mapping[str, Any]) -> str:
    """The one line the parked step shows (§10.1): never "waiting" without why."""
    progress = sfx.get("progress") or {}
    status = str(sfx.get("status") or "")
    if status == AWAITING_APPROVAL:
        return f"BOSS fixed itself and needs your approval to install it ({sfx['id']})."
    if progress.get("sentence"):
        return str(progress["sentence"])
    return f"{AGENT_NAME} is fixing BOSS ({status.replace('_', ' ')})."


__all__ = [n for n in dir() if not n.startswith("_") and n not in (
    "annotations", "json", "logging", "os", "re", "datetime", "Path", "Any",
    "Iterable", "Mapping", "Optional", "text", "Connection", "emit_event", "make_id",
    "now_utc", "log")]
