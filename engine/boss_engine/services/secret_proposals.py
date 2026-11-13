"""BOSSAPP-871: an agent proposes a secret; a person types it and approves.

WHY THIS EXISTS
---------------
Only operators wrote secrets. An agent finishing a setup flow ("connect Stripe", "add the
webhook signing secret") had to stop and ask in prose, and the person pasted the key into chat —
where it sits in a transcript for ever.

THE LIFECYCLE
-------------
* PROPOSE (``propose_secret``, the registry tool in tools/builtin.py): the agent names the secret,
  where it goes (the project, a deployment target's named requirement, or itself as an
  agent-scoped secret) and why. **The agent never supplies the value** — the tool has no value
  argument and this module never takes one from an agent. A decision opens with
  ``recommendation.action = 'store_proposed_secret'``; a ``secret_proposals`` row records the ask
  (it has no value column).
* STORE (the person, in the console): the card has a write-only value field. The console sends it
  to ``POST /v1/secret-proposals/{decision_id}/store``, which approves the decision through the
  one approval door with the value held only in a context variable for that call
  (:func:`typed_value`). The approval arm (decisions._handle_approved_follow_on) writes it through
  ``secrets_service.put_secret`` and binds it to the named target, in the approval's transaction.
  The value is never written to the decision (no answer, no note, no event).
* APPROVE WITHOUT A VALUE (MCP ``decide``, curl, the plain Approve button) is REFUSED: the value
  can only come from the person's form.
* REFUSE: the proposal is marked refused; asking for the same name again within a day answers
  with that refusal instead of a new card.
* SCRUB: the stored value is registered with the run's exact-value redaction (BOSSAPP-866), so it
  never reaches that run's turns, tool logs or PR text.
"""
from __future__ import annotations

import contextlib
import contextvars
import re
from typing import Any, Iterator, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import create_blocking_decision, emit_event, make_id

ACTION = "store_proposed_secret"
DECISION_REASON = "secret_proposal"
REFUSAL_QUIET_HOURS = 24
TARGET_KINDS = ("project", "deployment_target", "agent")
KINDS = ("api_token", "access_key", "client_secret", "private_key", "ssh_private_key",
         "service_account", "kubeconfig")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{1,79}$")

#: The value a person typed on the card, for the duration of ONE approval call. Never stored.
_TYPED: contextvars.ContextVar[Optional[tuple[str, str]]] = contextvars.ContextVar(
    "boss_secret_proposal_value", default=None)


class SecretProposalError(ValueError):
    """Something the agent or person can act on, said for them."""


@contextlib.contextmanager
def typed_value(decision_id: str, value: str) -> Iterator[None]:
    """Hold the person's typed value for exactly this decision while it is approved."""
    token = _TYPED.set((str(decision_id), value))
    try:
        yield
    finally:
        _TYPED.reset(token)


def _target(connection: Connection, project_id: str, kind: str, target_id: Optional[str],
            role: str) -> tuple[str, Optional[str]]:
    kind = (kind or "project").strip().lower()
    if kind not in TARGET_KINDS:
        raise SecretProposalError(f"target is one of {', '.join(TARGET_KINDS)}")
    if kind == "project":
        return kind, None
    if kind == "agent":
        return kind, role                     # an agent proposes a secret for ITSELF only
    found = connection.execute(text(
        "SELECT id FROM deployment_targets WHERE project_id = :p AND (id = :t OR app_name = :t) "
        "ORDER BY (id = :t) DESC LIMIT 1"), {"p": project_id, "t": str(target_id or "")}).scalar()
    if not found:
        raise SecretProposalError(f"No deployment target {target_id!r} in this project.")
    return kind, str(found)


def propose(connection: Connection, project_id: str, *, role_key: str, name: str, reason: str,
            target_kind: str = "project", target_id: Optional[str] = None,
            binding_name: Optional[str] = None, secret_kind: str = "api_token",
            actor: str, run_id: Optional[str] = None) -> dict[str, Any]:
    """Open the card asking a person to store this secret. Takes NO value. Never stores anything."""
    from .run_secret_redaction import current_run_id
    from .tool_access_requests import _role_exists

    run_id = run_id or current_run_id()
    role = str(role_key or "").strip().lower()
    if not role or not _role_exists(connection, project_id, role):
        raise SecretProposalError("Only an agent of this project can propose a secret.")
    name = str(name or "").strip()
    if not _NAME.match(name):
        raise SecretProposalError("Name it with letters, digits, _ . or - (2-80 characters), "
                                  "e.g. stripe_api_key.")
    kind = str(secret_kind or "api_token").strip().lower()
    if kind not in KINDS:
        raise SecretProposalError(f"kind is one of {', '.join(KINDS)}")
    why = " ".join(str(reason or "").split())
    if len(why) < 8:
        raise SecretProposalError("Say why it is needed, in a sentence.")
    tkind, tid = _target(connection, project_id, target_kind, target_id, role)
    bind = str(binding_name or name).strip()[:80] if tkind == "deployment_target" else None
    refused = connection.execute(text(
        "SELECT decision_id FROM secret_proposals WHERE project_id = :p AND name = :n "
        "AND status = 'refused' AND resolved_at > now() - make_interval(hours => :h) "
        "ORDER BY resolved_at DESC LIMIT 1"),
        {"p": project_id, "n": name, "h": REFUSAL_QUIET_HOURS}).scalar()
    if refused:
        return {"status": "refused_recently", "decision_id": str(refused),
                "message": "A person said no to this secret in the last day. Ask them directly."}
    open_ = connection.execute(text(
        "SELECT decision_id FROM secret_proposals WHERE project_id = :p AND name = :n "
        "AND status = 'asked' LIMIT 1"), {"p": project_id, "n": name}).scalar()
    if open_:
        return {"status": "already_asked", "decision_id": str(open_)}
    where = {"project": "this project",
             "agent": f"{role} only",
             "deployment_target": f"deployment target {tid} as {bind}"}[tkind]
    question = f"{role} asks you to store the secret {name} for {where}. Type the value to approve."
    proposal_id = make_id("sprop")
    rec = {"action": ACTION, "card": "secret_proposal", "proposal_id": proposal_id,
           "name": name, "secret_kind": kind, "target_kind": tkind, "target_id": tid,
           "binding_name": bind, "role_key": role, "reason": why[:1000], "run_id": run_id,
           "requested_by": actor}
    decision_id = create_blocking_decision(connection, project_id, None, question, rec, {},
                                           DECISION_REASON, risk_tier=2)
    connection.execute(text(
        "INSERT INTO secret_proposals (id, project_id, decision_id, name, secret_kind, "
        " target_kind, target_id, binding_name, reason, requested_by, run_id) VALUES "
        "(:id, :p, :d, :n, :k, :tk, :ti, :b, :r, :by, :run)"),
        {"id": proposal_id, "p": project_id, "d": decision_id, "n": name, "k": kind, "tk": tkind,
         "ti": tid, "b": bind, "r": why[:1000], "by": role, "run": run_id})
    emit_event(connection, project_id, "secret.proposed", actor, "decision", decision_id,
               {"name": name, "role_key": role, "target_kind": tkind, "target_id": tid})
    return {"status": "asked", "decision_id": decision_id, "proposal_id": proposal_id,
            "name": name, "target_kind": tkind, "target_id": tid}


def _proposal(connection: Connection, row: dict[str, Any]) -> dict[str, Any]:
    found = connection.execute(text(
        "SELECT * FROM secret_proposals WHERE decision_id = :d FOR UPDATE"),
        {"d": str(row["id"])}).mappings().first()
    if not found:
        raise SecretProposalError("This card's proposal is gone.")
    return dict(found)


def on_approved(connection: Connection, settings: Any, row: dict[str, Any], actor: str) -> dict:
    """The approval arm: write the person's typed value through and bind it. Refused (the
    approval is not recorded) without a typed value for THIS decision."""
    held = _TYPED.get()
    if held is None or held[0] != str(row["id"]) or not held[1]:
        raise SecretProposalError(
            "Type the value on the card in the console. BOSS never takes a secret's value from "
            "an agent, a note or a plain approval.")
    value = held[1]
    proposal = _proposal(connection, row)
    if proposal["status"] != "asked":
        raise SecretProposalError(f"This proposal is already {proposal['status']}.")
    from .secret_backends import durable_secret_backend
    from .secrets_service import bind_secret_to_target, put_secret

    tkind = proposal["target_kind"]
    scope = {"project": ("project", None), "agent": ("agent", proposal["target_id"]),
             "deployment_target": ("deployment_target", proposal["target_id"])}[tkind]
    stored = put_secret(connection, settings, project_id=str(row["project_id"]),
                        name=proposal["name"], value=value, secret_kind=proposal["secret_kind"],
                        backend=durable_secret_backend(settings), scope_type=scope[0],
                        scope_id=scope[1], actor=actor)
    if tkind == "deployment_target":
        bind_secret_to_target(connection, target_id=proposal["target_id"],
                              name=proposal["binding_name"] or proposal["name"],
                              secret_ref_id=stored["secret_ref_id"],
                              project_id=str(row["project_id"]), actor=actor)
    from .run_secret_redaction import register

    register(value, run_id=proposal.get("run_id") or None)
    connection.execute(text(
        "UPDATE secret_proposals SET status = 'stored', secret_ref_id = :s, resolved_by = :a, "
        "resolved_at = now() WHERE id = :id"),
        {"s": stored["secret_ref_id"], "a": actor, "id": proposal["id"]})
    emit_event(connection, str(row["project_id"]), "secret.proposal_stored", actor, "decision",
               str(row["id"]), {"name": proposal["name"], "secret_ref_id": stored["secret_ref_id"]})
    return stored


def on_refused(connection: Connection, row: dict[str, Any], actor: str) -> None:
    connection.execute(text(
        "UPDATE secret_proposals SET status = 'refused', resolved_by = :a, resolved_at = now() "
        "WHERE decision_id = :d AND status = 'asked'"), {"a": actor, "d": str(row["id"])})
