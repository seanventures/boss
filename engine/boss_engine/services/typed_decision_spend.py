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
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..config import Settings
from .core import create_blocking_decision, emit_event
from .jobs import JOB_PRIORITY_INTERACTIVE, JobDeferredError, LeaseNotFoundError
from .model_gateway import (
    ModelCapacityDeniedError,
    ModelGatewayLeaseError,
)
from .typed_decisions import DecisionBatch, Question, _step_output


# Same wall as workflow_capability_runner.LEASE_SECONDS. A decision call is one
# round trip; the lease still heartbeats and expires like any other worker lease.
LEASE_TTL_SECONDS = 120
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
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    name = type(error).__name__.lower()
    return "timeout" in name or "connect" in name


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
                "reason": str(error)[:300],
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
            raise TypedDecisionSpendError(str(error)) from error
        if role == "primary":
            self._primary_backend = getattr(decider, "backend", "")
        source_id = getattr(decider, "source_id", None)
        authority = self.commit(lambda connection: self._prepare(
            connection, settings, binding, state, identity, role, source_id,
        ))
        if authority.outcome == "replay":
            return authority.output or {}
        if authority.outcome == "refused":
            raise TypedDecisionSpendError(authority.message)
        if authority.outcome == "reconcile":
            raise TypedDecisionReconciliationRequired(authority.message)
        if authority.outcome == "prior_failure":
            raise TypedDecisionSpendError(authority.message)
        try:
            if self.before_provider_call is not None:
                self.before_provider_call()
        except TypedDecisionCancelled as cancelled:
            reason = cancelled
            self.commit(lambda connection: self._fail(
                connection, authority, reason, event_type="typed_decision.cancelled",
            ))
            raise TypedDecisionSpendError(str(reason)) from reason
        self._ensure_lease(authority)
        try:
            batch = self._call(decider, binding, state)
        except Exception as error:
            reason = error
            if _uncertain(reason):
                self.commit(lambda connection: self._reconcile(connection, authority, reason))
                raise TypedDecisionReconciliationRequired(
                    "typed decision outcome is uncertain and needs reconciliation "
                    f"({reason})"
                ) from reason
            self.commit(lambda connection: self._fail(
                connection, authority, reason, event_type="typed_decision.attempt_failed",
            ))
            raise TypedDecisionSpendError(str(reason)) from reason
        try:
            output = _step_output(binding["typed_decision"], batch)
        except RuntimeError as error:
            reason = error
            self.commit(lambda connection: self._fail(
                connection, authority, reason, event_type="typed_decision.attempt_failed",
                actual=int(batch.cost_usd_micros or 0),
            ))
            raise TypedDecisionSpendError(str(reason)) from reason
        try:
            return self.commit(lambda connection: self._settle_success(
                connection, authority, batch, output,
            ))
        except (ModelGatewayLeaseError, LeaseNotFoundError, RuntimeError) as error:
            reason = error
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
        reserved = self.budget.reserve(
            connection,
            self.project_id,
            "model_gateway",
            MAX_COST_USD_MICROS,
            f"Typed decision {self.step_id} attempt {self.attempt}",
            min(240, max(5, math.ceil(self.ttl_seconds / 60) + 5)),
            job_id=None,
            risk_tier=1,
        )
        if reserved.get("status") != "reserved":
            decision_id = reserved.get("decision_id") or self._decision(
                connection,
                f"Budget refused typed decision {self.run_id}/{self.step_id}",
                "budget_denied",
                {"attempt": self.attempt, "message": reserved.get("message")},
            )
            self._emit(connection, "typed_decision.spend_refused", dedupe, {
                "decision_id": decision_id,
                "attempt": self.attempt,
                "message": reserved.get("message"),
            })
            return _Authority(
                outcome="refused",
                message=reserved.get("message") or "Budget reservation denied.",
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
            self.budget.release(connection, self.project_id, reservation_id, str(missing))
            self._emit(connection, "typed_decision.reconciliation_required", dedupe, {
                "attempt": self.attempt, "message": str(missing),
            })
            return _Authority(
                outcome="reconcile", message=str(missing), invocation_dedupe=dedupe, role=role,
            )
        except JobDeferredError as deferred:
            self.budget.release(
                connection, self.project_id, reservation_id, str(deferred),
            )
            decision_id = self._decision(
                connection,
                f"Budget deferred typed decision {self.run_id}/{self.step_id}",
                "budget_deferred",
                {"attempt": self.attempt, "reason": str(deferred)},
            )
            self._emit(connection, "typed_decision.spend_refused", dedupe, {
                "decision_id": decision_id, "attempt": self.attempt, "message": str(deferred),
            })
            return _Authority(
                outcome="refused", message=str(deferred), invocation_dedupe=dedupe, role=role,
            )
        try:
            acquired = self.gateway.acquire(
                connection,
                self.project_id,
                job_id=job["id"],
                worker_id=self.worker_id,
                job_lease_token=leased["lease_token"],
                agent_id=AGENT_ID,
                skill_id=SKILL_ID,
                preferred_capabilities=["classify"],
                risk_tier=1,
                # The person the work is for. A subscription is owner-scoped: booking it for
                # the project read as pooling the operator's own Claude login, and refused.
                consumer_owner_id=self.actor,
                estimated_cost_usd_micros=MAX_COST_USD_MICROS,
                max_usage={"total_tokens": 20_000},
                dedupe_key=dedupe,
                ttl_seconds=self.ttl_seconds,
                existing_reservation_id=reservation_id,
                source_id=source_id,
            )
        except ModelCapacityDeniedError as denied:
            self.budget.release(connection, self.project_id, reservation_id, str(denied))
            decision_id = self._decision(
                connection,
                f"Model capacity refused typed decision {self.run_id}/{self.step_id}",
                "model_capacity_denied",
                {"attempt": self.attempt, "message": str(denied)},
            )
            self._emit(connection, "typed_decision.spend_refused", dedupe, {
                "decision_id": decision_id, "attempt": self.attempt, "message": str(denied),
            })
            return _Authority(
                outcome="refused", message=str(denied), invocation_dedupe=dedupe, role=role,
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
                "stage": "typed_decision",
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
        )

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
            "agent_id": AGENT_ID,
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
            JOB_KIND,
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
        return decider.decide(context, [question])

    def _settle_success(
        self, connection, authority: _Authority, batch: DecisionBatch, output: dict,
    ) -> dict:
        self._heartbeat(
            connection, authority.job_id, authority.job_lease_token,
            authority.lease_id, authority.lease_token,
        )
        actual = int(batch.cost_usd_micros or 0)
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

    def _fail(
        self, connection, authority: _Authority, error: BaseException, *,
        event_type: str, actual: int = 0,
    ) -> None:
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
            error={"reason": str(error)[:500]},
        )
        try:
            self.jobs.fail(
                connection, self.worker_id, authority.job_id, authority.job_lease_token,
                {"message": str(error)[:500]}, False, 0,
            )
        except (LeaseNotFoundError, RuntimeError):
            pass
        self._emit(connection, event_type, authority.invocation_dedupe, {
            "attempt": self.attempt,
            "receipt_id": authority.receipt_id,
            "message": str(error)[:500],
        })

    def _reconcile(self, connection, authority: _Authority, error: BaseException) -> None:
        self._decision(
            connection,
            f"Reconcile typed decision {self.run_id}/{self.step_id}",
            "typed_decision_reconciliation",
            {
                "attempt": self.attempt,
                "receipt_id": authority.receipt_id,
                "message": str(error)[:500],
            },
        )
        self._emit(connection, "typed_decision.reconciliation_required", authority.invocation_dedupe, {
            "run_id": self.run_id,
            "step_id": self.step_id,
            "attempt": self.attempt,
            "receipt_id": authority.receipt_id,
            "message": str(error)[:500],
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
