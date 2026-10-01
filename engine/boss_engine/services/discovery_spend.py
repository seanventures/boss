"""Spend one Discovery research stage only after its reservation, lease, and receipt commit.

Discovery used to run its four research calls on a daemon thread inside the API: at most one
budget reservation for the whole run, no worker lease, and after a restart it began again from
the first stage, repeating calls that had already been paid for. Community therefore refused to
run it at all (docs/operations/DISCOVERY_COMMUNITY_GA_BLOCKER.md).

A Community discovery is now a durable `discovery.run` job that a worker leases, and each stage
is one governed model call under that job's lease, the same shape as a typed decision
(typed_decision_spend.py):

    reserve budget -> model lease on the source that answers -> receipt in_progress   (commit)
    call the research agent
    settle the receipt and the reservation at the actual cost, keep the stage's output (commit)

The receipt is also the stage's memory. When the job is retried or re-leased after a crash, a
stage whose receipt SUCCEEDED returns the output it already paid for, so a stage is never paid
for twice. A receipt still IN PROGRESS means a call may have been charged without its answer
being kept; that stage blocks for reconciliation instead of being called again.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Callable, Optional

from ..config import Settings
from .core import create_blocking_decision, emit_event
from .typed_decision_spend import (
    _result_of,
    _uncertain,
    capacity_after_failure,
    model_receipt_dedupe,
)

JOB_KIND = "discovery.run"
AGENT_ID = "discovery"
SKILL_ID = "discovery.research"
# A research stage can legitimately run for many minutes (web search, many turns). The job
# lease's own ceiling is an hour, and the model lease is held for at most the same.
STAGE_TTL_SECONDS = 3600
# The research agent is the Claude CLI, so its capacity is the Claude subscription.
RESEARCH_SOURCE_ID = "anthropic_claude_code"
# The ceiling reserved before a stage; settlement keeps only what the call actually cost.
STAGE_ESTIMATE_USD_MICROS = 2_000_000


class DiscoverySpendRefused(RuntimeError):
    """Budget or model capacity refused the stage before anything was called.

    `transient` marks a refusal that clears by itself — the per-minute burst cap — so the job
    retries later instead of ending the run."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class DiscoveryReconciliationRequired(RuntimeError):
    """A stage may already have been charged. It must be reconciled, not called again."""


class DiscoveryStageSpend:
    def __init__(
        self,
        settings: Settings,
        *,
        project_id: str,
        run_id: str,
        job_id: str,
        worker_id: str,
        job_lease_token: str,
        actor: str,
        commit: Callable,
        budget: Any = None,
        gateway: Any = None,
        receipts: Any = None,
        events: Any = None,
        decisions: Any = None,
        estimate_usd_micros: Optional[int] = None,
        ttl_seconds: int = STAGE_TTL_SECONDS,
    ) -> None:
        from .budget import BudgetGovernor
        from .model_gateway import ModelGatewayService
        from .receipts import ReceiptService

        self.settings = settings
        self.project_id = project_id
        self.run_id = run_id
        self.job_id = job_id
        self.worker_id = worker_id
        self.job_lease_token = job_lease_token
        self.actor = actor or worker_id
        self.commit = commit
        self.budget = budget or BudgetGovernor(settings)
        self.gateway = gateway or ModelGatewayService(settings)
        self.receipts = receipts or ReceiptService()
        self.events = events
        self.decisions = decisions
        self.estimate = int(STAGE_ESTIMATE_USD_MICROS if estimate_usd_micros is None
                            else estimate_usd_micros)
        self.ttl_seconds = int(ttl_seconds)

    @classmethod
    def for_engine(cls, settings: Settings, engine: Any, **kwargs: Any) -> "DiscoveryStageSpend":
        def commit(fn):
            with engine.begin() as connection:
                return fn(connection)

        return cls(settings, commit=commit, **kwargs)

    # -- the one public call ------------------------------------------------------------------
    def run(self, stage: str, call: Callable[[], dict], *,
            source_id: Optional[str] = None) -> dict:
        """Run one stage's model call under committed spend authority, or replay it."""
        identity = f"discovery:{self.run_id}:{stage}"
        # A refusal is RETURNED from inside the transaction and raised after it commits: raised
        # inside, it rolled back the very event and decision that say why the stage stopped.
        prior = self.commit(lambda c: self._prior(c, identity, stage))
        if prior.get("replay") is not None:
            return prior["replay"]
        if prior.get("reconcile"):
            raise DiscoveryReconciliationRequired(prior["reconcile"])
        dedupe = identity if not prior["failed"] else f"{identity}:retry:{prior['failed'] + 1}"
        authority = self.commit(lambda c: self._open(c, dedupe, stage, source_id))
        if authority.get("refused"):
            raise DiscoverySpendRefused(authority["refused"],
                                        transient=bool(authority.get("transient")))
        if authority.get("reconcile"):
            raise DiscoveryReconciliationRequired(authority["reconcile"])
        try:
            result = call()
        except Exception as error:  # noqa: BLE001 — every outcome is recorded below
            reason = error
            if _uncertain(reason):
                self.commit(lambda c: self._reconcile(c, authority, stage, reason))
                raise DiscoveryReconciliationRequired(
                    f"Discovery stage '{stage}' may have been charged without an answer "
                    f"({reason}); reconcile receipt {authority['receipt_id']} before retrying."
                ) from reason
            self.commit(lambda c: self._settle(c, authority, stage, None, reason))
            raise
        return self.commit(lambda c: self._settle(c, authority, stage, result, None))

    # -- steps ----------------------------------------------------------------------------------
    def _prior(self, connection, identity: str, stage: str) -> dict:
        org_id = self.gateway._project_scope(connection, self.project_id)["org_id"]
        rows = self.receipts.list_by_dedupe_prefix(
            connection, org_id, model_receipt_dedupe(self.project_id, identity))
        # Only this stage: "sweep" must not match "sweep_extra".
        mine = [row for row in rows
                if row["dedupe_key"] == model_receipt_dedupe(self.project_id, identity)
                or row["dedupe_key"].startswith(
                    model_receipt_dedupe(self.project_id, f"{identity}:retry:"))]
        for row in mine:
            if row.get("status") == "succeeded":
                output = _result_of(row).get("output")
                if isinstance(output, dict):
                    self._emit(connection, "discovery.stage_replayed",
                               {"stage": stage, "receipt_id": row["id"]})
                    return {"replay": output, "failed": 0}
        if any(row.get("status") == "in_progress" for row in mine):
            self._decision(connection, f"Reconcile Discovery {self.run_id} stage {stage}",
                           "discovery_reconciliation",
                           {"stage": stage, "receipts": [row["id"] for row in mine]})
            self._emit(connection, "discovery.reconciliation_required", {"stage": stage})
            return {"reconcile": (
                f"Discovery stage '{stage}' has a call in progress from an earlier attempt that "
                "may already have been charged; reconcile it before research continues.")}
        return {"replay": None,
                "failed": sum(1 for row in mine if row.get("status") == "failed")}

    def _open(self, connection, dedupe: str, stage: str, source_id: Optional[str]) -> dict:
        from .model_gateway import ModelCapacityDeniedError

        # NO LARGER THAN THE PROJECT'S PER-MINUTE CAP. The burst cap sums the last minute's
        # reservations, so a stage reserving more than the cap could never be granted: the
        # default policy allows $0.10/min and a stage reserved $2, so every Community discovery
        # was refused at its first stage. The actual cost is still settled in full, and the
        # daily and monthly caps still govern every later reservation.
        burst_cap = int(getattr(self.budget, "_burst_cap", lambda *_: 0)(
            connection, self.project_id) or 0)
        amount = min(self.estimate, burst_cap) if burst_cap > 0 else self.estimate
        reserved = self.budget.reserve(
            connection, self.project_id, "model_gateway", amount,
            f"Discovery {self.run_id} stage {stage}",
            min(240, max(5, math.ceil(self.ttl_seconds / 60) + 5)),
            job_id=self.job_id, risk_tier=1,
        )
        if reserved.get("status") != "reserved":
            message = reserved.get("message") or "Budget reservation denied."
            # The per-minute cap clears within a minute: retry, and no decision for a person.
            transient = "burst cap" in message.lower()
            if not transient:
                self._decision(connection, f"Budget refused Discovery {self.run_id} stage {stage}",
                               "budget_denied", {"stage": stage, "message": message})
            self._emit(connection, "discovery.spend_refused",
                       {"stage": stage, "message": message, "transient": transient})
            return {"refused": message, "transient": transient}
        reservation_id = reserved["reservation_id"]
        try:
            acquired = self.gateway.acquire(
                connection, self.project_id,
                job_id=self.job_id, worker_id=self.worker_id,
                job_lease_token=self.job_lease_token,
                agent_id=AGENT_ID, skill_id=SKILL_ID,
                preferred_capabilities=["research"], risk_tier=1,
                # The person the research is for; a subscription is owner-scoped.
                consumer_owner_id=self.actor,
                estimated_cost_usd_micros=amount,
                max_usage={"total_tokens": 2_000_000},
                dedupe_key=dedupe, ttl_seconds=self.ttl_seconds,
                existing_reservation_id=reservation_id, source_id=source_id,
            )
        except ModelCapacityDeniedError as denied:
            self.budget.release(connection, self.project_id, reservation_id, str(denied))
            self._emit(connection, "discovery.spend_refused", {"stage": stage, "message": str(denied)})
            return {"refused": str(denied)}
        if acquired.get("duplicate") or not acquired.get("lease_token"):
            self.budget.release(connection, self.project_id, reservation_id,
                                "Model lease already existed for this stage.")
            return {"reconcile": f"Discovery stage '{stage}' already holds a model lease; reconcile it."}
        lease_id = acquired["lease"]["id"]
        started = self.gateway.start_invocation(
            connection, self.project_id, lease_id,
            worker_id=self.worker_id, lease_token=acquired["lease_token"],
            job_lease_token=self.job_lease_token,
            request_hash=hashlib.sha256(dedupe.encode("utf-8")).hexdigest(),
            dedupe_key=dedupe,
            metadata={"stage": f"discovery:{stage}"[:120], "operation": self.run_id[:256]},
        )
        invocation = started["invocation"]
        self._emit(connection, "discovery.stage_started",
                   {"stage": stage, "receipt_id": invocation["receipt_id"],
                    "reservation_id": reservation_id})
        return {"reservation_id": reservation_id, "lease_id": lease_id,
                "lease_token": acquired["lease_token"], "invocation_id": invocation["id"],
                "receipt_id": invocation["receipt_id"], "dedupe": dedupe}

    def _settle(self, connection, authority: dict, stage: str,
                result: Optional[dict], error: Optional[BaseException]) -> dict:
        succeeded = error is None
        actual = int((result or {}).get("cost_usd_micros") or 0)
        usage = {key: int(value) for key, value in ((result or {}).get("usage") or {}).items()
                 if isinstance(value, (int, float)) and not isinstance(value, bool)}
        self.gateway.settle_invocation(
            connection, self.project_id, authority["lease_id"], authority["invocation_id"],
            worker_id=self.worker_id, lease_token=authority["lease_token"],
            job_lease_token=self.job_lease_token, succeeded=succeeded,
            provider_ref=(f"discovery:{(result or {}).get('model') or 'research'}"[:200]
                          if succeeded else None),
            usage_units=usage, actual_cost_usd_micros=actual,
            observed_capacity=({"capacity_state": "available"} if succeeded
                               else capacity_after_failure(error)),
            error={} if succeeded else {"reason": str(error)[:500]},
        )
        if succeeded:
            # The stage's output lives on its receipt: a retry replays it instead of paying again.
            self.receipts.merge_result(connection, authority["receipt_id"],
                                       {"stage": stage, "output": result})
        self._emit(connection,
                   "discovery.stage_settled" if succeeded else "discovery.stage_failed",
                   {"stage": stage, "receipt_id": authority["receipt_id"],
                    "reservation_id": authority["reservation_id"],
                    "actual_cost_usd_micros": actual,
                    **({} if succeeded else {"message": str(error)[:500]})})
        return result or {}

    def _reconcile(self, connection, authority: dict, stage: str, error: BaseException) -> None:
        self._decision(connection, f"Reconcile Discovery {self.run_id} stage {stage}",
                       "discovery_reconciliation",
                       {"stage": stage, "receipt_id": authority["receipt_id"],
                        "message": str(error)[:500]})
        self._emit(connection, "discovery.reconciliation_required",
                   {"stage": stage, "receipt_id": authority["receipt_id"],
                    "message": str(error)[:500]})

    # -- records --------------------------------------------------------------------------------
    def _emit(self, connection, event_type: str, data: dict) -> None:
        data = {"run_id": self.run_id, "job_id": self.job_id, **data}
        if self.events is not None:
            self.events(connection, self.project_id, event_type, self.actor,
                        "discovery_run", self.run_id, data)
            return
        emit_event(connection, self.project_id, event_type, self.actor,
                   "discovery_run", self.run_id, data)

    def _decision(self, connection, question: str, reason: str, detail: dict) -> str:
        if self.decisions is not None:
            return self.decisions(connection, self.project_id, question, reason, detail)
        return create_blocking_decision(connection, self.project_id, None, question,
                                        {"action": reason}, detail, reason)
