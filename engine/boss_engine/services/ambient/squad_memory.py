"""Squad memory: what one squad tried on one item, and what failed (BOSSAPP-908; design §13.1).

SHORT-LIVED, PER ITEM, ON THE EXISTING LEDGER
---------------------------------------------
A squad member joining late, or taking a turn after a long pause, must not re-run an approach
the squad already ruled out — "the single costliest thing a resumed session does" is exactly
that (agent_memory.py). BOSS already has the store for it: the within-session ledger
(`agent_session_ledger`: goal, phase, next step, what was tried and rejected, artifacts). A
squad's memory is that ledger keyed `squad:<work item id>`; nothing new is stored.

    goal       the item's title
    phase      where the squad is ("Round 2 · proposal open")
    next_step  what the lead said comes next
    rejected   TRIED AND FAILED: a proposal the critic sank, a hypothesis ruled out, a run that
               failed — with why (capped, newest kept)
    artifacts  the runs, panels and PRs the squad started

Findings are not copied here: they are on the board, and `pack` reads them from there, so the
memory and the board can never disagree.

AT CLOSE, A LEARNING
--------------------
When the item ends, `close` finishes the ledger (it stops being injected) and promotes what the
squad found and ruled out into ONE knowledge-graph learning (kind `investigation`: tried,
ruled_out, found) through `learning_writeback.record` — the same write every agent's learning
takes, so dedupe, scrub, provenance and quarantine apply. A store that refuses it (no evidence,
a secret shape) leaves the item closed and says why.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.engine import Connection

from .. import agent_memory
from . import collab
from ._common import clean_text


def thread_of(item_id: str) -> str:
    return f"squad:{item_id}"


def _project(connection: Connection, item_id: str) -> str:
    from . import board

    item = board.get(connection, item_id)
    if item is None:
        raise LookupError(f"no ambient work item {item_id}")
    return item["project_id"]


def open(connection: Connection, item_id: str, *, goal: str, actor: str,  # noqa: A001
         phase: Optional[str] = None) -> dict[str, Any]:
    return agent_memory.write_ledger(
        connection, project_id=_project(connection, item_id), thread_id=thread_of(item_id),
        actor=actor, goal=clean_text(goal, 300) or "Work the item", phase=phase)


def note(connection: Connection, item_id: str, *, actor: str, phase: Optional[str] = None,
         next_step: Optional[str] = None, tried_and_failed: Optional[str] = None,
         artifact: Optional[str] = None) -> dict[str, Any]:
    """Add to the squad's memory. `tried_and_failed` APPENDS (the expensive knowledge)."""
    if agent_memory.read_ledger(connection, thread_id=thread_of(item_id)) is None:
        from . import board

        open(connection, item_id, goal=(board.get(connection, item_id) or {}).get("title") or "",
             actor=actor)
    return agent_memory.write_ledger(
        connection, project_id=_project(connection, item_id), thread_id=thread_of(item_id),
        actor=actor, phase=clean_text(phase, 120) or None,
        next_step=clean_text(next_step, 300) or None,
        rejected=clean_text(tried_and_failed, 300) or None, artifact_id=artifact)


def read(connection: Connection, item_id: str) -> dict[str, Any]:
    """{goal, phase, next_step, tried_and_failed[], artifacts[], findings[], open: bool}."""
    ledger = agent_memory.read_ledger(connection, thread_id=thread_of(item_id)) or {}
    findings = [{"by": m["sender"], "kind": m["kind"], "summary": (m["body"] or {}).get("summary"),
                 **({"confidence": m["body"].get("confidence")}
                    if m["kind"] == "hypothesis" else {})}
                for m in collab.messages(connection, item_id, kinds=("finding", "hypothesis"))]
    return {"goal": ledger.get("goal"), "phase": ledger.get("phase"),
            "next_step": ledger.get("next_step"),
            "tried_and_failed": list(ledger.get("rejected") or []),
            "artifacts": list(ledger.get("artifact_ids") or []), "findings": findings,
            "open": bool(ledger)}


def pack(connection: Connection, item_id: str, *, budget: int = 2_400) -> str:
    """The memory as a prompt block, read FIRST by every member's turn. Bounded."""
    memory = read(connection, item_id)
    lines = [f"GOAL: {memory['goal'] or '(none)'}"]
    if memory["phase"]:
        lines.append(f"WHERE WE ARE: {memory['phase']}")
    if memory["next_step"]:
        lines.append(f"NEXT (the lead said): {memory['next_step']}")
    if memory["tried_and_failed"]:
        lines.append("TRIED AND FAILED — do not propose these again:")
        lines += [f"  - {r}" for r in memory["tried_and_failed"]]
    if memory["findings"]:
        lines.append("FOUND SO FAR:")
        lines += [f"  - {f['by']}: {f['summary']}" for f in memory["findings"][-10:]]
    out = "\n".join(lines)
    return out if len(out) <= budget else out[:budget] + "\n  … (older entries dropped)"


def close(connection: Connection, item_id: str, *, actor: str) -> dict[str, Any]:
    """Finish the squad's memory and promote what it learned into one KG learning."""
    memory = read(connection, item_id)
    agent_memory.finish_ledger(connection, thread_id=thread_of(item_id), actor=actor)
    found = [f["summary"] for f in memory["findings"] if f["kind"] == "finding" and f["summary"]]
    if not (found or memory["tried_and_failed"]):
        return {"learning": None, "skipped": "the squad recorded nothing to learn"}
    from . import board

    item = board.get(connection, item_id) or {}
    try:
        from ..knowledge import learning_writeback

        with connection.begin_nested():
            learned = learning_writeback.record(
                connection, project_id=item["project_id"], learning_kind="investigation",
                title=clean_text(f"Squad: {item.get('title')}", 200),
                statement=clean_text("; ".join(found[:3]) or memory["tried_and_failed"][0], 1800),
                detail={"tried": [clean_text(a, 200) for a in memory["artifacts"]][:10]
                        or ["the squad's board"],
                        "ruled_out": [clean_text(r, 300) for r in memory["tried_and_failed"]],
                        "found": clean_text("; ".join(found), 1800) or "nothing conclusive"},
                about={"areas": [a for a in (item.get("app"), item.get("subject_ref"),
                                             item.get("task_class")) if a]},
                evidence=[{"type": "log", "ref": f"work_item:{item_id}",
                           "excerpt": clean_text(f"{len(memory['findings'])} findings, "
                                                 f"{len(memory['tried_and_failed'])} ruled out "
                                                 "on the squad board", 300)}],
                confidence=0.5)
        return {"learning": learned, "skipped": None}
    except Exception as exc:  # noqa: BLE001 — the item is closed either way; say why not
        return {"learning": None, "skipped": f"{type(exc).__name__}: {str(exc)[:200]}"}


__all__ = ["thread_of", "open", "note", "read", "pack", "close"]
