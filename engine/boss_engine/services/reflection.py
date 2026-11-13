"""The reflection agent: reads an agent's recent runs and PROPOSES prompt and skill changes (BOSSAPP-888).

WHY. Agents did not improve their own instructions from evidence. Each run's retrospective said
what went wrong; agents filed friction (BOSSAPP-889); skill invocations recorded every failure and
refusal — and nothing turned that into a change to how the agent works.

WHAT IT DOES, FOR ONE ROLE
  1. EVIDENCE (deterministic, no model): the role's open friction reports (agent_feedback) and its
     failed or refused skill invocations (skill_invocations) in a window, each with the run it was
     seen in. Credentials are redacted before anything is quoted.
  2. PROPOSALS, only where the evidence repeats (MIN_EVIDENCE):
     * skill_diff — an AUTHORED skill the role kept failing on or complaining about gets a
       "Known pitfalls" section appended to its knowledge, each line citing its run. Opened with
       skill_definitions.propose_change: the existing draft_skill + BOSSAPP-843 consent path, so
       a person approves exactly that diff and nothing is written before.
     * agent_prompt_diff — everything else (briefs, grants, tools, models, built-in skills) becomes
       one proposed instruction document for the agent, "Lessons from recent runs". Opened as a
       change_consent (subject 'prompt') whose decision a PERSON approves; only then is the
       document added (agent_instructions.add, as that person — agents never write instructions).
     Each proposal carries its cited runs and its replay cases: the runs a re-run would check.
  3. RECORD: a reflection_runs row with the evidence counts and the decisions it opened.

IT ONLY PROPOSES. It never writes a skill or an instruction, never approves, and an open proposal
for the same skill or agent is not proposed again. PAUSED BY DEFAULT: the schedule (`reflect_due`)
runs only for a project that turned it on (reflection_settings.enabled); a person may press
Reflect now at any time.

DEFERRED, SAID PLAINLY: the drafts are assembled from the evidence, not written by a model, and an
approved change is applied at once — the self_improvement canary/eval-gated rollout is not wired.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

_log = logging.getLogger("boss.reflection")

#: Who proposes. An agent principal, so every write it leads to needs a person's consent.
REFLECTION_ACTOR = "agent:reflection"
#: The decision a person answers for an agent_prompt_diff.
PROMPT_ACTION = "apply_agent_prompt_diff"
#: Evidence items about one target before a change is proposed for it.
MIN_EVIDENCE = 2
WINDOW_DAYS = 14
MAX_LINES = 12
LINE_MAX = 300
LESSONS_NAME = "Lessons from recent runs"


class ReflectionError(ValueError):
    """Something a person can fix, in words."""


def _redact(value: Any) -> str:
    from .redaction import redact_secrets

    return redact_secrets(" ".join(str(value or "").split()))[:LINE_MAX]


# ── settings ────────────────────────────────────────────────────────────────────────────────

def get_settings(connection: Connection, project_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT enabled, every_hours, roles, last_run_at, updated_by, updated_at "
        "FROM reflection_settings WHERE project_id = :p"), {"p": project_id}).mappings().first()
    if row is None:
        return {"enabled": False, "every_hours": 168, "roles": [], "last_run_at": None,
                "updated_by": None, "paused_by_default": True}
    roles = row["roles"]
    if isinstance(roles, str):
        roles = json.loads(roles)
    return {"enabled": bool(row["enabled"]), "every_hours": int(row["every_hours"]),
            "roles": list(roles or []),
            "last_run_at": row["last_run_at"].isoformat() if row["last_run_at"] else None,
            "updated_by": row["updated_by"], "paused_by_default": False}


def set_settings(connection: Connection, project_id: str, *, actor: str,
                 enabled: Optional[bool] = None, every_hours: Optional[int] = None,
                 roles: Optional[list[str]] = None) -> dict[str, Any]:
    now = get_settings(connection, project_id)
    hours = int(every_hours if every_hours is not None else now["every_hours"])
    if not 1 <= hours <= 2160:
        raise ReflectionError("every_hours must be between 1 and 2160 (90 days)")
    keys = [str(r).strip().lower() for r in (roles if roles is not None else now["roles"])
            if str(r).strip()]
    connection.execute(text(
        "INSERT INTO reflection_settings (project_id, enabled, every_hours, roles, updated_by) "
        "VALUES (:p, :e, :h, CAST(:r AS JSONB), :a) ON CONFLICT (project_id) DO UPDATE SET "
        "enabled = EXCLUDED.enabled, every_hours = EXCLUDED.every_hours, roles = EXCLUDED.roles, "
        "updated_by = EXCLUDED.updated_by, updated_at = now()"),
        {"p": project_id, "e": bool(now["enabled"] if enabled is None else enabled), "h": hours,
         "r": json.dumps(sorted(set(keys))), "a": actor})
    out = get_settings(connection, project_id)
    emit_event(connection, project_id, "reflection.settings_changed", actor,
               "reflection_settings", project_id,
               {"enabled": out["enabled"], "every_hours": out["every_hours"],
                "roles": out["roles"]})
    return out


# ── evidence ────────────────────────────────────────────────────────────────────────────────

def gather(connection: Connection, project_id: str, role_key: str, *,
           window_days: int = WINDOW_DAYS) -> dict[str, Any]:
    """The role's friction reports and failed/refused skill calls in the window, redacted."""
    feedback = connection.execute(text(
        "SELECT id, kind, target_kind, target, body, run_ref, created_at FROM agent_feedback "
        "WHERE project_id = :p AND role_key = :r AND status IN ('open', 'acknowledged') "
        "AND created_at > now() - make_interval(days => :d) ORDER BY created_at"),
        {"p": project_id, "r": role_key, "d": int(window_days)}).mappings().all()
    failures = connection.execute(text(
        "SELECT id, skill_id, status, reason, created_by_job_id, created_at FROM skill_invocations "
        "WHERE project_id = :p AND caller = :r AND status IN ('failed', 'denied') "
        "AND created_at > now() - make_interval(days => :d) ORDER BY created_at"),
        {"p": project_id, "r": role_key, "d": int(window_days)}).mappings().all()
    items: list[dict[str, Any]] = []
    for f in feedback:
        items.append({"source": "feedback", "id": f["id"], "target_kind": f["target_kind"],
                      "target": f["target"], "said": _redact(f"{f['kind']}: {f['body']}"),
                      "run": f["run_ref"] or None})
    for s in failures:
        items.append({"source": "skill_invocation", "id": s["id"], "target_kind": "skill",
                      "target": s["skill_id"],
                      "said": _redact(f"{s['status']}: {s['reason'] or 'no reason recorded'}"),
                      "run": s["created_by_job_id"] or s["id"]})
    return {"role_key": role_key, "window_days": int(window_days), "items": items,
            "feedback": len(feedback), "skill_failures": len(failures)}


def _authored(connection: Connection, project_id: str, key: str) -> Optional[dict[str, Any]]:
    from .skill_definitions import get_skill

    return get_skill(connection, project_id, key)


def _cited(items: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    runs = sorted({str(i["run"]) for i in items if i.get("run")})
    replay = [{"run": i.get("run"), "target": f"{i['target_kind']} {i['target']}",
               "what_happened": i["said"]} for i in items if i.get("run")][:MAX_LINES]
    return runs, replay


def _lines(items: list[dict[str, Any]], *, with_target: bool) -> list[str]:
    out = []
    for i in items[:MAX_LINES]:
        where = f" (seen in {i['run']})" if i.get("run") else ""
        head = f"{i['target_kind']} {i['target']}: " if with_target else ""
        out.append(f"- {head}{i['said']}{where}")
    return out


# ── proposals ───────────────────────────────────────────────────────────────────────────────

def _propose_skill_diff(connection: Connection, project_id: str, role_key: str, skill: dict,
                        items: list[dict[str, Any]], run_id: str) -> Optional[dict[str, Any]]:
    from . import change_consent
    from .skill_definitions import prepare_skill, propose_change

    key = skill["skill_key"]
    if change_consent.pending(connection, project_id, "skill", key):
        return None                                   # one open proposal at a time
    stamp = datetime.now(timezone.utc).date().isoformat()
    section = "\n".join([f"## Known pitfalls (reflection on {role_key}'s runs, {stamp})",
                         *_lines(items, with_target=False)])
    knowledge = (str(skill.get("knowledge") or "").rstrip() + "\n\n" + section).strip()
    prepared = prepare_skill(
        connection, project_id=project_id, skill_key=key, label=skill["label"],
        behaviour_kind=skill["behaviour_kind"], summary=skill.get("summary") or "",
        knowledge=knowledge, inputs=skill.get("inputs") or [],
        produces=skill.get("produces") or [], config=skill.get("config") or {},
        risk_tier=skill.get("risk_tier", 1), mode="update")
    runs, replay = _cited(items)
    opened = propose_change(
        connection, project_id=project_id, actor=REFLECTION_ACTOR, prepared=prepared,
        mode="update", reason="reflection_skill_diff",
        meta={"reflection_run_id": run_id, "role_key": role_key, "cited_runs": runs,
              "replay_cases": replay},
        note=f"proposed by reflection on {role_key}'s runs ({len(items)} items of evidence)")
    return {"kind": "skill_diff", "target": key, "decision_id": opened["decision_id"],
            "consent_id": opened["consent_id"], "cited_runs": runs, "replay_cases": replay,
            "evidence": len(items)}


def _instructions_base(connection: Connection, project_id: str, role_key: str) -> str:
    """What the agent's instructions are now, as a hash: a proposal is written onto exactly this."""
    from .agent_instructions import list_for

    docs = [{"name": d.get("name"), "sha256": d.get("sha256"), "modes": d.get("modes")}
            for d in list_for(connection, project_id, role_key)]
    return hashlib.sha256(json.dumps(docs, sort_keys=True, default=str).encode()).hexdigest()


def _propose_prompt_diff(connection: Connection, project_id: str, role_key: str,
                         items: list[dict[str, Any]], run_id: str) -> Optional[dict[str, Any]]:
    from . import change_consent
    from .agent_instructions import list_for

    if change_consent.pending(connection, project_id, "prompt", role_key):
        return None
    stamp = datetime.now(timezone.utc).date().isoformat()
    name = f"{LESSONS_NAME} {stamp}"
    if any(str(d.get("name") or "").lower() == name.lower()
           for d in list_for(connection, project_id, role_key)):
        return None
    body = "\n".join([
        "These notes come from your own recent runs. Take them into account; they never widen "
        "what you may do.", *_lines(items, with_target=True)])
    proposed = {"name": name, "body": body, "modes": ["work", "chat"], "source": "pasted"}
    base = _instructions_base(connection, project_id, role_key)
    diff = "\n".join([f"agent {role_key}: add instruction document {name!r}",
                      *[f"+ {line}" for line in body.splitlines()]])
    runs, replay = _cited(items)
    opened = change_consent.request(
        connection, project_id=project_id, subject_kind="prompt", subject_key=role_key,
        base_hash=base, proposed=proposed, diff=diff, requested_by=REFLECTION_ACTOR,
        question=f"Add {name!r} to {role_key}'s instructions?\n\n{diff}",
        action=PROMPT_ACTION,
        recommendation={"role_key": role_key, "proposed": proposed, "cited_runs": runs,
                        "replay_cases": replay, "reflection_run_id": run_id},
        reason="reflection_prompt_diff", risk_tier=1,
        meta={"reflection_run_id": run_id})
    return {"kind": "agent_prompt_diff", "target": role_key, "decision_id": opened["decision_id"],
            "consent_id": opened["consent_id"], "cited_runs": runs, "replay_cases": replay,
            "evidence": len(items)}


def reflect(connection: Connection, project_id: str, role_key: str, *, actor: str,
            trigger: str = "manual", window_days: int = WINDOW_DAYS) -> dict[str, Any]:
    """Reflect on one role's recent runs and open the proposals the evidence supports."""
    role = str(role_key or "").strip().lower()
    known = connection.execute(text(
        "SELECT 1 FROM roles WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": role}).first()
    if not known:
        raise ReflectionError(f"no agent {role!r} on this project")
    run_id = make_id("rfl")
    evidence = gather(connection, project_id, role, window_days=window_days)
    by_target: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in evidence["items"]:
        by_target[(item["target_kind"], item["target"])].append(item)
    proposals: list[dict[str, Any]] = []
    for_prompt: list[dict[str, Any]] = []
    for (kind, target), items in sorted(by_target.items()):
        skill = _authored(connection, project_id, target) if kind in ("skill", "tool") else None
        if skill is not None and len(items) >= MIN_EVIDENCE:
            made = _propose_skill_diff(connection, project_id, role, skill, items, run_id)
            if made:
                proposals.append(made)
        elif skill is None:
            for_prompt.extend(items)
    if len(for_prompt) >= MIN_EVIDENCE:
        made = _propose_prompt_diff(connection, project_id, role, for_prompt, run_id)
        if made:
            proposals.append(made)
    status = "proposed" if proposals else "nothing_to_propose"
    summary = {"feedback": evidence["feedback"], "skill_failures": evidence["skill_failures"],
               "targets": len(by_target), "window_days": evidence["window_days"]}
    connection.execute(text(
        "INSERT INTO reflection_runs (id, project_id, role_key, trigger, status, evidence, "
        "proposals, created_by, created_at) VALUES (:i, :p, :r, :t, :s, CAST(:e AS JSONB), "
        "CAST(:pr AS JSONB), :a, clock_timestamp())"),
        {"i": run_id, "p": project_id, "r": role, "t": trigger, "s": status,
         "e": json.dumps(summary), "pr": json.dumps(proposals, default=str), "a": actor})
    emit_event(connection, project_id, "reflection.ran", actor, "reflection_run", run_id,
               {"role_key": role, "status": status, "proposals": len(proposals), **summary})
    return {"id": run_id, "role_key": role, "status": status, "evidence": summary,
            "proposals": proposals}


def list_runs(connection: Connection, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT id, role_key, trigger, status, evidence, proposals, error, created_by, created_at "
        "FROM reflection_runs WHERE project_id = :p ORDER BY created_at DESC LIMIT :l"),
        {"p": project_id, "l": max(1, min(int(limit), 100))}).mappings().all()
    out = []
    for r in rows:
        load = lambda v: json.loads(v) if isinstance(v, str) else v  # noqa: E731
        out.append({"id": r["id"], "role_key": r["role_key"], "trigger": r["trigger"],
                    "status": r["status"], "evidence": load(r["evidence"]) or {},
                    "proposals": load(r["proposals"]) or [], "error": r["error"],
                    "created_by": r["created_by"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None})
    return out


# ── approval of an agent_prompt_diff (decisions._handle_approved_follow_on) ──────────────────

class PromptDiffStale(ReflectionError):
    """The agent's instructions changed after the proposal: it is no longer the diff shown."""


def apply_prompt_diff(connection: Connection, decision: dict[str, Any], actor: str) -> dict:
    """A PERSON approved the proposal: add exactly the document shown, as that person."""
    from . import change_consent
    from .agent_instructions import add

    consent = change_consent.approve_for_decision(connection, str(decision["id"]), actor)
    if consent is None:
        raise ReflectionError("this proposal has no open consent to approve")
    project_id = str(decision["project_id"])
    role = consent["subject_key"]
    proposed = {k: v for k, v in (consent["proposed"] or {}).items() if k != "_meta"}
    base = _instructions_base(connection, project_id, role)
    digest = change_consent.diff_hash("prompt", role, base, proposed)
    if change_consent.consume(connection, project_id=project_id, subject_kind="prompt",
                              subject_key=role, digest=digest) is None:
        raise PromptDiffStale(
            f"{role}'s instructions changed after this was proposed, so it is no longer the "
            "change you are looking at. Reject it; the next reflection proposes it again.")
    return add(connection, project_id, role, actor=actor, name=proposed["name"],
               body=proposed["body"], source=proposed.get("source") or "pasted",
               modes=proposed.get("modes"))


# ── the schedule (paused by default) ────────────────────────────────────────────────────────

def _roles_with_evidence(connection: Connection, project_id: str, window_days: int) -> list[str]:
    rows = connection.execute(text(
        "SELECT DISTINCT role_key FROM agent_feedback WHERE project_id = :p AND role_key IS NOT "
        "NULL AND created_at > now() - make_interval(days => :d) UNION SELECT DISTINCT caller "
        "FROM skill_invocations WHERE project_id = :p AND status IN ('failed', 'denied') "
        "AND created_at > now() - make_interval(days => :d)"),
        {"p": project_id, "d": int(window_days)}).scalars().all()
    return sorted({str(r) for r in rows if r})


def reflect_due(engine: Any, *, limit: int = 10) -> list[dict[str, Any]]:
    """Every ENABLED project whose last reflection is older than its cadence: claimed atomically
    (so two API processes never both run it), then each named role — or every role with evidence
    — is reflected on. A project that never turned reflection on is never touched."""
    with engine.begin() as c:
        claimed = c.execute(text(
            "UPDATE reflection_settings SET last_run_at = now() WHERE project_id IN ("
            "SELECT project_id FROM reflection_settings WHERE enabled AND (last_run_at IS NULL "
            "OR last_run_at < now() - make_interval(hours => every_hours)) "
            "ORDER BY last_run_at NULLS FIRST LIMIT :l FOR UPDATE SKIP LOCKED) "
            "RETURNING project_id, roles"), {"l": int(limit)}).mappings().all()
    done: list[dict[str, Any]] = []
    for row in claimed:
        roles = row["roles"]
        if isinstance(roles, str):
            roles = json.loads(roles)
        with engine.begin() as c:
            targets = list(roles or []) or _roles_with_evidence(c, row["project_id"], WINDOW_DAYS)
        for role in targets:
            try:
                with engine.begin() as c:
                    done.append(reflect(c, row["project_id"], role, actor=REFLECTION_ACTOR,
                                        trigger="scheduled"))
            except Exception as exc:  # noqa: BLE001 — one role never stops the others
                _log.warning("reflection on %s failed", role, exc_info=True)
                with engine.begin() as c:
                    c.execute(text(
                        "INSERT INTO reflection_runs (id, project_id, role_key, trigger, status, "
                        "error, created_by) VALUES (:i, :p, :r, 'scheduled', 'failed', :e, :a)"),
                        {"i": make_id("rfl"), "p": row["project_id"], "r": str(role),
                         "e": f"{type(exc).__name__}: {exc}"[:500], "a": REFLECTION_ACTOR})
    return done


_STARTED = threading.Event()


def start(engine: Any, settings: Any) -> None:
    """The schedule's tick: every BOSS_REFLECTION_TICK_MINUTES (default 60; 0 = off). Each tick
    does work only for projects that turned reflection on."""
    import os

    minutes = int(os.environ.get("BOSS_REFLECTION_TICK_MINUTES", "60") or 0)
    if minutes <= 0 or _STARTED.is_set():
        return
    _STARTED.set()

    def loop() -> None:
        time.sleep(120)  # let the API finish booting first
        while True:
            try:
                reflect_due(engine)
            except Exception:  # noqa: BLE001 — the loop must outlive one bad tick
                _log.warning("reflection tick failed", exc_info=True)
            time.sleep(max(60, minutes * 60))

    threading.Thread(target=loop, name="boss-reflection", daemon=True).start()
