from __future__ import annotations

import json

import re
import threading
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from . import decision_actions
from .core import create_artifact, emit_event, now_utc
from .infrastructure import InfrastructureService
from .intake import CommandIntakeService
from .jobs import JOB_PRIORITY_URGENT, JobLeaseService
from .members import MemberDirectoryService
from .role_governance import RoleGovernanceService
from .self_improvement import SelfImprovementService
from .specpack import AgentModuleNotFoundError, SpecPackService
from .jobs import JOB_PRIORITY_INTERACTIVE


class DecisionNotFoundError(Exception):
    pass


class InvalidDecisionTransitionError(Exception):
    pass


class ForceNeedsANote(InvalidDecisionTransitionError, ValueError):
    """A forced merge without its reason. Still a ValueError for the callers that caught it."""


#: The actor recorded when a policy, not a person, resolves a gate. Deliberately not a member
#: of any org: it must be impossible to sign in as, and obvious in an audit trail.
SYSTEM_ACTOR = "auto_approval"


class DecisionService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)
        self.jobs = JobLeaseService(settings)
        self.infrastructure = InfrastructureService(settings)
        self.intake = CommandIntakeService(settings)
        self.self_improvement = SelfImprovementService(settings)
        self.role_governance = RoleGovernanceService(settings)
        self.specpack = SpecPackService(settings)
        self.members = MemberDirectoryService(settings)

    def expire_overdue(
        self, connection: Connection, project_id: Optional[str] = None
    ) -> int:
        params: dict[str, object] = {"now": now_utc()}
        project_filter = ""
        if project_id:
            params["project_id"] = project_id
            project_filter = "AND project_id = :project_id"
        self.hold_plan_gates(connection, project_id)

        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, default_action, deadline_at
                FROM decisions
                WHERE status IN ('pending', 'delegated')
                  AND deadline_at IS NOT NULL
                  AND deadline_at <= :now
                  {project_filter}
                """
            ),
            params,
        ).mappings().all()

        expired = 0
        for row in rows:
            updated = connection.execute(
                text(
                    """
                    UPDATE decisions
                    SET status = 'expired', updated_at = :updated_at
                    WHERE id = :decision_id AND status IN ('pending', 'delegated')
                    """
                ),
                {
                    "decision_id": row["id"],
                    "updated_at": now_utc(),
                },
            )
            if updated.rowcount != 1:
                continue

            expired += 1
            emit_event(
                connection,
                row["project_id"],
                "decision.expired",
                "system",
                "decision",
                row["id"],
                {
                    "deadline_at": row["deadline_at"].isoformat(),
                    "default_action": row["default_action"] or "hold",
                    # "hold" means DO NOTHING, so nothing was applied. Reporting
                    # `safe_default_applied: hold` read as "handled safely" when the truth is
                    # "nobody answered and the work is waiting" — and 77% of decisions on the
                    # live project took this path.
                    "safe_default_applied": (
                        None if (row["default_action"] or "hold") == "hold"
                        else row["default_action"]
                    ),
                    "still_answerable": (row["default_action"] or "hold") == "hold",
                    "note": (
                        "Nobody answered before the deadline. The safe default is 'hold', so "
                        "nothing was applied and this decision can still be answered."
                        if (row["default_action"] or "hold") == "hold"
                        else f"Safe default {row['default_action']!r} was applied."
                    ),
                },
            )
        return expired

    def hold_plan_gates(self, connection: Connection, project_id: Optional[str] = None) -> list[str]:
        """A BUILD WAITING AT ITS PLAN WAITS FOR A PERSON, SO ITS QUESTION STAYS OPEN.

        A plan approval (recommendation.action = resume_code_task) was a hold-default decision
        with a 24h deadline. It expired, dropped out of Needs action, the pill and the Command
        Center, while the build sat at "Needs you · plan" for weeks: the one thing that moves
        it was hidden everywhere but the Factory. Every plan question whose build is still
        blocked at awaiting_plan_approval is kept pending with no deadline; one that already
        expired is reopened (`decision.reopened`). Driven from the blocked builds — a handful
        of rows — so it is cheap enough to run on every read that expires decisions.
        Returns the ids reopened."""
        scope = "AND c.project_id = :project_id" if project_id else ""
        reopened = connection.execute(
            text(
                f"""
                WITH held AS (
                  SELECT d.id, d.project_id, d.status AS was
                  FROM code_task_runs c
                  JOIN decisions d ON d.id = c.decision_id
                  WHERE c.status = 'blocked' AND c.phase = 'awaiting_plan_approval'
                    {scope}
                    AND d.recommendation->>'action' = 'resume_code_task'
                    AND (d.status = 'expired'
                         OR (d.status IN ('pending', 'delegated') AND d.deadline_at IS NOT NULL))
                  FOR UPDATE OF d
                )
                UPDATE decisions d
                SET status = CASE WHEN held.was = 'expired' THEN 'pending' ELSE d.status END,
                    deadline_at = NULL, updated_at = :now
                FROM held
                WHERE d.id = held.id
                RETURNING d.id, d.project_id, held.was
                """
            ),
            {"now": now_utc(), **({"project_id": project_id} if project_id else {})},
        ).mappings().all()
        out = []
        for row in reopened:
            if row["was"] != "expired":
                continue
            out.append(str(row["id"]))
            emit_event(connection, row["project_id"], "decision.reopened", "system", "decision",
                       row["id"], {"why": "Its build is still waiting at the plan for a person.",
                                   "was": "expired"})
        return out

    def create_digest(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        max_items: int,
        low_risk_max: int,
        urgent_risk_min: int,
    ) -> dict[str, Any]:
        self.expire_overdue(connection, project_id)
        # What the digest lists is what the Approvals inbox calls Needs action
        # (decision_actions.attention). It was pending and delegated, so a pull request stuck
        # in a dead-end revision_requested was never in a digest (review round 3).
        rows = decision_actions.waiting_on_operator(connection, project_id)[:max_items]
        items = [self._digest_item(dict(row)) for row in rows]
        urgent_items = [
            item for item in items if int(item["risk_tier"]) >= urgent_risk_min
        ]
        batched_items = [
            item for item in items if int(item["risk_tier"]) <= low_risk_max
        ]
        standard_items = [
            item
            for item in items
            if item["id"]
            not in {entry["id"] for entry in [*urgent_items, *batched_items]}
        ]
        content = {
            "project_id": project_id,
            "generated_by": actor,
            "generated_at": now_utc().isoformat(),
            "policy": {
                "max_items": max_items,
                "low_risk_max": low_risk_max,
                "urgent_risk_min": urgent_risk_min,
            },
            "urgent": urgent_items,
            "standard": standard_items,
            "batched_low_risk": batched_items,
        }
        body = self._digest_body(content)
        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "decision_digest",
            "Decision digest",
            {**content, "body": body},
        )
        emit_event(
            connection,
            project_id,
            "decision.digest_created",
            actor,
            "artifact",
            artifact_id,
            {
                "decision_count": len(items),
                "urgent_count": len(urgent_items),
                "batched_count": len(batched_items),
            },
        )
        return {
            "status": "created",
            "artifact_id": artifact_id,
            "decision_count": len(items),
            "urgent_count": len(urgent_items),
            "batched_count": len(batched_items),
            "body": body,
            "content": content,
        }

    def get_row(
        self, connection: Connection, decision_id: str, *, for_update: bool = False
    ) -> dict:
        lock_clause = "FOR UPDATE" if for_update else ""
        row = connection.execute(
            text(
                f"""
                SELECT id, project_id, task_id, question, status, risk_tier, default_action,
                       recommendation, options, cost_summary, deadline_at,
                       approved_by, approved_at, delegated_to, delegated_by,
                       delegated_at, revision_requested_by, revision_requested_at,
                       executed_by, executed_at, resolution_note, external_ref, via_delegate, via_delegation,
                       created_at, updated_at
                FROM decisions
                WHERE id = :decision_id
                {lock_clause}
                """
            ),
            {"decision_id": decision_id},
        ).mappings().first()
        if not row:
            raise DecisionNotFoundError("Decision not found")
        return dict(row)

    def _digest_item(self, row: dict[str, Any]) -> dict[str, Any]:
        recommendation = row.get("recommendation") or {}
        action = recommendation.get("action") if isinstance(recommendation, dict) else None
        return {
            "id": row["id"],
            "task_id": row.get("task_id"),
            "question": row["question"],
            "status": row["status"],
            "risk_tier": int(row["risk_tier"]),
            "default_action": row.get("default_action") or "hold",
            "recommendation_action": action or "review_required",
            "deadline_at": row["deadline_at"].isoformat()
            if row.get("deadline_at")
            else None,
            "created_at": row["created_at"].isoformat(),
            "cost_summary": row.get("cost_summary") or {},
        }

    def _digest_body(self, content: dict[str, Any]) -> str:
        lines = [
            "Decision Digest",
            f"Generated: {content['generated_at']}",
            "",
        ]
        sections = [
            ("Urgent", content["urgent"]),
            ("Standard", content["standard"]),
            ("Low-risk batch", content["batched_low_risk"]),
        ]
        for title, items in sections:
            lines.append(f"{title}:")
            if not items:
                lines.append("- None")
            for item in items:
                deadline = item.get("deadline_at") or "no deadline"
                lines.append(
                    "- "
                    f"{item['id']} / tier {item['risk_tier']} / "
                    f"{item['recommendation_action']} / default {item['default_action']} / "
                    f"{deadline}: {item['question']}"
                )
            lines.append("")
        return "\n".join(lines).strip()

    def transition(
        self,
        connection: Connection,
        decision_id: str,
        *,
        action: str,
        actor: str,
        note: Optional[str],
        delegate_to: Optional[str] = None,
        external_ref: Optional[str] = None,
        force: bool = False,
        system_policy: bool = False,
        content_hash: Optional[str] = None,
    ) -> dict:
        if action not in {"approve", "reject", "revise", "delegate", "execute"}:
            raise ValueError(f"Unsupported action: {action}")
        if force and not (note or "").strip():
            # A force merge overrides BOSS's own safety verdict. The reason is the ONLY
            # record of why, so it is mandatory — an unexplained override is indistinguishable
            # from a bug six months later. A refusal (409 in words), never a 500: a merge with
            # checks failing (checks_failing.py) is sent exactly this way.
            raise ForceNeedsANote(
                "force requires a note: state why BOSS's merge gate is being overridden.")

        row = self.get_row(connection, decision_id, for_update=True)
        current_status = row["status"]
        timestamp = now_utc()
        # AN APPROVAL DELEGATE (identity/approval_delegations.py, IDENTITY.md §6.15) answers FOR a
        # person: its grant, scope, cap and the hard limits are checked here, before anything
        # else, and from here on the actor is the PERSON (approved_by records them; via_delegate
        # names the delegate). Any other caller is untouched.
        delegated = None
        if not system_policy:
            _early_ctx = _identity_decides(connection, row)
            if _early_ctx is not None:
                from ..identity import approval_delegations as _approval_delegations

                if _approval_delegations.is_delegate_context(connection, _early_ctx):
                    delegated = _approval_delegations.authorize(connection, _early_ctx, row,
                                                                action=action)
                    actor = delegated.person_actor
        # A policy resolving a gate is not a person acting, and member validation asks the
        # wrong question of it: SYSTEM_ACTOR is deliberately not an org member, so requiring
        # membership would have silently disabled every policy-driven approval.
        #
        # Narrow on purpose. Only an approval, only under the reserved actor name, only when
        # the caller states it is policy — anything else still needs a member with
        # decision_resolve. The row records SYSTEM_ACTOR either way, so an auditor can always
        # separate the approvals a person made from the ones a setting made.
        # AN ARCHITECTURE IS APPROVED BY A PERSON, NEVER BY AN AGENT OR A POLICY — refused here,
        # before any check that might record part of the approval (architecture_docs).
        if action == "approve":
            from .architecture_docs import ArchitectureApprovalRefused, require_a_person

            try:
                require_a_person(connection, row, actor, system_policy=system_policy)
            except ArchitectureApprovalRefused as refused:
                raise InvalidDecisionTransitionError(str(refused)) from refused
            # A BOSS SELF-FIX INSTALL IS APPROVED BY A PERSON (BOSSAPP-571 R5): never a policy,
            # an agent or a system account — refused before anything is recorded.
            from .self_fix_install import InstallRefused, refuse_policy_approval

            try:
                refuse_policy_approval(row, system_policy=system_policy, actor=actor)
            except InstallRefused as refused:
                raise InvalidDecisionTransitionError(str(refused)) from refused
        identity_ctx = None if system_policy else _identity_decides(connection, row)
        if system_policy and action == "approve" and actor == SYSTEM_ACTOR:
            pass
        elif identity_ctx is not None:
            # IDENTITY §6.13: the route's/tool's declaration already checked the permission; the
            # seat, the human session and the two-person rule are checked at the approve itself,
            # below — after the buttons/run checks, so a refusal there is never half-applied.
            if action == "delegate":
                from ..identity import approvals as _approvals

                _approvals.require_delegate_target(connection, identity_ctx, delegate_to)
        else:
            self.members.require_decision_actor(
                connection,
                row["project_id"],
                actor,
                action=action,
                risk_tier=row["risk_tier"],
                delegate_to=delegate_to,
                delegated_to=row.get("delegated_to"),
            )

        # THE BUTTONS THE CONSOLE DRAWS ARE THE ONLY ONES THAT WORK. A budget refusal approved
        # from the chat agent, or a workflow gate sent back for "changes" the runner never reads,
        # recorded an answer nothing acted on. See decision_actions for the contract; a policy
        # approval is exempt because it is not a person choosing a button. After the
        # member check, so somebody not allowed to decide at all is told that first.
        if not system_policy:
            try:
                decision_actions.require_allowed(row, action)
            except decision_actions.DecisionActionNotAllowed as refused:
                raise InvalidDecisionTransitionError(str(refused)) from refused

        # A QUESTION WHOSE RUN IS GONE IS NOT ANSWERED. A gate of a stopped or removed run sat
        # in Approvals with Approve on it (boss-1, 2026-10-04); approving would have released a
        # step of a run that no longer exists. Withdrawn decisions, and any whose run ended by
        # a path that did not withdraw them, are refused here — for a policy approval too.
        if action != "execute":
            from .run_decisions import refusal_to_answer

            gone = refusal_to_answer(connection, row)
            if gone:
                raise InvalidDecisionTransitionError(gone)

        if action in {"approve", "reject", "revise", "delegate"}:
            # A DEADLINE NOBODY MET MUST NOT ALSO DESTROY THE WORK.
            #
            # Measured on the live project: 127 of 165 decisions (77%) expired, 22 were
            # approved — and EVERY one of the 165 had `default_action: hold`.
            #
            # "Hold" means do nothing. So expiring a hold-default decision does not apply a
            # safe default; it removes the operator's ability to answer and strands the run
            # forever, while the refusal below claims "the safe default was applied". Nothing
            # was applied. That message was false and the outcome was silent data loss.
            #
            # So expiry stays as a REPORTING state — it is true and useful to record that
            # nobody answered in time — but for a hold default it no longer locks the
            # decision. Answering reopens it and the work proceeds.
            #
            # Deliberately narrow: a decision whose default is anything OTHER than hold really
            # did apply something on expiry, and reversing that silently would be a different
            # and worse bug. Those still refuse.
            safe_default = str(row.get("default_action") or "hold").lower()
            reopenable = safe_default == "hold"

            # A DEAD-END "REQUEST CHANGES" IS REOPENED TOO. Before decision_actions, every pull
            # request and general card offered "Request changes", and nothing reads that status
            # for them: a PR sent back that way could never be approved and merged from BOSS.
            # Those rows exist on real installs. For a type whose follow-on DOES read the note
            # (self-improvement, autonomy changes) the status still means what it says.
            stuck_revision = (current_status == "revision_requested"
                              and not decision_actions.consumes_revision(row))
            if current_status not in {"pending", "delegated"}:
                if not ((current_status == "expired" and reopenable) or stuck_revision):
                    raise InvalidDecisionTransitionError(
                        f"Decision is already {current_status} and cannot be {action}d."
                    )
                connection.execute(
                    text(
                        """
                        UPDATE decisions SET status = 'pending', updated_at = :ts
                        WHERE id = :decision_id AND status = :was
                        """
                    ),
                    {"decision_id": decision_id, "ts": timestamp, "was": current_status},
                )
                emit_event(
                    connection, row["project_id"], "decision.reopened", actor,
                    "decision", decision_id,
                    {"was": current_status, "default_action": safe_default,
                     "why": ("changes were requested on a decision nothing reads them for, "
                             "so it is answered again" if stuck_revision else
                             "answered after its deadline; a hold default applied nothing, "
                             "so there is nothing to reverse")},
                )
                current_status = "pending"

            deadline_at = row["deadline_at"]
            if deadline_at and deadline_at <= timestamp and not reopenable:
                self.expire_overdue(connection, row["project_id"])
                raise InvalidDecisionTransitionError(
                    f"Decision deadline has passed and the safe default "
                    f"({safe_default}) was applied."
                )

        if action == "approve":
            # CI GATES (services/ci_gate.py): a build that changed .github/workflows/ needs a
            # PERSON — never a policy, an agent, an API key or a run token — and a deploy (or an
            # unforced merge) waits for CI to pass. Before anything is recorded.
            from . import ci_gate

            try:
                ci_gate.refuse_approval(connection, row, actor, identity_ctx=identity_ctx,
                                        system_policy=system_policy, force=force,
                                        delegated=delegated)
            except ci_gate.CiGateRefused as refused:
                raise InvalidDecisionTransitionError(refused.message) from refused
            # A RELEASE CARD IS APPROVED AS SHOWN (release_scope): only with the hash of the
            # commit list it showed, and — when that list carries commits from rejected or
            # unreviewed runs — only with the person's stated reason (force + note, as a merge
            # with checks failing). Before anything is recorded.
            from . import release_scope

            try:
                release_scope.refuse_approval(row.get("recommendation"),
                                              content_hash=content_hash, force=force, note=note)
            except release_scope.ReleaseApprovalRefused as refused:
                raise InvalidDecisionTransitionError(str(refused)) from refused
            if force and release_scope.needs_reason(row.get("recommendation")):
                _card = release_scope.of_decision(row.get("recommendation")) or {}
                emit_event(connection, row["project_id"], release_scope.FORCED_EVENT, actor,
                           "decision", row["id"],
                           {"reason": (note or "").strip()[:500],
                            "head_sha": _card.get("head_sha"),
                            "rejected_runs": _card.get("rejected_runs") or [],
                            "unreviewed_runs": _card.get("unreviewed_runs") or [],
                            "unattributed": _card.get("unattributed") or 0})
            # AN APPROVAL OF WHAT THE APPROVER SAW. A gated step's decision carries a preview of
            # what it will send and a hash of that content; the console sends back the hash it
            # showed. If the preview has moved on since (an upstream step was re-run), this
            # approval was of other content and is refused. The hash that WAS approved is
            # recorded, and the runner refuses to run the step on anything else.
            # C-3: checked BEFORE require_approval, so a stale first-of-two approval is never
            # recorded; each recorded approval carries the hash it approved.
            recommendation = row.get("recommendation")
            if isinstance(recommendation, str):
                try:
                    recommendation = json.loads(recommendation)
                except ValueError:
                    recommendation = None
            preview = (recommendation or {}).get("action_preview") \
                if isinstance(recommendation, dict) else None
            current_hash = (preview or {}).get("content_hash") if isinstance(preview, dict) \
                else None
            if content_hash and current_hash and content_hash != current_hash:
                raise InvalidDecisionTransitionError(
                    "What this step would send changed while you were reviewing it, so this "
                    "approval was not recorded. Reload, read the updated preview, and approve "
                    "again if it is still right.")
            if identity_ctx is not None:
                from ..identity import approvals as _approvals

                _approvals.require_approval(connection, identity_ctx, row, content_hash=current_hash,
                                            delegated=delegated)
            connection.execute(
                text(
                    """
                    UPDATE decisions
                    SET status = 'approved',
                        approved_by = :actor,
                        approved_at = :timestamp,
                        resolution_note = :note,
                        recommendation = CASE WHEN CAST(:approved_hash AS TEXT) IS NULL
                            THEN recommendation
                            ELSE COALESCE(recommendation, '{}'::jsonb)
                                 || jsonb_build_object('approved_content_hash',
                                                       CAST(:approved_hash AS TEXT)) END,
                        updated_at = :timestamp
                    WHERE id = :decision_id
                    """
                ),
                {
                    "decision_id": decision_id,
                    "actor": actor,
                    "timestamp": timestamp,
                    "note": note,
                    "approved_hash": current_hash,
                },
            )
            emit_event(
                connection,
                row["project_id"],
                "decision.approved",
                actor,
                "decision",
                decision_id,
                {
                    "from_status": current_status,
                    "to_status": "approved",
                    "note": note,
                    "default_action": row["default_action"] or "hold",
                    # Activity says "by <delegate>, for <person>" (approval_delegations).
                    **(_delegate_words(delegated) if delegated is not None else {}),
                },
            )
            if delegated is not None:
                from ..identity import approval_delegations as _approval_delegations

                _approval_delegations.record_use(connection, identity_ctx, delegated, row,
                                                 action="approve", note=note)
            refreshed = self.get_row(connection, decision_id)
            self._handle_approved_follow_on(
                connection, refreshed, actor, note=note, force=force
            )
            # The person's approval is signed now, under the step it releases — not a tick later.
            from . import agent_acts

            agent_acts.quietly(connection, "sign an approval", agent_acts.on_decision, decision_id)
            return self.get_row(connection, decision_id)

        if action == "reject":
            connection.execute(
                text(
                    """
                    UPDATE decisions
                    SET status = 'rejected',
                        resolution_note = :note,
                        updated_at = :timestamp
                    WHERE id = :decision_id
                    """
                ),
                {
                    "decision_id": decision_id,
                    "note": note,
                    "timestamp": timestamp,
                },
            )
            emit_event(
                connection,
                row["project_id"],
                "decision.rejected",
                actor,
                "decision",
                decision_id,
                {
                    "from_status": current_status,
                    "to_status": "rejected",
                    "note": note,
                    "default_action": row["default_action"] or "hold",
                    # Activity says "by <delegate>, for <person>" (approval_delegations).
                    **(_delegate_words(delegated) if delegated is not None else {}),
                },
            )
            if delegated is not None:
                from ..identity import approval_delegations as _approval_delegations

                _approval_delegations.record_use(connection, identity_ctx, delegated, row,
                                                 action="reject", note=note)
            refreshed = self.get_row(connection, decision_id)
            self._handle_non_approved_follow_on(
                connection, refreshed, action="reject", actor=actor, note=note
            )
            self._handle_rejected_follow_on(connection, refreshed, actor=actor)
            # A gate answered no FAILS ITS STEP now (agents-home D3): the run stops there with
            # Retry and Skip, and its acts say so at once (workflows.refuse_gates_of).
            from . import agent_acts, workflows as workflow_runtime

            with connection.begin_nested():
                workflow_runtime.refuse_gates_of(connection, decision_id)

            agent_acts.quietly(connection, "close a refused gate", agent_acts.on_decision,
                               decision_id)
            return self.get_row(connection, decision_id)

        if action == "revise":
            if not note or not note.strip():
                raise ValueError("note is required when requesting revision")
            connection.execute(
                text(
                    """
                    UPDATE decisions
                    SET status = 'revision_requested',
                        revision_requested_by = :actor,
                        revision_requested_at = :timestamp,
                        resolution_note = :note,
                        updated_at = :timestamp
                    WHERE id = :decision_id
                    """
                ),
                {
                    "decision_id": decision_id,
                    "actor": actor,
                    "timestamp": timestamp,
                    "note": note.strip(),
                },
            )
            emit_event(
                connection,
                row["project_id"],
                "decision.revision_requested",
                actor,
                "decision",
                decision_id,
                {
                    "from_status": current_status,
                    "to_status": "revision_requested",
                    "note": note.strip(),
                },
            )
            refreshed = self.get_row(connection, decision_id)
            self._handle_non_approved_follow_on(
                connection, refreshed, action="revise", actor=actor, note=note
            )
            return self.get_row(connection, decision_id)

        if action == "delegate":
            if not delegate_to or not delegate_to.strip():
                raise ValueError("delegate_to is required when delegating a decision")
            delegate_target = delegate_to.strip()
            connection.execute(
                text(
                    """
                    UPDATE decisions
                    SET status = 'delegated',
                        delegated_to = :delegate_to,
                        delegated_by = :actor,
                        delegated_at = :timestamp,
                        resolution_note = :note,
                        updated_at = :timestamp
                    WHERE id = :decision_id
                    """
                ),
                {
                    "decision_id": decision_id,
                    "delegate_to": delegate_target,
                    "actor": actor,
                    "timestamp": timestamp,
                    "note": note,
                },
            )
            emit_event(
                connection,
                row["project_id"],
                "decision.delegated",
                actor,
                "decision",
                decision_id,
                {
                    "from_status": current_status,
                    "to_status": "delegated",
                    "delegate_to": delegate_target,
                    "note": note,
                },
            )
            return self.get_row(connection, decision_id)

        if current_status != "approved":
            raise InvalidDecisionTransitionError(
                f"Decision is {current_status} and cannot be executed."
            )
        if not (note and note.strip()) and not (external_ref and external_ref.strip()):
            raise ValueError(
                "note or external_ref is required when executing a decision manually"
            )

        execution_note = note.strip() if isinstance(note, str) and note.strip() else None
        external_reference = (
            external_ref.strip()
            if isinstance(external_ref, str) and external_ref.strip()
            else None
        )
        execution_artifact_id = create_artifact(
            connection,
            self.settings,
            row["project_id"],
            row["task_id"],
            "manual_execution_record",
            f"Manual execution record for {row['question'][:80]}",
            {
                "decision_id": decision_id,
                "question": row["question"],
                "executed_by": actor,
                "executed_at": timestamp.isoformat(),
                "note": execution_note,
                "external_ref": external_reference,
            },
        )
        connection.execute(
            text(
                """
                UPDATE decisions
                SET status = 'executed',
                    executed_by = :actor,
                    executed_at = :timestamp,
                    resolution_note = :note,
                    external_ref = :external_ref,
                    updated_at = :timestamp
                WHERE id = :decision_id
                """
            ),
            {
                "decision_id": decision_id,
                "actor": actor,
                "timestamp": timestamp,
                "note": execution_note,
                "external_ref": external_reference,
            },
        )
        emit_event(
            connection,
            row["project_id"],
            "decision.executed",
            actor,
            "decision",
            decision_id,
            {
                "from_status": current_status,
                "to_status": "executed",
                "artifact_id": execution_artifact_id,
                "external_ref": external_reference,
            },
        )
        if row["task_id"]:
            updated = connection.execute(
                text(
                    """
                    UPDATE tasks
                    SET status = 'completed',
                        updated_at = :timestamp
                    WHERE id = :task_id AND status <> 'completed'
                    """
                ),
                {"task_id": row["task_id"], "timestamp": timestamp},
            )
            if updated.rowcount == 1:
                emit_event(
                    connection,
                    row["project_id"],
                    "task.completed",
                    actor,
                    "task",
                    row["task_id"],
                    {"decision_id": decision_id},
                )
        return self.get_row(connection, decision_id)

    def _handle_approved_follow_on(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        *,
        note: Optional[str] = None,
        force: bool = False,
    ) -> None:
        """Approval context must be explicitly threaded into the follow-on.

        A forced merge is valid only with its operator rationale recorded in the
        audit event. Defaulting to a non-forced, empty-note context keeps the
        safe behaviour for callers that do not supply either value."""
        recommendation = row["recommendation"] or {}
        action = recommendation.get("action")
        if action == "approve_change_set":
            # ONE APPROVAL FOR A MULTI-REPO CHANGE (multirepo/change_sets.py): pins the exact head
            # SHAs it covered and queues the merge of the whole set. Refused (and the approval not
            # recorded) when a PR moved since the question was asked.
            from ..multirepo import change_sets
            from ..multirepo.common import MultiRepoError

            try:
                change_sets.on_decision(connection, row, status="approved", actor=actor, jobs=self.jobs)
            except MultiRepoError as refused:
                raise InvalidDecisionTransitionError(refused.message) from refused
            return
        if action == "execute_agent_tool":
            # A high-risk agent tool call that was deferred for approval — run it now.
            from .agent_loop import execute_agent_tool

            execute_agent_tool(
                connection,
                row["project_id"],
                self.settings,
                str(recommendation.get("tool")),
                recommendation.get("args") or {},
                actor,
                task_id=row["task_id"],
            )
            # Meeting PRD publish → auto-start a governed code task so the
            # approved outcome advances into coding (previously a dead-end).
            self._start_code_task_after_prd(connection, row, actor, recommendation)
        elif action == "resume_agent_run":
            # A durable agent run paused for approval — resume it via a worker job
            # so the conversation continues with the approved tool's result.
            run_id = recommendation.get("run_id")
            self.jobs.create_job(
                connection,
                project_id=row["project_id"],
                task_id=row["task_id"],
                kind="agent.resume",
                priority=45,
                payload={"run_id": run_id},
            )
            emit_event(
                connection,
                row["project_id"],
                "agent.resume_queued",
                actor,
                "agent_run",
                str(run_id),
                {"decision_id": row["id"]},
            )
        elif action == "resume_code_task":
            # A governed code task paused for human plan approval — resume it via a worker job.
            # The runner reuses the persisted design and continues (write tests -> implement -> PR).
            run_id = recommendation.get("run_id")
            # Flip the paused run back to running so the re-enqueued code.task job proceeds
            # (the runner reuses the persisted design and skips the design phase + gate).
            connection.execute(
                text("UPDATE code_task_runs SET status='running', updated_at=now() WHERE id=:id"),
                {"id": run_id},
            )
            self.jobs.create_job(
                connection,
                project_id=row["project_id"],
                task_id=row["task_id"],
                kind="code.task",
                # A person clicked approve and is watching. This used to be a bare 46, the
                # REQUESTED band — behind other batch work, for a run whose operator is
                # sitting in front of it. Same inversion as I6, one layer further in.
                priority=JOB_PRIORITY_INTERACTIVE,
                payload={"run_id": run_id},
            )
            emit_event(
                connection,
                row["project_id"],
                "code_task.resume_queued",
                actor,
                "code_task_run",
                str(run_id),
                {"decision_id": row["id"]},
            )
        elif action == "review_plan_draft":
            self._queue_pr_proposal_from_plan(connection, row, actor, recommendation)
        elif action == "github.community.dispatch_plan":
            plan_id = recommendation.get("plan_id") or recommendation.get(
                "proposed_plan_id"
            )
            if not isinstance(plan_id, str) or not plan_id:
                emit_event(
                    connection,
                    row["project_id"],
                    "decision.follow_on_skipped",
                    actor,
                    "decision",
                    row["id"],
                    {"reason": "missing_community_plan_id"},
                )
                return
            result = self.intake.dispatch_plan(
                connection,
                plan_id,
                project_id=row["project_id"],
                actor=actor,
            )
            emit_event(
                connection,
                row["project_id"],
                "github.community_plan_authorized",
                actor,
                "decision",
                row["id"],
                {"plan_id": plan_id, "phase_task_ids": result["phase_task_ids"]},
            )
        elif action == "github.community.merge_authorization":
            # Approval records authorization only. Community webhook work never
            # calls merge; a maintainer must still use the governed merge path.
            emit_event(
                connection,
                row["project_id"],
                "github.community_merge_authorized",
                actor,
                "decision",
                row["id"],
                {
                    "repository": recommendation.get("github_repository"),
                    "pull_request_number": recommendation.get("github_pr_number"),
                    "head_sha": recommendation.get("head_sha"),
                },
            )
        elif action == "review_pull_request":
            # OPERATOR FORCE MERGE. Automated merging stays conservative — it refuses when
            # BOSS's own gate found a real problem. But the operator can still say "merge it
            # anyway", and BOSS performs it so the action stays inside the governed path with
            # a receipt, instead of the human reaching for `gh pr merge` outside the system.
            # The override is recorded with WHO, WHY and exactly which verdict was overridden.
            ci_gate = None if force else self._pull_request_ci_gate(connection, row)
            if force:
                _overridden = self._pull_request_ci_gate(connection, row)
                emit_event(
                    connection, row["project_id"], "decision.merge_forced", actor,
                    "decision", row["id"],
                    {"reason": (note or "").strip()[:500],
                     "overridden_status": (_overridden or {}).get("status", "none"),
                     "overridden_reason": (_overridden or {}).get("reason", ""),
                     "pull_request_url": recommendation.get("pull_request_url"),
                     "note": "operator override — BOSS's automated merge gate did not pass"},
                )
                from . import checks_failing as _checks_failing

                _failing = _checks_failing.of_decision(row)
                if _failing is not None:
                    # A PERSON DECIDED TO MERGE IT WITH ITS CHECKS FAILING: who, why, and
                    # exactly which checks and whether security was among them.
                    emit_event(
                        connection, row["project_id"], _checks_failing.MERGE_EVENT, actor,
                        "decision", row["id"],
                        {"reason": (note or "").strip()[:500],
                         "failing_gates": _checks_failing.gate_names(_failing),
                         "security": bool(_failing.get("security")),
                         "pull_request_url": recommendation.get("pull_request_url")},
                    )
            if ci_gate is not None:
                self._create_merge_gate_block(
                    connection,
                    row,
                    actor,
                    recommendation,
                    status=ci_gate["status"],
                    reason=ci_gate["reason"],
                    next_action=ci_gate["next_action"],
                    event_reason=ci_gate["event_reason"],
                    ci_validation_status=ci_gate["validation_status"],
                    ci_validation_summary=ci_gate["validation_summary"],
                )
                return
            if self.settings.enable_sandbox_merge or force:
                self._queue_merge_job_from_review(
                    connection, row, actor, recommendation
                )
            else:
                self._create_merge_gate_block(connection, row, actor, recommendation)
        elif action == "review_deployment":
            if self.settings.enable_sandbox_deploy:
                self._queue_deployment_job_from_review(
                    connection, row, actor, recommendation
                )
            else:
                self._create_deploy_gate_block(connection, row, actor, recommendation)
        elif action == "review_infra_provisioning":
            if self.settings.infra_real_provision_allowed:
                self._queue_infra_job_from_review(
                    connection, row, actor, recommendation
                )
            else:
                self._create_infra_gate_block(connection, row, actor, recommendation)
        elif action in {"self_improvement.begin_canary", "self_improvement.rollout"}:
            self.self_improvement.apply_decision_outcome(
                connection, row, "approve", actor, row.get("resolution_note")
            )
        elif action == "role.autonomy_change":
            self.role_governance.apply_decision_outcome(
                connection, row, "approve", actor, row.get("resolution_note")
            )
        elif action == "self_fix.install":
            # A person approved a BOSS self-fix: it is approved and its signed, single-use
            # install ticket issued for the host self-installer (services/self_fix_install.py).
            # Refused (the approval not recorded) when BOSS's stack moved since it was checked.
            from .self_fix_install import InstallRefused, on_approved as approve_self_fix

            try:
                approve_self_fix(connection, self.settings, row, actor)
            except InstallRefused as refused:
                raise InvalidDecisionTransitionError(str(refused)) from refused
        elif action == "self_fix.remove":
            # A person approved taking installed self-fixes off this one-box: the signed
            # `remove` / `remove_all` ticket for the host self-installer.
            from .self_fix_install import InstallRefused, on_remove_approved

            try:
                on_remove_approved(connection, self.settings, row, actor)
            except InstallRefused as refused:
                raise InvalidDecisionTransitionError(str(refused)) from refused
        elif action == "deploy_stack":
            # A stack deployment held for approval: approving it approves its ChangeSet and
            # queues the apply as a durable worker job (services/stack_deploy.py).
            from .stack_deploy import on_decision_approved

            on_decision_approved(connection, self.settings, row, actor)
        elif action == "infra_operation":
            # A destroy/restore of a provisioned resource held for a person (platform/
            # operations.py): approving it queues the leased `infra.operation` job.
            from ..platform.operations import on_decision_approved as approve_infra_operation

            approve_infra_operation(connection, self.settings, row, actor)
        elif action == "review_architecture":
            # A person approved an architecture: recorded on its review, which the waiting
            # workflow step reads at the next tick.
            from .architecture_docs import on_decision_answered

            on_decision_answered(connection, row, action="approve", actor=actor, note=note)
        elif action == "deploy_host":
            # A host deployment held for approval: a person's approval approves its ChangeSet
            # and queues the apply as a durable worker job (services/governed_host_deploy.py).
            from .governed_host_deploy import on_decision_approved as approve_host

            approve_host(connection, self.settings, row, actor)
        elif action in ("architecture_reconciliation", "typed_decision_reconciliation"):
            # A paid call whose outcome was never settled. Approving settles it: a reply the
            # receipt holds is recorded (usage and cost) and replayed by the step, never paid
            # for again; with no reply the receipt is marked failed. Either way the stopped
            # step is put back in the queue. Refused (approval not recorded) when another
            # worker took the call (typed_decision_spend.on_reconciliation_approved).
            from .typed_decision_spend import ReconciliationRefused, on_reconciliation_approved

            try:
                on_reconciliation_approved(connection, self.settings, row, actor)
            except ReconciliationRefused as refused:
                raise InvalidDecisionTransitionError(str(refused)) from refused

    def _handle_rejected_follow_on(
        self, connection: Connection, row: dict[str, Any], *, actor: str
    ) -> None:
        """What REFUSING releases. Distinct from the revise path: nothing here reads a note.

        A rejected stack deployment closes its ChangeSet and hands back its reservation, so
        the console stops showing it as waiting for somebody.
        """
        rec = row["recommendation"] or {}
        if rec.get("action") == "deploy_stack":
            from .stack_deploy import on_decision_not_approved

            on_decision_not_approved(connection, self.settings, row, actor, "rejected")
        elif rec.get("action") == "resume_code_task" and rec.get("run_id"):
            # A PLAN SOMEBODY SAID NO TO ENDS ITS BUILD (independent re-check): it stayed
            # "Needs you · plan" in the Factory and on the shelf after Reject. The build is
            # stopped the way a person's Stop stops it, and says the plan was rejected.
            from .code_task_service import CancelRefused, cancel_run

            try:
                with connection.begin_nested():
                    cancel_run(connection, str(rec["run_id"]), str(row["project_id"]), actor=actor,
                               reason="The plan was rejected")
                    connection.execute(text(
                        "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) "
                        "|| jsonb_build_object('plan_rejected', true) WHERE id = :id"),
                        {"id": str(rec["run_id"])})
            except (CancelRefused, LookupError):
                pass
        elif (row["recommendation"] or {}).get("action") == "deploy_host":
            from .governed_host_deploy import on_decision_not_approved as refuse_host

            refuse_host(connection, self.settings, row, actor, "rejected")

    def _handle_non_approved_follow_on(
        self,
        connection: Connection,
        row: dict[str, Any],
        *,
        action: str,
        actor: str,
        note: Optional[str],
    ) -> None:
        recommendation = row["recommendation"] or {}
        recommendation_action = recommendation.get("action")
        if recommendation_action == "approve_change_set":
            # Send back: every PR of the set gets a fix build from the note, and the set is asked
            # again. Reject: the set stops; nothing merges, nothing deploys.
            from ..multirepo import change_sets
            from ..multirepo.common import MultiRepoError

            try:
                change_sets.on_decision(connection, row, actor=actor, jobs=self.jobs, note=note,
                                        status="revision_requested" if action == "revise" else "rejected")
            except MultiRepoError as refused:
                raise InvalidDecisionTransitionError(refused.message) from refused
        elif recommendation_action in {
            "self_improvement.begin_canary",
            "self_improvement.rollout",
        }:
            self.self_improvement.apply_decision_outcome(
                connection, row, action, actor, note
            )
        elif recommendation_action == "role.autonomy_change":
            self.role_governance.apply_decision_outcome(
                connection, row, action, actor, note
            )
        elif recommendation_action == "self_fix.install":
            # Request changes: one more repair round with the note, then a new card. Reject: the
            # self-fix ends and its recommendation reopens with the person's note.
            from .self_fix_install import on_rejected, on_revise

            if action == "revise":
                on_revise(connection, row, actor, note)
            else:
                on_rejected(connection, row, actor, note)
        elif recommendation_action == "review_architecture":
            # Changes requested (the architect revises from the note) or rejected (the step
            # fails and the run stops) — architecture_docs.
            from .architecture_docs import on_decision_answered

            on_decision_answered(connection, row, action=action, actor=actor, note=note)

    def _start_code_task_after_prd(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
    ) -> None:
        """After a meeting's PRD publish decision is approved, auto-start a
        governed code task seeded from the PRD so the task advances into coding
        (the meeting flow otherwise dead-ends at 'PRD published'). Idempotent —
        emits a visible event on start, skip-duplicate, or no-repo."""
        args = recommendation.get("args") or {}
        is_prd_publish = recommendation.get("tool") == "publish_document" and (
            bool(recommendation.get("start_code_task"))
            or args.get("repo_subdir") == "docs/prd"
        )
        task_id = row.get("task_id")
        if not is_prd_publish or not task_id:
            return
        # A WORKFLOW THAT CONVENED THIS PANEL BUILDS ITS SPEC ITSELF (spec_gate). Starting one
        # here as well was a second build of the same spec, racing the first on the same branch
        # (dec_eb911c56 / wfr_723213d8). The approval attaches to the run: its Build step starts
        # once the panel's step sees the approval, or is already running if it did not wait.
        from . import spec_gate

        governed = spec_gate.workflow_build_for_prd(connection, str(row["id"]), str(task_id))
        if governed:
            build = governed.get("build")
            if build:
                emit_event(connection, row["project_id"], "code_task.already_started", actor,
                           "code_task_run", build["id"],
                           {"decision_id": row["id"], "task_id": task_id,
                            "workflow_run_id": governed["workflow_run_id"],
                            "reason": "the_workflow_builds_it"})
            else:
                emit_event(connection, row["project_id"], "code_task.not_started", actor,
                           "task", task_id,
                           {"decision_id": row["id"], "reason": "the_workflow_builds_it",
                            "workflow_run_id": governed["workflow_run_id"],
                            "detail": "The workflow that convened this panel builds the "
                                      "approved spec in its own Build step."})
            return
        # WHICH APP THIS PANEL SPECCED A FEATURE FOR, if any — structured first, the old text
        # marker only for rows written before it was structured (see extend_run_of_prd).
        extend_run_id = extend_run_of_prd(connection, row, recommendation)
        if extend_run_id:
            # The operator chose "convene a panel, then build this onto that app" in one act.
            # `auto_code_task_after_prd` exists to stop a whole multi-feature PRD being dumped
            # into one code task by default; it is not a reason to drop an explicit request to
            # build onto a named app, so this path does not consult it.
            self._build_prd_onto_app(connection, row, actor, recommendation, extend_run_id)
            return
        # Publishing the PRD IS the deliverable. Auto-dumping a whole multi-feature PRD (or a
        # discovery task's write-up) into a single governed code task mis-routes research/spec work
        # into codegen — the pipeline then authors a sprawling, unfocused codebase that fails the
        # quality gate. Default OFF: stop at 'published' and let a human create scoped coding tasks.
        if not getattr(self.settings, "auto_code_task_after_prd", False):
            emit_event(
                connection,
                row["project_id"],
                "code_task.not_started",
                actor,
                "task",
                task_id,
                {"decision_id": row["id"],
                 "reason": "auto_codegen_after_prd_disabled",
                 "detail": "PRD published. Create scoped coding tasks explicitly rather than "
                           "auto-implementing the whole PRD."},
            )
            return
        # Idempotency: never double-start a code task for the same task.
        existing = connection.execute(
            text(
                "SELECT id FROM code_task_runs "
                "WHERE project_id = :p AND task_id = :t "
                "AND status NOT IN ('failed', 'cancelled') "
                "LIMIT 1"
            ),
            {"p": row["project_id"], "t": task_id},
        ).first()
        if existing is not None:
            emit_event(
                connection,
                row["project_id"],
                "code_task.already_started",
                actor,
                "code_task_run",
                existing[0],
                {"decision_id": row["id"], "task_id": task_id},
            )
            return
        # WHERE IT IS BUILT is the project's connected repository, resolved the way
        # create_code_task resolves it — the environment's single repo only when the project
        # has none registered. Reading the env alone meant a project with a connected repo and
        # no BOSS_TARGET_GITHUB_* set approved its PRD and nothing was ever built.
        from .repo_registry import RepositoryResolutionError, resolve_repository_target

        resolution_error = ""
        try:
            target = resolve_repository_target(connection, self.settings, row["project_id"])
            owner, repo = target.get("owner"), target.get("repo")
        except RepositoryResolutionError as error:
            owner = repo = None
            resolution_error = str(error)
        if not owner or not repo:
            emit_event(
                connection,
                row["project_id"],
                "code_task.not_started",
                actor,
                "task",
                task_id,
                {"decision_id": row["id"], "reason": "no_target_repo_configured",
                 **({"detail": resolution_error[:400]} if resolution_error else {})},
            )
            return
        raw_title = str(args.get("title") or "Code task")
        title = raw_title[5:].strip() if raw_title.startswith("PRD: ") else raw_title
        prd_markdown = args.get("markdown") or ""
        directive = prd_markdown or f"Implement the approved plan: {title}"

        # The beginning of this outcome, recorded so the build can be drawn as the continuation
        # of the panel that specced it rather than as an unrelated run.
        # The console addresses a panel by its RUN id (?meeting=mrun_...), not by the task the
        # panel was about. Recording only task_id meant Spec resolved to the meetings list --
        # a link that works, lands somewhere real, and is still the wrong page.
        meeting_run_id = None
        try:
            from sqlalchemy import text as _text
            meeting_run_id = connection.execute(_text(
                "SELECT id FROM meeting_runs WHERE task_id = :t ORDER BY created_at DESC LIMIT 1"
            ), {"t": task_id}).scalar()
        except Exception:  # noqa: BLE001 — a missing back-link must not stop the build
            meeting_run_id = None

        # LINEAGE IS A CHAIN; ORIGIN RECORDED ONE HOP.
        #
        # The real path is Discovery -> task -> PRD meeting -> code task. Promoting a discovery
        # outcome stores `discovery_run_id` in the TASK's inputs, and this dict — built at the
        # last hop — dropped it. So a build that genuinely started from a research dossier
        # rendered "Research: none" on the journey spine, and the operator had no way to see
        # that the panel they approved came from anything.
        #
        # The task is the join, and it is already in hand. Read one more level.
        discovery_run_id = None
        try:
            from sqlalchemy import text as _text
            raw_inputs = connection.execute(_text(
                "SELECT inputs FROM tasks WHERE id = :t"
            ), {"t": task_id}).scalar()
            if isinstance(raw_inputs, str):
                import json as _json
                raw_inputs = _json.loads(raw_inputs)
            if isinstance(raw_inputs, dict):
                discovery_run_id = raw_inputs.get("discovery_run_id")
        except Exception:  # noqa: BLE001 — a missing back-link must not stop the build
            discovery_run_id = None

        origin = {
            "kind": "meeting",
            "task_id": task_id,
            "meeting_run_id": meeting_run_id,
            "decision_id": row["id"],
            "title": title,
            # Present only when this work really did start from research. The spine reads the
            # FIELD rather than `kind`, because kind names the nearest hop and can only ever
            # describe one of them.
            **({"discovery_run_id": discovery_run_id} if discovery_run_id else {}),
        }

        # Turn the approved PRD into a build. Never dump a whole multi-feature PRD into ONE giant
        # governed task (mis-routes -> sprawling code -> gate fail). Two proven paths:
        #  - WEB APP  -> start_web_app_build: scaffold + ONE focused task -> one PR + one approval.
        #                This is the reliable, end-to-end-verified path for a website (no fragile
        #                multi-slice chain), so prefer it whenever the PRD looks like a web app.
        #  - OTHERWISE -> start_decomposed_build: scoped, chained slices for genuinely complex work.
        # Both make an LLM call, so run OFF the approve request thread (mirrors start_discovery).
        project_id = row["project_id"]
        settings = self.settings
        jobs = self.jobs
        origin_ctx = origin

        # THE RUN THAT ALREADY GOVERNS THIS WORK.
        #
        # Two workflows for one app, observed: "Interview Scorecard App" ran as
        # wfr_de0ff (Standard Product Delivery v2, 10 steps) — research, panel, spec approved —
        # and the approval of its OWN spec_review gate launched the build, which then opened a
        # SECOND outer run, wfr_666b (v1, 8 steps), that adopted the very code task it had just
        # created. Both then sat waiting on crun_33a0a77e6ccf4d17b36e, and the operator was
        # shown two projects where they had started one.
        #
        # `start_code_task` guards against exactly this — `_already_governed` — but the guard
        # reads `finalize_ctx["workflow_run_id"]`, and nothing on the approval path set it. The
        # tool path (`create_code_task`) passes it; this path, which is how a project born from
        # a panel actually starts, never has.
        #
        # The link was there to be read the whole time: a gate step records its decision in
        # `workflow_step_runs.decision_id`. One query turns "this approval" into "the run this
        # approval belongs to".
        # THE DECISION LINK IS NOT WRITTEN YET WHEN THIS RUNS. Measured on the duplicate that
        # got through after the first version of this fix:
        #
        #     05:53:02  decision approved      <- this code runs, finds nothing
        #     05:53:23  code task created      <- opens a SECOND workflow run
        #     05:53:26  step.decision_id set   <- the link appears, three seconds too late
        #
        # A spec_review gate ADOPTS a decision the meeting already created, so at approval time
        # the workflow has not claimed it. The meeting HAS been claimed — the run's own meeting
        # step produced it — and that link exists well before anyone approves anything.
        #
        # Decision first because it is exact; meeting second because it is available earlier.
        governing_run_id = None
        meeting_id = (origin_ctx or {}).get("meeting_run_id")
        for sql_text, params in (
            ("SELECT workflow_run_id FROM workflow_step_runs "
             "WHERE decision_id = :d ORDER BY created_at DESC LIMIT 1",
             {"d": row["id"]}),
            # Live runs only: a finished run is not governing anything, and attaching this build
            # to it would hide the build rather than place it.
            ("SELECT s.workflow_run_id FROM workflow_step_runs s "
             "JOIN workflow_runs r ON r.id = s.workflow_run_id "
             "WHERE s.project_id = :p AND r.status IN ('running', 'waiting') "
             "  AND (s.output->>'meeting_run_id' = :m "
             "       OR s.output->'await'->>'id' = :m) "
             "ORDER BY s.created_at DESC LIMIT 1",
             {"p": row["project_id"], "m": meeting_id}),
        ):
            if params.get("m") is None and "meeting_run_id" in sql_text:
                continue
            try:
                governing_run_id = connection.execute(text(sql_text), params).scalar()
            except Exception:  # noqa: BLE001 — an unlinked decision still builds, as before
                governing_run_id = None
            if governing_run_id:
                break
        if governing_run_id:
            origin_ctx = {**(origin_ctx or {}), "workflow_run_id": str(governing_run_id)}

        def _launch_build() -> None:
            try:
                from ..db import get_engine
                from .prd_decomposition import (
                    _looks_like_web_app, start_decomposed_build, start_web_app_build,
                )

                engine = get_engine()
                if _looks_like_web_app(directive):
                    start_web_app_build(
                        engine, settings, jobs,
                        project_id=project_id, task_id=task_id, directive=directive,
                        owner=owner, repo=repo, review=False, origin=origin_ctx)
                else:
                    start_decomposed_build(
                        engine, settings, jobs,
                        project_id=project_id, task_id=task_id,
                        prd_or_directive=directive, owner=owner, repo=repo, base_branch=None,
                        origin=origin_ctx)
            except Exception:  # noqa: BLE001 — best-effort; failure is visible via run status
                return

        threading.Thread(target=_launch_build, daemon=True).start()
        emit_event(
            connection,
            row["project_id"],
            "code_task.started_from_prd",
            actor,
            "task",
            task_id,
            {"decision_id": row["id"], "task_id": task_id, "title": title,
             "mode": "decomposed_build"},
        )

    def _build_prd_onto_app(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
        extend_run_id: str,
    ) -> None:
        """An approved PRD for a FEATURE of an app that exists: that app's next build.

        The follow-up path already knows how to continue an app — its branch, its repository
        (inherited from the app's own run, never the environment's single repo), its lineage
        and app id — so the PRD is handed to it as the instruction. It makes no model call,
        so it runs on the approval's own transaction rather than a thread: the build exists
        the moment the approval does, or the approval says why it does not.
        """
        from .code_task_service import (
            is_simulated_run, load_code_task_run, start_followup_task, title_for_change,
        )

        project_id = row["project_id"]
        task_id = row.get("task_id")
        args = recommendation.get("args") or {}
        raw_title = str(args.get("title") or "")
        feature = raw_title[5:].strip() if raw_title.startswith("PRD: ") else raw_title
        prd = strip_extend_marker(str(args.get("markdown") or "")) or feature

        def _not_started(reason: str, **extra: Any) -> None:
            emit_event(connection, project_id, "code_task.not_started", actor, "task", task_id,
                       {"decision_id": row["id"], "reason": reason,
                        "extends_run_id": extend_run_id, **extra})

        already = connection.execute(text(
            "SELECT id FROM code_task_runs WHERE project_id = :p "
            "AND finalize_ctx->'origin'->>'decision_id' = :d "
            "AND status NOT IN ('failed', 'cancelled') LIMIT 1"
        ), {"p": project_id, "d": row["id"]}).scalar()
        if already:
            emit_event(connection, project_id, "code_task.already_started", actor,
                       "code_task_run", already, {"decision_id": row["id"], "task_id": task_id})
            return
        parent = load_code_task_run(connection, extend_run_id)
        if not parent or str(parent.get("project_id")) != str(project_id):
            _not_started("extended_app_not_in_project")
            return
        if is_simulated_run(parent):
            _not_started("extended_app_is_simulated")
            return
        meeting = connection.execute(text(
            "SELECT id, directive FROM meeting_runs WHERE task_id = :t ORDER BY created_at DESC LIMIT 1"
        ), {"t": task_id}).mappings().first()
        meeting_run_id = meeting["id"] if meeting else None
        # NAMED FOR THE FEATURE, not the panel's framing: the PRD's title is the panel's directive
        # ("Add a feature to "X" — an existing, working application…"), which made the build
        # "X: Add a feature to "X" — …". The operator's own ask is the "What is wanted:" line.
        feature = feature_asked(str((meeting or {}).get("directive") or "")) or feature_asked(feature) or feature
        origin = {"kind": "meeting", "task_id": task_id, "meeting_run_id": meeting_run_id,
                  "decision_id": row["id"], "title": feature}
        # A panel convened inside a workflow is governed by that run; one convened from the
        # composer is not, and the change is filed under no run rather than an old one.
        governing = connection.execute(text(
            "SELECT workflow_run_id FROM workflow_step_runs WHERE decision_id = :d "
            "ORDER BY created_at DESC LIMIT 1"), {"d": row["id"]}).scalar()
        try:
            started = start_followup_task(
                connection,
                parent_run_id=extend_run_id,
                instructions=(
                    "A panel specced this FEATURE for the app that already exists on this "
                    "branch. Extend it — do not re-scaffold, do not rebuild.\n\n" + prd),
                jobs=self.jobs,
                title=title_for_change(parent, feature),
                actor=actor,
                workflow_run_id=str(governing) if governing else None,
                finalize_extra={"origin": origin},
            )
        except ValueError as error:
            _not_started("followup_refused", detail=str(error)[:400])
            return
        emit_event(connection, project_id, "code_task.started_from_prd", actor, "task", task_id,
                   {"decision_id": row["id"], "task_id": task_id, "title": feature,
                    "mode": "extend_app", "extends_run_id": extend_run_id,
                    "run_id": started["run_id"]})

    def _boss_own_verdict(
        self, connection: Connection, row: dict[str, Any], recommendation: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """BOSS's OWN quality gate for the PR's run — the authoritative evidence.

        BOSS runs the test suite itself; that is a stronger signal than an external CI
        service, and it was never consulted here. The merge gate asked GitHub first and,
        when GitHub answered 403, blocked citing "ci_validation_incomplete" while holding
        far more damning evidence of its own: on PR #62 the run had already recorded
        existing_tests_intact=FAILED with the two pre-existing Sudoku test files it had
        edited. The operator saw a CI plumbing error instead of "this change weakens your
        tests".
        """
        branch = str(recommendation.get("branch_name") or "")
        root = branch.rsplit("/", 1)[-1] if branch.startswith("boss/code/") else ""
        if not root:
            return None
        gate = connection.execute(
            text(
                """
                SELECT q.gates_passed, q.gates_failed, q.code_task_run_id
                FROM quality_gates q
                JOIN code_task_runs r ON r.id = q.code_task_run_id
                WHERE q.project_id = :p
                  AND COALESCE(r.finalize_ctx->>'root_run_id', r.id) = :root
                ORDER BY q.created_at DESC
                LIMIT 1
                """
            ),
            {"p": row["project_id"], "root": root},
        ).mappings().first()
        if not gate:
            return None
        failed = gate["gates_failed"] or []
        if isinstance(failed, str):
            try:
                failed = json.loads(failed)
            except Exception:  # noqa: BLE001
                failed = []
        passed = gate["gates_passed"] or []
        if isinstance(passed, str):
            try:
                passed = json.loads(passed)
            except Exception:  # noqa: BLE001
                passed = []
        return {"passed": list(passed), "failed": list(failed),
                "run_id": gate["code_task_run_id"]}

    def _pull_request_ci_gate(
        self, connection: Connection, row: dict[str, Any]
    ) -> Optional[dict[str, str]]:
        recommendation = row["recommendation"] or {}

        # (1) BOSS'S OWN VERDICT FIRST. BOSS runs the suite itself; that outranks an external
        # CI signal and was previously never consulted here.
        own = self._boss_own_verdict(connection, row, recommendation)
        if own:
            # (3) HARD STOP: a change that edited or deleted PRE-EXISTING tests must never be
            # auto-merged. A green suite proves nothing if the guard tests were weakened, and
            # a merge into main is irreversible. never-block means "ship the PR and record the
            # dissent" -- which already happened -- NOT "merge damage into main".
            if "existing_tests_intact" in own["failed"]:
                return {
                    "status": "blocked_weakened_existing_tests",
                    "reason": ("This change modified or deleted PRE-EXISTING test files, so a "
                               "passing suite does not prove nothing broke. BOSS will not "
                               "auto-merge it."),
                    "next_action": ("Restore the original test files (or justify each edit) and "
                                    "re-run; the PR stays open in the meantime."),
                    "event_reason": "weakened_existing_tests",
                    "validation_status": "failed",
                    "validation_summary": ("BOSS gate existing_tests_intact FAILED for run "
                                           f"{own['run_id']}."),
                }

        artifact_id = recommendation.get("test_report_artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            return {
                "status": "blocked_ci_validation_missing",
                "reason": "No test report artifact is linked to this sandbox PR review.",
                "next_action": "Re-run or regenerate the proposal so BOSS can capture validation evidence before merge.",
                "event_reason": "missing_test_report",
                "validation_status": "missing",
                "validation_summary": "No test report artifact was attached to the review decision.",
            }

        artifact = connection.execute(
            text(
                """
                SELECT content
                FROM artifacts
                WHERE id = :artifact_id AND project_id = :project_id
                """
            ),
            {"artifact_id": artifact_id, "project_id": row["project_id"]},
        ).mappings().first()
        if not artifact:
            return {
                "status": "blocked_ci_validation_missing",
                "reason": "The linked sandbox test report artifact could not be found.",
                "next_action": "Re-run or regenerate the proposal so BOSS can capture validation evidence before merge.",
                "event_reason": "missing_test_report",
                "validation_status": "missing",
                "validation_summary": "The linked test report artifact was not found.",
            }

        content = artifact["content"] or {}
        validation_status = str(content.get("status") or "missing")
        validation_summary = str(
            content.get("summary")
            or content.get("notes")
            or "Validation evidence is incomplete."
        )
        if validation_status == "passed":
            return None
        if validation_status == "failed":
            return {
                "status": "blocked_ci_validation_failed",
                "reason": "Sandbox validation failed for this PR proposal.",
                "next_action": "Fix the failing checks and regenerate or update the proposal before merge approval.",
                "event_reason": "ci_validation_failed",
                "validation_status": validation_status,
                "validation_summary": validation_summary,
            }
        # (2) "COULD NOT OBTAIN" IS NOT "FAILED". A 403 on check-runs, or a repo with no CI at
        # all, is absence of evidence -- not evidence against. Blocking on it makes merging
        # impossible for any repo without CI (boss-sandbox has no .github/workflows) while BOSS
        # has already run the authoritative suite itself. Fall back to BOSS's own gate: if its
        # tests passed and it did not weaken the existing ones, proceed and record WHY. An
        # explicit CI failure (handled above) still blocks.
        if validation_status in ("unavailable", "missing", "incomplete") and own:
            _blocking = {"written_tests_pass", "nothing_broke", "existing_tests_intact"}
            _bad = sorted(_blocking.intersection(own["failed"]))
            if not _bad:
                emit_event(
                    connection, row["project_id"], "decision.ci_evidence_substituted",
                    "boss", "decision", row["id"],
                    {"external_status": validation_status,
                     "external_summary": validation_summary[:300],
                     "boss_gates_passed": own["passed"],
                     "reason": "external CI evidence unobtainable; BOSS ran the suite itself"},
                )
                return None
            return {
                "status": "blocked_boss_validation_failed",
                "reason": ("External CI evidence was unavailable AND BOSS's own checks did not "
                           f"pass: {_bad}."),
                "next_action": "Fix the failing BOSS gates and re-run before merge approval.",
                "event_reason": "boss_validation_failed",
                "validation_status": "failed",
                "validation_summary": f"BOSS gates failed: {_bad}",
            }
        return {
            "status": "blocked_ci_validation_incomplete",
            "reason": "GitHub validation has not completed successfully for this sandbox PR proposal.",
            "next_action": "Wait for the checks to finish successfully before merge approval.",
            "event_reason": "ci_validation_incomplete",
            "validation_status": validation_status,
            "validation_summary": validation_summary,
        }

    def _queue_pr_proposal_from_plan(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
    ) -> None:

        artifact_id = recommendation.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_plan_artifact_id"},
            )
            return

        artifact = connection.execute(
            text(
                """
                SELECT content
                FROM artifacts
                WHERE id = :artifact_id AND project_id = :project_id
                """
            ),
            {"artifact_id": artifact_id, "project_id": row["project_id"]},
        ).mappings().first()
        if not artifact:
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "plan_artifact_not_found", "artifact_id": artifact_id},
            )
            return

        task = self._task_context(connection, row["task_id"])
        artifact_content = artifact["content"] or {}
        module_descriptor = artifact_content.get("agent_module") or {}
        agent_module_id = module_descriptor.get("id")
        if isinstance(agent_module_id, str) and agent_module_id:
            try:
                module_spec = self.specpack.resolve_installed_agent_module(
                    connection,
                    row["project_id"],
                    agent_module_id,
                )
            except AgentModuleNotFoundError:
                agent_module_id = None
            else:
                runtime = module_spec.get("runtime") or {}
                if "software_factory.propose_pr" not in (
                    runtime.get("job_kinds") or []
                ):
                    agent_module_id = None
        if not isinstance(agent_module_id, str) or not agent_module_id:
            default_module = self.specpack.find_default_agent_module_for_job_kind(
                connection,
                row["project_id"],
                "software_factory.propose_pr",
            )
            agent_module_id = (
                default_module.get("id")
                if isinstance(default_module, dict)
                else None
            )
        reservation = self.budget.reserve(
            connection,
            row["project_id"],
            "tool",
            self.settings.software_factory_pr_budget_usd_micros,
            "Approved plan draft; queue sandbox PR proposal.",
            60,
        )
        if reservation["status"] != "reserved":
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_budget_blocked",
                actor,
                "decision",
                row["id"],
                {
                    "budget_decision_id": reservation.get("decision_id"),
                    "requested_usd_micros": self.settings.software_factory_pr_budget_usd_micros,
                },
            )
            return

        branch_suffix = row["task_id"] or row["id"]
        branch_prefix = "boss/"
        if agent_module_id:
            module_spec = self.specpack.resolve_installed_agent_module(
                connection, row["project_id"], agent_module_id
            )
            config = module_spec.get("config") or {}
            configured_prefix = config.get("branch_prefix")
            if isinstance(configured_prefix, str) and configured_prefix.strip():
                branch_prefix = configured_prefix.strip()
        if not branch_prefix.endswith("/"):
            branch_prefix = f"{branch_prefix}/"
        branch_name = f"{branch_prefix}proposal-{branch_suffix}"
        title = self._proposal_title(task)
        summary = self._proposal_summary(task, artifact_content)
        file_path = f"proposals/{branch_suffix}.md"
        file_content = self._proposal_markdown(task, artifact_content, actor)
        job = self.jobs.create_job(
            connection,
            project_id=row["project_id"],
            task_id=row["task_id"],
            kind="software_factory.propose_pr",
            priority=40,
            payload={
                "project_id": row["project_id"],
                "task_id": row["task_id"],
                "reservation_id": reservation["reservation_id"],
                "dedupe_key": f"approved-plan-proposal-{row['id']}",
                "title": title,
                "branch_name": branch_name,
                "summary": summary,
                "files": [{"path": file_path, "content": file_content}],
                "agent_module_id": agent_module_id,
                "requires_reservation": True,
            },
        )
        self.budget.attach_job(
            connection,
            row["project_id"],
            reservation["reservation_id"],
            job["id"],
        )
        emit_event(
            connection,
            row["project_id"],
            "decision.follow_on_job_queued",
            actor,
            "job",
            job["id"],
            {
                "decision_id": row["id"],
                "reservation_id": reservation["reservation_id"],
                "kind": "software_factory.propose_pr",
                "agent_module_id": agent_module_id,
            },
        )

    def _queue_merge_job_from_review(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
    ) -> None:
        pull_request_number = self._pull_request_number(recommendation)
        pull_request_url = recommendation.get("pull_request_url")
        if not pull_request_number or not isinstance(pull_request_url, str):
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_pull_request_metadata"},
            )
            return

        title = recommendation.get("title")
        if not isinstance(title, str) or not title.strip():
            title = row["question"]
        branch_name = recommendation.get("branch_name")
        if branch_name is not None and not isinstance(branch_name, str):
            branch_name = None

        job = self.jobs.create_job(
            connection,
            project_id=row["project_id"],
            task_id=row["task_id"],
            kind="github.pull_request.merge",
            priority=20,
            payload={
                "project_id": row["project_id"],
                "decision_id": row["id"],
                "dedupe_key": f"approved-pr-merge-{row['id']}",
                "pull_request_number": pull_request_number,
                "pull_request_url": pull_request_url,
                "title": title,
                "branch_name": branch_name,
            },
        )
        emit_event(
            connection,
            row["project_id"],
            "decision.follow_on_job_queued",
            actor,
            "job",
            job["id"],
            {
                "decision_id": row["id"],
                "kind": "github.pull_request.merge",
                "pull_request_url": pull_request_url,
            },
        )

    def _create_merge_gate_block(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
        *,
        status: str = "blocked_manual_merge_required",
        reason: str = "Automatic merge and deploy effects are still disabled in this phase.",
        next_action: str = "Review and merge the sandbox PR manually in GitHub. Deploy remains outside BOSS automation in this phase.",
        event_reason: str = "manual_merge_required",
        ci_validation_status: Optional[str] = None,
        ci_validation_summary: Optional[str] = None,
    ) -> None:
        title = recommendation.get("title")
        if not isinstance(title, str) or not title.strip():
            title = row["question"]
        merge_gate_artifact_id = create_artifact(
            connection,
            self.settings,
            row["project_id"],
            row["task_id"],
            "merge_gate",
            f"Merge gate for {title}",
            {
                "status": status,
                "reason": reason,
                "approved_review_decision_id": row["id"],
                "approved_by": actor,
                "pull_request_url": recommendation.get("pull_request_url"),
                "branch_name": recommendation.get("branch_name"),
                "review_packet_artifact_id": recommendation.get("artifact_id"),
                "diff_artifact_id": recommendation.get("diff_artifact_id"),
                "test_report_artifact_id": recommendation.get("test_report_artifact_id"),
                "ci_validation_status": ci_validation_status,
                "ci_validation_summary": ci_validation_summary,
                "receipt_id": recommendation.get("receipt_id"),
                "next_action": next_action,
            },
        )
        emit_event(
            connection,
            row["project_id"],
            "decision.follow_on_blocked",
            actor,
            "artifact",
            merge_gate_artifact_id,
            {
                "decision_id": row["id"],
                "blocked_action": "github.pull_request.merge",
                "reason": event_reason,
                "pull_request_url": recommendation.get("pull_request_url"),
                "receipt_id": recommendation.get("receipt_id"),
            },
        )

    def _queue_deployment_job_from_review(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
    ) -> None:
        merged_sha = recommendation.get("merged_sha")
        environment = recommendation.get("environment")
        pull_request_url = recommendation.get("pull_request_url")
        title = recommendation.get("title")
        branch_name = recommendation.get("branch_name")
        if not isinstance(merged_sha, str) or not merged_sha:
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_deployment_sha"},
            )
            return
        if not isinstance(environment, str) or not environment:
            environment = self.settings.sandbox_deploy_environment
        if not isinstance(pull_request_url, str) or not pull_request_url:
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_deployment_pull_request_url"},
            )
            return
        if not isinstance(title, str) or not title.strip():
            title = row["question"]
        if branch_name is not None and not isinstance(branch_name, str):
            branch_name = None

        job = self.jobs.create_job(
            connection,
            project_id=row["project_id"],
            task_id=row["task_id"],
            kind="github.deployment.create",
            priority=JOB_PRIORITY_URGENT,
            payload={
                "project_id": row["project_id"],
                "decision_id": row["id"],
                "dedupe_key": f"approved-deploy-{row['id']}",
                "merged_sha": merged_sha,
                "pull_request_url": pull_request_url,
                "title": title,
                "branch_name": branch_name,
                "environment": environment,
            },
        )
        emit_event(
            connection,
            row["project_id"],
            "decision.follow_on_job_queued",
            actor,
            "job",
            job["id"],
            {
                "decision_id": row["id"],
                "kind": "github.deployment.create",
                "environment": environment,
                "pull_request_url": pull_request_url,
            },
        )

    def _create_deploy_gate_block(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
    ) -> None:
        title = recommendation.get("title")
        if not isinstance(title, str) or not title.strip():
            title = row["question"]
        deploy_gate_artifact_id = create_artifact(
            connection,
            self.settings,
            row["project_id"],
            row["task_id"],
            "deployment_gate",
            f"Deployment gate for {title}",
            {
                "status": "blocked_manual_deploy_required",
                "reason": "Automatic sandbox deploy is disabled in this phase.",
                "approved_review_decision_id": row["id"],
                "approved_by": actor,
                "pull_request_url": recommendation.get("pull_request_url"),
                "branch_name": recommendation.get("branch_name"),
                "merged_sha": recommendation.get("merged_sha"),
                "environment": recommendation.get("environment")
                or self.settings.sandbox_deploy_environment,
                "next_action": "Run the sandbox deployment manually or enable the deploy policy.",
            },
        )
        emit_event(
            connection,
            row["project_id"],
            "decision.follow_on_blocked",
            actor,
            "artifact",
            deploy_gate_artifact_id,
            {
                "decision_id": row["id"],
                "blocked_action": "github.deployment.create",
                "reason": "manual_deploy_required",
                "pull_request_url": recommendation.get("pull_request_url"),
            },
        )

    def _queue_infra_job_from_review(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
    ) -> None:
        title = recommendation.get("title")
        region = recommendation.get("region")
        size = recommendation.get("size")
        reservation_id = recommendation.get("reservation_id")
        ttl_minutes = recommendation.get("ttl_minutes")
        tags = recommendation.get("tags") or []
        data_boundary = recommendation.get("data_boundary")
        if not isinstance(title, str) or not title.strip():
            title = row["question"]
        if not isinstance(region, str) or not region.strip():
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_infra_region"},
            )
            return
        if not isinstance(size, str) or not size.strip():
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_infra_size"},
            )
            return
        if not isinstance(reservation_id, str) or not reservation_id.strip():
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_infra_reservation"},
            )
            return
        if not isinstance(ttl_minutes, int):
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_infra_ttl"},
            )
            return
        job = self.infrastructure.queue_droplet_provision(
            connection,
            project_id=row["project_id"],
            task_id=row["task_id"],
            decision_id=row["id"],
            reservation_id=reservation_id,
            dedupe_key=f"approved-infra-provision-{row['id']}",
            title=title,
            region=region,
            size=size,
            ttl_minutes=ttl_minutes,
            tags=tags if isinstance(tags, list) else [],
            actor=actor,
            priority=JOB_PRIORITY_URGENT,
            max_attempts=1,
            data_boundary=data_boundary if isinstance(data_boundary, str) else None,
        )
        emit_event(
            connection,
            row["project_id"],
            "decision.follow_on_job_queued",
            actor,
            "job",
            job["id"],
            {
                "decision_id": row["id"],
                "kind": "infra.droplet.create",
                "reservation_id": reservation_id,
            },
        )

    def _create_infra_gate_block(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
    ) -> None:
        title = recommendation.get("title")
        region = recommendation.get("region")
        size = recommendation.get("size")
        reservation_id = recommendation.get("reservation_id")
        ttl_minutes = recommendation.get("ttl_minutes")
        data_boundary = recommendation.get("data_boundary")
        if not isinstance(title, str) or not title.strip():
            title = row["question"]
        if not isinstance(region, str) or not region:
            region = "unknown"
        if not isinstance(size, str) or not size:
            size = "unknown"
        if not isinstance(reservation_id, str):
            reservation_id = ""
        if not isinstance(ttl_minutes, int):
            ttl_minutes = 0
        self.infrastructure.create_provision_gate_block(
            connection,
            project_id=row["project_id"],
            task_id=row["task_id"],
            decision_id=row["id"],
            title=title,
            region=region,
            size=size,
            ttl_minutes=ttl_minutes,
            reservation_id=reservation_id,
            data_boundary=data_boundary if isinstance(data_boundary, str) else "internal",
            actor=actor,
        )

    def _task_context(
        self, connection: Connection, task_id: Optional[str]
    ) -> Optional[dict[str, Any]]:
        if not task_id:
            return None
        task = connection.execute(
            text(
                """
                SELECT id, title, description, inputs
                FROM tasks
                WHERE id = :task_id
                """
            ),
            {"task_id": task_id},
        ).mappings().first()
        return dict(task) if task else None

    def _proposal_title(self, task: Optional[dict[str, Any]]) -> str:
        if not task:
            return "BOSS proposal: sandbox proposal"
        title = task.get("title")
        if isinstance(title, str) and title.strip():
            return f"BOSS proposal: {title[:80]}"
        return "BOSS proposal: sandbox proposal"

    def _proposal_summary(
        self, task: Optional[dict[str, Any]], plan_content: dict[str, Any]
    ) -> str:
        if task:
            description = task.get("description")
            if isinstance(description, str) and description.strip():
                return description
        summary = plan_content.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary
        return "Create a sandbox PR proposal from an approved BOSS plan draft."

    def _proposal_markdown(
        self,
        task: Optional[dict[str, Any]],
        plan_content: dict[str, Any],
        actor: str,
    ) -> str:
        command = None
        if task:
            inputs = task.get("inputs")
            if isinstance(inputs, dict):
                command = inputs.get("command")
        if not isinstance(command, str) or not command.strip():
            command = self._proposal_summary(task, plan_content)

        lines = [
            "# BOSS Approved Plan Proposal",
            "",
            f"- Approved by: {actor}",
            f"- Command: {command}",
            "",
            "## Objectives",
        ]
        for objective in plan_content.get("objectives", []):
            lines.append(f"- {objective}")
        lines.extend(["", "## Steps"])
        for step in plan_content.get("steps", []):
            lines.append(f"- {step}")
        lines.extend(["", "## Risks"])
        for risk in plan_content.get("risks", []):
            lines.append(f"- {risk}")
        lines.extend(["", "## Done When"])
        for item in plan_content.get("done_when", []):
            lines.append(f"- {item}")
        return "\n".join(lines) + "\n"

    def _pull_request_number(self, recommendation: dict[str, Any]) -> Optional[int]:
        direct = recommendation.get("pull_request_number")
        if isinstance(direct, int) and direct > 0:
            return direct
        url = recommendation.get("pull_request_url")
        if not isinstance(url, str):
            return None
        match = re.search(r"/pull/(\d+)$", url)
        if not match:
            return None
        return int(match.group(1))


#: The console's old way of naming the app a panel specced a feature for, inside the directive.
#: Read only for rows written before the convene endpoint carried it as data.
_EXTEND_MARKER = re.compile(r"\[boss:extend_run=(crun_[a-z0-9]+)\]")


def feature_asked(text_: str) -> str:
    """The operator's ask inside a spec-first panel's directive ("What is wanted: …"), or ""."""
    found = re.search(r"What is wanted:\s*(.+)", text_ or "")
    return found.group(1).strip() if found else ""


def strip_extend_marker(text_value: str) -> str:
    """`text_value` without the old marker, so it never reaches a PRD or a build brief."""
    return re.sub(r"\s*\[boss:extend_run=crun_[a-z0-9]+\]\s*", " ", text_value or "").strip()


def extend_run_of_prd(connection: Connection, row: dict[str, Any],
                      recommendation: dict[str, Any]) -> Optional[str]:
    """The app build (crun_…) a PRD decision's panel specced a feature for, or None.

    STRUCTURED FIRST: POST /v1/meetings/prd validates `constraints.extends_run_id`, stores it
    on the meeting run, and the panel's PRD decision carries it as `extends_run_id`. The old
    `[boss:extend_run=...]` text marker was searched for in the PRD markdown, where the panel
    never writes it — so every "spec it first" approval built a new app or nothing. It is
    still honoured from the meeting's directive or the task, for rows made before.
    """
    found = str(recommendation.get("extends_run_id") or "").strip()
    if found:
        return found
    task_id = row.get("task_id")
    texts: list[str] = [str((recommendation.get("args") or {}).get("markdown") or "")]
    if task_id:
        meeting = connection.execute(text(
            "SELECT constraints, directive FROM meeting_runs WHERE task_id = :t "
            "ORDER BY created_at DESC LIMIT 1"), {"t": task_id}).mappings().first()
        if meeting:
            constraints = meeting["constraints"]
            if isinstance(constraints, str):
                try:
                    constraints = json.loads(constraints)
                except ValueError:
                    constraints = {}
            found = str((constraints or {}).get("extends_run_id") or "").strip()
            if found:
                return found
            texts.append(str(meeting["directive"] or ""))
        description = connection.execute(text("SELECT description FROM tasks WHERE id = :t"),
                                         {"t": task_id}).scalar()
        texts.append(str(description or ""))
    for candidate in texts:
        match = _EXTEND_MARKER.search(candidate)
        if match:
            return match.group(1)
    return None


def approve_by_policy(connection: Connection, decision_id: str, *, project_id: str, actor: str,
                      note: str) -> bool:
    """The ONLY other writer of `decisions.status = 'approved'` besides `transition` (C-1): a
    setting (the autonomy policy) approving a gate no person reviewed. It approves only a decision
    whose risk tier needs NO seat and no second person — anything that does goes through
    `transition` and `approvals.require_approval`, whoever asks. True when it approved."""
    from ..identity import approvals as _approvals

    row = connection.execute(text(
        "SELECT risk_tier, status, recommendation->>'action' AS action FROM decisions "
        "WHERE id = :id AND project_id = :p FOR UPDATE"),
        {"id": decision_id, "p": project_id}).mappings().first()
    if row is None or row["status"] != "pending":
        return False
    if row.get("action") in ("review_architecture", "self_fix.install", "self_fix.remove"):
        # An architecture, and a BOSS self-fix install, are approved by a person, never by a
        # setting (architecture_docs, self_fix_install; auto-install is off in v1).
        return False
    tier = int(row["risk_tier"] or 0)
    if _approvals.seats_for_tier(tier) or tier in _approvals.TWO_PERSON_TIERS:
        return False
    # A CI change, or CI not passed, is never approved by a setting (services/ci_gate.py).
    from . import ci_gate

    full = connection.execute(text("SELECT * FROM decisions WHERE id = :id"),
                              {"id": decision_id}).mappings().first()
    try:
        ci_gate.refuse_approval(connection, dict(full), actor, system_policy=True)
    except ci_gate.CiGateRefused:
        return False
    done = connection.execute(text(
        "UPDATE decisions SET status = 'approved', approved_by = :actor, approved_at = now(), "
        "resolution_note = :note WHERE id = :id AND project_id = :p AND status = 'pending' "
        "RETURNING id"), {"id": decision_id, "p": project_id, "actor": actor, "note": note}).first()
    return done is not None


def _delegate_words(delegated: Any) -> dict:
    """The event fields a delegate's answer carries: who answered, for whom, under which grant."""
    return {"via_delegate": delegated.delegate_principal_id,
            "via_delegate_name": delegated.delegate_name,
            "via_delegation": delegated.delegation_id,
            "for_person": delegated.person_principal_id,
            "for_person_name": delegated.person_name}


def _identity_decides(connection: Connection, row: dict) -> Any:
    """The verified caller when identity decides this approval (enforced, org claimed), else None
    — then today's member rules decide (shadow, off, before the claim, BOSS's own work)."""
    from ..identity import approvals, claim

    ctx = approvals.deciding_context()
    if ctx is None:
        return None
    try:
        if not claim.is_claimed(connection, ctx.principal.org_id):
            return None
    except Exception:  # noqa: BLE001 — an install without the identity tables: today's rules
        return None
    return ctx

