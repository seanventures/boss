"""AN INTERRUPT ASKED OVER A PROTOCOL, ANSWERED UNDER BOSS'S OWN GOVERNANCE (AP-P5, BOSSAPP-1056;
AGENT_PLATFORM §4.5 "Approvals become `input_required`").

A run that waits on a person (P2: every interrupt is a Decision) is shown to an MCP client as
`input_required` and to an A2A client as `TASK_STATE_INPUT_REQUIRED`. Both protocols let the
client send the answer back in-band (MCP `inputResponses` / `tasks/update`, an A2A message on the
task). The answer is NEVER trusted because it arrived on the right task: it is applied only as
the decision's own transition, by the verified caller of that request:

  1. a verified identity is required (no answer from nobody);
  2. the PDP decides `decision:approve` at the decision's scope — which includes STEP-UP: a
     bearer (a PAT, a service-account key, a run token) cannot step up, so it is refused with
     `step_up_required` and the client is pointed at the console (a URL-mode elicitation /
     the task's status message) where the person approves with a fresh second factor;
  3. DecisionService.transition then applies the seat, human-session and two-person rules
     (identity/approvals.py) exactly as the Decision API does.

Approval-delegate keys are deliberately NOT accepted here: their authority is the delegate
routes only (identity/approval_delegations.DELEGATE_ROUTES — "not MCP"). A delegate approves
through POST /v1/decisions/{id}/approve and the protocol client then resumes.
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping, Optional

from sqlalchemy import text

#: The verdicts an approval-shaped interrupt accepts in-band.
VERDICTS = ("approve", "reject")


class AnswerRefused(Exception):
    """The caller may not answer this interrupt. `code` is a short machine reason."""

    def __init__(self, code: str, message: str, *, decision_id: Optional[str] = None,
                 status: int = 403):
        super().__init__(message)
        self.code, self.message, self.decision_id, self.status = code, message, decision_id, status

    def body(self) -> dict:
        out = {"error": self.code, "message": self.message}
        if self.decision_id:
            out["decision_id"] = self.decision_id
            out["approve_url"] = console_url(self.decision_id)
        return out


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def console_url(decision_id: str) -> str:
    """Where a person approves with step-up (the console's decision card)."""
    base = (os.environ.get("BOSS_PUBLIC_URL") or os.environ.get("BOSS_CONSOLE_URL")
            or os.environ.get("BOSS_MCP_OAUTH_ISSUER") or "").rstrip("/")
    return f"{base}/decisions/{decision_id}" if base else f"/decisions/{decision_id}"


def pending_for(connection: Any, run_id: str) -> list:
    """The open interrupts of a run (an agent run or a code task run): its pending decisions,
    oldest first. Each is what a protocol shows as one input request."""
    rows = connection.execute(text(
        "SELECT id, project_id, interrupt_id, question, risk_tier, recommendation "
        "FROM decisions WHERE recommendation->>'run_id' = :r AND status = 'pending' "
        "ORDER BY created_at, id"), {"r": run_id}).mappings().all()
    out = []
    for r in rows:
        rec = _json(r["recommendation"], {}) or {}
        out.append({"decision_id": r["id"], "interrupt_id": r["interrupt_id"],
                    "question": r["question"], "risk_tier": int(r["risk_tier"] or 0),
                    "kind": rec.get("interrupt_kind") or "approval",
                    "schema": rec.get("response_schema") or rec.get("schema")})
    return out


def input_key(decision_id: str) -> str:
    """The inputRequests / inputResponses key of one interrupt (server-assigned, stable)."""
    return f"decision:{decision_id}"


def decision_of_key(key: str) -> Optional[str]:
    return key[len("decision:"):] if isinstance(key, str) and key.startswith("decision:") else None


def form_elicitation(item: Mapping[str, Any]) -> dict:
    """An MCP `elicitation/create` (form mode) asking for one interrupt's answer."""
    props: dict = {"verdict": {"type": "string", "enum": list(VERDICTS),
                               "description": "approve or reject"},
                   "note": {"type": "string", "description": "why (recorded on the decision)"}}
    if item.get("kind") == "question" and isinstance(item.get("schema"), Mapping):
        props["answer"] = {"type": "object", "description": "the typed answer"}
    return {"method": "elicitation/create", "params": {
        "mode": "form", "message": str(item.get("question") or "BOSS needs a decision"),
        "requestedSchema": {"type": "object", "properties": props, "required": ["verdict"]}}}


def url_elicitation(item: Mapping[str, Any], *, reason: str = "") -> dict:
    """An MCP `elicitation/create` (URL mode): approve in BOSS, where step-up happens."""
    did = str(item.get("decision_id"))
    message = str(item.get("question") or "BOSS needs a decision")
    if reason:
        message = f"{message} ({reason}: approve it in BOSS with a fresh sign-in check)"
    return {"method": "elicitation/create", "params": {"mode": "url", "message": message,
                                                       "url": console_url(did)}}


def parse_response(value: Any) -> Optional[dict]:
    """An MCP ElicitResult ({action, content}) or a bare {verdict, note} → {verdict, note,
    answer}, or None when the person declined / cancelled / said nothing usable."""
    if not isinstance(value, Mapping):
        return None
    if "action" in value:
        if value.get("action") != "accept":
            return {"verdict": "reject", "note": f"declined over MCP ({value.get('action')})",
                    "answer": None} if value.get("action") == "decline" else None
        value = value.get("content") or {}
        if not isinstance(value, Mapping):
            return None
    verdict = str(value.get("verdict") or ("approve" if value.get("approved") is True else
                                           "reject" if value.get("approved") is False else ""))
    if verdict not in VERDICTS:
        return None
    answer = value.get("answer")
    return {"verdict": verdict, "note": str(value.get("note") or "")[:2000],
            "answer": answer if isinstance(answer, dict) else None}


def answer(connection: Any, auth: Any, *, project_id: str, decision_id: str, verdict: str,
           note: str = "", answer_value: Optional[dict] = None, via: str = "mcp") -> dict:
    """Apply one in-band answer as the caller. Returns {"decision_id", "status"}; raises
    AnswerRefused when the caller may not (the decision is then untouched)."""
    from ..identity import authz, lineage
    from ..identity.approvals import ApprovalPending, ApprovalRefused
    from ..identity.scope import ResolvedScope

    if verdict not in VERDICTS:
        raise AnswerRefused("invalid_answer", "answer approve or reject", status=422)
    if auth is None:
        raise AnswerRefused("identity_required", "present your own BOSS credential to answer",
                            decision_id=decision_id, status=401)
    row = connection.execute(text(
        "SELECT id, project_id, status FROM decisions WHERE id = :d"),
        {"d": decision_id}).mappings().first()
    if row is None or row["project_id"] != project_id:
        raise AnswerRefused("not_found", "no such decision on this task", status=404)
    if row["status"] != "pending":
        return {"decision_id": decision_id, "status": row["status"], "already_answered": True}
    scope = ResolvedScope.in_project(auth.principal.org_id, project_id,
                                     object_kind="decision", object_id=decision_id)
    verdict_pdp = authz.check(auth, "decision:approve", scope, db=connection)
    if not verdict_pdp.allowed:
        reason = str(getattr(verdict_pdp, "reason", "") or "missing_permission")
        words = ("approving needs a person's fresh sign-in check (step-up); a key or token "
                 "cannot step up" if "step_up" in reason else
                 "this caller may not approve decisions in this project")
        raise AnswerRefused(reason, words, decision_id=decision_id)
    from ..config import get_settings
    from .decisions import (DecisionNotFoundError, DecisionService,
                            InvalidDecisionTransitionError)

    try:
        with lineage.acting(auth):
            out = DecisionService(get_settings()).transition(
                connection, decision_id, action=verdict, actor=auth.principal.id,
                note=(note or f"answered over {via}"), answer=answer_value)
    except ApprovalPending:
        return {"decision_id": decision_id, "status": "awaiting_second_approval"}
    except ApprovalRefused as refused:
        raise AnswerRefused(refused.code, refused.message, decision_id=decision_id) from refused
    except DecisionNotFoundError as missing:
        raise AnswerRefused("not_found", str(missing), status=404) from missing
    except InvalidDecisionTransitionError as invalid:
        raise AnswerRefused("invalid_transition", str(invalid), decision_id=decision_id,
                            status=409) from invalid
    except Exception as other:  # noqa: BLE001 — a member-rule refusal outside identity
        name = type(other).__name__
        if "Member" in name or "Permission" in name:
            raise AnswerRefused("not_allowed", "this caller may not approve decisions here",
                                decision_id=decision_id) from other
        raise
    return {"decision_id": decision_id, "status": (out or {}).get("status") or verdict}


__all__ = ["AnswerRefused", "VERDICTS", "answer", "console_url", "decision_of_key",
           "form_elicitation", "input_key", "parse_response", "pending_for", "url_elicitation"]
