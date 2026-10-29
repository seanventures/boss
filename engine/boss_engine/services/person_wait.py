"""Pause -> ask -> verify -> resume for a run blocked on a person (L2b).

A run that is refused for something only a person can clear (no grant, no seat, a
credential, a budget cap, a stale repo, an ambiguous requirement) enters
`waiting_on_person` instead of failing. The Engineering Captain opens one thread with
one concrete ask, holds no spend while waiting, and resumes the same job from the same
stage once the blocker clears -- verified with data, not the person's word.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from .core import create_blocking_decision, make_id, now_utc
from .decisions import SYSTEM_ACTOR, DecisionService, InvalidDecisionTransitionError
from .jobs import JOB_PRIORITY_INTERACTIVE

BLOCKER_AUTHORITY_NO_GRANT: str = "authority_no_grant"
STATUS_WAITING_ON_PERSON: str = "waiting_on_person"
PHASE_AWAITING_GRANT: str = "awaiting_grant"

# PAT-shaped / token-shaped strings: never persisted or echoed verbatim.
_SECRET_PATTERN = re.compile(
    r"\b(ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9]{20,}|xox[a-zA-Z]-[A-Za-z0-9-]{10,})\b"
)

_REDACTED_NOTICE = "that looked like a credential, so I didn't save it"


def grant_scope_for_run(connection: Any, run: Mapping[str, Any]) -> dict[str, Any]:
    """The scope a grant must cover to resume this run: the same shape consent.require_authority
    checked when the run was started (permissions/agents/repos), read back from the run's own
    row rather than re-derived from the caller."""
    from . import code_task_service as _cts
    from ..identity import consent as _consent

    full = _load_run(connection, run.get("run_id") or run.get("id") or "") or dict(run)
    spec = full.get("spec") or {}
    role_key = spec.get("role_key") or full.get("builder")
    return {
        "permissions": sorted(_consent.CODE_TASK_PERMISSIONS),
        "agents": [role_key] if role_key else [],
        "repos": _cts._consent_repos(spec.get("owner"), spec.get("repo"), spec.get("repo_host")),
    }


def recipient_for_run(connection: Any, run: Mapping[str, Any]) -> Optional[str]:
    """The run's authority principal: whoever holds the live delegation grant this run's task
    was started under, else the project's owner."""
    run_id = run.get("run_id") or run.get("id") or ""
    project_id = run.get("project_id")

    held = connection.execute(
        "SELECT user_principal_id FROM identity_delegation_grants "
        "WHERE task_run_id = :run_id AND status = 'active' ORDER BY granted_at DESC LIMIT 1",
        {"run_id": f"run_ct:{run_id}"},
    ).scalar()
    if held:
        return str(held)

    owner = connection.execute(
        "SELECT b.subject_id FROM identity_role_bindings b JOIN projects p ON p.id = :p "
        "JOIN workspaces w ON w.id = p.workspace_id WHERE b.subject_type = 'principal' "
        "AND b.role_id = 'arl_owner' AND ((b.scope_type = 'org' AND b.scope_id = w.org_id) "
        "OR (b.scope_type = 'project' AND b.scope_id = :p)) ORDER BY b.granted_at LIMIT 1",
        {"p": project_id},
    ).scalar()
    return str(owner) if owner else None


def open_thread_and_ask(
    connection: Any,
    *,
    project_id: str,
    run_id: str,
    recipient_principal_id: str,
    scope: dict[str, Any],
    question: str,
) -> str:
    """Opens (or reuses) the Captain thread for this run and posts the one plain-language ask,
    carrying a deep link to the run's own console screen."""
    from ..app_links.console import build_console_link

    # Area/param names the console actually reads for a build (see pr_description.py,
    # scorecards.py): `area=code_tasks&build=<run_id>` opens straight into this run's cockpit,
    # where the attention card holding this same ask is waiting.
    link = build_console_link("code_tasks", project_id=project_id, build=run_id)
    text_with_link = f"{question} {link}"

    existing = connection.execute(
        "SELECT id FROM agent_threads WHERE project_id = :project_id AND surface = 'agent' "
        "AND external_ref = :external_ref ORDER BY updated_at DESC LIMIT 1",
        {"project_id": project_id, "external_ref": run_id},
    ).scalar()
    thread_id = str(existing) if existing else make_id("thr")
    if not existing:
        connection.execute(
            "INSERT INTO agent_threads (id, project_id, actor, surface, external_ref, agent_role) "
            "VALUES (:id, :project_id, :actor, 'agent', :external_ref, 'person_wait')",
            {"id": thread_id, "project_id": project_id, "actor": recipient_principal_id,
             "external_ref": run_id},
        )

    connection.execute(
        "INSERT INTO agent_messages (id, thread_id, role, agent_role, content) "
        "VALUES (:id, :thread_id, 'assistant', 'person_wait', :content)",
        {"id": make_id("msg"), "thread_id": thread_id, "content": text_with_link},
    )

    try:
        from . import notify as _notify

        _notify.owner_email(connection, project_id)
    except Exception:  # noqa: BLE001 — the ask is already posted; a notification bell is best-effort
        pass

    return thread_id


def _load_run(connection: Any, run_id: str) -> Optional[dict[str, Any]]:
    # code_task_runs's primary key is `id`, not `run_id` -- matches load_code_task_run and
    # every other reader of this table (code_task_service.py, run_decisions.py, pr_merge.py).
    row = connection.execute(
        "SELECT * FROM code_task_runs WHERE id = :id", {"id": run_id}
    ).mappings().first()
    return dict(row) if row else None


def enter_waiting_on_person(
    connection: Any,
    budget: Any,
    *,
    run_id: str,
    project_id: str,
    task_id: Optional[str],
    reason: str,
    question: str,
    blocker_type: str = BLOCKER_AUTHORITY_NO_GRANT,
) -> dict[str, Any]:
    """Pauses a run on a person: preserves workspace/branch/stage, releases any held
    budget reservation, raises one blocking decision, and opens exactly one thread."""
    scope = grant_scope_for_run(connection, {"run_id": run_id, "project_id": project_id, "task_id": task_id})
    recipient = recipient_for_run(connection, {"run_id": run_id, "project_id": project_id})

    budget.release(run_id=run_id)

    decision_id = create_blocking_decision(
        connection,
        project_id,
        task_id,
        question,
        {
            "action": "resume_code_task",
            "run_id": run_id,
            "blocker_type": blocker_type,
        },
        {},
        reason,
        default_action="hold",
    )

    thread_id = open_thread_and_ask(
        connection,
        project_id=project_id,
        run_id=run_id,
        recipient_principal_id=recipient,
        scope=scope,
        question=question,
    )

    opened_at = now_utc()
    person_wait_ctx = {
        "scope": scope,
        "decision_id": decision_id,
        "thread_id": thread_id,
        "blocker_type": blocker_type,
        "reason": reason,
        "opened_at": opened_at,
        "recipient_principal_id": recipient,
    }

    connection.execute(
        "UPDATE code_task_runs SET status = :status, phase = :phase, decision_id = :decision_id, "
        "finalize_ctx = finalize_ctx || :person_wait WHERE id = :id",
        {
            "status": STATUS_WAITING_ON_PERSON,
            "phase": PHASE_AWAITING_GRANT,
            "decision_id": decision_id,
            "person_wait": {"person_wait": person_wait_ctx},
            "id": run_id,
        },
    )

    return {
        "status": STATUS_WAITING_ON_PERSON,
        "blocker_type": blocker_type,
        "decision_id": decision_id,
        "thread_id": thread_id,
        "scope": scope,
        "opened_at": opened_at,
    }


def scan_reply_for_secrets(text: str) -> tuple[str, bool]:
    """Blocks a PAT/token-shaped reply before storage or echo; never a silent drop."""
    if _SECRET_PATTERN.search(text):
        return _REDACTED_NOTICE, True
    return text, False


def post_reply(connection: Any, *, thread_id: str, run_id: str, actor: str, text: str) -> dict[str, Any]:
    """Scans before it ever reaches storage, then appends to the real Captain-thread table
    (`agent_messages`) -- there is no `thread_messages` table in this schema."""
    safe_text, blocked = scan_reply_for_secrets(text)
    # `role` is the SPEAKER (CHECK user|assistant|tool), not which person/agent spoke --
    # a person's reply in this thread is always "user".
    connection.execute(
        "INSERT INTO agent_messages (id, thread_id, role, agent_role, content) "
        "VALUES (:id, :thread_id, :role, :agent_role, :content)",
        {
            "id": make_id("msg"),
            "thread_id": thread_id,
            "role": "user",
            "agent_role": actor,
            "content": safe_text,
        },
    )
    return {"text": safe_text, "blocked": blocked}


def verify_and_resume(engine: Any, settings: Any, jobs: Any, *, run_id: str) -> dict[str, Any]:
    """Re-checks the blocker with data (not the person's word) and resumes the same job
    from the same stage exactly once."""
    from ..identity import grants as identity_grants

    run = _load_run(engine, run_id=run_id)
    if not run or run.get("status") != STATUS_WAITING_ON_PERSON:
        return {"resumed": False}

    person_wait_ctx = (run.get("finalize_ctx") or {}).get("person_wait") or {}
    scope = person_wait_ctx.get("scope") or {}

    covering_grant = identity_grants.find_covering(
        person_wait_ctx.get("recipient_principal_id") or run.get("person_id"),
        agent_ids=scope.get("agents", []),
        permissions=frozenset(scope.get("permissions", [])),
        project_id=run.get("project_id"),
        repos=scope.get("repos", []),
        run_id=run_id,
        db=engine,
    )
    if not covering_grant:
        return {"resumed": False}

    decision_service = DecisionService(settings)
    try:
        decision_service.transition(
            engine,
            run.get("decision_id"),
            action="approve",
            actor=SYSTEM_ACTOR,
            note=None,
            system_policy=True,
        )
    except InvalidDecisionTransitionError:
        return {"resumed": False}

    from .jobs import enqueue_job

    enqueue_job(
        engine,
        run.get("project_id"),
        "code.task",
        {"run_id": run_id},
        priority=JOB_PRIORITY_INTERACTIVE,
    )

    blocker_type = person_wait_ctx.get("blocker_type", BLOCKER_AUTHORITY_NO_GRANT)
    record_resolution(
        engine,
        run_id=run_id,
        project_id=run.get("project_id"),
        blocker_type=blocker_type,
        resolution="person",
        opened_at=person_wait_ctx.get("opened_at"),
    )

    return {"resumed": True, "grant_id": covering_grant.get("grant_id")}


def record_resolution(
    connection: Any,
    *,
    run_id: str,
    project_id: str,
    blocker_type: str,
    resolution: str,
    opened_at: str,
) -> None:
    """One history row: blocker_type, resolution, and time-to-unblock. Also attempts one
    learning-layer write, swallowed on failure (mirrors `run_supervisor.write_lesson`)."""
    resolved_at = now_utc().isoformat()
    entry = {
        "blocker_type": blocker_type,
        "resolution": resolution,
        "opened_at": opened_at,
        "resolved_at": resolved_at,
    }
    # jsonb_set + COALESCE on the existing array APPENDS one entry; a run that pauses on a
    # person more than once must keep every prior resolution, not overwrite it.
    connection.execute(
        "UPDATE code_task_runs SET finalize_ctx = jsonb_set("
        "COALESCE(finalize_ctx, '{}'::jsonb), '{person_wait_history}', "
        "COALESCE(finalize_ctx->'person_wait_history', '[]'::jsonb) || CAST(:entry AS jsonb)) "
        "WHERE id = :id",
        {"entry": json.dumps([entry]), "id": run_id},
    )

    try:
        from .knowledge import learning_writeback

        learning_writeback.record(
            connection,
            project_id=project_id,
            learning_kind="decision",
            title=f"run {run_id}: {blocker_type} resolved by {resolution}",
            statement=f"blocker {blocker_type} cleared via {resolution}",
            detail={"blocker_type": blocker_type, "resolution": resolution},
            about={"areas": ["person_wait"], "symbols": [], "rules": []},
            evidence=[],
            confidence=0.5,
            observed={"run_id": run_id, "opened_at": opened_at, "resolved_at": resolved_at},
        )
    except Exception:
        pass


def poll_due(engine: Any, settings: Any, *, now: Optional[Any] = None) -> int:
    """Re-checks every run currently waiting on a person; returns the number resumed.
    An archived project's runs are never re-checked -- it takes no new work."""
    from sqlalchemy import text

    from . import projects_archive
    from .jobs import JobLeaseService

    jobs = JobLeaseService(settings)
    rows = engine.execute(
        text("SELECT id AS run_id FROM code_task_runs WHERE status = :status AND "
             + projects_archive.live_sql("project_id")),
        {"status": STATUS_WAITING_ON_PERSON},
    ).mappings().all()

    resumed = 0
    for row in rows:
        result = verify_and_resume(engine, settings, jobs, run_id=row["run_id"])
        if result.get("resumed"):
            resumed += 1
    return resumed
