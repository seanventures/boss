"""Skills learned from repeated agent work, reviewed like lessons (BOSSAPP-941; Kiro gap 8).

Detects repeated tool-call procedures across distinct agent runs, compiles each into a named
Markdown skill draft, and routes it through the exact same diff-bound consent & review inbox
used by lessons and authored skills (``skill_definitions.prepare_skill`` +
``skill_definitions.propose_change``). Approving the decision publishes the skill.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import change_consent, skill_definitions
from .core import emit_event

MIN_REPEATED_RUNS = 3
MIN_SEQUENCE_LENGTH = 2
MAX_SEQUENCE_LENGTH = 12
LEARNED_ACTOR = "agent:skill_learner"


@dataclass(frozen=True)
class DetectedProcedure:
    signature: str
    skill_key: str
    role_key: str
    steps: tuple[str, ...]
    run_ids: tuple[str, ...]
    occurrences: int


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return cleaned or "procedure"


def make_skill_key(role_key: str, steps: Sequence[str]) -> str:
    """Deterministic lower_snake_case skill key (3-49 chars) for a repeated procedure."""
    digest = hashlib.sha256(f"{role_key}:{'|'.join(steps)}".encode()).hexdigest()[:6]
    head = _slug(f"learned_{steps[0]}_{steps[-1]}")[:36].strip("_")
    key = f"{head}_{digest}"
    if not key[0].isalpha():
        key = f"s_{key}"
    return key[:49]


def detect_repeated_sequences(
    traces: Sequence[Mapping[str, Any]],
    *,
    min_occurrences: int = MIN_REPEATED_RUNS,
    min_length: int = MIN_SEQUENCE_LENGTH,
) -> list[DetectedProcedure]:
    """Find tool-call sequences repeated across at least ``min_occurrences`` distinct runs.

    Each trace dict has ``{"run_id": str, "role_key": str, "tools": list[str]}``.
    """
    grouped: dict[tuple[str, tuple[str, ...]], list[str]] = defaultdict(list)
    for trace in traces:
        run_id = str(trace.get("run_id") or "").strip()
        role_key = str(trace.get("role_key") or "software_engineer").strip().lower()
        raw_tools = trace.get("tools") or []
        tools = tuple(
            str(t).strip().lower()
            for t in raw_tools
            if str(t).strip()
        )[:MAX_SEQUENCE_LENGTH]
        if not run_id or len(tools) < min_length:
            continue
        bucket = grouped[(role_key, tools)]
        if run_id not in bucket:
            bucket.append(run_id)

    found: list[DetectedProcedure] = []
    for (role_key, steps), run_ids in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(run_ids) < min_occurrences:
            continue
        sig = hashlib.sha256(f"{role_key}:{'->'.join(steps)}".encode()).hexdigest()[:16]
        found.append(
            DetectedProcedure(
                signature=sig,
                skill_key=make_skill_key(role_key, steps),
                role_key=role_key,
                steps=steps,
                run_ids=tuple(sorted(run_ids)),
                occurrences=len(run_ids),
            )
        )
    return found


def load_recent_run_traces(
    connection: Connection,
    project_id: str,
    *,
    window_days: int = 30,
) -> list[dict[str, Any]]:
    """Read successful tool-call sequences grouped by run from ``tool_call_audit`` (and
    ``skill_invocations`` as fallback) for a project."""
    traces_by_run: dict[tuple[str, str], list[str]] = defaultdict(list)
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text(
                    "SELECT COALESCE(run_id, request_id, id) AS rid, "
                    "COALESCE(actor, 'software_engineer') AS role_key, tool_name "
                    "FROM tool_call_audit "
                    "WHERE project_id = :p AND status IN ('ok', 'succeeded', 'completed') "
                    "AND created_at > now() - make_interval(days => :d) "
                    "ORDER BY created_at, id"
                ),
                {"p": project_id, "d": int(window_days)},
            ).mappings().all()
            for r in rows:
                role = str(r["role_key"]).removeprefix("agent:")
                traces_by_run[(str(r["rid"]), role)].append(str(r["tool_name"]))
    except Exception:  # noqa: BLE001 — fallback when tool_call_audit schema differs
        pass

    if not traces_by_run:
        try:
            with connection.begin_nested():
                rows = connection.execute(
                    text(
                        "SELECT COALESCE(created_by_job_id, id) AS rid, "
                        "COALESCE(caller, 'software_engineer') AS role_key, skill_id "
                        "FROM skill_invocations "
                        "WHERE project_id = :p AND status IN ('ok', 'succeeded', 'completed') "
                        "AND created_at > now() - make_interval(days => :d) "
                        "ORDER BY created_at, id"
                    ),
                    {"p": project_id, "d": int(window_days)},
                ).mappings().all()
                for r in rows:
                    role = str(r["role_key"]).removeprefix("agent:")
                    traces_by_run[(str(r["rid"]), role)].append(str(r["skill_id"]))
        except Exception:  # noqa: BLE001
            pass

    return [
        {"run_id": rid, "role_key": role, "tools": tools}
        for (rid, role), tools in traces_by_run.items()
    ]


def render_skill_markdown(proc: DetectedProcedure) -> str:
    """Compile a detected procedure into named Markdown skill instructions."""
    lines = [
        f"# Learned Procedure: {proc.skill_key}",
        "",
        f"Repeated **{proc.occurrences} times** by `{proc.role_key}` across distinct runs.",
        "",
        "## Steps",
    ]
    for idx, tool in enumerate(proc.steps, start=1):
        lines.append(f"{idx}. Invoke `{tool}` and verify its output before continuing.")
    lines.extend(
        [
            "",
            "## Verification",
            "- Confirm every step returns a non-error result with evidence refs.",
            "- Stop and escalate if any step deviates from the verified sequence.",
            "",
            "## Observed Runs",
            *[f"- `{rid}`" for rid in proc.run_ids[:10]],
        ]
    )
    return "\n".join(lines)


def draft_learned_skills(
    connection: Connection,
    project_id: str,
    *,
    traces: Optional[Sequence[Mapping[str, Any]]] = None,
    min_occurrences: int = MIN_REPEATED_RUNS,
    actor: str = LEARNED_ACTOR,
    window_days: int = 30,
) -> dict[str, Any]:
    """Detect repeated agent procedures and draft Markdown skills into the lessons/consent
    review inbox (never writes directly to active skills before human approval)."""
    source_traces = (
        list(traces)
        if traces is not None
        else load_recent_run_traces(connection, project_id, window_days=window_days)
    )
    detected = detect_repeated_sequences(source_traces, min_occurrences=min_occurrences)
    drafted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for proc in detected:
        existing = skill_definitions.get_skill(connection, project_id, proc.skill_key)
        if existing is not None:
            skipped.append({"skill_key": proc.skill_key, "reason": "already_installed"})
            continue
        pending = change_consent.pending(connection, project_id, "skill", proc.skill_key)
        if pending:
            skipped.append(
                {
                    "skill_key": proc.skill_key,
                    "reason": "already_pending_review",
                    "decision_id": pending[0]["decision_id"],
                }
            )
            continue

        label = f"Learned: {' -> '.join(proc.steps[:3])}"[:80]
        summary = (
            f"Auto-drafted from {proc.occurrences} repeated runs by {proc.role_key}: "
            f"{' -> '.join(proc.steps)}"
        )[:240]
        markdown = render_skill_markdown(proc)
        prepared = skill_definitions.prepare_skill(
            connection,
            project_id=project_id,
            skill_key=proc.skill_key,
            label=label,
            behaviour_kind="guide",
            summary=summary,
            knowledge=markdown,
            mode="create",
        )
        proposed = skill_definitions.propose_change(
            connection,
            project_id=project_id,
            actor=actor,
            prepared=prepared,
            mode="create",
            reason="learned_skill",
            meta={
                "source": "learned_from_repeated_work",
                "signature": proc.signature,
                "role_key": proc.role_key,
                "steps": list(proc.steps),
                "run_ids": list(proc.run_ids),
                "occurrences": proc.occurrences,
            },
            note=f"Learned from {proc.occurrences} repeated runs ({', '.join(proc.run_ids[:4])}).",
        )
        emit_event(
            connection,
            project_id,
            "learned_skill.drafted",
            actor,
            "skill",
            proc.skill_key,
            {
                "decision_id": proposed["decision_id"],
                "consent_id": proposed["consent_id"],
                "occurrences": proc.occurrences,
                "steps": list(proc.steps),
            },
        )
        drafted.append(
            {
                "skill_key": proc.skill_key,
                "label": label,
                "occurrences": proc.occurrences,
                "steps": list(proc.steps),
                "decision_id": proposed["decision_id"],
                "consent_id": proposed["consent_id"],
            }
        )

    return {
        "project_id": project_id,
        "detected": len(detected),
        "drafted": drafted,
        "skipped": skipped,
    }


def list_pending_learned_skills(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Return learned-skill drafts currently awaiting person review in the consent/lessons inbox."""
    rows = connection.execute(
        text(
            "SELECT c.id AS consent_id, c.decision_id, c.subject_key AS skill_key, "
            "c.status, c.proposed, c.created_at, d.question, d.status AS decision_status "
            "FROM change_consents c "
            "JOIN decisions d ON d.id = c.decision_id "
            "WHERE c.project_id = :p AND c.subject_kind = 'skill' AND c.status = 'pending' "
            "ORDER BY c.created_at DESC"
        ),
        {"p": project_id},
    ).mappings().all()
    out = []
    for r in rows:
        item = dict(r)
        if hasattr(item.get("created_at"), "isoformat"):
            item["created_at"] = item["created_at"].isoformat()
        out.append(item)
    return out


def approve_and_publish_learned_skill(
    connection: Connection,
    project_id: str,
    decision_id: str,
    *,
    actor: str,
) -> dict[str, Any]:
    """Approve a pending learned-skill draft in the review inbox and publish it as an active
    project skill (person-only consent)."""
    if str(actor or "").startswith(("agent:", "agt_", "sys_")):
        raise PermissionError("Only a person may approve and publish a learned skill.")
    row = connection.execute(
        text(
            "SELECT id, subject_key, proposed, diff_hash, base_hash "
            "FROM change_consents "
            "WHERE project_id = :p AND decision_id = :d AND subject_kind = 'skill' "
            "AND status = 'pending'"
        ),
        {"p": project_id, "d": decision_id},
    ).mappings().first()
    if row is None:
        raise LookupError(f"no pending skill consent for decision {decision_id}")

    proposed = row["proposed"]
    if isinstance(proposed, str):
        import json

        proposed = json.loads(proposed)
    proposed = dict(proposed or {})

    connection.execute(
        text(
            "UPDATE decisions SET status = 'approved', approved_by = :a, approved_at = now() "
            "WHERE id = :d AND project_id = :p"
        ),
        {"a": actor, "d": decision_id, "p": project_id},
    )
    connection.execute(
        text(
            "UPDATE change_consents SET status = 'consumed', decided_by = :a, "
            "decided_at = now(), consumed_at = now() WHERE id = :cid"
        ),
        {"a": actor, "cid": row["id"]},
    )
    saved = skill_definitions.save_skill(
        connection,
        project_id=project_id,
        actor=actor,
        skill_key=str(row["subject_key"]),
        label=str(proposed.get("label") or row["subject_key"]),
        behaviour_kind=str(proposed.get("behaviour_kind") or "guide"),
        summary=str(proposed.get("summary") or ""),
        knowledge=str(proposed.get("knowledge") or ""),
        inputs=proposed.get("inputs") or (),
        produces=proposed.get("produces") or (),
        config=proposed.get("config") or {},
        risk_tier=int(proposed.get("risk_tier") or 1),
        enabled=True,
        mode="upsert",
        origin="learned",
    )
    emit_event(
        connection,
        project_id,
        "learned_skill.published",
        actor,
        "skill",
        str(row["subject_key"]),
        {"decision_id": decision_id, "consent_id": row["id"]},
    )
    return {"skill": saved, "decision_id": decision_id, "consent_id": row["id"]}


__all__ = [
    "DetectedProcedure",
    "MIN_REPEATED_RUNS",
    "make_skill_key",
    "detect_repeated_sequences",
    "load_recent_run_traces",
    "render_skill_markdown",
    "draft_learned_skills",
    "list_pending_learned_skills",
    "approve_and_publish_learned_skill",
]
