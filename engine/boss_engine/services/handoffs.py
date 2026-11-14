"""HANDOFFS: a conversation handed to another agent (AP-P7, BOSSAPP-1058; AGENT_PLATFORM §4.5).

Sub-agents-as-tools are for work the supervisor keeps answering for. A HANDOFF is for a
multi-turn, user-facing conversation that another agent should carry on (Ask BOSS → the SRE):

    transfer_to_agent(agent, reason)

sets the thread's `active_agent`; the thread's NEXT turn is answered by that agent — its
published spec compiled, its identity on the turn — until it is handed on or back. Every
transfer is an `agent_acts` act of kind `handoff` (who handed it, to whom, why), so the chain of a
conversation says who was speaking when. A transfer to the thread's own agent (or to "boss")
hands it back: `active_agent` is cleared.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event

TOOL = "transfer_to_agent"
BACK = ("boss", "back", "general", "")


class HandoffError(ValueError):
    pass


def active_agent(connection: Connection, thread_id: Optional[str]) -> Optional[str]:
    """The agent answering this thread's next turn, or None for the thread's own agent."""
    if not thread_id:
        return None
    value = connection.execute(text(
        "SELECT active_agent FROM agent_threads WHERE id = :t"), {"t": thread_id}).scalar()
    return str(value) if value else None


def transfer(connection: Connection, *, project_id: str, thread_id: str, to_agent: str,
             actor: str, by_role: Optional[str] = None, reason: str = "",
             parent_act_id: Optional[str] = None) -> dict:
    """Hand the thread to `to_agent` (a key, a name or a callsign of this project's agents).
    Returns {ok, active_agent, act_id}. Refuses an agent this project does not have."""
    from . import agent_acts, agent_identity

    thread = connection.execute(text(
        "SELECT id, agent_role, active_agent FROM agent_threads WHERE id = :t "
        "AND project_id = :p"), {"t": thread_id, "p": project_id}).mappings().first()
    if thread is None:
        raise HandoffError("no such conversation in this project.")
    wanted = str(to_agent or "").strip()
    back = wanted.lower() in BACK
    target = None if back else agent_identity.resolve_in(connection, project_id, wanted)
    if not back:
        exists = target and connection.execute(text(
            "SELECT 1 FROM roles WHERE project_id = :p AND role_key = :r"),
            {"p": project_id, "r": target}).scalar()
        if not exists:
            raise HandoffError(f"this project has no agent called {wanted!r}.")
    own = str(thread["agent_role"] or "")
    if target and target == own:
        target = None
    previous = thread["active_agent"] or (own or None)
    connection.execute(text(
        "UPDATE agent_threads SET active_agent = :a, updated_at = now() WHERE id = :t"),
        {"a": target, "t": thread_id})
    act_id = None
    if target:
        act_id = agent_acts.quietly(
            connection, "record a handoff", agent_acts.open_act, project_id=project_id,
            kind="handoff", role_key=target, how="model_pick",
            handed_by_kind="agent" if by_role else "boss", handed_by_id=by_role or None,
            parent_act_id=parent_act_id, accountable=actor, subject_kind="agent_thread",
            subject_id=thread_id, thread_id=thread_id, why=(reason or "")[:500] or None,
            label=f"Handed to {target}" + (f" by {by_role}" if by_role else ""))
    emit_event(connection, project_id, "agent.handoff", actor, "agent_thread", thread_id,
               {"from": previous, "to": target or own or None, "reason": (reason or "")[:300],
                "act_id": act_id})
    return {"ok": True, "active_agent": target, "from": previous, "act_id": act_id,
            "message": (f"The conversation is now with {target}; its next turn is theirs."
                        if target else "The conversation is handed back.")}


__all__ = ["BACK", "HandoffError", "TOOL", "active_agent", "transfer"]
