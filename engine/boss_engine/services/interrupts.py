"""INTERRUPTS ARE DECISIONS (AP-P2, BOSSAPP-1053; kernel/interrupts.py is the pure half).

Every place BOSS stops to wait on a person opens its wait HERE, and every resume finds what it
waits on HERE, by the interrupt's stable id:

  open_interrupt(...)       a Decision carrying the interrupt (decisions.interrupt_id, and the
                            id, kind, binding and response schema in its recommendation). The
                            one door for: a kernel run's held tool call (DecisionInterrupts, the
                            run's interrupt port), agent_loop's held calls and ask_human, the
                            code-task plan gate, and person_wait's blocker card.
  answer_for(...)           the answer a resume brings for one interrupt id: the decision's
                            verdict, its typed answer, and the binding the approval was given
                            for — resumed by id, never by the order things were asked.
  binding_refusal(...)      why a resume may not use this answer: not answered, rejected, or the
                            thing it approved has changed since (a plan rewritten after sign-off,
                            a tool call whose arguments moved) — "" when it may.
  on_answered(...)          the follow-on of an interrupt decision (decisions.py): a kernel run
                            waiting on several interrupts resumes ONCE, when the last of them is
                            answered, with every answer — one decision batch.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..kernel.interrupts import (APPROVAL, PERSON, PLAN, QUESTION, UNKNOWN_OUTCOME, Interrupt,
                                 content_hash, interrupt_id, pending_interrupts)
from .core import create_blocking_decision, emit_event

#: The decision action of a kernel run's interrupt: answering it may resume the run.
ACTION = "resume_interrupt"


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def open_interrupt(connection: Connection, *, project_id: str, task_id: Optional[str],
                   item: Interrupt, question: str, recommendation: dict,
                   reason: str, risk_tier: int = 2, cost_summary: Optional[dict] = None,
                   default_action: str = "hold", create: Any = None) -> str:
    """THE ONE DOOR a wait on a person goes through: a Decision that carries the interrupt —
    its stable id (also on decisions.interrupt_id, indexed, so a resume finds it by id), its
    kind, what it is bound to and the answer's schema (written into `recommendation`, which the
    caller passes fresh). `create` is a caller's own create_blocking_decision. Returns the
    decision id."""
    recommendation.update({"interrupt_id": item.id, "interrupt_kind": item.kind})
    if item.binding:
        # The approval binds to THIS: the decision card shows it and a resume re-checks it.
        for key, value in item.binding.items():
            recommendation.setdefault(key, value)
        recommendation["interrupt_binding"] = dict(item.binding)
    if item.schema:
        recommendation["interrupt_schema"] = dict(item.schema)
    if create is not None:
        decision_id = create(connection, project_id, task_id, question[:500], recommendation,
                             cost_summary or {}, reason, risk_tier=risk_tier,
                             default_action=default_action)
    else:
        decision_id = create_blocking_decision(
            connection, project_id, task_id, question[:500], recommendation,
            cost_summary or {}, reason, risk_tier=risk_tier, default_action=default_action)
    if _on_postgres(connection):
        connection.execute(text("UPDATE decisions SET interrupt_id = :i WHERE id = :d"),
                           {"i": item.id, "d": decision_id})
        emit_event(connection, project_id, "interrupt.opened", "boss", "decision", decision_id,
                   {"interrupt_id": item.id, "kind": item.kind, "reason": reason})
    return decision_id


def _on_postgres(connection: Any) -> bool:
    """A real database connection (the recommendation carries the id either way; a caller's
    in-memory stand-in has no decisions table to index it in)."""
    dialect = getattr(getattr(connection, "engine", None), "dialect", None) \
        or getattr(connection, "dialect", None)
    return getattr(dialect, "name", "") == "postgresql"


def find(connection: Connection, interrupt: str) -> Optional[dict]:
    """The decision answering interrupt `interrupt` (the newest, if it was asked again)."""
    row = connection.execute(text(
        "SELECT id, project_id, task_id, status, recommendation, answer, answered_by, "
        "approved_by, resolution_note, question, interrupt_id FROM decisions "
        "WHERE interrupt_id = :i ORDER BY created_at DESC LIMIT 1"), {"i": interrupt}
    ).mappings().first()
    if row is None:
        return None
    out = dict(row)
    out["recommendation"] = _json(out.get("recommendation"), {}) or {}
    out["answer"] = _json(out.get("answer"), None)
    return out


def answer_for(connection: Connection, interrupt: str) -> Optional[dict]:
    """The answer a resume brings for interrupt `interrupt`, in the kernel's shape — or None while
    nobody has answered. An approval carries the binding it was given for (the recommendation's
    args digest and signature), so the kernel's `approval_refusal` checks it against the call."""
    row = find(connection, interrupt)
    if row is None or row["status"] not in ("approved", "rejected", "executed"):
        return None
    rec = row["recommendation"]
    kind = rec.get("interrupt_kind") or APPROVAL
    base = {"decision_id": row["id"], "interrupt_id": interrupt}
    if row["status"] == "rejected":
        return {**base, "rejected": True, "reason": row.get("resolution_note") or ""}
    if kind == QUESTION:
        return {**base, "answered": True, "answer": row.get("answer") or {}}
    if kind == UNKNOWN_OUTCOME:
        answer = row.get("answer") or {}
        return {**base, "happened": bool(answer.get("happened")),
                "result": answer.get("result")}
    return {**{k: v for k, v in rec.items() if k not in ("action",)}, **base, "approved": True}


def answers_for_run(connection: Connection, pending: Optional[Mapping[str, Any]]) -> dict:
    """Every answer a suspended run's interrupts have, by id (the unanswered ones absent)."""
    out: dict = {}
    for item in pending_interrupts(pending):
        iid = str(item.get("id") or "")
        answer = answer_for(connection, iid) if iid else None
        if answer is None and item.get("decision_id"):
            # An interrupt raised before its decision carried the id: found by its decision.
            answer = _answer_by_decision(connection, str(item["decision_id"]), item)
        if answer is not None:
            out[iid] = answer
    return out


def _answer_by_decision(connection: Connection, decision_id: str, item: dict) -> Optional[dict]:
    row = connection.execute(text(
        "SELECT status, recommendation FROM decisions WHERE id = :d"), {"d": decision_id}
    ).mappings().first()
    if row is None or row["status"] not in ("approved", "rejected", "executed"):
        return None
    rec = _json(row["recommendation"], {}) or {}
    if row["status"] == "rejected":
        return {"decision_id": decision_id, "rejected": True}
    return {**{k: v for k, v in rec.items() if k != "action"}, "decision_id": decision_id,
            "approved": True}


def binding_refusal(connection: Connection, interrupt: str, *, binding: Mapping[str, Any]) -> str:
    """Why the answer to `interrupt` may not release what waits on it, or "". The answer must be
    an approval, and every key of `binding` (what the waiting party holds NOW: a plan's digest,
    a blocker's) must equal what the approval was given for."""
    row = find(connection, interrupt)
    if row is None:
        return "no decision answers this interrupt"
    if row["status"] not in ("approved", "executed"):
        return f"the decision is {row['status']}, not approved"
    given = dict(row["recommendation"].get("interrupt_binding") or {})
    for key, value in binding.items():
        if given.get(key) != value:
            return f"what was approved has changed since ({key})"
    return ""


def on_answered(connection: Connection, settings: Any, decision: Mapping[str, Any], *,
                verdict: str, actor: str) -> dict:
    """An interrupt decision was answered (decisions.py follow-on, approve or reject). A kernel
    run resumes when EVERY interrupt it waits on has an answer: one `agent.resume` job for the
    whole batch, queued by whichever answer came last."""
    rec = _json(decision.get("recommendation"), {}) or {}
    run_id = str(rec.get("run_id") or "")
    if not run_id:
        return {"resumes": None}
    pending = _run_pending(connection, run_id)
    waiting = pending_interrupts(pending)
    answered = answers_for_run(connection, pending)
    left = [i for i in waiting if str(i.get("id") or "") not in answered]
    emit_event(connection, decision["project_id"], "interrupt.answered", actor, "decision",
               decision["id"], {"interrupt_id": rec.get("interrupt_id"), "verdict": verdict,
                                "run_id": run_id, "still_waiting": len(left)})
    if left:
        return {"resumes": None, "waiting_on": [i.get("id") for i in left]}
    from .typed_forms import queue_agent_resume

    queue_agent_resume(connection, project_id=str(decision["project_id"]), run_id=run_id,
                       actor=actor, task_id=decision.get("task_id"),
                       decision_id=str(decision["id"]))
    return {"resumes": run_id}


def _run_pending(connection: Connection, run_id: str) -> Optional[dict]:
    row = connection.execute(text(
        "SELECT pending, finalize_ctx FROM agent_runs WHERE id = :r"), {"r": run_id}
    ).mappings().first()
    if row is not None and row["pending"]:
        return _json(row["pending"], None)
    checkpoint = connection.execute(text(
        "SELECT state->'pending' FROM agent_checkpoints WHERE run_id = :r "
        "ORDER BY seq DESC LIMIT 1"), {"r": run_id}).scalar()
    return _json(checkpoint, None)


class DecisionInterrupts:
    """A kernel run's interrupt port (RunSpec.interrupts): each interrupt a tool raises becomes
    a Decision through `open_interrupt`, on the run's own connection."""

    def __init__(self, connection: Connection, *, project_id: str,
                 task_id: Optional[str] = None, risk_tier: int = 2):
        self.connection = connection
        self.project_id = project_id
        self.task_id = task_id
        self.risk_tier = risk_tier

    def open(self, state: Any, item: Interrupt) -> str:
        payload = dict(item.payload or {})
        question = str(payload.get("question") or payload.get("summary")
                       or f"Approve '{item.tool}'?")
        recommendation = {"action": ACTION, "run_id": state.run_id, "tool": item.tool,
                          "args": item.args or {}, "summary": payload.get("summary") or question}
        decision_id = open_interrupt(
            self.connection, project_id=self.project_id, task_id=self.task_id, item=item,
            question=question, recommendation=recommendation,
            reason=f"agent_run_interrupt:{item.kind}",
            risk_tier=1 if item.kind == QUESTION else self.risk_tier)
        if item.schema and item.kind in (QUESTION, UNKNOWN_OUTCOME):
            connection = self.connection
            connection.execute(text(
                "UPDATE decisions SET form_schema = CAST(:f AS JSONB) WHERE id = :d"),
                {"f": json.dumps(item.schema), "d": decision_id})
        return decision_id


# ── The bespoke waits, on the one primitive ───────────────────────────────────────────────────

def plan_interrupt(run_id: str, design: Mapping[str, Any]) -> Interrupt:
    """The code-task plan gate: one interrupt per (build, plan), bound to the plan's text, so an
    approval of one plan can never release a different one (a redesign after sign-off)."""
    plan = {"plan": str(design.get("plan") or ""), "plan_ok": bool(design.get("plan_ok", True))}
    digest = content_hash(plan)
    return Interrupt(id=interrupt_id("code_task_plan", run_id, digest[:16]), kind=PLAN,
                     payload={"summary": "approve the build's plan before code is written"},
                     binding={"plan_sha256": digest})


def person_interrupt(project_id: str, agent_key: str, action: str, blocker: str) -> Interrupt:
    """person_wait: ONE card per (project, builder, blocker) — every run waiting on the same
    thing joins it — so one interrupt id per card, bound to the blocker it is about."""
    return Interrupt(id=interrupt_id("person_wait", project_id, agent_key, action), kind=PERSON,
                     payload={"summary": f"a person must clear {blocker}"},
                     binding={"blocker": blocker, "action": action})


def question_interrupt(run_id: Optional[str], question: str, schema: Mapping[str, Any], *,
                       step: int = 0, call_id: str = "") -> Interrupt:
    """ask_human: a typed question, bound to the form it asked (an answer to another form of the
    same words is refused)."""
    return Interrupt(id=(interrupt_id(run_id, int(step), call_id) if run_id and call_id
                         else interrupt_id("ask_human", run_id or "", question,
                                           content_hash(schema)[:16])),
                     kind=QUESTION, payload={"question": question}, schema=dict(schema),
                     binding={"form_sha256": content_hash(schema)})


__all__ = ["ACTION", "DecisionInterrupts", "answer_for", "answers_for_run", "binding_refusal",
           "find", "on_answered", "open_interrupt", "person_interrupt", "plan_interrupt",
           "question_interrupt"]
