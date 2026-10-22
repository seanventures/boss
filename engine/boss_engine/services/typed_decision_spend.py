"""Spend a typed-decision attempt only after the reservation, lease, and receipt commit.

The workflow tick that claims the step is one transaction. A reservation written
there disappears if the process dies after the provider accepts the call. This
path commits on a second connection, then calls the model, then settles.

It does not keep its own balance. BudgetGovernor reserves, ModelGatewayService
leases and settles, ReceiptService records the attempt, JobLeaseService holds
the worker lease the gateway already requires.
"""
from __future__ import annotations

import hashlib
import json
import math
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event, Thread
from typing import Any, Callable, Optional

from sqlalchemy import text

from ..config import Settings
from .core import create_blocking_decision, emit_event
from .jobs import JOB_PRIORITY_INTERACTIVE, JobDeferredError, LeaseNotFoundError
from .model_gateway import (
    ModelCapacityDeniedError,
    ModelGatewayLeaseError,
)
from .typed_decisions import (
    Decision, DecisionBatch, Question, TypeSafeAuthenticationRefused, _step_output,
)


# Same wall as workflow_capability_runner.LEASE_SECONDS. A decision call is one
# round trip; the lease still heartbeats and expires like any other worker lease.
LEASE_TTL_SECONDS = 120

#: The token ceiling a typed decision's model lease declares. MEASURED, not guessed: it was
#: 20,000 and every architecture decision overran it (model_gateway.usage_limit_exceeded, 8 on
#: 2026-10-13 alone). The ten measured calls (model_gateway_invocations on 20k leases,
#: 2026-10-11..13) used 21,023–30,009 total tokens, p50 22,990, p95 28,255 — the prompt is the
#: whole architecture brief and the answer ~10–20k tokens. Over twice the largest seen, so
#: the limit would hold if it were enforced, and still catches a runaway.
TYPED_DECISION_MAX_TOTAL_TOKENS = 64_000

MAX_COST_USD_MICROS = 10_000
JOB_KIND = "typed_decision.execute"
AGENT_ID = "typed_decision"
SKILL_ID = "typed_decision"


class TypedDecisionSpendError(RuntimeError):
    """The attempt did not produce an answer. A later attempt may try again."""


class TypedDecisionReconciliationRequired(RuntimeError):
    """The provider may already have charged this attempt. Do not call it again."""


class TypedDecisionCancelled(Exception):
    """The caller stopped the attempt before the provider was contacted."""


class ReconciliationRefused(RuntimeError):
    """A person's approval cannot settle this call: another worker took it, or it is gone."""


@dataclass
class _Authority:
    outcome: str
    output: Optional[dict] = None
    message: str = ""
    reservation_id: Optional[str] = None
    job_id: Optional[str] = None
    job_lease_token: Optional[str] = None
    lease_id: Optional[str] = None
    lease_token: Optional[str] = None
    invocation_id: Optional[str] = None
    receipt_id: Optional[str] = None
    invocation_dedupe: str = ""
    role: str = "primary"
    included_in_subscription: bool = False
    source_id: Optional[str] = None


def model_receipt_dedupe(project_id: str, invocation_dedupe: str) -> str:
    """The receipt key ModelGatewayService.start_invocation writes."""
    return f"model-gateway:{project_id}:{invocation_dedupe}"


def _identity(project_id: str, run_id: str, step_id: str, binding: dict, state: Any) -> str:
    spec = binding.get("typed_decision") or {}
    material = json.dumps(
        {"question": spec.get("question"), "options": spec.get("options"), "state": state},
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"typed_decision:{project_id}:{run_id}:{step_id}:{digest}"


def _invocation_dedupe(identity: str, attempt: int, role: str) -> str:
    key = identity if attempt <= 1 else f"{identity}:retry:{attempt}"
    if role == "careful":
        key = f"{key}:second"
    return key


def _result_of(row: dict) -> dict:
    result = row.get("result") or {}
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return {}
    return result if isinstance(result, dict) else {}


def _uncertain(error: BaseException) -> bool:
    # Once the decider is entered, a transport, parser, or arbitrary adapter
    # error says nothing about provider acceptance or cost. Only an explicit
    # adapter-observed refusal may close this attempt as unused.
    return not isinstance(error, TypeSafeAuthenticationRefused)


def _safe_error(error: Any) -> str:
    from .redaction import redact_secrets

    return redact_secrets(str(error))


def consumer_for(connection, project_id: str, source_id: Optional[str], actor: str,
                 settings=None) -> str:
    """Whom a governed call books capacity FOR, given the source that will answer it.

    The gateway refuses to pool owner-scoped capacity: the consumer must be the source's owner.
    The seed gives every subscription to the WORKSPACE OPERATOR (owner_type workspace_operator,
    owner_id "founder"), and sources are seeded per project — so a call made inside that
    project is the workspace operator's own work, and is booked for them. Using the actor
    ("ceo") instead refused every governed discovery stage and decision on a default install
    ("…owner scoped and cannot be pooled"). A PERSONALLY owned source (owner_type operator)
    still needs the actor to be its owner: one person's subscription is not lent to another.
    """
    if not source_id:
        return actor
    row = connection.execute(
        text("SELECT owner_type, owner_id FROM entitlement_sources "
             "WHERE project_id = :p AND source_id = :s"),
        {"p": project_id, "s": source_id},
    ).mappings().first()
    if row and row["owner_type"] == "workspace_operator" and row["owner_id"]:
        return str(row["owner_id"])
    # COMMUNITY HAS ONE OPERATOR AND NO IDENTITY LAYER. Telling one person's subscription from
    # another's needs users BOSS can tell apart, which Community does not have yet — so the
    # owner of an owner-scoped source was whichever principal last pressed Connect (on boss-1
    # the console's own "executive_console"), and research the member "ceo" started was
    # refused as "pooling" the only subscription on the install. In Community the project's
    # connected subscription serves the project's work. Editions with real identities keep the
    # per-person check.
    edition = str(getattr(settings, "boss_edition", "community") or "community").strip().lower()
    if row and row["owner_id"] and edition in {"community", "oss", "local"}:
        return str(row["owner_id"])
    return actor


def capacity_after_failure(error: BaseException) -> dict:
    """What a failed call says about the SOURCE's capacity: only a capacity failure counts.

    Every failure used to be recorded as `capacity_state: unavailable`, and the router skips an
    unavailable source — so one crashed research call, or one rejected key, took the operator's
    only Claude subscription out of routing until something marked it available again.
    """
    text_ = str(error).lower()
    if any(term in text_ for term in ("rate limit", "rate_limit", " 429", "too many requests")):
        return {"capacity_state": "rate_limited"}
    if any(term in text_ for term in ("quota", "usage limit", "limit reached", "overloaded",
                                      " 529", "exhausted")):
        return {"capacity_state": "unavailable"}
    return {"capacity_state": "available"}


def governed_decider(settings: Settings, role: str):
    """One provider, one try. Internal retries would be a second call on one receipt.

    The primary is TypeSafe whenever it decides on this project (chosen, the default, and a key
    held); everything else — no key, the fallback after a Jev failure, the second opinion when
    Jev is unsure — is Claude, or the worker's API model where the CLI is absent.
    """
    from .typed_decisions import ClaudeDecider, TypeSafeDecider, typesafe_decides

    if role == "primary" and typesafe_decides(settings):
        return TypeSafeDecider(settings, attempts=1)
    return ClaudeDecider(
        settings, model=getattr(settings, "decision_model", None) or None, attempts=1,
    )


class TypedDecisionSpend:
    # Other single-call capabilities reuse the same durable authority envelope.
    # Defaults preserve typed-decision routing, quotes and job identity.
    max_cost_usd_micros = MAX_COST_USD_MICROS
    job_kind = JOB_KIND
    agent_id = AGENT_ID
    skill_id = SKILL_ID
    preferred_capabilities = ("classify",)
    stage = "typed_decision"

    def __init__(
        self,
        *,
        project_id: str,
        run_id: str,
        step_id: str,
        attempt: int,
        actor: str,
        worker_id: str,
        budget: Any,
        jobs: Any,
        gateway: Any,
        receipts: Any,
        commit: Callable,
        events: Any = None,
        decisions: Any = None,
        decider_factory: Any = None,
        org_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        before_provider_call: Any = None,
        ttl_seconds: int = LEASE_TTL_SECONDS,
    ) -> None:
        self.project_id = project_id
        self.run_id = run_id
        self.step_id = step_id
        self.attempt = max(1, int(attempt))
        self.actor = actor or worker_id
        self.worker_id = worker_id
        self.budget = budget
        self.jobs = jobs
        self.gateway = gateway
        self.receipts = receipts
        self.commit = commit
        self.events = events
        self.decisions = decisions
        self.decider_factory = decider_factory
        self.org_id = org_id
        self.workspace_id = workspace_id
        self.before_provider_call = before_provider_call
        self.ttl_seconds = ttl_seconds
        self._primary_backend = ""

    @classmethod
    def for_runner(
        cls,
        settings: Settings,
        *,
        engine: Any,
        project_id: str,
        run_id: str,
        step_id: str,
        attempt: int,
        actor: str,
        worker_id: str,
    ) -> "TypedDecisionSpend":
        from .budget import BudgetGovernor
        from .jobs import JobLeaseService
        from .model_gateway import ModelGatewayService
        from .receipts import ReceiptService

        def commit(fn):
            with engine.begin() as connection:
                return fn(connection)

        return cls(
            project_id=project_id,
            run_id=run_id,
            step_id=step_id,
            attempt=attempt,
            actor=actor,
            worker_id=worker_id,
            budget=BudgetGovernor(settings),
            jobs=JobLeaseService(settings),
            gateway=ModelGatewayService(settings),
            receipts=ReceiptService(),
            commit=commit,
            decider_factory=lambda role, bound: governed_decider(bound, role),
        )

    def execute(self, settings: Settings, binding: dict, state: Any, *,
                fallback: bool = True) -> dict:
        """`fallback=False` is for proving a key: an answer from another model would prove
        nothing about it."""
        identity = _identity(self.project_id, self.run_id, self.step_id, binding, state)
        try:
            primary = self._attempt(settings, binding, state, identity, "primary")
        except TypedDecisionSpendError as error:
            # JEV FAILED OUTRIGHT, so Claude answers — its own attempt, lease and receipt.
            # Not after an UNCERTAIN outcome: that one stays for reconciliation.
            if not (fallback and self._primary_backend == "typesafe"):
                raise
            self._emit_now("typed_decision.fallback", {
                "run_id": self.run_id, "step_id": self.step_id, "attempt": self.attempt,
                "reason": _safe_error(error)[:300],
            })
            return self._attempt(settings, binding, state, identity, "careful")
        if not fallback or not _needs_second_opinion(settings, primary):
            return primary
        self._emit_now("typed_decision.second_opinion", {
            "run_id": self.run_id, "step_id": self.step_id, "attempt": self.attempt,
        })
        second = self._attempt(settings, binding, state, identity, "careful")
        merged = _merge_opinions(binding, primary, second)
        self.commit(lambda connection: self.receipts.merge_result(
            connection, primary["receipt_id"], merged,
        ))
        return merged

    def _emit_now(self, event_type: str, data: dict) -> None:
        """A second opinion is its own attempt; the marker commits before that attempt."""
        self.commit(lambda connection: self._emit(
            connection, event_type, f"{self.run_id}:{self.step_id}", data,
        ))

    def _attempt(
        self, settings: Settings, binding: dict, state: Any, identity: str, role: str,
    ) -> dict:
        # THE DECIDER FIRST, because it names the source that will answer — the lease is booked
        # against that source. Building one makes no call; one that cannot be built is a
        # refusal before anything is reserved.
        factory = self.decider_factory or (lambda asked, bound: governed_decider(bound, asked))
        try:
            decider = factory(role, settings)
        except RuntimeError as error:
            raise TypedDecisionSpendError(_safe_error(error)) from None
        if role == "primary":
            self._primary_backend = getattr(decider, "backend", "")
        source_id = getattr(decider, "source_id", None)
        authority = self.commit(lambda connection: self._prepare(
            connection, settings, binding, state, identity, role, source_id,
        ))
        if authority.outcome == "replay":
            return authority.output or {}
        if authority.outcome == "refused":
            raise TypedDecisionSpendError(_safe_error(authority.message))
        if authority.outcome == "reconcile":
            raise TypedDecisionReconciliationRequired(_safe_error(authority.message))
        if authority.outcome == "prior_failure":
            raise TypedDecisionSpendError(_safe_error(authority.message))
        try:
            if self.before_provider_call is not None:
                self.before_provider_call()
        except TypedDecisionCancelled as cancelled:
            reason = cancelled
            self.commit(lambda connection: self._fail(
                connection, authority, reason, event_type="typed_decision.cancelled",
                nonacceptance_kind="cancelled_before_dispatch",
            ))
            raise TypedDecisionSpendError(_safe_error(reason)) from None
        self._ensure_lease(authority)
        try:
            with self._kept_alive(authority):
                batch = self._call(decider, binding, state)
        except Exception as error:
            reason = error
            if _uncertain(reason):
                self.commit(lambda connection: self._reconcile(connection, authority, reason))
                raise TypedDecisionReconciliationRequired(
                    "typed decision outcome is uncertain and needs reconciliation "
                    f"({_safe_error(reason)})"
                ) from None
            self.commit(lambda connection: self._fail(
                connection, authority, reason, event_type="typed_decision.attempt_failed",
                nonacceptance_kind="provider_auth_rejected",
            ))
            raise TypedDecisionSpendError(_safe_error(reason)) from None
        cost = batch.cost_usd_micros
        known_cost = isinstance(cost, int) and not isinstance(cost, bool) and cost >= 0
        if not known_cost and not (cost is None and authority.included_in_subscription):
            reason = RuntimeError("Typed decision reply has no usable cost evidence.")
            self.commit(lambda connection: self._reconcile(connection, authority, reason))
            raise TypedDecisionReconciliationRequired(
                "typed decision cost is uncertain and needs reconciliation"
            ) from reason
        try:
            output = self._output(binding, batch)
        except RuntimeError as error:
            reason = error
            try:
                self.commit(lambda connection: self._fail(
                    connection, authority, reason, event_type="typed_decision.attempt_failed",
                    actual=self._settlement_cost(connection, authority, batch),
                ))
            except TypedDecisionReconciliationRequired as cost_error:
                uncertainty = cost_error
                self.commit(lambda connection: self._reconcile(connection, authority, uncertainty))
                raise
            raise TypedDecisionSpendError(_safe_error(reason)) from None
        try:
            return self.commit(lambda connection: self._settle_success(
                connection, authority, batch, output,
            ))
        except (ModelGatewayLeaseError, LeaseNotFoundError, RuntimeError) as error:
            reason = error
            if isinstance(reason, TypedDecisionReconciliationRequired):
                self.commit(lambda connection: self._reconcile(connection, authority, reason))
                raise
            if "expired" not in str(reason).lower():
                raise
            self.commit(lambda connection: self._reconcile(connection, authority, reason))
            raise TypedDecisionReconciliationRequired(
                "typed decision lease expired after the provider call; reconcile the receipt"
            ) from reason

    def _prepare(
        self, connection, settings, binding, state, identity: str, role: str,
        source_id: Optional[str] = None,
    ) -> _Authority:
        dedupe = _invocation_dedupe(identity, self.attempt, role)
        existing = self._existing(connection, identity, dedupe)
        if existing is not None:
            return existing
        if self.attempt > 1 and role == "primary":
            self._emit(connection, "typed_decision.retry", dedupe, {
                "run_id": self.run_id,
                "step_id": self.step_id,
                "attempt": self.attempt,
            })
        # $0 of money on a subscription or local source the call is pinned to
        # (included_spend.py); the reservation is then a gate, not a hold.
        from .included_spend import source_by_id, source_is_included

        included = source_is_included(source_by_id(connection, self.project_id, source_id))
        estimate = 0 if included else int(self.max_cost_usd_micros)
        reserved = self.budget.reserve(
            connection,
            self.project_id,
            "model_gateway",
            estimate,
            f"Typed decision {self.step_id} attempt {self.attempt}",
            min(240, max(5, math.ceil(self.ttl_seconds / 60) + 5)),
            job_id=None,
            risk_tier=1,
        )
        if reserved.get("status") != "reserved":
            # A refusal that clears by itself (the per-minute cap) asks nobody to decide
            # anything: the runner waits and tries again (workflow_capability_runner._as_wait).
            decision_id = reserved.get("decision_id")
            if decision_id is None and not reserved.get("transient"):
                decision_id = self._decision(
                    connection,
                    f"Budget refused typed decision {self.run_id}/{self.step_id}",
                    "budget_denied",
                    {"attempt": self.attempt, "message": _safe_error(reserved.get("message") or "Budget reservation denied.")},
                )
            self._emit(connection, "typed_decision.spend_refused", dedupe, {
                "decision_id": decision_id,
                "attempt": self.attempt,
                "message": _safe_error(reserved.get("message") or "Budget reservation denied."),
            })
            return _Authority(
                outcome="refused",
                message=_safe_error(reserved.get("message") or "Budget reservation denied."),
                invocation_dedupe=dedupe,
                role=role,
            )
        reservation_id = reserved["reservation_id"]
        try:
            job = self._enqueue(connection, dedupe, role)
            self.budget.attach_job(connection, self.project_id, reservation_id, job["id"])
            leased = self.jobs.lease_owned_job(
                connection,
                project_id=self.project_id,
                job_id=job["id"],
                worker_id=self.worker_id,
                lease_ttl_seconds=self.ttl_seconds,
            )
        except LeaseNotFoundError as missing:
            self.budget.release(connection, self.project_id, reservation_id, _safe_error(missing))
            self._emit(connection, "typed_decision.reconciliation_required", dedupe, {
                "attempt": self.attempt, "message": _safe_error(missing),
            })
            return _Authority(
                outcome="reconcile", message=_safe_error(missing), invocation_dedupe=dedupe, role=role,
            )
        except JobDeferredError as deferred:
            self.budget.release(
                connection, self.project_id, reservation_id, _safe_error(deferred),
            )
            decision_id = self._decision(
                connection,
                f"Budget deferred typed decision {self.run_id}/{self.step_id}",
                "budget_deferred",
                {"attempt": self.attempt, "reason": _safe_error(deferred)},
            )
            self._emit(connection, "typed_decision.spend_refused", dedupe, {
                "decision_id": decision_id, "attempt": self.attempt, "message": _safe_error(deferred),
            })
            return _Authority(
                outcome="refused", message=_safe_error(deferred), invocation_dedupe=dedupe, role=role,
            )
        try:
            acquired = self.gateway.acquire(
                connection,
                self.project_id,
                job_id=job["id"],
                worker_id=self.worker_id,
                job_lease_token=leased["lease_token"],
                agent_id=self.agent_id,
                skill_id=self.skill_id,
                preferred_capabilities=list(self.preferred_capabilities),
                risk_tier=1,
                # Owner-scoped capacity is booked for its owner when the work is the
                # workspace's own; see consumer_for.
                consumer_owner_id=(self.gateway.consumer_for(
                    connection, self.project_id, source_id, self.actor)
                    if hasattr(self.gateway, "consumer_for") else self.actor),
                estimated_cost_usd_micros=estimate,
                max_usage={"total_tokens": TYPED_DECISION_MAX_TOTAL_TOKENS},
                dedupe_key=dedupe,
                ttl_seconds=self.ttl_seconds,
                existing_reservation_id=reservation_id,
                source_id=source_id,
            )
        except ModelCapacityDeniedError as denied:
            self.budget.release(connection, self.project_id, reservation_id, _safe_error(denied))
            self._capacity_refused(connection, job["id"], leased["lease_token"], denied)
            # Every lease held by other work is a queue, not something a person decides: the
            # step waits (workflow_capability_runner._as_wait reads the same classification).
            from .model_failures import waiting_for

            decision_id = None if waiting_for(denied) is not None else self._decision(
                connection,
                f"Model capacity refused typed decision {self.run_id}/{self.step_id}",
                "model_capacity_denied",
                {"attempt": self.attempt, "message": _safe_error(denied)},
            )
            self._emit(connection, "typed_decision.spend_refused", dedupe, {
                "decision_id": decision_id, "attempt": self.attempt, "message": _safe_error(denied),
            })
            return _Authority(
                outcome="refused", message=_safe_error(denied), invocation_dedupe=dedupe, role=role,
            )
        if acquired.get("duplicate") or not acquired.get("lease_token"):
            self.budget.release(
                connection, self.project_id, reservation_id,
                "Model lease already existed for this attempt.",
            )
            return _Authority(
                outcome="reconcile",
                message="typed decision lease already exists and must be reconciled",
                invocation_dedupe=dedupe,
                role=role,
            )
        lease_id = acquired["lease"]["id"]
        self._heartbeat(connection, job["id"], leased["lease_token"], lease_id, acquired["lease_token"])
        started = self.gateway.start_invocation(
            connection,
            self.project_id,
            lease_id,
            worker_id=self.worker_id,
            lease_token=acquired["lease_token"],
            job_lease_token=leased["lease_token"],
            request_hash=hashlib.sha256(dedupe.encode("utf-8")).hexdigest(),
            dedupe_key=dedupe,
            metadata={
                "stage": self.stage,
                "attempt": str(self.attempt),
                "workflow_run_id": self.run_id[:256],
            },
        )
        invocation = started["invocation"]
        if started.get("duplicate"):
            self.gateway.release(
                connection, self.project_id, lease_id,
                worker_id=self.worker_id,
                lease_token=acquired["lease_token"],
                job_lease_token=leased["lease_token"],
                reason="Duplicate typed-decision invocation.",
            )
            status = invocation.get("status")
            if status == "succeeded":
                return self._replay(connection, invocation.get("receipt_id"), dedupe, role)
            return _Authority(
                outcome="reconcile",
                message="typed decision attempt is already in progress",
                invocation_dedupe=dedupe,
                role=role,
            )
        return _Authority(
            outcome="ready",
            reservation_id=reservation_id,
            job_id=job["id"],
            job_lease_token=leased["lease_token"],
            lease_id=lease_id,
            lease_token=acquired["lease_token"],
            invocation_id=invocation["id"],
            receipt_id=invocation["receipt_id"],
            invocation_dedupe=dedupe,
            role=role,
            included_in_subscription=included,
            source_id=source_id,
        )

    def _capacity_refused(self, connection, job_id, job_lease_token, error) -> None:
        """Optional no-contact cleanup; ordinary typed-decision behavior is unchanged."""

    def _existing(self, connection, identity: str, dedupe: str) -> Optional[_Authority]:
        scope = self._scope(connection)
        prefix = model_receipt_dedupe(self.project_id, identity)
        rows = self.receipts.list_by_dedupe_prefix(connection, scope["org_id"], prefix)
        succeeded = [
            row for row in rows
            if row.get("status") == "succeeded" and _result_of(row).get("answer")
        ]
        if succeeded:
            original = succeeded[0]
            output = dict(_result_of(original))
            output["receipt_id"] = original["id"]
            return _Authority(
                outcome="replay", output=output, receipt_id=original["id"],
                invocation_dedupe=dedupe,
            )
        if any(row.get("status") == "in_progress" for row in rows):
            self._decision(
                connection,
                f"Reconcile typed decision {self.run_id}/{self.step_id}",
                "typed_decision_reconciliation",
                {"attempt": self.attempt, "dedupe_key": dedupe},
            )
            self._emit(connection, "typed_decision.reconciliation_required", dedupe, {
                "run_id": self.run_id, "step_id": self.step_id, "attempt": self.attempt,
            })
            return _Authority(
                outcome="reconcile",
                message=(
                    "typed decision attempt is still in progress and needs reconciliation"
                ),
                invocation_dedupe=dedupe,
            )
        receipt_key = model_receipt_dedupe(self.project_id, dedupe)
        failed = next(
            (row for row in rows
             if row.get("status") == "failed" and row.get("dedupe_key") == receipt_key),
            None,
        )
        if failed is not None:
            return _Authority(
                outcome="prior_failure",
                message=f"typed decision attempt already failed ({failed['id']})",
                receipt_id=failed["id"],
                invocation_dedupe=dedupe,
            )
        return None

    def _enqueue(self, connection, dedupe: str, role: str) -> dict:
        payload = {
            "agent_id": self.agent_id,
            "actor": self.actor,
            "workflow_run_id": self.run_id,
            "step_id": self.step_id,
            "attempt": self.attempt,
            "role": role,
        }
        enqueue = getattr(self.jobs, "enqueue_job", None)
        if enqueue is None:
            from .jobs import enqueue_job as enqueue
        return enqueue(
            connection,
            self.project_id,
            self.job_kind,
            payload,
            priority=JOB_PRIORITY_INTERACTIVE,
            max_attempts=1,
            dedupe_key=f"job:{dedupe}",
            workload_class="owner_sandbox",
            data_boundary="internal",
        )

    def _heartbeat(self, connection, job_id, job_token, lease_id, lease_token) -> None:
        self.jobs.heartbeat(
            connection, self.worker_id, job_id, job_token, self.ttl_seconds,
        )
        self.gateway.heartbeat(
            connection, self.project_id, lease_id,
            worker_id=self.worker_id, lease_token=lease_token,
            job_lease_token=job_token, ttl_seconds=self.ttl_seconds,
        )

    @contextmanager
    def _kept_alive(self, authority: _Authority):
        """Heartbeat the job and model leases for as long as the provider call runs.

        A lease is a couple of minutes; a model call can take longer. Without this the lease
        ran out mid-call, settlement then refused the expired lease, and a reply that had been
        paid for was left for a person to reconcile. Yields the heartbeat errors seen, so a
        caller can refuse to settle under a lease it lost.
        """
        stop = Event()
        errors: list[Exception] = []

        def beat():
            while not stop.wait(max(0.1, self.ttl_seconds / 3)):
                try:
                    self.commit(lambda connection: self._heartbeat(
                        connection, authority.job_id, authority.job_lease_token,
                        authority.lease_id, authority.lease_token,
                    ))
                except Exception as error:  # noqa: BLE001 — reported to the caller
                    errors.append(error)
                    return

        thread = Thread(target=beat, name=f"{self.stage}-lease-heartbeat", daemon=True)
        thread.start()
        try:
            yield errors
        finally:
            stop.set()
            thread.join()

    def _ensure_lease(self, authority: _Authority) -> None:
        try:
            self.commit(lambda connection: self._heartbeat(
                connection, authority.job_id, authority.job_lease_token,
                authority.lease_id, authority.lease_token,
            ))
        except (LeaseNotFoundError, ModelGatewayLeaseError, RuntimeError) as error:
            if "expired" not in str(error).lower():
                raise
            self.commit(lambda connection: self._expire(connection, authority))
            raise TypedDecisionSpendError(
                "typed decision lease expired before the provider call"
            ) from error

    def _expire(self, connection, authority: _Authority) -> None:
        self.gateway.abandon_expired_attempt(
            connection, self.project_id, authority.lease_id,
            worker_id=self.worker_id, invocation_id=authority.invocation_id,
            reason="Model capacity lease expired before the provider call.",
        )
        self._decision(
            connection,
            f"Typed decision lease expired {self.run_id}/{self.step_id}",
            "typed_decision_lease_expired",
            {"attempt": self.attempt, "receipt_id": authority.receipt_id},
        )
        self._emit(connection, "typed_decision.lease_expired", authority.invocation_dedupe, {
            "run_id": self.run_id,
            "step_id": self.step_id,
            "attempt": self.attempt,
            "receipt_id": authority.receipt_id,
        })

    def _call(self, decider, binding, state) -> DecisionBatch:
        spec = binding["typed_decision"]
        options = {str(key): str(value) for key, value in spec["options"].items()}
        question = Question("decision", str(spec["question"]), options)
        context = state if isinstance(state, str) else json.dumps(state, default=str)
        from .typed_decisions import decide_with_precedent

        precedent = str(getattr(self, "knowledge_precedent", "") or "")
        return decide_with_precedent(decider, context, [question], precedent)

    def _output(self, binding: dict, batch: DecisionBatch) -> dict:
        """What the receipt keeps and the caller gets back. A RuntimeError here means no usable
        answer: the attempt settles as failed with whatever it cost."""
        return _step_output(binding["typed_decision"], batch)

    def _settle_success(
        self, connection, authority: _Authority, batch: DecisionBatch, output: dict,
    ) -> dict:
        self._heartbeat(
            connection, authority.job_id, authority.job_lease_token,
            authority.lease_id, authority.lease_token,
        )
        actual = self._settlement_cost(connection, authority, batch)
        usage = {
            key: int(value)
            for key, value in (batch.usage or {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        self.gateway.settle_invocation(
            connection,
            self.project_id,
            authority.lease_id,
            authority.invocation_id,
            worker_id=self.worker_id,
            lease_token=authority.lease_token,
            job_lease_token=authority.job_lease_token,
            succeeded=True,
            provider_ref=f"{batch.backend}:{batch.model}"[:200],
            usage_units=usage,
            actual_cost_usd_micros=actual,
            observed_capacity={"capacity_state": "available"},
            error={},
        )
        output = dict(output)
        output["receipt_id"] = authority.receipt_id
        output["reservation_id"] = authority.reservation_id
        self.receipts.merge_result(connection, authority.receipt_id, output)
        self.jobs.complete(
            connection, self.worker_id, authority.job_id, authority.job_lease_token,
            {"receipt_id": authority.receipt_id, "answer": output.get("answer")},
        )
        self._emit(connection, "typed_decision.settled", authority.invocation_dedupe, {
            "receipt_id": authority.receipt_id,
            "reservation_id": authority.reservation_id,
            "actual_cost_usd_micros": actual,
            "attempt": self.attempt,
        })
        return output

    def _settlement_cost(self, connection, authority: _Authority, batch: DecisionBatch) -> int:
        cost = batch.cost_usd_micros
        if isinstance(cost, int) and not isinstance(cost, bool) and cost >= 0:
            return cost
        # Missing equivalent usage is harmless only when THIS still-active
        # authority names a canonical included source and zero money reservation.
        # Lock that source until settlement: its classification must not change
        # between this check and the gateway's own money calculation.
        if cost is None and authority.included_in_subscription:
            from .included_spend import source_is_included

            source = connection.execute(text(
                "SELECT s.entitlement_kind,s.cost_mode FROM model_gateway_leases l "
                "JOIN budget_reservations b ON b.id=l.budget_reservation_id "
                "AND b.project_id=l.project_id AND b.job_id=l.job_id "
                "JOIN entitlement_sources s ON s.id=l.entitlement_source_id "
                "AND s.project_id=l.project_id AND s.source_id=l.source_id "
                "WHERE l.id=:lease AND l.project_id=:project AND l.job_id=:job "
                "AND l.source_id=:source AND b.id=:reservation "
                "AND l.status='active' AND b.status='active' "
                "AND l.expires_at>now() AND b.expires_at>now() "
                "AND l.estimated_cost_usd_micros=0 AND b.amount_usd_micros=0 "
                "FOR UPDATE OF l,b,s"
            ), {"lease": authority.lease_id, "project": self.project_id,
                "job": authority.job_id, "source": authority.source_id,
                "reservation": authority.reservation_id}).mappings().first()
            if source_is_included(source):
                return 0
        raise TypedDecisionReconciliationRequired(
            "typed decision cost is uncertain and needs reconciliation"
        )

    def _fail(
        self, connection, authority: _Authority, error: BaseException, *,
        event_type: str, actual: int = 0, nonacceptance_kind: Optional[str] = None,
    ) -> None:
        message = _safe_error(error)[:500]
        evidence: dict[str, Any] = {"reason": message}
        # Produce matching, versioned evidence in the canonical invocation,
        # Receipt and event. Historical failed rows without this proof remain
        # ambiguous; do not infer nonacceptance from their error strings.
        if actual == 0 and (
            (nonacceptance_kind == "cancelled_before_dispatch"
             and isinstance(error, TypedDecisionCancelled)
             and event_type == "typed_decision.cancelled")
            or (nonacceptance_kind == "provider_auth_rejected"
                and isinstance(error, TypeSafeAuthenticationRefused)
                and event_type == "typed_decision.attempt_failed")
        ):
            evidence["typed_decision_nonacceptance"] = {
                "version": 1,
                "kind": nonacceptance_kind,
                "project_id": self.project_id,
                "invocation_id": authority.invocation_id,
                "receipt_id": authority.receipt_id,
                "reservation_id": authority.reservation_id,
                **({"provider": "typesafe", "http_status": 401}
                   if nonacceptance_kind == "provider_auth_rejected" else {}),
            }
        self.gateway.settle_invocation(
            connection,
            self.project_id,
            authority.lease_id,
            authority.invocation_id,
            worker_id=self.worker_id,
            lease_token=authority.lease_token,
            job_lease_token=authority.job_lease_token,
            succeeded=False,
            provider_ref=None,
            usage_units={},
            actual_cost_usd_micros=actual,
            observed_capacity=capacity_after_failure(error),
            error=evidence,
        )
        try:
            self.jobs.fail(
                connection, self.worker_id, authority.job_id, authority.job_lease_token,
                {"message": message}, False, 0,
            )
        except (LeaseNotFoundError, RuntimeError):
            pass
        self._emit(connection, event_type, authority.invocation_dedupe, {
            "attempt": self.attempt,
            "receipt_id": authority.receipt_id,
            "message": message,
            **({"typed_decision_nonacceptance": evidence["typed_decision_nonacceptance"]}
               if "typed_decision_nonacceptance" in evidence else {}),
        })

    def _reconcile(self, connection, authority: _Authority, error: BaseException) -> None:
        message = _safe_error(error)[:500]
        self._decision(
            connection,
            f"Reconcile typed decision {self.run_id}/{self.step_id}",
            "typed_decision_reconciliation",
            {
                "attempt": self.attempt,
                "receipt_id": authority.receipt_id,
                "message": message,
            },
        )
        self._emit(connection, "typed_decision.reconciliation_required", authority.invocation_dedupe, {
            "run_id": self.run_id,
            "step_id": self.step_id,
            "attempt": self.attempt,
            "receipt_id": authority.receipt_id,
            "message": message,
        })

    def _replay(self, connection, receipt_id: Optional[str], dedupe: str, role: str) -> _Authority:
        if not receipt_id:
            return _Authority(
                outcome="reconcile",
                message="typed decision replay had no receipt",
                invocation_dedupe=dedupe,
                role=role,
            )
        rows = self.receipts.list_by_dedupe_prefix(
            connection, self._scope(connection)["org_id"],
            model_receipt_dedupe(self.project_id, dedupe),
        )
        match = next((row for row in rows if row["id"] == receipt_id), None)
        output = dict(_result_of(match or {}))
        output["receipt_id"] = receipt_id
        return _Authority(
            outcome="replay", output=output, receipt_id=receipt_id,
            invocation_dedupe=dedupe, role=role,
        )

    def _scope(self, connection) -> dict[str, str]:
        if self.org_id:
            return {"org_id": self.org_id, "workspace_id": self.workspace_id or self.org_id}
        return self.gateway._project_scope(connection, self.project_id)

    def _emit(self, connection, event_type: str, entity_id: str, data: dict) -> None:
        if self.events is not None:
            self.events(
                connection, self.project_id, event_type, self.actor,
                "typed_decision_attempt", entity_id, data,
            )
            return
        emit_event(
            connection, self.project_id, event_type, self.actor,
            "typed_decision_attempt", entity_id, data,
        )

    def _decision(self, connection, question: str, reason: str, detail: dict) -> str:
        if self.decisions is not None:
            return self.decisions(connection, self.project_id, question, reason, detail)
        return create_blocking_decision(
            connection, self.project_id, None, question,
            {"action": reason}, detail, reason,
        )


def _needs_second_opinion(settings: Settings, output: dict) -> bool:
    """Only an answer Jev gave is second-guessed; Claude's own answer is not re-asked."""
    floor = float(getattr(settings, "decision_fallback_below", 0) or 0)
    if output.get("backend") != "typesafe" or floor <= 0:
        return False
    confidence = output.get("confidence")
    return output.get("answer") is None or confidence is None or float(confidence) < floor


def _merge_opinions(binding: dict, first: dict, second: dict) -> dict:
    merged = dict(second)
    merged["backend"] = f"{first.get('backend')}+{second.get('backend')}"
    merged["model"] = f"{first.get('model')}+{second.get('model')}"
    merged["cost_usd_micros"] = int(first.get("cost_usd_micros") or 0) + int(
        second.get("cost_usd_micros") or 0
    )
    merged["first_opinion"] = {
        "answer": first.get("answer"), "confidence": first.get("confidence"),
    }
    merged["receipt_id"] = first.get("receipt_id")
    merged["second_receipt_id"] = second.get("receipt_id")
    spec = binding.get("typed_decision") or {}
    floor = spec.get("min_confidence")
    confidence = merged.get("confidence")
    merged["confident"] = floor is None or (
        confidence is not None and float(confidence) >= float(floor)
    )
    return merged


class TypedDecisionBatchSpend(TypedDecisionSpend):
    """Several questions about one context on one governed attempt.

    The workflow step asks one question and gets one answer. The model router's classifier
    asks the tier question and the rigor questions together, because they share one context
    and the context is what costs. The envelope is the step's, unchanged: a committed budget
    reservation, a job lease and a model lease, one receipt per attempt, settlement with the
    actual cost. The receipt keeps every answer, so the same questions about the same context
    replay from it without a second call, and an attempt that failed is followed by the next
    numbered attempt (its `typed_decision.retry` event is the visible retry) rather than by a
    permanent "already failed" for a task the router will see again.

    No fallback to another model and no second opinion: the caller has its own (the router
    falls back to its rules). The first question is the one the caller needs; an attempt that
    does not answer it is settled as failed with whatever it cost.
    """

    stage = "typed_decision_batch"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._questions: list[Question] = []

    def decide(self, settings: Settings, context: str, questions: list[Question]) -> DecisionBatch:
        if not questions:
            raise TypedDecisionSpendError("a typed decision batch needs at least one question")
        self._questions = list(questions)
        binding = {"typed_decision": {
            "question": {q.key: q.instructions for q in questions},
            "options": {q.key: dict(q.options) for q in questions},
        }}
        identity = _identity(self.project_id, self.run_id, self.step_id, binding, context)
        return self._batch_of(self._attempt(settings, binding, context, identity, "primary"))

    def _prepare(self, connection, settings, binding, state, identity: str, role: str,
                 source_id: Optional[str] = None) -> _Authority:
        self.attempt = max(self.attempt, self._failed_attempts(connection, identity) + 1)
        return super()._prepare(connection, settings, binding, state, identity, role, source_id)

    def _failed_attempts(self, connection, identity: str) -> int:
        rows = self.receipts.list_by_dedupe_prefix(
            connection, self._scope(connection)["org_id"],
            model_receipt_dedupe(self.project_id, identity),
        )
        return sum(1 for row in rows if row.get("status") == "failed")

    def _call(self, decider, binding, state) -> DecisionBatch:
        return decider.decide(state, list(self._questions))

    def _output(self, binding: dict, batch: DecisionBatch) -> dict:
        answers: dict[str, dict] = {}
        for q in self._questions:
            d = batch.decisions.get(q.key)
            answers[q.key] = {
                "answer": d.answer if d else None,
                "confidence": d.confidence if d else None,
                "reason": (d.reason if d else "")[:300],
                "error": d.error if d else "nothing returned",
            }
        lead = answers[self._questions[0].key]
        if lead["answer"] is None:
            raise RuntimeError("the decision model gave no usable answer "
                               f"({lead['error'] or 'nothing returned'}).")
        return {
            "kind": "typed_decision_batch",
            "answer": lead["answer"], "confidence": lead["confidence"],
            "answers": answers,
            "backend": batch.backend, "model": batch.model,
            "latency_ms": batch.latency_ms, "cost_usd_micros": batch.cost_usd_micros,
        }

    def _batch_of(self, output: dict) -> DecisionBatch:
        """The stored answers as the DecisionBatch a Decider returns, fresh or replayed."""
        answers = output.get("answers") or {}
        decisions: dict[str, Decision] = {}
        for q in self._questions:
            given = answers.get(q.key) or {}
            answer = given.get("answer")
            if answer not in q.options:
                decisions[q.key] = Decision(None, None, error=str(given.get("error") or "no answer"))
                continue
            confidence = given.get("confidence")
            decisions[q.key] = Decision(
                answer, float(confidence) if confidence is not None else None,
                reason=str(given.get("reason") or ""),
            )
        cost = output.get("cost_usd_micros")
        return DecisionBatch(
            decisions=decisions, backend=str(output.get("backend") or "typesafe"),
            model=str(output.get("model") or ""), latency_ms=int(output.get("latency_ms") or 0),
            cost_usd_micros=cost if isinstance(cost, int) and not isinstance(cost, bool) else None,
            usage={"receipt_id": output.get("receipt_id"),
                   "reservation_id": output.get("reservation_id"), "attempt": self.attempt},
        )


def resolve_reconciliation_decisions(connection, project_id: str, receipt_id: str,
                                     note: str) -> list[str]:
    """Close the pending reconciliation cards of a receipt that has now been settled."""
    rows = connection.execute(
        text("UPDATE decisions SET status='resolved', resolution_note=:note, "
             "updated_at=clock_timestamp() WHERE project_id=:p AND status='pending' "
             "AND recommendation->>'action' IN "
             "('architecture_reconciliation', 'typed_decision_reconciliation') "
             "AND cost_summary->>'receipt_id' = :r RETURNING id"),
        {"p": project_id, "r": receipt_id, "note": note[:500]},
    ).scalars().all()
    return list(rows)


def _call_of_receipt(connection, project_id: str, receipt_id: str) -> Optional[dict]:
    row = connection.execute(
        text("SELECT i.id AS invocation_id, i.status AS invocation_status, l.source_id, "
             "j.payload AS job_payload FROM model_gateway_invocations i "
             "JOIN model_gateway_leases l ON l.id = i.model_gateway_lease_id "
             "LEFT JOIN jobs j ON j.id = i.job_id "
             "WHERE i.project_id = :p AND i.receipt_id = :r"),
        {"p": project_id, "r": receipt_id},
    ).mappings().first()
    return dict(row) if row else None


def on_reconciliation_approved(connection, settings, decision: dict, actor: str) -> dict:
    """A person approved reconciling a call: settle it, then put its step back in the queue.

    A reply the receipt kept is recorded with its usage and cost and marked succeeded, so the
    step's retry replays it and never pays for it again. With no reply the receipt is marked
    failed and its hold released, so the retry makes a fresh attempt. Approving used to record
    the decision and nothing else: the receipt stayed in progress, every retry raised the same
    card, and the run could never move again.
    """
    from .model_gateway import ModelGatewayError, ModelGatewayService
    project_id = decision["project_id"]
    detail = decision.get("cost_summary") or {}
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            detail = {}
    receipt_id = str((detail or {}).get("receipt_id") or "")
    receipt = connection.execute(
        text("SELECT id, status, result FROM receipts WHERE id = :r AND project_id = :p "
             "FOR UPDATE"),
        {"r": receipt_id, "p": project_id},
    ).mappings().first() if receipt_id else None
    if receipt is None:
        raise ReconciliationRefused(
            "This card names no call BOSS can find, so there is nothing to settle. Retry the "
            "step from its run.")
    call = _call_of_receipt(connection, project_id, receipt_id)
    outcome = receipt["status"]
    if receipt["status"] == "in_progress":
        if call is None:
            raise ReconciliationRefused("The call behind this card was never started.")
        reply = _result_of(dict(receipt)).get("unsettled_architecture_reply")
        try:
            with connection.begin_nested():
                if isinstance(reply, dict) and "text" in reply:
                    from .architecture_spend import settle_retained_reply

                    settle_retained_reply(connection, settings, project_id, receipt_id,
                                          reply, actor=actor, strict=False)
                    outcome = "settled"
                else:
                    ModelGatewayService(settings).settle_retained_invocation(
                        connection, project_id, call["invocation_id"], actor=actor,
                        succeeded=False, provider_ref=None, usage_units={},
                        actual_cost_usd_micros=0,
                        error={"reason": "A person reconciled this call as not answered."},
                    )
                    outcome = "failed"
        except (ModelGatewayError, ValueError, TypedDecisionSpendError) as refused:
            raise ReconciliationRefused(str(refused)) from refused
    resolve_reconciliation_decisions(connection, project_id, receipt_id,
                                     f"Reconciled by {actor}: {outcome}.")
    payload = (call or {}).get("job_payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    run_id, step_id = payload.get("workflow_run_id"), payload.get("step_id")
    retried = False
    if run_id and step_id:
        from . import workflows

        stopped = connection.execute(
            text("SELECT status FROM workflow_step_runs WHERE project_id=:p "
                 "AND workflow_run_id=:r AND step_id=:s ORDER BY iteration DESC LIMIT 1"),
            {"p": project_id, "r": run_id, "s": step_id},
        ).scalar()
        if stopped in workflows.OPERATOR_RECOVERABLE_STEP_STATES:
            point = connection.begin_nested()
            try:
                workflows.retry_failed_step(connection, project_id=project_id,
                                            run_id=run_id, step_id=step_id, actor=actor)
                point.commit()
                retried = True
            except Exception as error:  # noqa: BLE001 — the settlement stands; say why
                point.rollback()
                emit_event(connection, project_id, "decision.follow_on_skipped", actor,
                           "decision", decision["id"],
                           {"reason": "step_retry_refused", "message": str(error)[:300]})
    emit_event(connection, project_id, "typed_decision.reconciled", actor, "receipt",
               receipt_id, {"decision_id": decision["id"], "outcome": outcome,
                            "workflow_run_id": run_id, "step_id": step_id,
                            "step_retried": retried})
    return {"receipt_id": receipt_id, "outcome": outcome, "step_retried": retried}
