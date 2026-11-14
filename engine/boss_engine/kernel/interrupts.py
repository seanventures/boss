"""ONE INTERRUPT PRIMITIVE: a run waits on a person, by a stable id, bound to what was asked
(AP-P2, docs/design/AGENT_PLATFORM.md §4.3 "Interrupts become one primitive").

BOSS had four bespoke ways to stop and wait — agent_loop's held tool call (`resume_agent_run`),
the code-task plan gate (`resume_code_task`), `person_wait`'s blocker card and `ask_human`'s
typed form — each with its own pending shape and its own resume. They are one thing:

    interrupt(id, schema, binding)

  id        STABLE, derived from what waits: (run, step, tool call) for a run, (code task run,
            gate) for a build. The answer is stored against the id and a resume finds its waiting
            party by the id — never by the order interrupts were raised or answered. LangGraph
            matches resume values to interrupts by call ORDER ("never reorder interrupts"); this
            removes that footgun: two tools that interrupt in one step resume in one decision
            batch whichever answer arrives first.
  schema    what the answer must look like: None for approve/reject, a typed form
            (services/typed_forms.py field list) for a question.
  binding   what the approval is FOR: a content hash (the tool and its exact arguments; a plan's
            text; a blocker) that must still hold at resume, or the resume is refused — an
            argument changed after a person approved it is never executed on that approval
            (services/argument_binding.py, the same digest the decisions carry).

Every interrupt is a Decision (services/interrupts.py opens it; DecisionService.transition answers
it: two-person approval, step-up, the typed answer), so a resume always passes the decision's own
governance. This module is the pure half: ids, the pending shape a run checkpoints, and the
batch rule that matches answers to interrupts by id.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

#: An approval of an action (a held tool call, a plan): approve or reject, hash-bound.
APPROVAL = "approval"
#: A typed question (ask_human): the answer is data validated against `schema`.
QUESTION = "question"
#: A call that may or may not have happened (an irreversible tool that started and left no
#: result when its worker died): a person says what happened; it is never fired twice.
UNKNOWN_OUTCOME = "unknown_outcome"
#: A person must clear a blocker (person_wait): resumed once the blocker is verified cleared.
PERSON = "person"
#: A build's plan a person signs off before code is written (the code-task plan gate).
PLAN = "plan"

KINDS = frozenset({APPROVAL, QUESTION, UNKNOWN_OUTCOME, PERSON, PLAN})


def interrupt_id(*parts: Any) -> str:
    """The stable id of what waits: the same parts always give the same id."""
    raw = ":".join(str(p) for p in parts)
    return "int_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def call_interrupt_id(run_id: str, step: int, call_id: str) -> str:
    """A run's interrupt: (run, step, tool call)."""
    return interrupt_id(run_id, int(step), call_id)


def content_hash(value: Any) -> str:
    """The digest a binding carries for content that is not a tool call (a plan, a blocker)."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


@dataclass
class Interrupt:
    id: str
    kind: str
    payload: dict = field(default_factory=dict)
    schema: Optional[dict] = None
    binding: Optional[dict] = None
    decision_id: Optional[str] = None
    #: The held tool call, for a run's interrupt (tool, args, call_id, step).
    tool: Optional[str] = None
    args: Optional[dict] = None
    call_id: Optional[str] = None
    step: int = 0

    def as_pending(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def interrupt(kind: str, payload: Optional[Mapping[str, Any]] = None, *,
              schema: Optional[Mapping[str, Any]] = None,
              binding: Optional[Mapping[str, Any]] = None,
              id: Optional[str] = None) -> dict:
    """What a TOOL returns to make its run wait on a person. The kernel's mandatory `authz`
    middleware turns it into a hash-bound Decision (through the run's interrupt port) before the
    run suspends; the id is the call's own stable id unless the tool names one.

        return interrupt("approval", {"summary": "send the invoice"})
        return interrupt("question", {"question": "Which database?"}, schema={"fields": [...]})
    """
    if kind not in KINDS:
        raise ValueError(f"interrupt kind {kind!r} is not one of {sorted(KINDS)}")
    return {"status": "interrupt", "interrupt": {
        "kind": kind, "payload": dict(payload or {}), "schema": dict(schema) if schema else None,
        "binding": dict(binding) if binding else None, "id": id}}


def is_interrupt(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("status") == "interrupt" \
        and isinstance(value.get("interrupt"), Mapping)


# ── the pending shape a run checkpoints ───────────────────────────────────────────────────────

def pending_interrupts(pending: Optional[Mapping[str, Any]]) -> list:
    """Every interrupt a suspended run waits on. A pending from before AP-P2 (one held call, no
    `interrupts` list) is read as its one interrupt."""
    pending = dict(pending or {})
    listed = pending.get("interrupts")
    if isinstance(listed, list) and listed:
        return [dict(i) for i in listed if isinstance(i, Mapping)]
    if not pending.get("decision_id") and not pending.get("interrupt_id"):
        return []
    return [{"id": pending.get("interrupt_id") or "", "kind":
             QUESTION if pending.get("question") else APPROVAL,
             "decision_id": pending.get("decision_id"), "tool": pending.get("tool"),
             "args": pending.get("args"), "call_id": pending.get("call_id"),
             "step": int(pending.get("step") or 0),
             "binding": {"args_sha256": pending["args_sha256"]}
             if pending.get("args_sha256") else None}]


def answers_by_id(pending: Optional[Mapping[str, Any]], *, decision: Optional[dict] = None,
                  answers: Optional[Mapping[str, Any]] = None) -> dict:
    """The answers a resume brings, keyed by interrupt id. `answers` is already {id: answer};
    a single `decision` (the pre-AP-P2 resume) is matched to the interrupt that waits on ITS
    decision id — by id, never by position."""
    out = {str(k): dict(v) for k, v in (answers or {}).items() if isinstance(v, Mapping)}
    if decision:
        for item in pending_interrupts(pending):
            if str(item.get("decision_id") or "") == str(decision.get("decision_id") or "") \
                    or (decision.get("interrupt_id") and item.get("id") == decision.get(
                        "interrupt_id")):
                out.setdefault(str(item.get("id") or ""), dict(decision))
    return out


def batch_refusal(pending: Optional[Mapping[str, Any]], answers: Mapping[str, Any]) -> str:
    """Why this batch of answers does not release the run, or "" when it does. EVERY interrupt
    must be answered (a run resumes once, with all of them), each answer must be to ITS decision,
    and an approval must still match its binding — checked for all before anything runs, so a
    batch is released whole or not at all."""
    from .governance import approval_refusal

    waiting = pending_interrupts(pending)
    if not waiting:
        return "this run is not waiting on anything a person answers"
    unanswered = [i for i in waiting if str(i.get("id") or "") not in answers]
    if unanswered:
        return (f"{len(unanswered)} of {len(waiting)} interrupt(s) this run waits on "
                "are not answered yet")
    for item in waiting:
        answer = answers[str(item.get("id") or "")]
        if str(answer.get("decision_id") or "") != str(item.get("decision_id") or ""):
            return "that decision is not the one this run waits on"
        if answer.get("rejected") or answer.get("answered"):
            continue  # a person declined it, or answered a question: nothing is executed
        if item.get("kind") == UNKNOWN_OUTCOME:
            if "happened" not in answer:
                return "say whether the call happened"
            continue
        held = {"tool": item.get("tool"), "args": item.get("args"),
                "decision_id": item.get("decision_id"),
                **({"args_sha256": (item.get("binding") or {}).get("args_sha256")}
                   if (item.get("binding") or {}).get("args_sha256") else {})}
        refusal = approval_refusal(held, answer)
        if refusal:
            return refusal
    return ""


class MemoryInterrupts:
    """The interrupt port in memory (tests, scripted runs): each interrupt opened gets a decision
    id; `opened` lists them in the order they were raised."""

    def __init__(self) -> None:
        self.opened: list = []

    def open(self, state: Any, item: Interrupt) -> str:
        decision_id = f"dec_{item.id[4:16]}"
        self.opened.append({**item.as_pending(), "decision_id": decision_id,
                            "run_id": getattr(state, "run_id", None)})
        return decision_id


__all__ = ["APPROVAL", "Interrupt", "MemoryInterrupts", "KINDS", "PERSON", "PLAN", "QUESTION", "UNKNOWN_OUTCOME",
           "answers_by_id", "batch_refusal", "call_interrupt_id", "content_hash", "interrupt",
           "interrupt_id", "is_interrupt", "pending_interrupts"]
