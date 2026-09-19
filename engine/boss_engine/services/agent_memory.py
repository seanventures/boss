"""What BOSS remembers between sessions, and what a long session remembers about itself.

WHY THIS EXISTS
---------------
BOSS had 249 tables and none of them remembered anything an agent was told. Every thread
started cold, every correction was spent the moment it was given, and the same guidance had to
be repeated in each new conversation. Kiro Crew's own account of scaling to 50 concurrent
sessions puts this first — *"Stage 1 to 2 is free. Stage 2 to 3 is the one that pays"*, and
stage 3 is memory.

Two stores with different jobs:

* **Memory** (`agent_memory`) crosses sessions. Preferences, project context, and lessons.
* **The ledger** (`agent_session_ledger`) is *within* one long session: goal, phase, next step,
  and what was already tried and rejected. It exists because a long thread eventually has its
  early turns compacted away, and the objective is usually the first thing to fall out.

WHERE THIS IS DELIBERATELY STRICTER THAN KIRO CREW
--------------------------------------------------
A lesson is a RULE an agent will follow. Kiro Crew writes them straight to disk. Here they
arrive `proposed` and bind nothing until reviewed, because a wrong rule quietly steering every
future session is a worse failure than having no memory at all — and BOSS already owns the
review machinery that makes this cheap.

INJECTION IS BUDGETED, NOT BEST-EFFORT
--------------------------------------
Everything read here ends up in a prompt. An unbounded store would grow until it crowded out
the conversation it was meant to inform, and the failure would look like the model getting
worse rather than the context being full. `pack_for_turn` takes a hard character budget and
returns what fits, most-useful first.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

#: Kinds that inform vs. kinds that bind. A preference ("prefers terse answers") shapes tone;
#: a lesson ("always run the frontend checks first") is an instruction the agent will obey. Only
#: the second needs a human before it takes effect.
BINDING_KINDS = ("lesson",)

#: What one turn may spend on remembered context. Chosen to be a small fraction of the window:
#: memory that crowds out the question is memory that made the answer worse.
DEFAULT_PACK_BUDGET = 2_400

#: Ledger entries are short by construction — a goal, a phase, a next step. A ledger that grew
#: without limit would be a second transcript, which is the thing it exists to replace.
MAX_REJECTED_ENTRIES = 12


class MemoryError_(ValueError):
    """Something the caller can fix, phrased for them."""


def _hash(body: str) -> str:
    return hashlib.sha256(body.strip().lower().encode("utf-8")).hexdigest()[:32]


def remember(
    connection: Connection,
    *,
    project_id: str,
    actor: str,
    kind: str,
    body: str,
    scope: str = "project",
    scope_ref: Optional[str] = None,
    source_thread_id: Optional[str] = None,
) -> dict[str, Any]:
    """Record one thing worth carrying into later sessions.

    Returns the row, including `status` — which is the part callers must not assume. A lesson
    comes back `proposed` and is invisible to `pack_for_turn` until someone reviews it.
    """
    body = (body or "").strip()
    if not body:
        raise MemoryError_("nothing to remember: body is empty")
    if len(body) > 2000:
        # A memory is a sentence, not a document. Something this long is a transcript being
        # pasted in, and injecting it every turn would spend the whole budget on one entry.
        raise MemoryError_(
            f"that is {len(body)} characters; a memory should be a sentence or two. "
            "Summarise it, or write it to an artifact and remember the pointer.")
    if kind not in ("preference", "project", "lesson"):
        raise MemoryError_("kind must be one of: preference, project, lesson")
    if scope not in ("org", "project", "actor", "role"):
        raise MemoryError_("scope must be one of: org, project, actor, role")
    if scope in ("actor", "role") and not (scope_ref or "").strip():
        raise MemoryError_(f"scope '{scope}' needs scope_ref (the actor id or role key)")

    status = "proposed" if kind in BINDING_KINDS else "active"
    row = connection.execute(
        text(
            """
            INSERT INTO agent_memory (
                id, project_id, kind, scope, scope_ref, body, source_thread_id,
                created_by, status, content_hash
            )
            VALUES (:id, :p, :k, :s, :sr, :b, :t, :a, :st, :h)
            -- SAID TWICE IS STILL ONE RULE. The same correction repeated must not become two
            -- entries that both get injected; it refreshes the one that exists instead.
            ON CONFLICT (project_id, kind, scope, scope_ref, content_hash) DO UPDATE
               SET updated_at = now(),
                   source_thread_id = COALESCE(EXCLUDED.source_thread_id,
                                               agent_memory.source_thread_id)
            RETURNING id, kind, scope, scope_ref, body, status, created_at
            """
        ),
        {"id": make_id("mem"), "p": project_id, "k": kind, "s": scope,
         "sr": (scope_ref or "").strip(), "b": body,
         "t": source_thread_id, "a": actor, "st": status, "h": _hash(body)},
    ).mappings().one()
    emit_event(connection, project_id, "memory.recorded", actor, "agent_memory", row["id"],
               {"kind": kind, "scope": scope, "status": row["status"]})
    return dict(row)


def review(
    connection: Connection, *, project_id: str, memory_id: str, actor: str, decision: str,
) -> dict[str, Any]:
    """Let a proposed rule bind, or refuse it.

    The gate that makes lessons safe. Until this runs, a lesson is a note nobody acts on.
    """
    if decision not in ("active", "rejected"):
        raise MemoryError_("decision must be 'active' or 'rejected'")
    row = connection.execute(
        text(
            """
            UPDATE agent_memory
               SET status = :d, reviewed_by = :a, reviewed_at = now(), updated_at = now()
             WHERE id = :id AND project_id = :p AND status = 'proposed'
            RETURNING id, kind, body, status
            """
        ),
        {"d": decision, "a": actor, "id": memory_id, "p": project_id},
    ).mappings().first()
    if row is None:
        raise MemoryError_(
            f"no memory {memory_id!r} awaiting review on this project "
            "(it may already be active, rejected, or retired)")
    emit_event(connection, project_id, "memory.reviewed", actor, "agent_memory", memory_id,
               {"decision": decision})
    return dict(row)


def forget(
    connection: Connection, *, project_id: str, memory_id: str, actor: str,
) -> dict[str, Any]:
    """Retire a memory. RETIRED, NOT DELETED — a rule that shaped past answers stays auditable."""
    row = connection.execute(
        text("UPDATE agent_memory SET status='retired', updated_at=now() "
             " WHERE id=:id AND project_id=:p AND status <> 'retired' "
             "RETURNING id, kind, body"),
        {"id": memory_id, "p": project_id},
    ).mappings().first()
    if row is None:
        raise MemoryError_(f"no active memory {memory_id!r} on this project")
    emit_event(connection, project_id, "memory.retired", actor, "agent_memory", memory_id, {})
    return dict(row)


def recall(
    connection: Connection,
    *,
    project_id: str,
    actor: Optional[str] = None,
    role: Optional[str] = None,
    kinds: Optional[tuple[str, ...]] = None,
    include_proposed: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """What applies to this turn, most useful first.

    Scope filtering is the whole subtlety: an `actor` memory belongs to one person and must not
    leak into another's session, while `project` and `org` memories apply to everyone here.
    """
    statuses = ["active", "proposed"] if include_proposed else ["active"]
    rows = connection.execute(
        text(
            """
            SELECT id, kind, scope, scope_ref, body, status, use_count, last_used_at,
                   created_at
              FROM agent_memory
             WHERE project_id = :p
               AND status = ANY(:statuses)
               AND (expires_at IS NULL OR expires_at > now())
               AND (CAST(:kinds AS text[]) IS NULL OR kind = ANY(CAST(:kinds AS text[])))
               -- Scope gate. An actor-scoped memory is that person's; a role-scoped one is
               -- that seat's. Everything else is the project's.
               AND (
                    scope IN ('org', 'project')
                 OR (scope = 'actor' AND scope_ref = :actor)
                 OR (scope = 'role'  AND lower(scope_ref) = lower(:role))
               )
             -- A lesson outranks context, which outranks a preference: the most binding thing
             -- must survive the budget cut in `pack_for_turn`.
             ORDER BY CASE kind WHEN 'lesson' THEN 0 WHEN 'project' THEN 1 ELSE 2 END,
                      use_count DESC, created_at DESC
             LIMIT :lim
            """
        ),
        {"p": project_id, "statuses": statuses,
         "kinds": list(kinds) if kinds else None,
         "actor": actor or "", "role": role or "", "lim": max(1, min(int(limit), 500))},
    ).mappings().all()
    return [dict(r) for r in rows]


def pack_for_turn(
    connection: Connection,
    *,
    project_id: str,
    actor: Optional[str] = None,
    role: Optional[str] = None,
    budget: int = DEFAULT_PACK_BUDGET,
) -> str:
    """The block injected into a turn. Empty string when there is nothing to say.

    Returns text rather than rows because the caller is assembling a prompt, and because the
    budget has to be applied to the RENDERED size — counting rows would let one long entry blow
    past it.
    """
    entries = recall(connection, project_id=project_id, actor=actor, role=role)
    if not entries:
        return ""

    lines: list[str] = []
    used_ids: list[str] = []
    spent = 0
    for entry in entries:
        prefix = {"lesson": "RULE", "project": "CONTEXT", "preference": "PREFERENCE"}[
            entry["kind"]]
        line = f"- [{prefix}] {entry['body']}"
        if spent + len(line) > budget:
            # Stop at the budget rather than truncating mid-entry: half a rule is worse than
            # no rule, because it still reads like an instruction.
            break
        lines.append(line)
        used_ids.append(entry["id"])
        spent += len(line) + 1

    if not lines:
        return ""

    # USAGE IS WHAT KEEPS A MEMORY ALIVE. The pruner reads `last_used_at`, so this is the
    # signal that separates a rule that matters from one recorded once and never relevant again.
    connection.execute(
        text("UPDATE agent_memory SET use_count = use_count + 1, last_used_at = now() "
             " WHERE id = ANY(:ids)"),
        {"ids": used_ids},
    )
    return (
        "\n\n=== WHAT YOU ALREADY KNOW ABOUT THIS PROJECT ===\n"
        "Recorded in earlier sessions. A RULE is binding — follow it without being asked "
        "again. CONTEXT and PREFERENCE inform how you answer.\n"
        "Do not recite this list back to the operator; act on it.\n"
        + "\n".join(lines)
    )


def prune(connection: Connection, *, project_id: str, unused_days: int = 90) -> dict[str, Any]:
    """Retire memories that were never used, and drop what expired.

    Time-tiered rather than absolute, in the spirit of Kiro Crew's decay schedule: something
    injected and used stays regardless of age; something recorded and never once relevant is
    noise that costs budget on every turn.
    """
    retired = connection.execute(
        text(
            """
            UPDATE agent_memory SET status='retired', updated_at=now()
             WHERE project_id = :p AND status = 'active' AND use_count = 0
               AND created_at < now() - (:d || ' days')::interval
               -- A reviewed rule was a deliberate act; it does not decay from disuse.
               AND reviewed_at IS NULL
            RETURNING id
            """
        ),
        {"p": project_id, "d": int(unused_days)},
    ).scalars().all()
    expired = connection.execute(
        text("UPDATE agent_memory SET status='retired', updated_at=now() "
             " WHERE project_id=:p AND status='active' "
             "   AND expires_at IS NOT NULL AND expires_at <= now() RETURNING id"),
        {"p": project_id},
    ).scalars().all()
    return {"retired_unused": len(retired), "retired_expired": len(expired)}


# ── The session ledger ────────────────────────────────────────────────────────


def write_ledger(
    connection: Connection,
    *,
    project_id: str,
    thread_id: str,
    actor: str,
    goal: Optional[str] = None,
    phase: Optional[str] = None,
    next_step: Optional[str] = None,
    rejected: Optional[str] = None,
    artifact_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create or update this session's ledger.

    Every field is optional except on creation, where a goal is required — a ledger without one
    cannot do its job, which is telling a cold resume what it was trying to achieve.

    `rejected` APPENDS. That list is the expensive knowledge: the single costliest thing a
    resumed session does is re-run an approach that already failed.
    """
    existing = connection.execute(
        text("SELECT thread_id, goal, rejected, artifact_ids FROM agent_session_ledger "
             " WHERE thread_id = :t"),
        {"t": thread_id},
    ).mappings().first()

    if existing is None:
        if not (goal or "").strip():
            raise MemoryError_("a new ledger needs a goal")
        rejected_list = [rejected.strip()] if (rejected or "").strip() else []
        artifacts = [artifact_id] if artifact_id else []
        row = connection.execute(
            text(
                """
                INSERT INTO agent_session_ledger (
                    thread_id, project_id, goal, phase, next_step, rejected, artifact_ids,
                    updated_by)
                VALUES (:t, :p, :g, :ph, :ns, CAST(:rj AS JSONB), CAST(:ar AS JSONB), :a)
                RETURNING thread_id, goal, phase, next_step, rejected, artifact_ids
                """
            ),
            {"t": thread_id, "p": project_id, "g": goal.strip(), "ph": phase,
             "ns": next_step, "rj": json.dumps(rejected_list),
             "ar": json.dumps(artifacts), "a": actor},
        ).mappings().one()
        emit_event(connection, project_id, "ledger.opened", actor, "agent_thread", thread_id,
                   {"goal": goal.strip()[:200]})
        return dict(row)

    current_rejected = existing["rejected"] or []
    if isinstance(current_rejected, str):
        current_rejected = json.loads(current_rejected)
    if (rejected or "").strip():
        current_rejected = ([*current_rejected, rejected.strip()])[-MAX_REJECTED_ENTRIES:]
    current_artifacts = existing["artifact_ids"] or []
    if isinstance(current_artifacts, str):
        current_artifacts = json.loads(current_artifacts)
    if artifact_id and artifact_id not in current_artifacts:
        current_artifacts = [*current_artifacts, artifact_id]

    row = connection.execute(
        text(
            """
            UPDATE agent_session_ledger
               SET goal = COALESCE(:g, goal),
                   phase = COALESCE(:ph, phase),
                   next_step = COALESCE(:ns, next_step),
                   rejected = CAST(:rj AS JSONB),
                   artifact_ids = CAST(:ar AS JSONB),
                   updated_by = :a,
                   updated_at = now(),
                   -- Writing to a finished ledger reopens it: the work came back.
                   finished_at = NULL
             WHERE thread_id = :t
            RETURNING thread_id, goal, phase, next_step, rejected, artifact_ids
            """
        ),
        {"g": (goal or "").strip() or None, "ph": phase, "ns": next_step,
         "rj": json.dumps(current_rejected), "ar": json.dumps(current_artifacts),
         "a": actor, "t": thread_id},
    ).mappings().one()
    return dict(row)


def read_ledger(connection: Connection, *, thread_id: str) -> Optional[dict[str, Any]]:
    """This session's ledger, or None. A finished ledger is NOT returned."""
    row = connection.execute(
        text("SELECT thread_id, project_id, goal, phase, next_step, rejected, artifact_ids, "
             "       updated_at "
             "  FROM agent_session_ledger WHERE thread_id = :t AND finished_at IS NULL"),
        {"t": thread_id},
    ).mappings().first()
    return dict(row) if row else None


def finish_ledger(
    connection: Connection, *, thread_id: str, actor: str,
) -> Optional[dict[str, Any]]:
    """Mark the work done or abandoned, so it stops being re-injected.

    "A stale ledger quietly steering a session that has moved on" is the failure this prevents.
    """
    row = connection.execute(
        text("UPDATE agent_session_ledger SET finished_at = now(), updated_by = :a, "
             "       updated_at = now() "
             " WHERE thread_id = :t AND finished_at IS NULL RETURNING thread_id, project_id"),
        {"t": thread_id, "a": actor},
    ).mappings().first()
    if row:
        emit_event(connection, row["project_id"], "ledger.finished", actor, "agent_thread",
                   thread_id, {})
    return dict(row) if row else None


def pack_ledger(connection: Connection, *, thread_id: str) -> str:
    """The ledger as a prompt block. Empty when there is none.

    THIS IS THE HALF OF COMPACTION THAT SURVIVES. When early turns are dropped to make room,
    the objective is usually the first thing to go — so it is re-stated here, every turn,
    cheaply, from storage rather than from the transcript.
    """
    ledger = read_ledger(connection, thread_id=thread_id)
    if not ledger:
        return ""
    parts = [
        "\n\n=== WHERE THIS SESSION HAD GOT TO ===",
        "Read this before deciding what to do next; it survives context being compacted away.",
        f"GOAL: {ledger['goal']}",
    ]
    if ledger.get("phase"):
        parts.append(f"PHASE: {ledger['phase']}")
    if ledger.get("next_step"):
        parts.append(f"NEXT STEP: {ledger['next_step']}")
    rejected = ledger.get("rejected") or []
    if isinstance(rejected, str):
        rejected = json.loads(rejected)
    if rejected:
        parts.append("ALREADY TRIED AND REJECTED — do not repeat these:")
        parts.extend(f"  - {r}" for r in rejected)
    return "\n".join(parts)
