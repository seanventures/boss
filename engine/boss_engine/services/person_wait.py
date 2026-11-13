"""Pause -> ask -> verify -> resume for a run blocked on a person (L2b).

A run that is refused for something only a person can clear (no grant, no seat, a
credential, a budget cap, a stale repo, an ambiguous requirement) enters
`waiting_on_person` instead of failing. BOSS raises ONE card per blocker (every run waiting on
the same thing joins it), holds no spend while waiting, and resumes each run from the same stage
once the blocker clears -- verified with data, not the person's word.

THE FIRST VERSION NEVER RAN AGAINST A DATABASE. Every statement was a bare SQL string (SQLAlchemy
2 refuses those: `ObjectNotExecutableError`), the finalize_ctx merge bound a dict, the resume
read a grant object as a dict and flipped no status back, and its tests mocked the connection,
so all of it passed. On the laptop (2026-11-07) four always-on CVE builds hit `no_grant`, this
path 500'd, the worker could not report the failure, and each build was re-leased every five
minutes for hours with the console saying "Queued" (attempt 30 of 3). Every statement here is
`text()` and the suite runs it on Postgres (test_person_wait_on_a_real_database.py).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Mapping, Optional

from sqlalchemy import text

from .core import create_blocking_decision, emit_event, make_id, now_utc
from .decision_actions import OPEN_STATUSES
from .decisions import SYSTEM_ACTOR, DecisionService
from .jobs import JOB_PRIORITY_INTERACTIVE

BLOCKER_AUTHORITY_NO_GRANT: str = "authority_no_grant"
STATUS_WAITING_ON_PERSON: str = "waiting_on_person"
PHASE_AWAITING_GRANT: str = "awaiting_grant"
#: The card's action: a person gives the builder a grant; BOSS resumes by itself (poll_due).
ACTION_GRANT_BUILDER: str = "grant_builder_authority"
#: Where a person gives an agent a standing grant (apps/web/app/account/agents).
GRANT_PAGE: str = "/account/agents"
#: "Review always-on builds before they start" is on: the card a person approves to let the
#: build start on the project's Captain authority (identity/captain_authority.py).
ACTION_REVIEW_BUILD: str = "review_always_on_build"
BLOCKER_ALWAYS_ON_REVIEW: str = "always_on_review"


def review_setting_link(project_id: str) -> str:
    """Where the switch lives: Project › Settings › Autonomy & budget."""
    from .project_experience.common import href

    return href(project_id, area="project", tab="settings", section="autonomy")

# PAT-shaped / token-shaped strings: never persisted or echoed verbatim.
_SECRET_PATTERN = re.compile(
    r"\b(ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9]{20,}|xox[a-zA-Z]-[A-Za-z0-9-]{10,})\b"
)

_REDACTED_NOTICE = "that looked like a credential, so I didn't save it"


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _load_run(connection: Any, run_id: str) -> Optional[dict[str, Any]]:
    # code_task_runs's primary key is `id`, not `run_id`.
    row = connection.execute(
        text("SELECT * FROM code_task_runs WHERE id = :id"), {"id": run_id}
    ).mappings().first()
    return dict(row) if row else None


def _org_of_project(connection: Any, project_id: Optional[str]) -> Optional[str]:
    if not project_id:
        return None
    return connection.execute(text(
        "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"),
        {"p": project_id}).scalar()


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def grant_scope_for_run(connection: Any, run: Mapping[str, Any]) -> dict[str, Any]:
    """The scope a grant must cover to resume this run: the same shape consent.require_authority
    checks at a person's start -- the builder's Passport id, the delegable build permissions and
    the run's repository -- read back from the run's own row, never from the caller."""
    from . import code_task_service as _cts
    from ..identity import catalogue
    from ..identity import consent as _consent
    from ..identity import runs as _runs

    full = _load_run(connection, run.get("run_id") or run.get("id") or "") or dict(run)
    spec = _as_dict(full.get("spec"))
    ctx = _as_dict(full.get("finalize_ctx"))
    role_key = _runs.resolved_builder(spec, ctx)
    agent_id = _runs.agent_for_role(connection, full.get("project_id") or run.get("project_id"),
                                    role_key)
    permissions = sorted(k for k in _consent.CODE_TASK_PERMISSIONS
                         if catalogue.is_known(k) and catalogue.meta(k).delegable)
    return {
        "permissions": permissions,
        "agents": [agent_id] if agent_id else [],
        "role_keys": [role_key] if role_key else [],
        "repos": _cts._consent_repos(spec.get("owner"), spec.get("repo"), spec.get("repo_host")),
    }


def recipient_for_run(connection: Any, run: Mapping[str, Any]) -> Optional[str]:
    """The run's authority principal: whoever holds the live delegation grant this run's task
    was started under, else the project's owner."""
    run_id = run.get("run_id") or run.get("id") or ""
    project_id = run.get("project_id")

    held = connection.execute(text(
        "SELECT user_principal_id FROM identity_delegation_grants "
        "WHERE task_run_id = :run_id AND status = 'active' ORDER BY created_at DESC LIMIT 1"),
        {"run_id": f"run_ct:{run_id}"},
    ).scalar()
    if held:
        return str(held)

    owner = connection.execute(text(
        "SELECT b.subject_id FROM identity_role_bindings b JOIN projects p ON p.id = :p "
        "JOIN workspaces w ON w.id = p.workspace_id WHERE b.subject_type = 'principal' "
        "AND b.role_id = 'arl_owner' AND ((b.scope_type = 'org' AND b.scope_id = w.org_id) "
        "OR (b.scope_type = 'project' AND b.scope_id = :p)) ORDER BY b.granted_at LIMIT 1"),
        {"p": project_id},
    ).scalar()
    return str(owner) if owner else None


def _covering_grant(connection: Any, *, person: Optional[str], scope: Mapping[str, Any],
                    project_id: Optional[str], run_id: str) -> Any:
    """The person's live grant that covers this run's recorded scope, or None. A scope with no
    builder Passport is never covered (the builder cannot act for anyone)."""
    from ..identity import grants as identity_grants

    if not person or not scope.get("agents"):
        return None
    return identity_grants.find_covering(
        person, agent_ids=list(scope.get("agents") or []),
        permissions=frozenset(scope.get("permissions") or []),
        project_id=project_id, repos=list(scope.get("repos") or []),
        run_id=f"run_ct:{run_id}", db=connection)


def _enqueue_under_grant(connection: Any, *, run: Mapping[str, Any], person: str, grant_id: str,
                         org_id: Optional[str]) -> dict[str, Any]:
    """Put the run back in the queue as `<builder> for <person>` under that grant: the job row
    carries the grant (identity.runs.original_build_authority prefers a job with one)."""
    from ..identity.lineage import Acting
    from .jobs import enqueue_job

    acting = Acting(principal_id=person, org_id=str(org_id or ""), grant_id=grant_id,
                    run_id=f"run_ct:{run['id']}")
    connection.execute(text(
        "UPDATE code_task_runs SET status = 'running', phase = 'queued', updated_at = now() "
        "WHERE id = :id"), {"id": run["id"]})
    return enqueue_job(connection, run["project_id"], "code.task", {"run_id": run["id"]},
                       task_id=run.get("task_id"), priority=JOB_PRIORITY_INTERACTIVE,
                       on_behalf_of=acting)


def _builder_name(connection: Any, scope: Mapping[str, Any]) -> str:
    agents = list(scope.get("agents") or [])
    if agents:
        name = connection.execute(text(
            "SELECT display_name FROM identity_principals WHERE id = :i"), {"i": agents[0]}).scalar()
        if name:
            return str(name)
    keys = list(scope.get("role_keys") or [])
    return keys[0].replace("_", " ").capitalize() if keys else "the builder"


def _open_card(connection: Any, *, project_id: str, agent_key: str,
               action: str = ACTION_GRANT_BUILDER) -> Optional[dict[str, Any]]:
    """The one open card already asking for this builder's grant (or, with the review switch on,
    asking to review its always-on builds) in this project, if any."""
    row = connection.execute(text(
        "SELECT id, recommendation FROM decisions WHERE project_id = :p AND status = "
        "ANY(:open_statuses) AND recommendation->>'action' = :a "
        "AND recommendation->>'agent_key' = :k ORDER BY created_at LIMIT 1"),
        {"p": project_id, "a": action, "k": agent_key,
         "open_statuses": sorted(OPEN_STATUSES)}).mappings().first()
    return dict(row) if row else None


def open_thread_and_ask(
    connection: Any,
    *,
    project_id: str,
    run_id: str,
    recipient_principal_id: Optional[str],
    scope: dict[str, Any],
    question: str,
    external_ref: Optional[str] = None,
) -> str:
    """Opens (or reuses) the Captain thread for this ask and posts the one plain-language ask,
    carrying a deep link to the run's own console screen."""
    from ..app_links.console import build_console_link

    link = build_console_link("code_tasks", project_id=project_id, build=run_id)
    ref = external_ref or run_id
    existing = connection.execute(text(
        "SELECT id FROM agent_threads WHERE project_id = :project_id AND surface = 'agent' "
        "AND external_ref = :external_ref ORDER BY updated_at DESC LIMIT 1"),
        {"project_id": project_id, "external_ref": ref},
    ).scalar()
    if existing:
        return str(existing)
    thread_id = make_id("thr")
    connection.execute(text(
        "INSERT INTO agent_threads (id, project_id, actor, surface, external_ref, agent_role) "
        "VALUES (:id, :project_id, :actor, 'agent', :external_ref, 'person_wait')"),
        {"id": thread_id, "project_id": project_id,
         "actor": recipient_principal_id or "ceo", "external_ref": ref},
    )
    connection.execute(text(
        "INSERT INTO agent_messages (id, thread_id, role, agent_role, content) "
        "VALUES (:id, :thread_id, 'assistant', 'person_wait', :content)"),
        {"id": make_id("msg"), "thread_id": thread_id, "content": f"{question} {link}"},
    )
    return thread_id


def _captain_verdict(connection: Any, run_id: str, project_id: Optional[str]) -> Any:
    """The project's Captain authority's answer for this build (identity/captain_authority), or
    None when it cannot be read (never widened: the person's card asks instead)."""
    from ..identity import captain_authority

    try:
        with connection.begin_nested():
            return captain_authority.evaluate(connection, run_id=run_id, project_id=project_id)
    except Exception:  # noqa: BLE001
        return None


def _enqueue_under_captain(connection: Any, *, run: Mapping[str, Any], verdict: Any) -> dict[str, Any]:
    """Back in the queue on the project's Captain authority: the job carries it (OBO its sponsor),
    and the run's lineage says so."""
    from ..identity import captain_authority

    out = _enqueue_under_grant(connection, run=run, person=str(verdict.sponsor), grant_id=verdict.grant_id,
                               org_id=verdict.org_id)
    captain_authority.record_use(connection, verdict, run_id=run["id"])
    return out


#: The note a grant card closes with when the Captain authority started its builds.
CAPTAIN_NOTE = ("Not needed any more: always-on builds in this project start on the project's Captain "
                "authority (no person's grant needed), so BOSS started them. Merges and production "
                "deploys still come to you.")


def _withdraw_if_done(connection: Any, decision_id: Optional[str], *, note: str) -> bool:
    """Close a grant card nobody needs to answer: none of its builds still waits on it."""
    if not decision_id:
        return False
    waiting = connection.execute(text(
        "SELECT count(*) FROM code_task_runs WHERE status = :w AND (decision_id = :d OR "
        "finalize_ctx->'person_wait'->>'decision_id' = :d)"),
        {"w": STATUS_WAITING_ON_PERSON, "d": decision_id}).scalar()
    if waiting:
        return False
    changed = connection.execute(text(
        "UPDATE decisions SET status = 'withdrawn', resolution_note = :n, updated_at = now() "
        "WHERE id = :d AND status = ANY(:open) AND recommendation->>'action' = :a"),
        {"n": note[:1000], "d": decision_id, "open": sorted(OPEN_STATUSES),
         "a": ACTION_GRANT_BUILDER}).rowcount
    if changed:
        project_id = connection.execute(text("SELECT project_id FROM decisions WHERE id = :d"),
                                        {"d": decision_id}).scalar()
        emit_event(connection, project_id, "decision.withdrawn", "boss", "decision", decision_id,
                   {"why": note})
    return bool(changed)


def _chain_reasons() -> frozenset[str]:
    from .org_chain import PERSON_WAIT_CHAIN_REASONS

    return PERSON_WAIT_CHAIN_REASONS


def _offer_to_chain(connection: Any, *, run_id: str, project_id: str, task_id: Optional[str],
                    reason: str, question: str, blocker_type: str,
                    refused_grant_id: Optional[str], scope: Mapping[str, Any]
                    ) -> Optional[dict[str, Any]]:
    """BOSSAPP-885: offer the stalled run to its agent's manager before a person. None when the
    org chart has no manager agent above it (or the chain already had this run): the caller's
    person path runs unchanged."""
    from . import org_chain

    offer = org_chain.escalate_first(
        connection, None, project_id=project_id, origin="person_wait", run_id=run_id,
        reason=reason, question=question,
        fallback={"task_id": task_id, "reason": reason, "question": question,
                  "blocker_type": blocker_type, "refused_grant_id": refused_grant_id})
    if offer is None:
        return None
    opened_at = now_utc().isoformat()
    ctx = {"scope": scope, "decision_id": None, "blocker_type": blocker_type, "reason": reason,
           "opened_at": opened_at, "ask": question, "refused_grant_id": refused_grant_id,
           "chain_escalation_id": offer["id"], "offered_to": offer["offered_to"]}
    connection.execute(text(
        "UPDATE code_task_runs SET status = :status, phase = :phase, "
        "finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || CAST(:person_wait AS jsonb), "
        "updated_at = now() WHERE id = :id"),
        {"status": STATUS_WAITING_ON_PERSON, "phase": org_chain.PHASE_AWAITING_MANAGER,
         "person_wait": json.dumps({"person_wait": _jsonable(ctx)}), "id": run_id})
    emit_event(connection, project_id, "code_task.waiting_on_manager", "boss", "code_task_run",
               run_id, {"escalation_id": offer["id"], "offered_to": offer["offered_to"],
                        "reason": reason})
    return {"status": STATUS_WAITING_ON_PERSON, "phase": org_chain.PHASE_AWAITING_MANAGER,
            "blocker_type": blocker_type, "decision_id": None, "escalation_id": offer["id"],
            "offered_to": offer["offered_to"], "scope": dict(scope), "opened_at": opened_at}


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
    refused_grant_id: Optional[str] = None,
    chain: bool = True,
) -> dict[str, Any]:
    """Pauses a run on a person -- or, when that person's grant ALREADY covers it, puts it straight
    back in the queue under that grant (nothing to ask). Pausing preserves workspace/branch/stage,
    releases any held budget reservation, and joins (or raises) the ONE card for this builder in
    this project, so four squad builds refused for the same grant are one ask, not four.

    BOSSAPP-885: a STALL (org_chain.PERSON_WAIT_CHAIN_REASONS) whose agent has a manager agent on
    the org chart is offered to that manager first: the run pauses in `awaiting_manager` and no
    person is asked; org_chain.poll_due falls through to this same function (`chain=False`) when
    the chain is exhausted. No manager agent declared → exactly the path below, unchanged."""
    scope = grant_scope_for_run(connection, {"run_id": run_id, "project_id": project_id})
    recipient = recipient_for_run(connection, {"run_id": run_id, "project_id": project_id})
    run = _load_run(connection, run_id) or {"id": run_id, "project_id": project_id,
                                            "task_id": task_id}

    captain = _captain_verdict(connection, run_id, project_id)
    if captain is not None and captain.covered and not captain.review_pending \
            and captain.grant_id != refused_grant_id:
        # THE PROJECT'S CAPTAIN AUTHORITY covers it (an always-on build within its class's level):
        # back in the queue on it, nothing to ask.
        _enqueue_under_captain(connection, run=run, verdict=captain)
        emit_event(connection, project_id, "code_task.authority_found", "boss", "code_task_run",
                   run_id, {"grant_id": captain.grant_id, "for": captain.sponsor,
                            "basis": "captain_authority",
                            "reason": "the project's Captain authority covers this always-on build"})
        return {"status": "running", "resumed": True, "grant_id": captain.grant_id,
                "basis": "captain_authority", "blocker_type": blocker_type, "scope": scope}
    review = captain is not None and captain.covered and captain.review_pending \
        and captain.grant_id != refused_grant_id
    if review:
        blocker_type = BLOCKER_ALWAYS_ON_REVIEW

    grant = _covering_grant(connection, person=recipient, scope=scope, project_id=project_id,
                            run_id=run_id)
    if grant is not None and grant.id != refused_grant_id and not review:
        _enqueue_under_grant(connection, run=run, person=str(recipient), grant_id=grant.id,
                             org_id=_org_of_project(connection, project_id))
        emit_event(connection, project_id, "code_task.authority_found", "boss", "code_task_run",
                   run_id, {"grant_id": grant.id, "for": recipient,
                            "reason": "a standing grant already covers this build"})
        return {"status": "running", "resumed": True, "grant_id": grant.id,
                "blocker_type": blocker_type, "scope": scope}

    budget.release(run_id=run_id)

    if chain and not review and reason in _chain_reasons():
        held = _offer_to_chain(connection, run_id=run_id, project_id=project_id, task_id=task_id,
                               reason=reason, question=question, blocker_type=blocker_type,
                               refused_grant_id=refused_grant_id, scope=scope)
        if held is not None:
            return held

    agent_key = (scope.get("agents") or scope.get("role_keys") or ["builder"])[0]
    builder = _builder_name(connection, scope)
    action = ACTION_GRANT_BUILDER
    if review:
        action = ACTION_REVIEW_BUILD
        title = str((run or {}).get("title") or "an always-on build")
        ask = (f"Review before it starts: {builder} wants to build \"{title[:120]}\" for an "
               "always-on squad. Approve to let it start on the project's Captain authority (it "
               "opens a PR; merging still needs you), or reject to drop it. You asked for this "
               "review in Project ▸ Settings ▸ Autonomy & budget.")
    elif blocker_type == BLOCKER_AUTHORITY_NO_GRANT:
        ask = (f"Let {builder} build for you in this project: give it a standing grant under "
               "My account ▸ Agents acting for me. Its builds are paused until then and resume "
               "by themselves once the grant exists.")
    else:
        ask = question
    card = _open_card(connection, project_id=project_id, agent_key=agent_key, action=action)
    if card:
        decision_id = card["id"]
        connection.execute(text(
            "UPDATE decisions SET recommendation = jsonb_set(recommendation, '{run_ids}', "
            "COALESCE(recommendation->'run_ids', '[]'::jsonb) || CAST(:r AS jsonb)), "
            "updated_at = now() WHERE id = :id AND NOT (COALESCE(recommendation->'run_ids', "
            "'[]'::jsonb) @> CAST(:r AS jsonb))"),
            {"r": json.dumps([run_id]), "id": decision_id})
    else:
        decision_id = create_blocking_decision(
            connection, project_id, task_id, ask,
            {"action": action, "run_id": run_id, "run_ids": [run_id],
             "blocker_type": blocker_type, "agent_key": agent_key, "builder": builder,
             "link": review_setting_link(project_id) if review else GRANT_PAGE, "why": question,
             **({"basis": "captain_authority", "work_item_id": captain.work_item_id,
                 "decision": captain.act_id} if review else {})},
            {}, reason, default_action="hold",
        )

    thread_id = open_thread_and_ask(
        connection, project_id=project_id, run_id=run_id, recipient_principal_id=recipient,
        scope=scope, question=ask, external_ref=decision_id,
    )

    opened_at = now_utc().isoformat()
    person_wait_ctx = {
        "scope": scope, "decision_id": decision_id, "thread_id": thread_id,
        "blocker_type": blocker_type, "reason": reason, "opened_at": opened_at,
        "recipient_principal_id": recipient, "ask": ask, "builder": builder,
        "refused_grant_id": refused_grant_id,
    }
    connection.execute(text(
        "UPDATE code_task_runs SET status = :status, phase = :phase, decision_id = :decision_id, "
        "finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || CAST(:person_wait AS jsonb), "
        "updated_at = now() WHERE id = :id"),
        {"status": STATUS_WAITING_ON_PERSON, "phase": PHASE_AWAITING_GRANT,
         "decision_id": decision_id,
         "person_wait": json.dumps({"person_wait": _jsonable(person_wait_ctx)}), "id": run_id},
    )
    emit_event(connection, project_id, "code_task.waiting_on_person", "boss", "code_task_run",
               run_id, {"decision_id": decision_id, "blocker_type": blocker_type,
                        "builder": builder, "ask": ask})
    return {
        "status": STATUS_WAITING_ON_PERSON, "blocker_type": blocker_type,
        "decision_id": decision_id, "thread_id": thread_id, "scope": scope,
        "opened_at": opened_at,
    }


def scan_reply_for_secrets(text_in: str) -> tuple[str, bool]:
    """Blocks a PAT/token-shaped reply before storage or echo; never a silent drop."""
    if _SECRET_PATTERN.search(text_in):
        return _REDACTED_NOTICE, True
    return text_in, False


def post_reply(connection: Any, *, thread_id: str, run_id: str, actor: str,
               text: str) -> dict[str, Any]:  # noqa: A002 — the caller's keyword
    """Scans before it ever reaches storage, then appends to the Captain thread."""
    from sqlalchemy import text as _sql

    safe_text, blocked = scan_reply_for_secrets(text)
    connection.execute(_sql(
        "INSERT INTO agent_messages (id, thread_id, role, agent_role, content) "
        "VALUES (:id, :thread_id, 'user', :agent_role, :content)"),
        {"id": make_id("msg"), "thread_id": thread_id, "agent_role": actor, "content": safe_text},
    )
    return {"text": safe_text, "blocked": blocked}


def verify_and_resume(connection: Any, settings: Any, jobs: Any = None, *,
                      run_id: str) -> dict[str, Any]:
    """Re-checks the blocker with data (not the person's word) and resumes the run exactly once:
    status back to running and one code.task job carrying the covering grant."""
    run = _load_run(connection, run_id)
    if not run or run.get("status") != STATUS_WAITING_ON_PERSON:
        return {"resumed": False}
    person_wait_ctx = _as_dict(run.get("finalize_ctx")).get("person_wait") or {}
    captain = _resume_on_captain(connection, settings, {**run, "id": run.get("id") or run_id},
                                 person_wait_ctx)
    if captain is not None:
        return captain
    scope = person_wait_ctx.get("scope") or {}
    if not scope.get("agents"):
        # Recorded before the builder had a Passport: read the scope again now.
        scope = grant_scope_for_run(connection, run)
    person = person_wait_ctx.get("recipient_principal_id") or recipient_for_run(connection, run)
    grant = _covering_grant(connection, person=person, scope=scope,
                            project_id=run.get("project_id"), run_id=run_id)
    if grant is None:
        return {"resumed": False}

    # Claim the resume: only the caller that flips waiting -> running enqueues.
    claimed = connection.execute(text(
        "UPDATE code_task_runs SET status = 'running', phase = 'queued', updated_at = now() "
        "WHERE id = :id AND status = :waiting"),
        {"id": run_id, "waiting": STATUS_WAITING_ON_PERSON}).rowcount
    if not claimed:
        return {"resumed": False}
    _enqueue_under_grant(connection, run=run, person=str(person), grant_id=grant.id,
                         org_id=_org_of_project(connection, run.get("project_id")))

    decision_id = run.get("decision_id") or person_wait_ctx.get("decision_id")
    if decision_id:
        try:
            with connection.begin_nested():
                DecisionService(settings).transition(
                    connection, decision_id, action="approve", actor=SYSTEM_ACTOR,
                    note="The grant now covers this build; BOSS resumed it.", system_policy=True)
        except Exception:  # noqa: BLE001 — another run on the same card already closed it
            pass
    emit_event(connection, run["project_id"], "code_task.resumed_after_person", "boss",
               "code_task_run", run_id, {"grant_id": grant.id, "for": person})
    record_resolution(
        connection, run_id=run_id, project_id=run.get("project_id"),
        blocker_type=person_wait_ctx.get("blocker_type", BLOCKER_AUTHORITY_NO_GRANT),
        resolution="person", opened_at=person_wait_ctx.get("opened_at"),
    )
    return {"resumed": True, "grant_id": grant.id}


def _resume_on_captain(connection: Any, settings: Any, run: Mapping[str, Any],
                       person_wait_ctx: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The waiting build is an always-on squad's, and the project's Captain authority now covers it
    (the default; or the review card a person approved): resume it on that authority, exactly once,
    and close a grant card nobody needs to answer any more with a plain note. A review a person
    rejected drops the build. None: the Captain authority does not apply here."""
    from ..identity import captain_authority

    run_id = run["id"]
    declined = None
    try:
        with connection.begin_nested():
            declined = captain_authority.review_declined(connection, run_id)
    except Exception:  # noqa: BLE001
        declined = None
    if declined is not None:
        stopped = connection.execute(text(
            "UPDATE code_task_runs SET status = 'cancelled', phase = 'done', error = :e, updated_at = now() "
            "WHERE id = :id AND status = :waiting"),
            {"id": run_id, "waiting": STATUS_WAITING_ON_PERSON,
             "e": f"{declined['approved_by'] or 'A person'} declined this always-on build on review."}).rowcount
        if stopped:
            emit_event(connection, run["project_id"], "code_task.review_declined", "boss", "code_task_run",
                       run_id, {"decision_id": declined["id"]})
            record_resolution(connection, run_id=run_id, project_id=run.get("project_id"),
                              blocker_type=BLOCKER_ALWAYS_ON_REVIEW, resolution="declined",
                              opened_at=person_wait_ctx.get("opened_at"))
        return {"resumed": False, "declined": bool(stopped)}
    verdict = _captain_verdict(connection, run_id, run.get("project_id"))
    if verdict is None or not verdict.covered or verdict.review_pending \
            or verdict.grant_id == person_wait_ctx.get("refused_grant_id"):
        return None
    claimed = connection.execute(text(
        "UPDATE code_task_runs SET status = 'running', phase = 'queued', updated_at = now() "
        "WHERE id = :id AND status = :waiting"),
        {"id": run_id, "waiting": STATUS_WAITING_ON_PERSON}).rowcount
    if not claimed:
        return {"resumed": False}
    _enqueue_under_captain(connection, run=run, verdict=verdict)
    decision_id = run.get("decision_id") or person_wait_ctx.get("decision_id")
    _withdraw_if_done(connection, decision_id, note=CAPTAIN_NOTE)
    emit_event(connection, run["project_id"], "code_task.resumed_after_person", "boss",
               "code_task_run", run_id, {"grant_id": verdict.grant_id, "for": verdict.sponsor,
                                         "basis": "captain_authority",
                                         "reviewed_by": verdict.reviewed_by})
    record_resolution(
        connection, run_id=run_id, project_id=run.get("project_id"),
        blocker_type=person_wait_ctx.get("blocker_type", BLOCKER_AUTHORITY_NO_GRANT),
        resolution="person_review" if verdict.reviewed_by else "captain_authority",
        opened_at=person_wait_ctx.get("opened_at"),
    )
    return {"resumed": True, "grant_id": verdict.grant_id, "basis": "captain_authority"}


def record_resolution(
    connection: Any,
    *,
    run_id: str,
    project_id: Optional[str],
    blocker_type: str,
    resolution: str,
    opened_at: Optional[str],
) -> None:
    """One history row: blocker_type, resolution, and time-to-unblock. Also attempts one
    learning-layer write, swallowed on failure (mirrors `run_supervisor.write_lesson`)."""
    resolved_at = now_utc().isoformat()
    entry = {"blocker_type": blocker_type, "resolution": resolution,
             "opened_at": opened_at, "resolved_at": resolved_at}
    connection.execute(text(
        "UPDATE code_task_runs SET finalize_ctx = jsonb_set("
        "COALESCE(finalize_ctx, '{}'::jsonb), '{person_wait_history}', "
        "COALESCE(finalize_ctx->'person_wait_history', '[]'::jsonb) || CAST(:entry AS jsonb)) "
        "WHERE id = :id"),
        {"entry": json.dumps([entry]), "id": run_id},
    )
    try:
        from .knowledge import learning_writeback

        with connection.begin_nested():
            learning_writeback.record(
                connection, project_id=project_id, learning_kind="decision",
                title=f"run {run_id}: {blocker_type} resolved by {resolution}",
                statement=f"blocker {blocker_type} cleared via {resolution}",
                detail={"blocker_type": blocker_type, "resolution": resolution},
                about={"areas": ["person_wait"], "symbols": [], "rules": []},
                evidence=[], confidence=0.5,
                observed={"run_id": run_id, "opened_at": opened_at, "resolved_at": resolved_at},
            )
    except Exception:  # noqa: BLE001 — learning is best-effort; the resume already happened
        pass


def poll_due(connection: Any, settings: Any, *, now: Optional[Any] = None) -> int:
    """Re-checks every run currently waiting on a person; returns the number resumed.
    An archived project's runs are never re-checked -- it takes no new work."""
    from . import projects_archive

    rows = connection.execute(
        text("SELECT id AS run_id FROM code_task_runs WHERE status = :status AND "
             + projects_archive.live_sql("project_id")),
        {"status": STATUS_WAITING_ON_PERSON},
    ).mappings().all()
    resumed = 0
    for row in rows:
        try:
            with connection.begin_nested():
                result = verify_and_resume(connection, settings, None, run_id=row["run_id"])
        except Exception:  # noqa: BLE001 — one run's bad row never stops the others resuming
            continue
        if result.get("resumed"):
            resumed += 1
    return resumed
