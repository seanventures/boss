"""AN AGENT ASKS TO HIRE, OR TO GO OVER BUDGET FOR ONE RUN (BOSSAPP-884).

Agents were made only by people, and a budget refusal was a refusal with a remedy ("raise the
cap"), never a question a person could say yes to. A factory that finds a missing role, or a
build that needs to go over the cap to finish, could only stop and hope someone noticed.

TWO TYPED DECISIONS, NOTHING EXECUTES WITHOUT THE ANSWER:

* ``role.hire`` (``propose_hire``) — an agent drafts a new agent: name, purpose, skills, who it
  reports to, and why. The DRAFT lives on the decision; no role row exists until a person
  approves. Approving creates exactly the draft through agents.create_agent (the one door the
  console's quick create uses — the same key, skill and reporting-line checks), born
  ``{kind: blank}`` and sponsored by the approver; the agent that asked is recorded with it.
* ``budget.override`` (``request_override``) — an agent asks for ``extra_usd_micros`` over the
  project's caps for ONE build lineage (the run and its retries). Approving records a
  ``budget_overrides`` row; the stage-boundary cap check (build_budget.cap_reached with the
  run) lets that lineage spend up to cap + the override, and no other build. A build the cap
  already stopped is resumed (retried) once the approval is committed.

BOTH ARE BOUND to what the person saw (BOSSAPP-816's pattern): the draft / the run and amount
are digested and signed hs1 under the identity server key when the question is asked; a
decision whose payload changed since, or whose signature does not verify, refuses the approval
and nothing is created or raised.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import create_blocking_decision, emit_event, make_id

log = logging.getLogger(__name__)

HIRE = "role.hire"
OVERRIDE = "budget.override"
#: The most one override may add, in micro-dollars ($500).
MAX_OVERRIDE_USD_MICROS = 500_000_000
OVERRIDE_DAYS = 7
_LABEL = "agent-request"


class RequestError(ValueError):
    """A request BOSS refuses (or an approval whose payload no longer matches)."""


def _rec(decision: Mapping[str, Any]) -> dict[str, Any]:
    rec = decision.get("recommendation")
    if isinstance(rec, str):
        try:
            rec = json.loads(rec)
        except ValueError:
            rec = {}
    return rec if isinstance(rec, dict) else {}


def _digest(kind: str, payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps({"kind": kind, "payload": payload}, sort_keys=True,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def _sign(kind: str, digest: str) -> Optional[str]:
    try:
        from ..identity.authn_support import keyed_hash

        return "hs1:" + keyed_hash(_LABEL, f"{kind}:{digest}")
    except Exception:  # noqa: BLE001 — no key: unsigned, so it can never be approved
        return None


def _bound(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    digest = _digest(kind, payload)
    out = {"payload_sha256": digest, "binding_version": 2}
    sig = _sign(kind, digest)
    if sig:
        out["payload_sig"] = sig
    return out


def verify(decision: Mapping[str, Any], payload_key: str) -> dict[str, Any]:
    """The payload the person approved, verified against its digest and signature."""
    rec = _rec(decision)
    kind = str(rec.get("action") or "")
    payload = rec.get(payload_key)
    if not isinstance(payload, dict):
        raise RequestError("this request carries nothing to approve")
    digest = _digest(kind, payload)
    stored = rec.get("payload_sha256")
    if not isinstance(stored, str) or not hmac.compare_digest(stored, digest):
        raise RequestError("what this request asks for changed after it was asked, so nothing "
                           "was done; ask again")
    expected = _sign(kind, digest)
    given = rec.get("payload_sig")
    if expected is None or not isinstance(given, str) or not hmac.compare_digest(expected, given):
        raise RequestError("this request's signature does not verify, so nothing was done")
    return payload


def _role_of(actor: str) -> str:
    return actor.split(":", 1)[1] if actor.startswith("agent:") else actor


# ── hires ────────────────────────────────────────────────────────────────────────────────────

_KEY = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def propose_hire(connection: Connection, *, project_id: str, proposer: str, name: str,
                 purpose: str, reason: str, role_key: Optional[str] = None,
                 skills: Iterable[str] = (), reports_to: Optional[str] = None,
                 task_id: Optional[str] = None) -> dict[str, Any]:
    """Draft a new agent and ask a person. Creates nothing but the decision."""
    from .agents import _role, key_from_name

    name = str(name or "").strip()
    if not name:
        raise RequestError("name the agent you want to hire")
    if not str(reason or "").strip():
        raise RequestError("say why the factory needs this agent")
    key = str(role_key or "").strip().lower() or key_from_name(name)
    if not _KEY.match(key):
        raise RequestError(f"{key!r} cannot be an agent key: letters, digits and underscores, "
                           "starting with a letter")
    if _role(connection, project_id, key) is not None:
        raise RequestError(f"an agent {key!r} already exists on this project")
    if reports_to and _role(connection, project_id, str(reports_to)) is None:
        raise RequestError(f"no agent {reports_to!r} to report to on this project")
    pending = connection.execute(text(
        "SELECT id FROM decisions WHERE project_id = :p AND status IN ('pending', 'delegated') "
        "AND recommendation->>'action' = 'role.hire' "
        "AND recommendation->'draft'->>'role_key' = :k LIMIT 1"),
        {"p": project_id, "k": key}).scalar()
    if pending:
        return {"status": "already_asked", "decision_id": pending, "role_key": key}
    draft = {"name": name[:120], "role_key": key, "purpose": str(purpose or "").strip()[:2000],
             "skills": sorted({str(s).strip() for s in skills or [] if str(s).strip()})[:20],
             "reports_to": str(reports_to).strip() if reports_to else None}
    recommendation = {
        **_bound(HIRE, draft), "action": "role.hire", "asker": proposer, "draft": draft,
        "summary": f"{proposer} proposes hiring {name}: {str(reason).strip()[:600]}",
        "why": str(reason).strip()[:1000],
    }
    decision_id = create_blocking_decision(
        connection, project_id, task_id, f"Hire a new agent: {name}?"[:500], recommendation, {},
        "agent_proposed_a_hire", risk_tier=2, default_action="hold")
    emit_event(connection, project_id, "agent.hire_proposed", proposer, "decision", decision_id,
               {"role_key": key, "skills": draft["skills"], "reports_to": draft["reports_to"]})
    return {"status": "asked", "decision_id": decision_id, "role_key": key, "draft": draft}


def on_hire_approved(connection: Connection, decision: Mapping[str, Any], actor: str) -> dict:
    """Create exactly the draft (decisions' role.hire arm). Raises RequestError to refuse."""
    from .agents import create_agent

    draft = verify(decision, "draft")
    rec = _rec(decision)
    try:
        with connection.begin_nested():
            made = create_agent(connection, str(decision["project_id"]), actor=actor,
                                name=draft["name"], purpose=draft.get("purpose") or "",
                                role_key=draft["role_key"], skills=draft.get("skills") or [],
                                reports_to=draft.get("reports_to"), born_from={"kind": "blank"})
    except Exception as error:  # noqa: BLE001 — the agent door's own words, the approval refused
        raise RequestError(f"the agent could not be created: {error}") from error
    emit_event(connection, str(decision["project_id"]), "agent.hired", actor, "role",
               draft["role_key"], {"decision_id": decision["id"], "proposed_by": rec.get("asker")})
    return made


# ── budget overrides ────────────────────────────────────────────────────────────────────────

def lineage_root(connection: Connection, run_id: str) -> Optional[str]:
    row = connection.execute(text(
        "SELECT id, finalize_ctx->>'root_run_id' AS root FROM code_task_runs WHERE id = :r"),
        {"r": run_id}).mappings().first()
    if row is None:
        return None
    return str(row["root"] or row["id"])


def request_override(connection: Connection, *, project_id: str, asker: str, run_id: str,
                     extra_usd_micros: int, reason: str,
                     task_id: Optional[str] = None) -> dict[str, Any]:
    """Ask a person to let one build lineage spend `extra_usd_micros` over the caps."""
    run = connection.execute(text(
        "SELECT id, status, title FROM code_task_runs WHERE id = :r AND project_id = :p"),
        {"r": run_id, "p": project_id}).mappings().first()
    if run is None:
        raise LookupError(f"no build {run_id} in this project")
    extra = int(extra_usd_micros or 0)
    if extra <= 0 or extra > MAX_OVERRIDE_USD_MICROS:
        raise RequestError(f"ask for between $0.01 and ${MAX_OVERRIDE_USD_MICROS // 1_000_000} "
                           "over the cap")
    if not str(reason or "").strip():
        raise RequestError("say why this build needs to go over the cap")
    root = lineage_root(connection, run_id) or run_id
    ask = {"run_id": run_id, "root_run_id": root, "extra_usd_micros": extra}
    recommendation = {
        **_bound(OVERRIDE, ask), "action": "budget.override", "asker": asker, "override": ask,
        "summary": (f"{asker} asks to spend up to ${extra / 1_000_000:.2f} over the cap for "
                    f"“{run['title']}”: {str(reason).strip()[:600]}"),
        "why": str(reason).strip()[:1000],
    }
    decision_id = create_blocking_decision(
        connection, project_id, task_id,
        f"Let “{str(run['title'])[:120]}” spend ${extra / 1_000_000:.2f} over the budget cap?",
        recommendation, {}, "agent_asked_for_a_budget_override",
        risk_tier=2, default_action="hold")
    emit_event(connection, project_id, "budget.override_requested", asker, "code_task_run",
               run_id, {"decision_id": decision_id, "extra_usd_micros": extra})
    return {"status": "asked", "decision_id": decision_id, "root_run_id": root}


def on_override_approved(connection: Connection, decision: Mapping[str, Any], actor: str,
                         *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Record the override (decisions' budget.override arm). Returns whether a stopped build
    should be resumed once this commits."""
    ask = verify(decision, "override")
    now = now or datetime.now(timezone.utc)
    project_id = str(decision["project_id"])
    connection.execute(text(
        "INSERT INTO budget_overrides (id, project_id, root_run_id, run_id, extra_usd_micros, "
        " decision_id, approved_by, expires_at) VALUES (:i, :p, :root, :run, :x, :d, :a, :exp) "
        "ON CONFLICT (decision_id) DO NOTHING"),
        {"i": make_id("bov"), "p": project_id, "root": ask["root_run_id"], "run": ask["run_id"],
         "x": int(ask["extra_usd_micros"]), "d": decision["id"], "a": actor,
         "exp": now + timedelta(days=OVERRIDE_DAYS)})
    status = connection.execute(text(
        "SELECT status FROM code_task_runs WHERE id = :r"), {"r": ask["run_id"]}).scalar()
    resume = status in ("blocked", "failed") and stopped_by_budget(connection, ask["run_id"])
    emit_event(connection, project_id, "budget.override_approved", actor, "code_task_run",
               ask["run_id"], {"decision_id": decision["id"],
                               "extra_usd_micros": int(ask["extra_usd_micros"]),
                               "resume": resume})
    return {"run_id": ask["run_id"], "resume": resume}


def stopped_by_budget(connection: Connection, run_id: str) -> bool:
    return connection.execute(text(
        "SELECT 1 FROM events WHERE entity_type = 'code_task_run' AND entity_id = :r "
        "AND event_type = 'code_task.budget_stopped' LIMIT 1"), {"r": run_id}).first() is not None


def override_headroom(connection: Connection, project_id: str, run_id: Optional[str], *,
                      now: Optional[datetime] = None) -> int:
    """What approved overrides add to the caps for this run's lineage (0 for any other run)."""
    if not run_id:
        return 0
    root = lineage_root(connection, run_id) or run_id
    return int(connection.execute(text(
        "SELECT COALESCE(SUM(extra_usd_micros), 0) FROM budget_overrides WHERE project_id = :p "
        "AND root_run_id = :r AND status = 'active' AND expires_at > :now"),
        {"p": project_id, "r": root, "now": now or datetime.now(timezone.utc)}).scalar() or 0)


def resume_after_override(engine: Any, settings: Any, jobs: Any, *, decision_id: str,
                          run_id: str, wait_seconds: float = 15.0) -> Optional[dict]:
    """Retry the stopped build once the approval is committed (its own transactions). The
    retry is the person's (automatic=False): they approved this exact run going over."""
    import time

    from ..db import begin
    from .code_task_service import retry_code_task

    deadline = time.monotonic() + wait_seconds
    while True:
        with begin(engine) as c:
            approved = c.execute(text(
                "SELECT 1 FROM budget_overrides WHERE decision_id = :d"),
                {"d": decision_id}).first() is not None
        if approved:
            break
        if time.monotonic() > deadline:
            log.warning("budget override %s never committed; build %s not resumed",
                        decision_id, run_id)
            return None
        time.sleep(0.5)
    try:
        return retry_code_task(engine, settings, jobs, run_id,
                               fix_note="A person approved going over the budget cap for this "
                                        "build; continue from the kept work.")
    except Exception:  # noqa: BLE001 — the override stands; Retry in Code Tasks still works
        log.exception("build %s not resumed after budget override %s", run_id, decision_id)
        return None


__all__ = ["HIRE", "OVERRIDE", "RequestError", "on_hire_approved", "on_override_approved",
           "override_headroom", "propose_hire", "request_override", "resume_after_override",
           "verify"]
