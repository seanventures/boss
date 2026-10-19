"""Metatron Captain — always-on overwatch, memory, goals, and governed drive.

Metatron's historical surface is a one-shot status query. The captain keeps a
durable session, remembers operator facts, registers measurable goals, and
runs a constant observe → measure → validate → apply-safe → verify cycle.

Hard rules:
  * Every action is validated before it runs (chat, goals, watch, drive,
    overwatch apply). Failed checks stay visible. Nothing applies without
    a passing verdict.
  * Decisions and deadletters never auto-apply.
  * BOSS self-change launches a governed PR, never a silent write.
  * Watch/overwatch are no-LLM. Spendy apply needs a Receipt + dedupe_key.
  * Every failure is visible: validation, events, findings, or a Decision.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .agent_service import AgentService
from .core import emit_event, emit_if_changed, make_id
from .health import HealthSnapshotService
from .metatron import MetatronService
from .receipts import ReceiptService


CAPTAIN_EXTERNAL_REF = "metatron:captain"
CAPTAIN_SURFACE = "chat"
HELP_REPLY = (
    "I am Metatron, captain of this project. I stay on overwatch: observe, "
    "measure goals, validate every action, apply only safe moves, then verify. "
    "Decisions stay human. BOSS self-change is a PR. "
    "I can author and start agent processes (PRD handoffs) from chat; "
    "simulated starts never spend. I have overwatch of captains, agents, "
    "and official work, and I can re-prioritize or staff more force "
    "against high-level objectives. I never launch a second factory job "
    "while official work is in flight, invent a pass, or auto-resolve "
    "decisions.\n"
    "Try: remember … · set goal … · watch · overwatch · theater · "
    "reprioritize official · staff captains · measure · drive · "
    "drive auto · build … · build a process … · start process … · "
    "list processes · goals · memory"
)

GOAL_KINDS = frozenset({"working_software", "budget", "queue", "quality", "custom"})

# v1.1 craft dimensions factory_driver / working_software should keep chasing.
# Functionality (npm verify) is not enough. Missing evidence is never 100.
SCORECARD_QUALITY_DIMS = ("ui", "ux", "capability")
SCORECARD_QUALITY_FLOOR = 70

#: How far back to look for previews that already failed, and how many failures mean the
#: restart remedy is not working. Wide enough to span several captain ticks (they run every few
#: minutes) so the count is meaningful; short enough that a genuinely fixed app is retried again
#: soon rather than being written off for the rest of the day.
PREVIEW_FAILURE_WINDOW_MINUTES = 30
PREVIEW_FAILURE_GIVE_UP = 3

#: A Captain-started build that did not finish waits this long before the next one, doubling
#: per consecutive failure, up to the cap. Measured on an idle install before this existed: 17
#: improve passes in 55 minutes, 10 of them blocked, each one real model spend.
LAUNCH_BACKOFF_BASE_MINUTES = 10
LAUNCH_BACKOFF_CAP_MINUTES = 6 * 60
#: Statuses a Captain build is still going in: never start a second beside it.
_LAUNCH_LIVE = frozenset({"queued", "running", "repairing", "provisioning"})
#: Words in a block that mean authority or consent refused it. Retrying cannot change that;
#: only a person can, so the Captain holds until the run itself changes.
_AUTHORITY_MARKERS = ("forbidden", "not allowed", "permission", "consent", "authority",
                      "passport", "not permitted", "denied", "needs you", "approval")


def _is_authority_block(row: dict[str, Any]) -> Optional[str]:
    """Why a blocked Captain build is waiting on a person, in words, or None."""
    phase = str(row.get("phase") or "")
    if phase == "awaiting_plan_approval":
        return "its plan is waiting for a person's approval"
    if phase == "awaiting_gate_override":
        return "its checks failed and it is waiting for a person to waive or fix them"
    if str(row.get("reason_code") or "") == "budget_denied":
        return "the build budget refused it"
    error = str(row.get("error") or "")
    if any(marker in error.lower() for marker in _AUTHORITY_MARKERS) or re.search(
            r"needs [a-z_]+:[a-z_]+", error):
        return f"it was refused for authority or consent ({error.strip()[:160]})"
    return None


#: The project setting that lets the Captain start its own improve passes (project_settings).
AUTO_IMPROVE_KNOB = "captain_auto_improve"
AUTO_IMPROVE_OFF_MESSAGE = (
    "The Captain's own improve passes are off for this project, so it proposed this instead of "
    "starting it. Turn on 'Captain self-improves' in Govern to let it start them (each spends "
    "model budget)."
)


def captain_auto_improve_on(connection: Connection, project_id: str, settings: Any = None) -> bool:
    """May the Captain start an improve pass nobody asked for on this project? Off unless set.

    Reads the same knob Govern shows (project_settings.effective). Unreadable means off: an
    autonomous build that spends money is the thing that must not happen by accident."""
    from .project_settings import effective

    try:
        with connection.begin_nested():
            return bool(int(effective(connection, project_id, settings, AUTO_IMPROVE_KNOB) or 0))
    except Exception:  # noqa: BLE001
        return False


def captain_launch_gate(connection: Connection, project_id: str, *, settings: Any = None,
                        asked: bool = True) -> Optional[dict[str, Any]]:
    """Whether the Captain may start another build now: None, or the refusal in words.

    `asked=False` is a pass the Captain decided on itself (an overwatch tick, a drive with no
    directive): it starts only when the project turned `captain_auto_improve` on (Govern), and
    is otherwise held with that said — the first-run walk's unasked "QUALITY IMPROVE PASS".

    Every tick used to start a new improve pass whenever nothing was `running` — and a pass that
    ended blocked or failed is not running, so the next tick started the same pass again, with
    no memory of why the last one stopped. This is that memory:

    - a Captain build still going holds the next one;
    - a build blocked for authority or consent holds every later one until that run changes
      (a person approves, retries or cancels it) — retrying cannot grant a permission;
    - any other block or failure backs off exponentially from when it stopped, capped.
    """
    if not asked and not captain_auto_improve_on(connection, project_id, settings):
        return {"reason": "captain_auto_improve_off", "message": AUTO_IMPROVE_OFF_MESSAGE}
    try:
        with connection.begin_nested():
            rows = [dict(r) for r in connection.execute(text(
                "SELECT id, status, phase, error, finalize_ctx->>'reason_code' AS reason_code, "
                "       EXTRACT(EPOCH FROM (now() - updated_at)) AS age_s "
                "FROM code_task_runs WHERE project_id = :p "
                "  AND (finalize_ctx->>'source' = 'metatron_captain' OR title ILIKE 'Captain:%') "
                "ORDER BY created_at DESC LIMIT 12"), {"p": project_id}).mappings().all()]
    except Exception:  # noqa: BLE001 - no history readable: the in-flight check still applies
        return None
    if not rows:
        return None
    last = rows[0]
    status = str(last.get("status") or "")
    if status in _LAUNCH_LIVE:
        return {"reason": "captain_build_in_flight", "run_id": last["id"],
                "message": f"The last improve pass ({last['id']}) is still {status}; the next "
                           "one starts after it finishes."}
    if status == "blocked":
        why = _is_authority_block(last)
        if why:
            return {"reason": "captain_build_needs_a_person", "run_id": last["id"],
                    "message": f"The last improve pass ({last['id']}) is blocked: {why}. No new "
                               "pass starts until a person resolves, retries or cancels it."}
    streak = 0
    for row in rows:
        if str(row.get("status") or "") not in ("failed", "blocked"):
            break
        streak += 1
    if not streak:
        return None
    wait_min = min(LAUNCH_BACKOFF_BASE_MINUTES * (2 ** (streak - 1)), LAUNCH_BACKOFF_CAP_MINUTES)
    age_min = float(last.get("age_s") or 0) / 60.0
    if age_min >= wait_min:
        return None
    left = max(1, int(round(wait_min - age_min)))
    return {"reason": "captain_backoff", "run_id": last["id"], "streak": streak,
            "wait_minutes": wait_min,
            "message": f"Backing off: the last {streak} improve pass(es) ended {status}. The "
                       f"next one may start in about {left} minute(s)."}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metatron_goals (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  target JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_score NUMERIC,
  last_measured_at TIMESTAMPTZ,
  last_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  auto_drive BOOLEAN NOT NULL DEFAULT false,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_metatron_goals_project
  ON metatron_goals(project_id, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS metatron_memories (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  thread_id TEXT,
  kind TEXT NOT NULL DEFAULT 'fact',
  content TEXT NOT NULL,
  salience NUMERIC NOT NULL DEFAULT 0.5,
  source TEXT NOT NULL DEFAULT 'chat',
  refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_metatron_memories_project
  ON metatron_memories(project_id, created_at DESC);
CREATE TABLE IF NOT EXISTS metatron_watch_ticks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  observed JSONB NOT NULL DEFAULT '{}'::jsonb,
  findings JSONB NOT NULL DEFAULT '[]'::jsonb,
  proposed_moves JSONB NOT NULL DEFAULT '[]'::jsonb,
  receipt_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_metatron_watch_project
  ON metatron_watch_ticks(project_id, created_at DESC);
ALTER TABLE metatron_watch_ticks ADD COLUMN IF NOT EXISTS cycle TEXT NOT NULL DEFAULT 'watch';
ALTER TABLE metatron_watch_ticks ADD COLUMN IF NOT EXISTS verify JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE metatron_watch_ticks ADD COLUMN IF NOT EXISTS applied JSONB NOT NULL DEFAULT '[]'::jsonb;
"""


#: Intents interpret_turn reaches only by READING FREE TEXT (a verb somewhere in a sentence),
#: not from a typed command. A model decides those (`decide_free_text`); the reading is a hint.
INFERRED = frozenset({"act", "prioritize", "status", "theater_inferred"})

_FREE_TEXT_CANDIDATES = {
    "status": "report how the fleet stands; change nothing",
    "act": "start governed work for what they describe (a build, fix or change)",
    "prioritize": "change the priority of queued work as they describe",
    "theater": "command the fleet: reprioritise and staff captains automatically",
    "drive": "propose the next move without executing it",
}


def decide_free_text(connection: Connection, settings: Any, project_id: str, message: str,
                     parsed: dict[str, Any], *, adapter: Any = None) -> dict[str, Any]:
    """A captain turn that is not a typed command is decided by a model, never by its words.

    "fix" anywhere in a sentence made `act` (governed work); "prioritize" or "p1" made
    `prioritize`. Now those readings are hints to agent_decision; unsure is ONE question; no
    model means nothing is done and the captain says so."""
    if parsed.get("intent") not in INFERRED:
        return parsed
    from . import agent_decision

    hint = "theater" if parsed["intent"] == "theater_inferred" else parsed["intent"]
    verdict = agent_decision.decide(
        connection, settings, project_id,
        situation="A person typed this to the Metatron captain console (not a command word).",
        message=message, hints=[f"a keyword reading suggests '{hint}'"], agent="Metatron",
        ref=f"captain:{project_id}", adapter=adapter,
        candidates=[agent_decision.Candidate(k, v, acts=k not in ("status", "drive"))
                    for k, v in _FREE_TEXT_CANDIDATES.items()])
    if verdict.status == agent_decision.NO_MODEL:
        return {"intent": "no_model", "text": message,
                "reply": agent_decision.no_model_reply(verdict)}
    if verdict.status == agent_decision.CLARIFY:
        return {"intent": "clarify", "text": message, "reply": verdict.question}
    out = {"intent": verdict.action, "text": message, "decided_by": verdict.to_dict()}
    if verdict.action == "theater":
        out["auto"] = True
    return out


def interpret_turn(message: str) -> dict[str, Any]:
    """The captain's typed COMMANDS (exact words: overwatch, watch, go, remember …). Free text
    falls to an INFERRED intent that `decide_free_text` hands to a model."""
    raw = (message or "").strip()
    lowered = raw.lower()
    if not raw:
        return {"intent": "help", "text": ""}
    if lowered in {"help", "?", "what can you do"}:
        return {"intent": "help", "text": raw}
    if lowered in {"overwatch", "run overwatch", "overwatch now", "keep watch"}:
        return {"intent": "overwatch", "text": raw}
    if lowered in {"theater", "command", "fleet", "command theater"}:
        return {"intent": "theater", "text": raw, "auto": True}
    if re.search(
        r"\b(reprioritize|prioriti[sz]e official|staff captains?|assign (more )?resources|command the fleet)\b",
        lowered,
    ):
        return {"intent": "theater_inferred", "text": raw, "auto": True}
    if lowered in {"watch", "scan", "tick"}:
        return {"intent": "watch", "text": raw}
    if lowered in {"measure", "score", "how are we doing"}:
        return {"intent": "measure", "text": raw}
    if lowered in {"goals", "list goals"}:
        return {"intent": "goals", "text": raw}
    if lowered in {"memory", "memories", "what do you remember"}:
        return {"intent": "memory", "text": raw}
    if lowered.startswith("remember "):
        return {"intent": "remember", "text": raw[9:].strip()}
    if lowered.startswith("set goal "):
        rest = raw[9:].strip()
        kind = "custom"
        m = re.search(r"\bkind=([a-z_]+)\b", rest, re.I)
        if m and m.group(1).lower() in GOAL_KINDS:
            kind = m.group(1).lower()
            rest = (rest[: m.start()] + rest[m.end() :]).strip()
        return {"intent": "set_goal", "text": rest, "kind": kind}
    if lowered in {"drive auto", "go", "launch", "execute", "do it", "run it"} or lowered.startswith("drive auto"):
        return {"intent": "drive", "text": raw, "auto": True}
    if lowered.startswith("drive") or lowered in {"propose next", "next move"}:
        return {"intent": "drive", "text": raw, "auto": False}
    process = _interpret_process_turn(raw, lowered)
    if process is not None:
        return process
    if re.search(r"\b(build|implement|fix|change|refactor|add|update|ship)\b", lowered) and not lowered.endswith("?"):
        return {"intent": "act", "text": raw}
    if re.search(r"\b(prioriti[sz]e|p0|p1|p2)\b", lowered):
        return {"intent": "prioritize", "text": raw}
    return {"intent": "status", "text": raw}


_BUILD_PROCESS_PREFIX = re.compile(
    r"^(?:build a process|create a workflow|define a process|handoff process)\b[:\s]*(.*)$",
    re.I,
)
_START_PROCESS_PREFIX = re.compile(r"^start process\b(.*)$", re.I)
_RUN_PRD_PROCESS = re.compile(r"\brun the prd process\b", re.I)
_PROCESS_KEY_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_]{0,79}$")
DEFAULT_PROCESS_KEY = "prd_to_accepted_work"
_DEFAULT_PROCESS_MARKERS = (
    "prd",
    "accepted work",
    "prd_to_accepted",
    "handoff",
    "architect",
    "cto",
    "vp product",
)


def _interpret_process_turn(raw: str, lowered: str) -> dict[str, Any] | None:
    """Recognize process authoring/start/list before generic build/act."""
    if lowered in {"list processes", "list process"} or re.search(r"\blist processes\b", lowered):
        return {"intent": "list_processes", "text": raw}
    build = _BUILD_PROCESS_PREFIX.match(raw.strip())
    if build:
        return {"intent": "build_process", "text": (build.group(1) or "").strip()}
    start = _START_PROCESS_PREFIX.match(raw.strip())
    if start or _RUN_PRD_PROCESS.search(lowered):
        rest = (start.group(1) or "").strip() if start else ""
        auto_build = bool(re.search(r"\bauto[_\s-]?build\b", lowered))
        process_key = DEFAULT_PROCESS_KEY
        text = rest
        if rest:
            tokens = rest.split()
            head = tokens[0].lower()
            if head in {"auto_build", "autobuild", "auto-build"}:
                text = " ".join(tokens[1:]).strip()
            elif _PROCESS_KEY_TOKEN.match(head) and head not in {"auto", "the"}:
                process_key = head
                text = " ".join(tokens[1:]).strip()
                if text.lower() in {"auto_build", "autobuild", "auto-build"}:
                    text = ""
                elif text.lower().endswith(" auto_build"):
                    text = text[: -len(" auto_build")].strip()
        return {
            "intent": "start_process",
            "text": text,
            "process_key": process_key,
            "auto_build": auto_build,
        }
    return None


def matches_default_process_story(text: str) -> bool:
    """True when NL describes the shipped PRD-to-accepted-work handoff."""
    blob = (text or "").lower()
    if not blob:
        return False
    return any(marker in blob for marker in _DEFAULT_PROCESS_MARKERS)


def scorecard_from_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return an attached v1.1 scorecard. Missing stays None — never invent one."""
    if not isinstance(snapshot, dict):
        return None
    nested: list[Any] = [snapshot.get("scorecard")]
    for key in ("run", "latest_run", "completion_report"):
        blob = snapshot.get(key)
        if isinstance(blob, dict):
            nested.append(blob.get("scorecard"))
    for source in nested:
        if isinstance(source, dict) and source:
            return source
    return None


def scorecard_quality_gaps(
    scorecard: dict[str, Any] | None,
    *,
    floor: int = SCORECARD_QUALITY_FLOOR,
) -> list[dict[str, Any]]:
    """ui/ux/capability gaps from a real scorecard.

    A missing scorecard produces no gaps and no invented 100s. A carried
    scorecard that omits those dimensions is not_scored, not done.
    """
    if not isinstance(scorecard, dict) or not scorecard:
        return []
    by_id: dict[str, dict[str, Any]] = {}
    for item in scorecard.get("dimensions") or []:
        if isinstance(item, dict) and item.get("id"):
            by_id[str(item["id"])] = item
    if "dimensions" not in scorecard and not scorecard.get("version") and not by_id:
        return []
    gaps: list[dict[str, Any]] = []
    for dim in SCORECARD_QUALITY_DIMS:
        item = by_id.get(dim)
        score = None if item is None else item.get("score")
        if item is None or item.get("status") == "not_scored" or score is None:
            gaps.append({
                "dimension": dim,
                "status": "not_scored",
                "score": None,
                "reason": (item or {}).get("reason") or "missing evidence",
            })
            continue
        if isinstance(score, (int, float)) and int(score) < floor:
            gaps.append({
                "dimension": dim,
                "status": "scored",
                "score": int(score),
                "reason": f"below floor {floor}",
            })
    return gaps


def format_scorecard_gaps(gaps: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for gap in gaps:
        dim = str(gap.get("dimension") or "")
        if not dim:
            continue
        if gap.get("status") == "not_scored" or gap.get("score") is None:
            parts.append(f"{dim}=not_scored")
        else:
            parts.append(f"{dim}={int(gap['score'])}")
    return ", ".join(parts)


def quality_improve_directive(gaps: list[dict[str, Any]], *, focus: str = "") -> str:
    """Governed factory prompt: name measured gaps, never invent scores."""
    named = format_scorecard_gaps(gaps) or "ui/ux/capability"
    head = "QUALITY IMPROVE PASS"
    if focus:
        head += f" for {focus!r}"
    return (
        f"{head}. Measured scorecard gaps: {named}. "
        "Do not invent ui/ux/capability scores. "
        "Do not treat missing evidence as 100. "
        "Do not edit frozen tests. "
        "Close the named gaps with the smallest honest UI/UX/capability change. "
        "Leave npm run verify green."
    )


def measure_from_snapshot(kind: str, snapshot: dict[str, Any], target: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score one goal against an already-gathered watch snapshot. Pure."""
    tgt = target or {}
    if kind == "working_software":
        completed = int(snapshot.get("completed_verify_green") or 0)
        previews = int(snapshot.get("running_previews") or 0)
        score = 0
        if completed:
            score += 50
        if previews:
            score += 50
        evidence: dict[str, Any] = {
            "completed_verify_green": completed,
            "running_previews": previews,
        }
        gaps = scorecard_quality_gaps(scorecard_from_snapshot(snapshot))
        if gaps:
            evidence["scorecard_gaps"] = [
                {"dimension": item["dimension"], "status": item["status"], "score": item["score"]}
                for item in gaps
            ]
            # Verify-green + preview is not a product scorecard. Cap below the
            # pursue threshold so factory_driver keeps improving measured gaps.
            score = min(score, SCORECARD_QUALITY_FLOOR - 1)
        return {
            "score": score,
            "status": "achieved" if score >= 100 else "active",
            "evidence": evidence,
        }
    if kind == "budget":
        state = str(snapshot.get("budget_state") or "unknown")
        score = {"green": 100, "yellow": 70, "orange": 40, "red": 10}.get(state, 0)
        return {"score": score, "status": "achieved" if score >= 100 else "active",
                "evidence": {"budget_state": state}}
    if kind == "queue":
        dead = int(snapshot.get("deadletters") or 0)
        pending = int(snapshot.get("pending_decisions") or 0)
        max_dead = int(tgt.get("max_deadletters") or 0)
        max_pending = int(tgt.get("max_pending_decisions") or 3)
        score = 100
        if dead > max_dead:
            score -= min(60, 20 * (dead - max_dead))
        if pending > max_pending:
            score -= min(40, 10 * (pending - max_pending))
        return {"score": max(0, score), "status": "achieved" if score >= 80 else "active",
                "evidence": {"deadletters": dead, "pending_decisions": pending}}
    if kind == "quality":
        min_score = int(tgt.get("min_score") or 75)
        observed = snapshot.get("quality_score")
        if observed is None:
            return {"score": None, "status": "active", "evidence": {"reason": "quality_score not observed"}}
        score = int(observed)
        return {"score": score, "status": "achieved" if score >= min_score else "active",
                "evidence": {"quality_score": score, "min_score": min_score}}
    return {"score": None, "status": "active", "evidence": {"reason": "custom goal needs operator evidence"}}


def findings_from_snapshot(snapshot: dict[str, Any], goals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn a snapshot + goals into visible findings. Pure. Never spends."""
    findings: list[dict[str, Any]] = []
    dead = int(snapshot.get("deadletters") or 0)
    if dead:
        findings.append({
            "id": "deadletters",
            "severity": "high" if dead > 5 else "medium",
            "title": f"{dead} deadletter(s) need operator review",
            "action": "review_deadletters",
            "auto_safe": False,
        })
    pending = int(snapshot.get("pending_decisions") or 0)
    if pending:
        findings.append({
            "id": "decisions",
            "severity": "high",
            "title": f"{pending} pending decision(s) block autonomous progress",
            "action": "open_decision_inbox",
            "auto_safe": False,
        })
    preview_gap = (
        int(snapshot.get("running_previews") or 0) == 0
        and int(snapshot.get("completed_verify_green") or 0) > 0
    )
    recent_failures = int(snapshot.get("recent_failed_previews") or 0)
    if preview_gap and recent_failures >= PREVIEW_FAILURE_GIVE_UP:
        # STOP PROPOSING THE THING THAT KEEPS FAILING. The gap is real, but restarting is
        # demonstrably not the remedy: the same start has already failed this many times in the
        # last window. Escalate instead — `auto_safe: False` means a human sees it rather than
        # the loop quietly burning a build every few minutes forever.
        findings.append({
            "id": "preview_failing",
            "severity": "high",
            "title": (f"Preview cannot start: {recent_failures} attempts failed in the last "
                      f"{PREVIEW_FAILURE_WINDOW_MINUTES} minutes — the app does not build"),
            "action": "investigate_preview_failure",
            "auto_safe": False,
        })
    elif preview_gap:
        findings.append({
            "id": "preview_expired",
            "severity": "medium",
            "title": "Verify-green apps exist but no preview is running",
            "action": "restart_preview",
            "auto_safe": True,
        })
    if snapshot.get("budget_state") in {"orange", "red"}:
        findings.append({
            "id": "budget",
            "severity": "high",
            "title": f"Budget state is {snapshot.get('budget_state')}",
            "action": "reduce_spend_or_raise_cap",
            "auto_safe": False,
        })
    quality_gaps = scorecard_quality_gaps(scorecard_from_snapshot(snapshot))
    for goal in goals:
        if goal.get("status") != "active":
            continue
        kind = str(goal.get("kind") or "custom")
        measured = measure_from_snapshot(kind, snapshot, goal.get("target") or {})
        pursue_quality = kind in {"working_software", "quality"} and bool(quality_gaps)
        if measured.get("score") is None and not pursue_quality:
            continue
        if (
            measured.get("score") is not None
            and int(measured["score"]) >= 80
            and not pursue_quality
        ):
            continue
        # Missing preview is already a restart_preview finding — do not also
        # enqueue a factory job for the same working_software gap. Measured
        # UI/UX/capability gaps are a different job and still launch.
        if kind == "working_software" and preview_gap and not pursue_quality:
            continue
        if kind not in {"working_software", "quality"}:
            continue
        title = f"Goal '{goal.get('title')}' is at {measured['score']}"
        if pursue_quality:
            title = quality_improve_directive(quality_gaps, focus=str(goal.get("title") or ""))
        finding: dict[str, Any] = {
            "id": f"goal:{goal.get('id')}",
            "severity": "medium",
            "title": title,
            "action": "pursue_goal",
            "auto_safe": True,
            "goal_id": goal.get("id"),
        }
        if pursue_quality:
            finding["directive"] = title
            finding["gaps"] = quality_gaps
        findings.append(finding)
    if quality_gaps and not any(item.get("action") == "pursue_goal" for item in findings):
        title = quality_improve_directive(quality_gaps)
        findings.append({
            "id": "scorecard_gaps",
            "severity": "medium",
            "title": title,
            "action": "pursue_goal",
            "auto_safe": True,
            "directive": title,
            "gaps": quality_gaps,
        })
    from .metatron_command import fleet_findings

    findings.extend(fleet_findings(snapshot))
    if not findings:
        findings.append({
            "id": "steady",
            "severity": "info",
            "title": "No urgent captain finding. Continue measuring.",
            "action": "hold",
            "auto_safe": True,
        })
    return findings


def proposed_moves_from_findings(findings: list[dict[str, Any]], *, auto: bool) -> list[dict[str, Any]]:
    """Map findings to proposal-only moves. auto=True still refuses high-risk."""
    moves: list[dict[str, Any]] = []
    for finding in findings:
        action = finding.get("action")
        if action == "hold":
            continue
        if action == "open_decision_inbox":
            moves.append({
                "kind": "escalate_decision",
                "status": "needs_human",
                "detail": finding["title"],
                "finding_id": finding["id"],
            })
            continue
        if action == "review_deadletters":
            moves.append({
                "kind": "propose_deadletter_review",
                "status": "proposed",
                "detail": finding["title"],
                "finding_id": finding["id"],
            })
            continue
        if action == "restart_preview" and finding.get("auto_safe"):
            moves.append({
                "kind": "restart_preview",
                "status": "queued" if auto else "proposed",
                "detail": "Restart verify-green previews (no new code.task).",
                "finding_id": finding["id"],
                "apply": bool(auto),
            })
            continue
        if action == "pursue_goal":
            moves.append({
                "kind": "launch_factory_job",
                "status": "queued" if auto else "proposed",
                "detail": finding["title"],
                "finding_id": finding["id"],
                "goal_id": finding.get("goal_id"),
                "apply": bool(auto),
            })
            continue
        if action in {"reprioritize_and_staff", "staff_and_loop"}:
            from .metatron_command import command_moves

            moves.extend(command_moves([finding], auto=auto))
            continue
        moves.append({
            "kind": "propose",
            "status": "proposed",
            "detail": finding["title"],
            "finding_id": finding["id"],
            "requires_human": not finding.get("auto_safe"),
        })
    return moves


# Every captain action — even auto-drive — must pass these gates before apply.
BLOCKED_APPLY_KINDS = frozenset({
    "escalate_decision",
    "propose_deadletter_review",
    "propose",
})

# Chat / overwatch / drive kinds that validate_action recognizes.
ACTION_KINDS = frozenset({
    "remember", "set_goal", "watch", "measure", "goals", "memory",
    "drive", "overwatch", "act", "prioritize", "help", "status", "status_query",
    "restart_preview", "launch_factory_job",
    "escalate_decision", "propose_deadletter_review", "propose",
    "build_process", "start_process", "list_processes",
    "theater", "reprioritize_official", "staff_captain", "loop_playbook",
    "no_model", "clarify",
})

# Mutating intents that must pass validation before session_turn runs them.
# list_processes is read-only.
MUTATING_INTENTS = frozenset({
    "remember", "set_goal", "drive", "act", "prioritize", "overwatch",
    "build_process", "start_process", "theater",
})


def validate_action(
    kind: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    snapshot: Optional[dict[str, Any]] = None,
    auto: bool = False,
    in_flight: int = 0,
    budget_state: str = "unknown",
    verify_green_run_id: Optional[str] = None,
    self_mod_repo: str = "",
) -> dict[str, Any]:
    """Fail-closed validation for any captain action. Pure: never spends."""
    payload = payload or {}
    snapshot = snapshot or {}
    action = str(kind or payload.get("kind") or payload.get("intent") or "")
    if action in {
        "restart_preview", "launch_factory_job", "escalate_decision",
        "propose_deadletter_review", "propose",
        "reprioritize_official", "staff_captain", "loop_playbook",
    }:
        return validate_move(
            {**payload, "kind": action},
            snapshot,
            auto=auto,
            in_flight=in_flight,
            budget_state=budget_state,
            verify_green_run_id=verify_green_run_id,
            self_mod_repo=self_mod_repo,
        )

    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check("has_kind", bool(action), "action declares a kind" if action else "action has no kind")
    check(
        "known_kind",
        action in ACTION_KINDS,
        f"kind={action}" if action in ACTION_KINDS else f"unknown kind {action!r}",
    )
    if action == "remember":
        content = str(payload.get("content") or payload.get("text") or "").strip()
        check("nonempty_content", bool(content), "memory has content" if content else "empty memory")
        check("content_bounded", len(content) <= 2000, f"len={len(content)}")
    elif action == "set_goal":
        title = str(payload.get("title") or payload.get("text") or "").strip()
        goal_kind = str(payload.get("kind") or "custom")
        check("nonempty_title", bool(title), "goal has a title" if title else "empty goal title")
        check(
            "known_goal_kind",
            goal_kind in GOAL_KINDS,
            f"kind={goal_kind}" if goal_kind in GOAL_KINDS else f"unknown goal kind {goal_kind}",
        )
    elif action == "build_process":
        description = str(payload.get("text") or payload.get("description") or "").strip()
        check("nonempty_text", bool(description), "process has a description" if description else "empty process description")
        check("content_bounded", len(description) <= 4000, f"len={len(description)}")
    elif action == "start_process":
        process_key = str(payload.get("process_key") or DEFAULT_PROCESS_KEY).strip()
        check("has_process_key", bool(process_key), f"process_key={process_key or DEFAULT_PROCESS_KEY}")
        if payload.get("auto_build"):
            check(
                "budget_not_red",
                budget_state not in {"red", "orange"},
                f"budget_state={budget_state}",
            )
    elif action in {"drive", "act", "prioritize", "theater"} and auto:
        check(
            "budget_not_red",
            budget_state not in {"red", "orange"},
            f"budget_state={budget_state}",
        )
    ok = all(c["ok"] for c in checks)
    return {
        "ok": ok,
        "checks": checks,
        "failed": [c["name"] for c in checks if not c["ok"]],
    }


def verify_from_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
    applied: list[dict[str, Any]],
) -> dict[str, Any]:
    """Re-check that applied work is visible in a fresh snapshot. Pure."""
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check("snapshot_refreshed", bool(after), "fresh snapshot gathered")
    for move in applied:
        kind = str(move.get("kind") or "")
        if kind == "restart_preview":
            running = int(after.get("running_previews") or 0)
            check(
                "preview_running",
                running > 0,
                f"running_previews={running}",
            )
        elif kind == "launch_factory_job":
            run_id = move.get("run_id")
            inflight = int(after.get("in_flight") or 0)
            check(
                "factory_visible",
                bool(run_id) or inflight > 0,
                f"run_id={run_id} in_flight={inflight}",
            )
            check(
                "receipt_recorded",
                bool(move.get("receipt_id")),
                f"receipt_id={move.get('receipt_id')}",
            )
    if not applied:
        check("observe_only", True, "no apply this cycle; observation verified")
    ok = all(c["ok"] for c in checks)
    return {
        "ok": ok,
        "checks": checks,
        "failed": [c["name"] for c in checks if not c["ok"]],
        "before": {
            "running_previews": before.get("running_previews"),
            "in_flight": before.get("in_flight"),
            "completed_verify_green": before.get("completed_verify_green"),
        },
        "after": {
            "running_previews": after.get("running_previews"),
            "in_flight": after.get("in_flight"),
            "completed_verify_green": after.get("completed_verify_green"),
            "budget_state": after.get("budget_state"),
        },
    }


def validate_move(
    move: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    auto: bool,
    in_flight: int = 0,
    budget_state: str = "unknown",
    verify_green_run_id: Optional[str] = None,
    self_mod_repo: str = "",
) -> dict[str, Any]:
    """Fail-closed validation for one drive move. Pure: never spends.

    A move is valid to APPLY only when every check passes. Failed checks stay
    visible on the move so the operator can see why it did not launch.
    """
    checks: list[dict[str, Any]] = []
    kind = str(move.get("kind") or "")

    from .metatron_command import COMMAND_KINDS, validate_command

    if kind in COMMAND_KINDS:
        return validate_command(move, snapshot, auto=auto, budget_state=budget_state)

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check("has_kind", bool(kind), "move declares a kind" if kind else "move has no kind")
    check(
        "not_human_only",
        kind not in BLOCKED_APPLY_KINDS and not move.get("requires_human")
        and move.get("status") != "needs_human",
        "high-risk actions (decisions, DLQ, budget) stay human",
    )
    check(
        "budget_not_red",
        budget_state not in {"red", "orange"},
        f"budget_state={budget_state}",
    )
    level = int(snapshot.get("system_level") or 0)
    if kind == "restart_preview":
        check("host_has_room", level < 1,
              f"system level {level}: no preview restarts under memory pressure")
    if kind == "launch_factory_job":
        check("host_has_room", level < 2,
              f"system level {level}: the system doctor paused new builds")
    if kind == "restart_preview":
        check(
            "verify_green_target",
            bool(verify_green_run_id),
            f"run={verify_green_run_id}" if verify_green_run_id else "no completed verify-green run",
        )
    if kind == "launch_factory_job":
        check("no_duplicate_inflight", int(in_flight) == 0, f"in_flight={in_flight}")
        check("auto_requested", bool(auto), "factory launch requires auto=true")
        check(
            "self_mod_is_pr_only",
            True,
            "BOSS self-change launches a governed PR, never a silent write",
        )
        if self_mod_repo:
            check("repo_bound", bool(self_mod_repo), f"repo={self_mod_repo}")
    ok = all(c["ok"] for c in checks)
    return {
        "ok": ok,
        "checks": checks,
        "failed": [c["name"] for c in checks if not c["ok"]],
    }


def _system_level(connection: Connection) -> int:
    try:
        with connection.begin_nested():
            from .admission import current_level
            return int(current_level(connection))
    except Exception:  # noqa: BLE001 — no level known: normal
        return 0


class MetatronCaptain:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.metatron = MetatronService(settings)
        self.health = HealthSnapshotService(settings)
        self.agents = AgentService(settings)
        self.receipts = ReceiptService()
        self._schema_ready = False

    def ensure_schema(self, connection: Connection) -> None:
        if self._schema_ready:
            return
        for statement in (chunk.strip() for chunk in SCHEMA_SQL.split(";")):
            if statement:
                connection.exec_driver_sql(statement)
        self._schema_ready = True

    def session_turn(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        message: str,
        thread_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self.ensure_schema(connection)
        thread = self.agents.start_or_load_thread(
            connection,
            project_id=project_id,
            actor=actor,
            thread_id=thread_id,
            surface=CAPTAIN_SURFACE,
            external_ref=CAPTAIN_EXTERNAL_REF,
        )
        parsed = decide_free_text(connection, self.settings, project_id, message,
                                  interpret_turn(message))
        self.agents.append_message(connection, thread_id=thread["id"], role="user", content=message.strip() or parsed["intent"])
        actions: list[dict[str, Any]] = []
        reply = ""
        watch = None
        snapshot = self.gather_snapshot(connection, project_id)
        action_verdict = validate_action(
            parsed["intent"],
            parsed,
            snapshot=snapshot,
            auto=bool(parsed.get("auto")) or parsed["intent"] in {"act", "prioritize", "overwatch", "theater"},
            in_flight=int(snapshot.get("in_flight") or 0),
            budget_state=str(snapshot.get("budget_state") or "unknown"),
        )
        if parsed["intent"] in MUTATING_INTENTS and not action_verdict["ok"]:
            failed = ", ".join(action_verdict["failed"]) or "validation"
            reply = (
                f"Rejected `{parsed['intent']}` — validation failed: {failed}. "
                "Nothing was applied."
            )
            actions.append({
                "kind": "rejected",
                "intent": parsed["intent"],
                "validation": action_verdict,
            })
            assistant = self.agents.append_message(
                connection, thread_id=thread["id"], role="assistant", content=reply, actions=actions
            )
            emit_event(
                connection, project_id, "metatron.captain.turn", actor, "agent_thread",
                thread["id"], {"intent": parsed["intent"], "action_count": len(actions), "rejected": True},
            )
            return {
                "thread_id": thread["id"],
                "reply": reply,
                "intent": parsed["intent"],
                "actions": actions,
                "validation": action_verdict,
                "memories": self.list_memories(connection, project_id, limit=8),
                "goals": self.list_goals(connection, project_id),
                "watch": self.latest_watch(connection, project_id),
                "messages": self.agents.list_messages(connection, thread["id"])[-20:],
                "assistant_message_id": assistant.get("id"),
            }
        if parsed["intent"] in ("no_model", "clarify"):
            reply = parsed["reply"]
        elif parsed["intent"] == "help":
            reply = HELP_REPLY
        elif parsed["intent"] == "remember":
            mem = self.remember(connection, project_id=project_id, thread_id=thread["id"],
                                content=parsed["text"], source="chat", kind="fact")
            actions.append({"kind": "remembered", "memory_id": mem["id"]})
            reply = f"Remembered: {parsed['text']}"
        elif parsed["intent"] == "set_goal":
            goal = self.set_goal(connection, project_id=project_id, actor=actor,
                                 title=parsed["text"], kind=parsed.get("kind") or "custom")
            actions.append({"kind": "goal_set", "goal_id": goal["id"]})
            reply = f"Tracking goal '{goal['title']}' ({goal['kind']}). I will measure it on every watch."
        elif parsed["intent"] == "watch":
            watch = self.watch_once(connection, project_id=project_id, actor=actor)
            actions.append({"kind": "watched", "tick_id": watch["id"], "finding_count": len(watch["findings"])})
            reply = self._render_watch(watch)
        elif parsed["intent"] == "measure":
            goals = self.measure_all(connection, project_id=project_id)
            actions.append({"kind": "measured", "count": len(goals)})
            reply = self._render_goals(goals) or "No goals registered yet. `set goal …`"
        elif parsed["intent"] == "goals":
            reply = self._render_goals(self.list_goals(connection, project_id)) or "No goals yet."
        elif parsed["intent"] == "memory":
            reply = self._render_memories(self.list_memories(connection, project_id)) or "No memories yet."
        elif parsed["intent"] == "drive":
            drive = self.drive(
                connection, project_id=project_id, actor=actor,
                auto=bool(parsed.get("auto")),
            )
            actions.append({
                "kind": "drive",
                "auto": bool(parsed.get("auto")),
                "moves": len(drive.get("moves") or []),
                "applied": len(drive.get("applied") or []),
                "rejected": len(drive.get("rejected") or []),
            })
            reply = self._render_drive(drive)
        elif parsed["intent"] in {"act", "prioritize"}:
            drive = self.drive(
                connection, project_id=project_id, actor=actor, auto=True,
                directive=parsed["text"],
            )
            actions.append({
                "kind": "act",
                "applied": len(drive.get("applied") or []),
                "rejected": len(drive.get("rejected") or []),
                "validation": action_verdict,
            })
            reply = self._render_drive(drive)
        elif parsed["intent"] == "overwatch":
            cycle = self.overwatch_once(connection, project_id=project_id, actor=actor)
            watch = cycle.get("watch")
            actions.append({
                "kind": "overwatch",
                "applied": len(cycle.get("applied") or []),
                "rejected": len(cycle.get("rejected") or []),
                "verify_ok": bool((cycle.get("verify") or {}).get("ok")),
                "validation": action_verdict,
            })
            reply = self._render_overwatch(cycle)
        elif parsed["intent"] == "theater":
            cycle = self.command_once(
                connection, project_id=project_id, actor=actor,
                auto=bool(parsed.get("auto", True)),
            )
            watch = (cycle.get("overwatch") or {}).get("watch") or cycle.get("watch")
            actions.append({
                "kind": "theater",
                "applied": len(cycle.get("applied") or []),
                "rejected": len(cycle.get("rejected") or []),
                "fleet": cycle.get("fleet") or {},
                "validation": action_verdict,
            })
            reply = self._render_theater(cycle)
        elif parsed["intent"] == "build_process":
            built = self._build_process(
                connection, project_id=project_id, actor=actor, description=parsed.get("text") or "",
            )
            actions.append(built)
            reply = built.get("reply") or "Process template unchanged."
        elif parsed["intent"] == "start_process":
            started = self._start_process(
                connection,
                project_id=project_id,
                actor=actor,
                parsed=parsed,
                action_verdict=action_verdict,
            )
            actions.append(started)
            reply = started.get("reply") or "Process start unchanged."
        elif parsed["intent"] == "list_processes":
            listed = self._list_processes(connection, project_id=project_id)
            actions.append(listed)
            reply = listed.get("reply") or "No process templates or runs."
        else:
            query = self.metatron.answer_query(connection, project_id, parsed["text"] or message)
            reply = query["answer"]
            actions.append({"kind": "status_query", "category": query.get("category")})
            memories = self.list_memories(connection, project_id, limit=3)
            if memories:
                reply += "\n\nFrom memory: " + "; ".join(m["content"][:120] for m in memories)
        assistant = self.agents.append_message(
            connection, thread_id=thread["id"], role="assistant", content=reply, actions=actions
        )
        emit_event(connection, project_id, "metatron.captain.turn", actor, "agent_thread",
                   thread["id"], {"intent": parsed["intent"], "action_count": len(actions)})
        return {
            "thread_id": thread["id"],
            "reply": reply,
            "intent": parsed["intent"],
            "actions": actions,
            "memories": self.list_memories(connection, project_id, limit=8),
            "goals": self.list_goals(connection, project_id),
            "watch": watch or self.latest_watch(connection, project_id),
            "messages": self.agents.list_messages(connection, thread["id"])[-20:],
            "assistant_message_id": assistant.get("id"),
        }

    def load_session(self, connection: Connection, *, project_id: str, actor: str) -> dict[str, Any]:
        self.ensure_schema(connection)
        thread = self.agents.start_or_load_thread(
            connection, project_id=project_id, actor=actor,
            surface=CAPTAIN_SURFACE, external_ref=CAPTAIN_EXTERNAL_REF,
        )
        return {
            "thread_id": thread["id"],
            "messages": self.agents.list_messages(connection, thread["id"])[-40:],
            "memories": self.list_memories(connection, project_id, limit=20),
            "goals": self.list_goals(connection, project_id),
            "watch": self.latest_watch(connection, project_id),
            "last_overwatch": self.latest_overwatch(connection, project_id),
        }

    def remember(
        self, connection: Connection, *, project_id: str, content: str,
        thread_id: Optional[str] = None, kind: str = "fact", source: str = "chat",
        salience: float = 0.7, refs: Optional[list] = None,
    ) -> dict[str, Any]:
        self.ensure_schema(connection)
        row = connection.execute(
            text(
                """
                INSERT INTO metatron_memories (id, project_id, thread_id, kind, content, salience, source, refs)
                VALUES (:id, :p, :t, :k, :c, :s, :src, CAST(:refs AS JSONB))
                RETURNING id, project_id, kind, content, salience, source, created_at
                """
            ),
            {
                "id": make_id("mmem"), "p": project_id, "t": thread_id, "k": kind,
                "c": content.strip()[:2000], "s": max(0, min(1, salience)),
                "src": source, "refs": json.dumps(refs or []),
            },
        ).mappings().one()
        return self._row(row)

    def set_goal(
        self, connection: Connection, *, project_id: str, actor: str,
        title: str, kind: str = "custom", description: str = "",
        target: Optional[dict] = None, auto_drive: Optional[bool] = None,
    ) -> dict[str, Any]:
        self.ensure_schema(connection)
        resolved = kind if kind in GOAL_KINDS else "custom"
        if auto_drive is None:
            auto_drive = resolved in {"working_software", "quality"}
        row = connection.execute(
            text(
                """
                INSERT INTO metatron_goals
                  (id, project_id, title, description, kind, target, auto_drive, created_by)
                VALUES (:id, :p, :title, :d, :k, CAST(:tgt AS JSONB), :auto, :actor)
                RETURNING *
                """
            ),
            {
                "id": make_id("mgoal"), "p": project_id, "title": title.strip()[:200],
                "d": (description or "")[:1000], "k": resolved,
                "tgt": json.dumps(target or {}), "auto": bool(auto_drive), "actor": actor,
            },
        ).mappings().one()
        emit_event(connection, project_id, "metatron.goal.set", actor, "metatron_goal",
                   row["id"], {"kind": resolved, "title": title[:80]})
        return self._row(row)

    def list_goals(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        rows = connection.execute(
            text("SELECT * FROM metatron_goals WHERE project_id=:p ORDER BY updated_at DESC"),
            {"p": project_id},
        ).mappings().all()
        return [self._row(r) for r in rows]

    def list_memories(self, connection: Connection, project_id: str, limit: int = 12) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        rows = connection.execute(
            text(
                """
                SELECT id, kind, content, salience, source, created_at
                FROM metatron_memories WHERE project_id=:p
                ORDER BY created_at DESC LIMIT :n
                """
            ),
            {"p": project_id, "n": limit},
        ).mappings().all()
        return [self._row(r) for r in rows]

    def gather_snapshot(self, connection: Connection, project_id: str) -> dict[str, Any]:
        health = self.health.current_state(connection, project_id)
        summary = health.get("summary") or {}

        def _count(sql: str) -> int:
            # A missing optional table must stay zero without aborting the
            # surrounding transaction. A bare except leaves Postgres aborted.
            try:
                with connection.begin_nested():
                    return int(connection.execute(text(sql), {"p": project_id}).scalar_one() or 0)
            except Exception:
                return 0

        from .metatron_command import MetatronCommand

        snap = {
            "budget_state": health.get("budget_state"),
            "open_tasks": summary.get("open_tasks", 0),
            "pending_decisions": summary.get("pending_decisions", 0),
            "active_jobs": summary.get("active_jobs", 0),
            "deadletters": summary.get("deadletters", 0),
            "completed_verify_green": _count(
                "SELECT count(*) FROM code_task_runs WHERE project_id=:p AND status='completed' "
                "AND COALESCE((state->>'tests_passed')::boolean, false)"
            ),
            "running_previews": _count(
                "SELECT count(*) FROM app_previews WHERE project_id=:p AND status='running' "
                "AND COALESCE(mode,'') <> 'fixture'"
            ),
            # HOW OFTEN HAS STARTING ONE JUST FAILED?
            #
            # Without this the restart_preview remedy has no memory. A preview that cannot
            # build fails, `running_previews` returns to 0, and the very next tick sees the
            # identical gap and starts it again — measured at 49 consecutive failures, one
            # every ~3 minutes, each a full npm install and vite build leaving a ~250MB workdir
            # behind. A self-healing action that cannot heal has to stop and escalate.
            "recent_failed_previews": _count(
                "SELECT count(*) FROM app_previews WHERE project_id=:p AND status='failed' "
                "AND COALESCE(mode,'') <> 'fixture' "
                f"AND created_at > now() - interval '{PREVIEW_FAILURE_WINDOW_MINUTES} minutes'"
            ),
            "in_flight": _count(
                "SELECT count(*) FROM code_task_runs WHERE project_id=:p "
                "AND status IN ('running','queued','repairing')"
            ),
            "top_risks": health.get("top_risks") or [],
            "scorecard": self._latest_run_scorecard(connection, project_id),
            # THE SYSTEM DOCTOR'S RUNG (stability spec §4c): Metatron's own moves start
            # previews and builds, so they wait while the host is under pressure.
            "system_level": _system_level(connection),
        }
        return MetatronCommand(self.settings).overlay_snapshot(connection, project_id, snap)

    def _latest_run_scorecard(self, connection: Connection, project_id: str) -> dict[str, Any] | None:
        """Attach the newest completed run's v1.1 card. Missing stays None."""
        try:
            with connection.begin_nested():
                row = connection.execute(
                    text(
                        """
                        SELECT state, finalize_ctx, status
                        FROM code_task_runs
                        WHERE project_id=:p AND status='completed'
                        ORDER BY updated_at DESC LIMIT 1
                        """
                    ),
                    {"p": project_id},
                ).mappings().first()
        except Exception:
            return None
        if not row:
            return None
        state = row.get("state") or {}
        if not isinstance(state, dict):
            return None
        evidence = {
            "ui_evidence": state.get("ui_evidence") or {},
            "ux_evidence": state.get("ux_evidence") or {},
            "capability_evidence": state.get("capability_evidence") or {},
            "interface_evidence": state.get("interface_evidence") or {},
        }
        if not any(evidence.values()) and state.get("tests_passed") is None:
            return None
        try:
            from .factory_quality_loop import score_run_with_workspace

            return score_run_with_workspace(
                {
                    "status": row.get("status"),
                    "state": state,
                    "finalize_ctx": row.get("finalize_ctx") or {},
                },
                evidence,
            )
        except Exception:
            return None

    def measure_all(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        snapshot = self.gather_snapshot(connection, project_id)
        out: list[dict[str, Any]] = []
        for goal in self.list_goals(connection, project_id):
            measured = measure_from_snapshot(goal["kind"], snapshot, goal.get("target") or {})
            connection.execute(
                text(
                    """
                    UPDATE metatron_goals
                    SET last_score=:s, last_measured_at=now(),
                        last_evidence=CAST(:e AS JSONB),
                        status=CASE WHEN :st IN ('achieved','failed') THEN :st ELSE status END,
                        updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "s": measured.get("score"), "e": json.dumps(measured.get("evidence") or {}),
                    "st": measured.get("status") or "active", "id": goal["id"],
                },
            )
            goal.update({"last_score": measured.get("score"), "last_evidence": measured.get("evidence"),
                         "measured_status": measured.get("status")})
            out.append(goal)
        return out

    def watch_once(self, connection: Connection, *, project_id: str, actor: str) -> dict[str, Any]:
        self.ensure_schema(connection)
        snapshot = self.gather_snapshot(connection, project_id)
        goals = self.measure_all(connection, project_id)
        findings = findings_from_snapshot(snapshot, goals)
        moves = proposed_moves_from_findings(findings, auto=False)
        scope = self._scope(connection, project_id)
        minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        receipt, _ = self.receipts.record_completed(
            connection, scope["org_id"], scope["workspace_id"], project_id,
            f"metatron:watch:{project_id}:{minute}",
            "metatron.watch",
            {"project_id": project_id},
            {"findings": findings, "moves": moves, "snapshot": snapshot},
        )
        row = connection.execute(
            text(
                """
                INSERT INTO metatron_watch_ticks
                  (id, project_id, observed, findings, proposed_moves, receipt_id)
                VALUES (:id, :p, CAST(:o AS JSONB), CAST(:f AS JSONB), CAST(:m AS JSONB), :r)
                RETURNING *
                """
            ),
            {
                "id": make_id("mwt"), "p": project_id,
                "o": json.dumps(snapshot), "f": json.dumps(findings),
                "m": json.dumps(moves), "r": receipt.get("id"),
            },
        ).mappings().one()
        self.remember(
            connection, project_id=project_id, content=f"Watch: {findings[0]['title']}",
            kind="watch", source="watch", salience=0.6,
            refs=[{"object_type": "metatron_watch_tick", "object_id": row["id"]}],
        )
        # ON CHANGE ONLY (core.emit_if_changed): the same findings every two minutes are one
        # piece of news, not 943.
        emit_if_changed(connection, project_id, "metatron.watch.ticked", actor, "metatron_watch_tick",
                        row["id"], {"finding_count": len(findings), "receipt_id": receipt.get("id"),
                                    "findings": sorted(str(f.get("id") or f.get("title") or "")
                                                       for f in findings)},
                        compare=("finding_count", "findings"))
        return self._row(row)

    def latest_watch(self, connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
        self.ensure_schema(connection)
        row = connection.execute(
            text(
                """
                SELECT * FROM metatron_watch_ticks
                WHERE project_id=:p ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"p": project_id},
        ).mappings().first()
        return self._row(row) if row else None

    def latest_overwatch(self, connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
        self.ensure_schema(connection)
        try:
            row = connection.execute(
                text(
                    """
                    SELECT * FROM metatron_watch_ticks
                    WHERE project_id=:p AND cycle='overwatch'
                    ORDER BY created_at DESC LIMIT 1
                    """
                ),
                {"p": project_id},
            ).mappings().first()
        except Exception:
            return None
        return self._row(row) if row else None

    def overwatch_once(
        self, connection: Connection, *, project_id: str, actor: str,
    ) -> dict[str, Any]:
        """Always-on cycle: observe → measure → validate → apply safe → verify."""
        self.ensure_schema(connection)
        before = self.gather_snapshot(connection, project_id)
        cycle_verdict = validate_action(
            "overwatch",
            {"project_id": project_id},
            snapshot=before,
            auto=True,
            in_flight=int(before.get("in_flight") or 0),
            budget_state=str(before.get("budget_state") or "unknown"),
        )
        watch = self.watch_once(connection, project_id=project_id, actor=actor)
        # Budget-red overwatch still observes and verifies; it must not apply.
        drive = self.drive(
            connection, project_id=project_id, actor=actor,
            auto=bool(cycle_verdict["ok"]),
        )
        applied = list(drive.get("applied") or [])
        rejected = list(drive.get("rejected") or [])
        verify = self.verify_cycle(
            connection, project_id, before=before, applied=applied,
        )
        try:
            connection.execute(
                text(
                    """
                    UPDATE metatron_watch_ticks
                    SET cycle='overwatch',
                        verify=CAST(:v AS JSONB),
                        applied=CAST(:a AS JSONB)
                    WHERE id=:id
                    """
                ),
                {
                    "v": json.dumps(verify),
                    "a": json.dumps(applied),
                    "id": watch.get("id"),
                },
            )
        except Exception:
            pass
        scope = self._scope(connection, project_id)
        minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        receipt, _ = self.receipts.record_completed(
            connection, scope["org_id"], scope["workspace_id"], project_id,
            f"metatron:overwatch:{project_id}:{minute}",
            "metatron.overwatch",
            {"project_id": project_id},
            {
                "applied": len(applied),
                "rejected": len(rejected),
                "verify_ok": bool(verify.get("ok")),
                "watch_id": watch.get("id"),
            },
        )
        self.remember(
            connection,
            project_id=project_id,
            content=(
                f"Overwatch: applied={len(applied)} rejected={len(rejected)} "
                f"verify={'ok' if verify.get('ok') else 'needs_attention'}"
            ),
            kind="watch",
            source="overwatch",
            salience=0.7,
            refs=[{"object_type": "metatron_watch_tick", "object_id": watch.get("id")}],
        )
        emit_if_changed(
            connection, project_id, "metatron.overwatch.completed", actor,
            "metatron_watch_tick", watch.get("id") or "none",
            {
                "applied": len(applied),
                "rejected": len(rejected),
                "verify_ok": bool(verify.get("ok")),
                "receipt_id": receipt.get("id"),
            },
            compare=("applied", "rejected", "verify_ok"),
        )
        status = "verified" if verify.get("ok") else "needs_attention"
        if applied:
            status = "applied" if verify.get("ok") else "applied_unverified"
        return {
            "status": status,
            "validation": cycle_verdict,
            "watch": watch,
            "drive": drive,
            "verify": verify,
            "applied": applied,
            "rejected": rejected,
            "goals": self.list_goals(connection, project_id),
            "receipt_id": receipt.get("id"),
            "note": (
                f"Overwatch validated then "
                f"{'applied' if cycle_verdict['ok'] else 'held'} "
                f"{len(applied)} move(s); verify "
                f"{'ok' if verify.get('ok') else 'failed'}. "
                "Decisions and deadletters stay human."
            ),
        }

    def theater(self, connection: Connection, *, project_id: str, actor: str) -> dict[str, Any]:
        """Read-only fleet view: captains, official work, proposed command powers."""
        self.ensure_schema(connection)
        from .metatron_command import command_moves, fleet_findings

        snapshot = self.gather_snapshot(connection, project_id)
        findings = fleet_findings(snapshot)
        moves = command_moves(findings, auto=False)
        fleet = {
            "official_in_flight": snapshot.get("official_in_flight") or 0,
            "official_queued": snapshot.get("official_queued") or 0,
            "factory_stalled": snapshot.get("factory_stalled") or 0,
            "captain_assignments": snapshot.get("captain_assignments") or 0,
            "playbooks": snapshot.get("playbooks") or 0,
            "agent_process_runs": snapshot.get("agent_process_runs") or 0,
            "active_goals": snapshot.get("active_goals") or 0,
            "in_flight": snapshot.get("in_flight") or 0,
            "budget_state": snapshot.get("budget_state"),
        }
        return {
            "project_id": project_id,
            "actor": actor,
            "fleet": fleet,
            "findings": findings,
            "moves": moves,
            "latest_overwatch": self.latest_overwatch(connection, project_id),
            "note": (
                "Metatron theater is observe-only. POST with auto=true applies "
                "re-prioritize / staff / playbook loop — never a second factory job."
            ),
        }

    def command_once(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        auto: bool = True,
    ) -> dict[str, Any]:
        """Apply bounded command powers via the existing overwatch apply path."""
        view = self.theater(connection, project_id=project_id, actor=actor)
        if not auto:
            return {
                **view,
                "status": "proposed",
                "applied": [],
                "rejected": [],
            }
        cycle = self.overwatch_once(connection, project_id=project_id, actor=actor)
        return {
            **cycle,
            "fleet": view.get("fleet") or {},
            "command_findings": view.get("findings") or [],
            "command_moves": view.get("moves") or [],
            "watch": cycle.get("watch") or view.get("latest_overwatch"),
        }

    def verify_cycle(
        self,
        connection: Connection,
        project_id: str,
        *,
        before: dict[str, Any],
        applied: list[dict[str, Any]],
    ) -> dict[str, Any]:
        after = self.gather_snapshot(connection, project_id)
        goals = self.measure_all(connection, project_id)
        result = verify_from_snapshots(before, after, applied)
        result["goals"] = [
            {
                "id": g.get("id"),
                "title": g.get("title"),
                "kind": g.get("kind"),
                "last_score": g.get("last_score"),
                "status": g.get("status"),
            }
            for g in goals
        ]
        return result

    def drive(
        self, connection: Connection, *, project_id: str, actor: str,
        auto: bool = False, directive: Optional[str] = None,
    ) -> dict[str, Any]:
        """Validate every move, then apply only those that pass.

        Decisions, deadletters, and budget-red never apply. Preview restart and
        factory launch apply only when validation (target, budget, no duplicate)
        passes. BOSS self-change is a governed PR, never a silent write.
        """
        self.ensure_schema(connection)
        watch = self.latest_watch(connection, project_id) or self.watch_once(
            connection, project_id=project_id, actor=actor
        )
        findings = list(watch.get("findings") or [])
        snapshot = watch.get("observed") or self.gather_snapshot(connection, project_id)
        if directive:
            findings.append({
                "id": "directive",
                "severity": "medium",
                "title": directive,
                "action": "pursue_goal",
                "auto_safe": False,
            })
        moves = proposed_moves_from_findings(findings, auto=bool(auto))
        verify_run = self._latest_verify_green_without_preview(connection, project_id)
        in_flight = int(snapshot.get("in_flight") or 0)
        budget_state = str(snapshot.get("budget_state") or "unknown")
        repo = f"{self.settings.target_github_owner}/{self.settings.target_github_repo}"
        applied: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for move in moves:
            verdict = validate_move(
                move, snapshot, auto=bool(auto), in_flight=in_flight,
                budget_state=budget_state, verify_green_run_id=verify_run,
                self_mod_repo=repo,
            )
            move["validation"] = verdict
            if not (auto and move.get("apply") and verdict["ok"]):
                if not verdict["ok"] and move.get("apply"):
                    move["status"] = "rejected"
                    rejected.append(move)
                continue
            result = self._apply_validated_move(
                connection, project_id=project_id, actor=actor,
                move=move, verify_run=verify_run, directive=directive,
            )
            move.update(result)
            if result.get("status") == "applied":
                applied.append(move)
                if move.get("kind") == "launch_factory_job":
                    in_flight += 1
            else:
                rejected.append(move)
        human = [m for m in moves if m.get("status") == "needs_human" or m.get("requires_human")]
        emit_if_changed(
            connection, project_id, "metatron.drive.completed", actor, "metatron_watch_tick",
            watch.get("id") or "none",
            {
                "auto": bool(auto), "applied": len(applied), "rejected": len(rejected),
                "human_required": len(human),
            },
            compare=("auto", "applied", "rejected", "human_required"),
        )
        status = "applied" if applied else "proposed"
        return {
            "status": status,
            "auto": bool(auto),
            "applied": applied,
            "rejected": rejected,
            "moves": moves,
            "watch_id": watch.get("id"),
            "note": (
                f"Validated {len(moves)} move(s): {len(applied)} applied, "
                f"{len(rejected)} rejected, {len(human)} need a human. "
                "Decisions and deadletters are never auto-applied. "
                "BOSS self-change opens a PR."
            ),
        }

    def _latest_verify_green_without_preview(self, connection: Connection, project_id: str) -> Optional[str]:
        try:
            row = connection.execute(
                text(
                    """
                    SELECT r.id FROM code_task_runs r
                    WHERE r.project_id=:p AND r.status='completed'
                      AND COALESCE((r.state->>'tests_passed')::boolean, false)
                      AND NOT EXISTS (
                        SELECT 1 FROM app_previews p
                        WHERE p.run_id=r.id AND p.status='running'
                          AND COALESCE(p.mode,'') <> 'fixture'
                      )
                    ORDER BY r.updated_at DESC LIMIT 1
                    """
                ),
                {"p": project_id},
            ).mappings().first()
        except Exception:
            return None
        return str(row["id"]) if row else None

    def _apply_validated_move(
        self, connection: Connection, *, project_id: str, actor: str,
        move: dict[str, Any], verify_run: Optional[str], directive: Optional[str],
    ) -> dict[str, Any]:
        kind = move.get("kind")
        scope = self._scope(connection, project_id)
        from .metatron_command import COMMAND_KINDS, MetatronCommand

        if kind in COMMAND_KINDS:
            result = MetatronCommand(self.settings)._apply(
                connection, project_id=project_id, actor=actor, move=move,
            )
            if result.get("status") == "applied":
                minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
                receipt, _ = self.receipts.record_completed(
                    connection, scope["org_id"], scope["workspace_id"], project_id,
                    f"metatron:command:{kind}:{project_id}:{minute}",
                    f"metatron.command.{kind}",
                    {"kind": kind, "project_id": project_id},
                    result,
                )
                result["receipt_id"] = receipt.get("id")
            return result
        if kind == "restart_preview":
            if not verify_run:
                return {"status": "rejected", "error": "no verify-green run to preview"}
            try:
                from .preview_service import start_preview_for_run
                engine = connection.engine
                preview = start_preview_for_run(engine, self.settings, verify_run, ttl_minutes=60)
            except Exception as exc:  # noqa: BLE001
                return {"status": "rejected", "error": f"preview failed: {type(exc).__name__}"}
            ok = preview.get("status") == "running"
            receipt, _ = self.receipts.create_or_reuse(
                connection, scope["org_id"], scope["workspace_id"], project_id,
                f"metatron:preview:{verify_run}",
                "metatron.preview_restart",
                {"run_id": verify_run},
                {"preview": preview.get("id"), "status": preview.get("status")},
            )
            return {
                "status": "applied" if ok else "rejected",
                "preview_id": preview.get("id"),
                "run_id": verify_run,
                "receipt_id": receipt.get("id"),
                "error": None if ok else preview.get("error"),
            }
        if kind == "launch_factory_job":
            # ASKED: an operator's directive, or a focused agent's (someone created that duty
            # with this power). A pass the Captain found on its own needs the project's opt-in.
            held = captain_launch_gate(connection, project_id, settings=self.settings,
                                       asked=bool(directive))
            if held:
                return {"status": "rejected", "error": held["message"], "held": held}
            from .code_task_service import start_code_task
            from .jobs import JobLeaseService
            jobs = JobLeaseService(self.settings)
            title = (directive or move.get("detail") or "Captain factory launch")[:200]
            self_mod = "boss" in title.lower() or "self-improv" in title.lower()
            owner = self.settings.target_github_owner
            repo = self.settings.target_github_repo
            if self_mod:
                owner = getattr(self.settings, "github_owner", None) or owner
                repo = "boss"
            try:
                run_id = start_code_task(
                    connection,
                    project_id=project_id,
                    task_id=None,
                    title=f"Captain: {title}",
                    instructions=(
                        (directive or move.get("detail") or title)
                        + ("\n\nThis is a BOSS self-improvement change. Open a PR only. "
                           "Do not apply to production in place." if self_mod else "")
                    ),
                    acceptance_criteria=["npm run verify or equivalent exits 0"],
                    test_commands=["npm run verify"] if not self_mod else [],
                    owner=owner,
                    repo=repo,
                    base_branch="main",
                    finalize_ctx={
                        "source": "metatron_captain",
                        "actor": actor,
                        "self_mod": bool(self_mod),
                        "open_pr": True,
                    },
                    jobs=jobs,
                    open_pr=True,
                    risk_tier=1 if self_mod else 0,
                )
            except Exception as exc:  # noqa: BLE001
                return {"status": "rejected", "error": f"factory launch failed: {exc}"}
            receipt, _ = self.receipts.create_or_reuse(
                connection, scope["org_id"], scope["workspace_id"], project_id,
                f"metatron:factory:{run_id}",
                "metatron.factory_launch",
                {"run_id": run_id, "self_mod": self_mod},
                {"title": title},
            )
            return {
                "status": "applied",
                "run_id": run_id,
                "receipt_id": receipt.get("id"),
                "self_mod": bool(self_mod),
            }
        return {"status": "rejected", "error": f"no apply path for {kind}"}

    def _process_service(self):
        from .agent_process import AgentProcessService

        return AgentProcessService(self.settings)

    def _compile_process_spec(self, description: str) -> dict[str, Any] | None:
        """Use process_compiler when present. Missing compiler is not an error."""
        try:
            from . import process_compiler as compiler_mod
        except ImportError:
            return None
        candidates = []
        for name in ("compile_process", "compile", "compile_process_text"):
            fn = getattr(compiler_mod, name, None)
            if callable(fn):
                candidates.append(fn)
        cls = getattr(compiler_mod, "ProcessCompiler", None)
        if cls is not None:
            try:
                inst = cls(self.settings) if getattr(cls, "__init__", None) else cls()
            except TypeError:
                inst = cls()
            for name in ("compile", "compile_process"):
                fn = getattr(inst, name, None)
                if callable(fn):
                    candidates.append(fn)
        for fn in candidates:
            try:
                compiled = fn(description)
            except TypeError:
                try:
                    compiled = fn(text=description)
                except Exception:
                    continue
            except Exception:
                continue
            if isinstance(compiled, dict) and compiled:
                return compiled
        return None

    def _build_process(
        self, connection: Connection, *, project_id: str, actor: str, description: str,
    ) -> dict[str, Any]:
        """Publish a process template. Simulated authoring never spends."""
        from .agent_process import PRD_TO_ACCEPTED_SPEC

        svc = self._process_service()
        svc.ensure_schema(connection)
        compiled = self._compile_process_spec(description)
        spec: dict[str, Any] | None = None
        if isinstance(compiled, dict) and compiled.get("steps"):
            spec = compiled
        elif matches_default_process_story(description):
            spec = dict(PRD_TO_ACCEPTED_SPEC)
        if spec is None:
            return {
                "kind": "build_process",
                "status": "needs_compiler",
                "reply": (
                    "I can publish the default PRD-to-accepted-work process from that "
                    "description, or compile a custom one when the process compiler is "
                    "available. Try: build a process for PRD to accepted work."
                ),
            }
        process_key = str(spec.get("key") or DEFAULT_PROCESS_KEY)
        name = str(spec.get("name") or description or process_key)[:160]
        existing = svc.get_template_by_key(connection, project_id, process_key)
        if existing:
            template = existing
            created = False
        else:
            try:
                template = svc.create_template(
                    connection,
                    project_id=project_id,
                    actor=actor,
                    process_key=process_key,
                    name=name,
                    description=description or spec.get("description") or "",
                    spec=spec,
                )
            except ValueError as exc:
                return {
                    "kind": "build_process",
                    "status": "rejected",
                    "process_key": process_key,
                    "reply": f"Process spec was rejected: {exc}",
                }
            created = True
        scope = self._scope(connection, project_id)
        receipt, _ = self.receipts.create_or_reuse(
            connection, scope["org_id"], scope["workspace_id"], project_id,
            f"metatron:process:{project_id}:{process_key}",
            "metatron.process_template",
            {"process_key": process_key, "simulated": True},
            {"template_id": template.get("id"), "created": created},
        )
        verb = "Published" if created else "Already have"
        return {
            "kind": "build_process",
            "status": "applied" if created else "reused",
            "template_id": template.get("id"),
            "process_key": process_key,
            "receipt_id": receipt.get("id"),
            "reply": (
                f"{verb} process '{name}' ({process_key}). "
                "Simulated authoring — no budget spend. "
                f"Start it with: start process {process_key} <prd>."
            ),
        }

    def _start_process(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        parsed: dict[str, Any],
        action_verdict: dict[str, Any],
    ) -> dict[str, Any]:
        """Start a process run. Chat defaults to simulated; auto_build needs validation."""
        prd = str(parsed.get("text") or "").strip()
        process_key = str(parsed.get("process_key") or DEFAULT_PROCESS_KEY).strip() or DEFAULT_PROCESS_KEY
        auto_requested = bool(parsed.get("auto_build"))
        auto_ok = bool(auto_requested and action_verdict.get("ok"))
        if not prd:
            return {
                "kind": "start_process",
                "status": "needs_prd",
                "process_key": process_key,
                "reply": (
                    f"Need PRD text to start `{process_key}`. "
                    f"Try: start process {process_key} <prd text>. "
                    "Chat starts are simulated unless you say auto_build and validation passes."
                ),
            }
        svc = self._process_service()
        try:
            run = svc.start_run(
                connection,
                project_id=project_id,
                actor=actor,
                prd=prd,
                title=prd[:80],
                process_key=process_key,
                simulated=not auto_ok,
                auto_build=auto_ok,
            )
        except ValueError as exc:
            return {
                "kind": "start_process",
                "status": "rejected",
                "process_key": process_key,
                "reply": f"Could not start process `{process_key}`: {exc}",
            }
        mode = "live auto_build" if auto_ok else "simulated (no code.task)"
        status = run.get("status")
        return {
            "kind": "start_process",
            "status": "applied",
            "run_id": run.get("id"),
            "process_key": run.get("process_key") or process_key,
            "run_status": status,
            "simulated": not auto_ok,
            "auto_build": auto_ok,
            "reply": (
                f"Started `{process_key}` as {mode}. Run {run.get('id')} is {status}. "
                "Decisions stay human. Simulated starts never spend."
            ),
        }

    def _list_processes(self, connection: Connection, *, project_id: str) -> dict[str, Any]:
        """List templates + recent runs. Honest empty — do not seed a default."""
        svc = self._process_service()
        try:
            svc.ensure_schema(connection)
            rows = connection.execute(
                text(
                    """
                    SELECT id, process_key, name, status, updated_at
                    FROM agent_process_templates
                    WHERE project_id=:p
                    ORDER BY updated_at DESC
                    """
                ),
                {"p": project_id},
            ).mappings().all()
            templates = [svc._row(row) for row in rows]
            runs = svc.list_runs(connection, project_id, limit=8)
        except Exception:
            templates = []
            runs = []
        if not templates and not runs:
            return {
                "kind": "list_processes",
                "status": "empty",
                "templates": [],
                "runs": [],
                "reply": (
                    "No process templates or runs yet. "
                    "Author one with: build a process for PRD to accepted work."
                ),
            }
        lines = ["Process templates:"]
        for item in templates[:12]:
            lines.append(f"- {item.get('name')} ({item.get('process_key')}) {item.get('status')}")
        if not templates:
            lines.append("- (none)")
        lines.append("Recent runs:")
        if runs:
            for item in runs[:8]:
                lines.append(
                    f"- {item.get('id')} {item.get('template_name') or item.get('process_key')} "
                    f"{item.get('status')}"
                )
        else:
            lines.append("- (none)")
        return {
            "kind": "list_processes",
            "status": "ok",
            "templates": templates,
            "runs": runs,
            "reply": "\n".join(lines),
        }

    def _scope(self, connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT p.id AS project_id, w.id AS workspace_id, o.id AS org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if row is None:
            return {"project_id": project_id, "workspace_id": "workspace_unknown", "org_id": "org_unknown"}
        return dict(row)

    @staticmethod
    def _row(row) -> dict[str, Any]:
        out = dict(row)
        for key, value in list(out.items()):
            if hasattr(value, "isoformat"):
                out[key] = value.isoformat()
            elif hasattr(value, "__float__") and key in {"last_score", "salience"}:
                try:
                    out[key] = float(value)
                except Exception:
                    pass
        return out

    @staticmethod
    def _render_overwatch(cycle: dict[str, Any]) -> str:
        verify = cycle.get("verify") or {}
        lines = [
            cycle.get("note") or "Overwatch cycle complete.",
            f"Verify: {'ok' if verify.get('ok') else 'needs attention'}.",
        ]
        for move in (cycle.get("applied") or [])[:6]:
            lines.append(f"- applied {move.get('kind')}: {move.get('status')}")
        for move in (cycle.get("rejected") or [])[:4]:
            failed = (move.get("validation") or {}).get("failed") or []
            lines.append(f"- rejected {move.get('kind')}: {', '.join(failed) or move.get('error')}")
        if not cycle.get("applied") and not cycle.get("rejected"):
            lines.append("No apply this cycle. Still watching.")
        return "\n".join(lines)

    @staticmethod
    def _render_theater(cycle: dict[str, Any]) -> str:
        fleet = cycle.get("fleet") or {}
        lines = [
            cycle.get("note") or "Metatron theater.",
            (
                f"Fleet: official={fleet.get('official_in_flight') or 0} "
                f"queued={fleet.get('official_queued') or 0} "
                f"stalled={fleet.get('factory_stalled') or 0} "
                f"captain assignments={fleet.get('captain_assignments') or 0} "
                f"playbooks={fleet.get('playbooks') or 0}."
            ),
        ]
        for finding in (cycle.get("command_findings") or cycle.get("findings") or [])[:4]:
            lines.append(f"- {finding.get('id')}: {finding.get('title')}")
        for move in (cycle.get("applied") or [])[:6]:
            lines.append(f"- applied {move.get('kind')}: {move.get('status')}")
        if not cycle.get("applied"):
            lines.append("No command power applied. Official work was not duplicated.")
        return "\n".join(lines)

    @staticmethod
    def _render_watch(watch: dict[str, Any]) -> str:
        findings = watch.get("findings") or []
        lines = ["Watch complete."]
        for item in findings[:6]:
            lines.append(f"- [{item.get('severity')}] {item.get('title')}")
        return "\n".join(lines)

    @staticmethod
    def _render_goals(goals: list[dict[str, Any]]) -> str:
        if not goals:
            return ""
        lines = ["Goals:"]
        for goal in goals:
            score = goal.get("last_score")
            score_s = "—" if score is None else str(score)
            lines.append(f"- {goal.get('title')} ({goal.get('kind')}) score={score_s} {goal.get('status')}")
        return "\n".join(lines)

    @staticmethod
    def _render_memories(memories: list[dict[str, Any]]) -> str:
        if not memories:
            return ""
        return "Memory:\n" + "\n".join(f"- {m.get('content')}" for m in memories[:8])

    @staticmethod
    def _render_drive(drive: dict[str, Any]) -> str:
        moves = drive.get("moves") or []
        if not moves:
            return "No drive proposed. The watch is steady."
        lines = [drive.get("note") or "Proposed moves (not applied):"]
        for move in moves:
            lines.append(f"- {move.get('kind')}: {move.get('detail')}")
        return "\n".join(lines)
