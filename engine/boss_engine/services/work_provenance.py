"""Who started a piece of build work: a person, or the system on its own.

One answer, read in two places that used to guess it separately:

* the job queue (`start_code_task` -> job priority): a build the system chose to do — a
  Metatron captain pass, an agent-process swarm build, a shepherd repair — is BACKGROUND, so it
  never takes a build slot ahead of a person. Before this, a captain's "QUALITY IMPROVE PASS"
  was enqueued at the same priority as the operator's build and took 7 of 13 builds while the
  operator's waited.
* the cost footer (`workflow_costs`): what a build the operator starts costs is not the price
  of the system's own loops, and an automatic gate retry is not a change anybody asked for.

Provenance is read from what the starter recorded at creation (`finalize_ctx.source`, carried
down the lineage from the root run) and from the note a retry carries — never from the title.
"""
from __future__ import annotations

from typing import Any, Optional

#: `finalize_ctx.source` values written by starters that run without a person asking.
AUTONOMOUS_SOURCES: frozenset[str] = frozenset({
    "metatron_captain",   # Metatron captain moves ("Captain: QUALITY IMPROVE PASS ...")
    "agent_process",      # agent-process / captain swarm builds
    "reliability",        # incident auto-remediation
    "shepherd",           # task shepherd fix tasks
})

#: How the system words the note on a retry it started by itself. A retry carrying one of these
#: is a gate or platform repair, not feedback from a person.
AUTOMATIC_RETRY_NOTE_PREFIXES: tuple[str, ...] = (
    "Automatic retry by the task shepherd",
    "Task Shepherd:",
    "Autonomous review-repair",
)


def note_is_automatic(note: Optional[str]) -> bool:
    text = str(note or "").strip()
    return any(text.startswith(prefix) for prefix in AUTOMATIC_RETRY_NOTE_PREFIXES)


def is_autonomous_source(source: Any) -> bool:
    return str(source or "").strip() in AUTONOMOUS_SOURCES


def _root_source(connection, finalize_ctx: dict[str, Any]) -> str:
    root = finalize_ctx.get("root_run_id") or finalize_ctx.get("parent_run_id")
    if not root or connection is None:
        return ""
    from sqlalchemy import text

    try:
        with connection.begin_nested():
            value = connection.execute(
                text("SELECT finalize_ctx->>'source' FROM code_task_runs WHERE id = :r"),
                {"r": str(root)},
            ).scalar()
    except Exception:  # noqa: BLE001 — an unreadable lineage is a person's work, as before
        return ""
    return str(value or "")


def started_autonomously(connection, finalize_ctx: Optional[dict[str, Any]]) -> bool:
    """True when nobody asked for this attempt: the system started it, or it continues work the
    system started and the latest note is not a person's."""
    ctx = finalize_ctx or {}
    if is_autonomous_source(ctx.get("source")):
        return True
    if not is_autonomous_source(_root_source(connection, ctx)):
        return False
    history = ctx.get("feedback_history") or []
    last_note = (history[-1] or {}).get("fix_note") if history and isinstance(
        history[-1], dict) else ""
    # A person who wrote what to change has taken the work over; anything else is still the
    # system's own loop.
    return not str(last_note or "").strip() or note_is_automatic(last_note)


def code_task_job_priority(connection, finalize_ctx: Optional[dict[str, Any]],
                           explicit: Optional[int] = None) -> int:
    """The `code.task` job priority: what the caller said, else REQUESTED for a person's work and
    BACKGROUND for work the system started by itself."""
    from .jobs import JOB_PRIORITY_BACKGROUND, JOB_PRIORITY_REQUESTED

    if explicit is not None:
        return int(explicit)
    if started_autonomously(connection, finalize_ctx):
        return JOB_PRIORITY_BACKGROUND
    return JOB_PRIORITY_REQUESTED
