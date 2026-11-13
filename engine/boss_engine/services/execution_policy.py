"""EXECUTION POLICY: review → approval stages that intercept "done" on ANY work (BOSSAPP-831).

A work item (and a run, whose own policy wins) may carry an ordered list of stages::

    {"stages": [
        {"kind": "review",   "participants": ["agent:qa_lead"], "comment_required": false,
         "max_rounds": 3},
        {"kind": "approval", "participants": ["person:ceo"],    "comment_required": true}
    ]}

THE STAGE MACHINE. Every completion path calls :func:`apply_transition` (through
work_items.complete_item / on_run_completed) instead of writing ``done``:

    done requested ──► no stages / all passed ──► done (and what it blocked wakes)
          │
          └──► in_review ── stage i raises a decision (the ordinary decisions queue: Needs you,
                            the decide tool for an agent seat) ──┐
                 approve ──► stage i+1, or done when it was the last ◄┘
                 reject / send back ──► back to the worker WITH the comment (status active,
                            round + 1); the worker's next done re-enters at stage i
                 the max_rounds-th rejection of a stage ──► escalated: the same stage is raised
                            to the item's responsible person, who settles it

Every raise, answer, return and escalation is a row in ``execution_decisions``.

The CODE pipeline's own hard gates (tests, review panel, CI, merge) are untouched: a build's
work item meets this policy only at the build's post-gate ``completed`` (run_terminal), i.e.
after every gate already passed. A merge or deploy is never decided here.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import create_blocking_decision, emit_event, make_id

log = logging.getLogger(__name__)

STAGE_KINDS = ("review", "approval")
DEFAULT_MAX_ROUNDS = 3
MAX_STAGES = 8
ACTION = "execution_stage"
_PARTICIPANT = re.compile(r"^(agent|person|role):[A-Za-z0-9_.@:+-]{1,200}$")


class PolicyError(ValueError):
    """A policy that cannot run (unknown stage kind, nobody to ask...)."""


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def normalize_policy(policy: Any) -> dict[str, Any]:
    """Validate and fill defaults. Accepts {"stages": [...]} or the bare list."""
    policy = _json(policy, policy)
    stages = policy.get("stages") if isinstance(policy, Mapping) else policy
    if not isinstance(stages, list):
        raise PolicyError("an execution policy is {\"stages\": [...]}")
    if len(stages) > MAX_STAGES:
        raise PolicyError(f"at most {MAX_STAGES} stages")
    out = []
    for i, stage in enumerate(stages):
        if not isinstance(stage, Mapping):
            raise PolicyError(f"stage {i + 1} must be an object")
        kind = str(stage.get("kind") or "").strip().lower()
        if kind not in STAGE_KINDS:
            raise PolicyError(f"stage {i + 1}: kind must be review or approval")
        people = stage.get("participants") or []
        if isinstance(people, str):
            people = [people]
        people = [str(p).strip() for p in people if str(p).strip()]
        if not people:
            raise PolicyError(f"stage {i + 1}: name at least one participant")
        bad = [p for p in people if not _PARTICIPANT.match(p)]
        if bad:
            raise PolicyError(f"stage {i + 1}: {bad[0]!r} is not agent:<role>, role:<key> or "
                              "person:<id>")
        try:
            rounds = int(stage.get("max_rounds") or DEFAULT_MAX_ROUNDS)
        except (TypeError, ValueError) as error:
            raise PolicyError(f"stage {i + 1}: max_rounds must be a number") from error
        out.append({"kind": kind, "participants": list(dict.fromkeys(people)),
                    "comment_required": bool(stage.get("comment_required")),
                    "max_rounds": max(1, min(rounds, 10))})
    return {"stages": out}


def effective_policy(connection: Connection, item: Mapping[str, Any],
                     origin: Optional[str]) -> Optional[dict[str, Any]]:
    """The run's own policy when the completion came from a run that has one, else the item's."""
    kind, _, run_id = str(origin or "").partition(":")
    table = {"workflow_run": "workflow_runs", "code_task": "code_task_runs"}.get(kind)
    if table and run_id:
        own = connection.execute(text(f"SELECT execution_policy FROM {table} WHERE id = :id"),
                                 {"id": run_id}).scalar()
        if own:
            return normalize_policy(own)
    if item.get("execution_policy"):
        return normalize_policy(item["execution_policy"])
    return None


def _log(connection: Connection, item: Mapping[str, Any], *, stage_index: int, stage_kind: str,
         round_: int, event: str, decision_id: Optional[str] = None, participants: Any = (),
         actor: str = "boss", comment: Optional[str] = None, origin: Optional[str] = None) -> None:
    connection.execute(text(
        "INSERT INTO execution_decisions (id, project_id, work_item_id, origin, stage_index, "
        "stage_kind, round, event, decision_id, participants, actor, comment) VALUES "
        "(:id, :p, :w, :o, :i, :k, :r, :e, :d, CAST(:pp AS JSONB), :a, :c)"),
        {"id": make_id("exd"), "p": item["project_id"], "w": item["id"], "o": origin,
         "i": stage_index, "k": stage_kind, "r": round_, "e": event, "d": decision_id,
         "pp": json.dumps(list(participants or [])), "a": actor,
         "c": (comment or "").strip()[:4000] or None})


def _save_state(connection: Connection, item_id: str, review: Optional[dict[str, Any]], *,
                status: Optional[str] = None) -> None:
    connection.execute(text(
        "UPDATE work_items SET execution_state = CASE WHEN CAST(:r AS JSONB) IS NULL "
        "  THEN COALESCE(execution_state, '{}'::jsonb) - 'review' "
        "  ELSE COALESCE(execution_state, '{}'::jsonb) || jsonb_build_object('review', CAST(:r AS JSONB)) END, "
        "status = COALESCE(:s, status), updated_at = now() WHERE id = :id"),
        {"r": json.dumps(review) if review is not None else None, "s": status, "id": item_id})


def _raise_stage(connection: Connection, item: Mapping[str, Any], stage: Mapping[str, Any], *,
                 index: int, round_: int, origin: Optional[str], escalated: bool = False,
                 feedback: Optional[str] = None) -> str:
    participants = list(stage["participants"])
    if escalated:
        participants = [item["responsible_person"]] if item.get("responsible_person") else participants
    verb = "Review" if stage["kind"] == "review" else "Approve"
    key = f"{item['human_key']} " if item.get("human_key") else ""
    question = f"{verb}: {key}{item['title']}"[:280]
    if escalated:
        question += " (escalated)"
    elif round_ > 1:
        question += f" (round {round_})"
    agents = [p.split(":", 1)[1] for p in participants if p.startswith(("agent:", "role:"))]
    recommendation = {
        "action": ACTION, "work_item_id": item["id"], "stage_index": index,
        "stage_kind": stage["kind"], "round": round_, "max_rounds": stage["max_rounds"],
        "participants": participants, "comment_required": stage["comment_required"],
        "escalated": escalated, "origin": origin, "title": item["title"],
        **({"owner_role_key": agents[0]} if agents else {}),
        **({"previous_feedback": feedback} if feedback else {}),
        "summary": (f"{verb} {item['title']!r} before it counts as done"
                    + (f" — escalated after {stage['max_rounds']} rounds" if escalated else "")),
    }
    decision_id = create_blocking_decision(
        connection, item["project_id"], None, question, recommendation, {},
        "execution_policy_stage", risk_tier=2 if stage["kind"] == "approval" else 1,
        default_action="hold")
    _log(connection, item, stage_index=index, stage_kind=stage["kind"], round_=round_,
         event="escalated" if escalated else "raised", decision_id=decision_id,
         participants=participants, origin=origin)
    emit_event(connection, item["project_id"], "work_item.stage_raised", "boss", "work_item",
               item["id"], {"stage": index, "kind": stage["kind"], "round": round_,
                            "decision_id": decision_id, "escalated": escalated})
    return decision_id


def apply_transition(connection: Connection, settings: Any = None, *, project_id: str,
                     work_item_id: str, actor: str, origin: Optional[str] = None) -> dict[str, Any]:
    """A worker says the item is done. Returns {"status": "done"} when nothing stands in the
    way, else {"status": "in_review", "decision_id", "stage"} (the item is now in review)."""
    item = connection.execute(text(
        "SELECT * FROM work_items WHERE id = :id AND project_id = :p FOR UPDATE"),
        {"id": work_item_id, "p": project_id}).mappings().first()
    if item is None:
        raise LookupError(f"no work item {work_item_id} in this project")
    item = dict(item)
    policy = effective_policy(connection, item, origin)
    stages = (policy or {}).get("stages") or []
    state = _json(item.get("execution_state"), {}) or {}
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    if not stages:
        return {"status": "done", "stages": 0}
    if item["status"] == "in_review" and review.get("decision_id"):
        # Already waiting on a stage: a second "done" from the same worker changes nothing.
        return {"status": "in_review", "decision_id": review["decision_id"],
                "stage": review.get("stage", 0)}
    index = int(review.get("stage") or 0)
    if index >= len(stages):
        index = 0
    round_ = int(review.get("round") or 1)
    escalated = bool(review.get("escalated"))
    decision_id = _raise_stage(connection, item, stages[index], index=index, round_=round_,
                               origin=origin or review.get("origin"), escalated=escalated,
                               feedback=review.get("feedback"))
    _save_state(connection, work_item_id, {
        "stage": index, "round": round_, "decision_id": decision_id, "escalated": escalated,
        "origin": origin or review.get("origin"),
        "worker": review.get("worker") or item.get("assignee") or actor,
    }, status="in_review")
    return {"status": "in_review", "decision_id": decision_id, "stage": index,
            "round": round_, "stages": len(stages)}


def on_stage_answered(connection: Connection, settings: Any, decision: Mapping[str, Any], *,
                      verdict: str, actor: str, note: Optional[str]) -> Optional[dict[str, Any]]:
    """A stage's decision was answered (DecisionService follow-on). approve → next stage or
    done; reject / revise → back to the worker with the comment, or escalate at max_rounds.

    Raises PolicyError when the stage needs a comment and none was given (the transition is
    then refused and nothing is recorded)."""
    from . import work_items

    rec = _json(decision.get("recommendation"), {}) or {}
    item_id = rec.get("work_item_id")
    item = connection.execute(text("SELECT * FROM work_items WHERE id = :id FOR UPDATE"),
                              {"id": item_id}).mappings().first()
    if item is None:
        return None
    item = dict(item)
    state = _json(item.get("execution_state"), {}) or {}
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    if review.get("decision_id") != decision["id"]:
        return None  # a stale stage (the policy moved on); its answer changes nothing
    comment = (note or "").strip()
    if (rec.get("comment_required") or verdict != "approve") and not comment:
        raise PolicyError("this stage needs a comment: say what you checked, or what to change")
    policy = effective_policy(connection, item, review.get("origin")) or {"stages": []}
    stages = policy["stages"]
    index = int(review.get("stage") or 0)
    stage = stages[index] if index < len(stages) else {
        "kind": rec.get("stage_kind") or "review", "participants": rec.get("participants") or [],
        "comment_required": False, "max_rounds": DEFAULT_MAX_ROUNDS}
    round_ = int(review.get("round") or 1)
    origin = review.get("origin")
    if verdict == "approve":
        _log(connection, item, stage_index=index, stage_kind=stage["kind"], round_=round_,
             event="approved", decision_id=decision["id"], actor=actor, comment=comment,
             origin=origin)
        if index + 1 < len(stages):
            nxt = index + 1
            decision_id = _raise_stage(connection, item, stages[nxt], index=nxt, round_=1,
                                       origin=origin)
            _save_state(connection, item["id"], {**review, "stage": nxt, "round": 1,
                                                 "decision_id": decision_id, "escalated": False,
                                                 "feedback": None})
            return {"status": "in_review", "stage": nxt, "decision_id": decision_id}
        _log(connection, item, stage_index=index, stage_kind=stage["kind"], round_=round_,
             event="completed", actor=actor, origin=origin)
        _save_state(connection, item["id"], None)
        woke = work_items.mark_done(connection, settings, project_id=item["project_id"],
                                    item_id=item["id"], actor=actor)
        return {"status": "done", "woke": woke}
    # Rejected or sent back.
    _log(connection, item, stage_index=index, stage_kind=stage["kind"], round_=round_,
         event="rejected", decision_id=decision["id"], actor=actor, comment=comment,
         origin=origin)
    if not review.get("escalated") and round_ >= int(stage["max_rounds"]):
        decision_id = _raise_stage(connection, item, stage, index=index, round_=round_,
                                   origin=origin, escalated=True, feedback=comment)
        _save_state(connection, item["id"], {**review, "decision_id": decision_id,
                                             "escalated": True, "feedback": comment})
        return {"status": "in_review", "stage": index, "escalated": True,
                "decision_id": decision_id}
    _save_state(connection, item["id"], {
        **review, "decision_id": None, "round": round_ + (0 if review.get("escalated") else 1),
        "feedback": comment, "returned_by": actor}, status="active")
    emit_event(connection, item["project_id"], "work_item.returned", actor, "work_item",
               item["id"], {"stage": index, "round": round_, "comment": comment[:500],
                            "worker": review.get("worker")})
    _return_to_worker(connection, settings, item, review, comment, actor)
    return {"status": "active", "returned": True, "round": round_ + 1}


def _return_to_worker(connection: Connection, settings: Any, item: Mapping[str, Any],
                      review: Mapping[str, Any], comment: str, actor: str) -> None:
    """The worker hears it: the comment is posted on the item's own record (a `critique` on the
    work board, which the item drawer and the agents' board both read) and stays on
    execution_state.review.feedback; the item is `active` again, so the worker's next done
    re-enters the stage that sent it back."""
    try:
        with connection.begin_nested():
            connection.execute(text(
                "INSERT INTO work_item_messages (id, project_id, work_item_id, kind, sender, body) "
                "VALUES (:id, :p, :w, 'critique', :s, CAST(:b AS JSONB))"),
                {"id": make_id("wim"), "p": item["project_id"], "w": item["id"],
                 "s": actor if ":" in actor else f"person:{actor}",
                 "b": json.dumps({"stage": review.get("stage", 0), "round": review.get("round", 1),
                                  "comment": comment, "worker": review.get("worker")})})
    except Exception:  # noqa: BLE001 — the item already says it was sent back
        log.warning("review comment not posted on %s", item["id"], exc_info=True)


__all__ = ["normalize_policy", "effective_policy", "apply_transition", "on_stage_answered",
           "PolicyError", "ACTION", "STAGE_KINDS", "DEFAULT_MAX_ROUNDS"]
