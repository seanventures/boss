"""Governed typed-decision spend: reserve, lease, one receipt, settle, visible retry.

The gateway double is not a second ledger. It refuses a model attempt that has no
active job lease and no active budget reservation, which is the same gate
ModelGatewayService.acquire enforces for a paid call.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from boss_engine.services.typed_decisions import Decision, DecisionBatch


PROJECT = "proj"
RUN = "run-1"
STEP = "route"
QUOTE = 10_000


def _binding() -> dict:
    return {
        "typed_decision": {
            "question": "Which team handles this ticket?",
            "options": {"billing": "Payments", "technical": "Bugs"},
            "min_confidence": 0.8,
        },
        "inputs": {"state": {"from_input": "ticket"}},
    }


class Clock:
    def __init__(self) -> None:
        self.now_at = datetime(2026, 9, 29, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.now_at

    def advance(self, seconds: int) -> None:
        self.now_at += timedelta(seconds=seconds)


class Budget:
    """Active reservations count against the cap. Consuming one drops that hold
    and keeps only the settled actual."""

    def __init__(self, cap_usd_micros: int) -> None:
        self.cap = cap_usd_micros
        self.reservations: dict[str, dict] = {}
        self.actual = 0
        self.decisions: list[dict] = []

    def active_reserved(self) -> int:
        return sum(
            row["amount"] for row in self.reservations.values() if row["status"] == "active"
        )

    def available(self) -> int:
        return self.cap - self.active_reserved() - self.actual

    def reserve(self, connection, project_id, category, amount, reason, expires_in_minutes,
                job_id=None, allocation_id=None, risk_tier=None):
        if amount > self.available():
            decision_id = f"dec-{len(self.decisions) + 1}"
            self.decisions.append({
                "id": decision_id,
                "reason": "budget_denied",
                "requested": amount,
                "available": self.available(),
            })
            return {
                "status": "denied",
                "reservation_id": None,
                "decision_id": decision_id,
                "message": "Budget cap exceeded. Decision created.",
            }
        reservation_id = f"res-{len(self.reservations) + 1}"
        self.reservations[reservation_id] = {
            "id": reservation_id,
            "amount": amount,
            "status": "active",
            "job_id": job_id,
            "category": category,
        }
        return {
            "status": "reserved",
            "reservation_id": reservation_id,
            "decision_id": None,
            "message": "Reservation created.",
        }

    def attach_job(self, connection, project_id, reservation_id, job_id) -> None:
        self.reservations[reservation_id]["job_id"] = job_id

    def assert_active_reservation(self, connection, project_id, reservation_id):
        row = self.reservations.get(reservation_id)
        if not row or row["status"] != "active":
            return None
        return {"id": reservation_id, "amount_usd_micros": row["amount"], "status": "active"}

    def release(self, connection, project_id, reservation_id, reason) -> dict:
        row = self.reservations.get(reservation_id)
        if not row:
            return {"status": "not_found", "reservation_id": reservation_id}
        if row["status"] != "active":
            return {"status": row["status"], "reservation_id": reservation_id}
        row["status"] = "released"
        row["release_reason"] = reason
        return {"status": "released", "reservation_id": reservation_id}

    def consume(self, connection, project_id, reservation_id, receipt_id, category,
                skill_or_model_id, estimated, actual, actor) -> dict:
        row = self.assert_active_reservation(connection, project_id, reservation_id)
        if not row:
            raise ValueError("Active budget reservation not found.")
        charged = row["amount_usd_micros"] if actual is None else int(actual)
        self.actual += charged
        self.reservations[reservation_id]["status"] = "consumed"
        self.reservations[reservation_id]["actual"] = charged
        return {"status": "consumed", "reservation_id": reservation_id}


class Jobs:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.rows: dict[str, dict] = {}
        self.by_dedupe: dict[str, str] = {}

    def enqueue_job(self, connection, project_id, kind, payload=None, **kwargs) -> dict:
        dedupe_key = kwargs["dedupe_key"]
        existing = self.by_dedupe.get(dedupe_key)
        if existing:
            return {**self.rows[existing], "created": False}
        job_id = f"job-{len(self.rows) + 1}"
        self.rows[job_id] = {
            "id": job_id,
            "project_id": project_id,
            "kind": kind,
            "status": "queued",
            "payload": payload or {},
            "dedupe_key": dedupe_key,
            "lease_token": None,
            "lease_owner": None,
            "expires_at": None,
        }
        self.by_dedupe[dedupe_key] = job_id
        return {**self.rows[job_id], "created": True}

    def lease_owned_job(self, connection, *, project_id, job_id, worker_id, lease_ttl_seconds):
        row = self.rows[job_id]
        if row["project_id"] != project_id:
            raise RuntimeError("wrong project")
        if row["status"] != "queued":
            raise RuntimeError(f"Job is {row['status']}.")
        row["status"] = "leased"
        row["lease_owner"] = worker_id
        row["lease_token"] = f"lease-{job_id}"
        row["expires_at"] = self.clock.now() + timedelta(seconds=lease_ttl_seconds)
        return {"id": job_id, "lease_token": row["lease_token"], "project_id": project_id}

    def require_active_lease(self, connection, worker_id, job_id, lease_token) -> dict:
        row = self.rows[job_id]
        if row["lease_owner"] != worker_id or row["lease_token"] != lease_token:
            raise RuntimeError("Lease token is invalid.")
        if row["status"] not in {"leased", "running"}:
            raise RuntimeError("Job is not actively leased.")
        if row["expires_at"] <= self.clock.now():
            raise RuntimeError("Lease has expired.")
        return {"id": job_id, "project_id": row["project_id"], "payload": row["payload"]}

    def heartbeat(self, connection, worker_id, job_id, lease_token, lease_ttl_seconds) -> dict:
        self.require_active_lease(connection, worker_id, job_id, lease_token)
        row = self.rows[job_id]
        row["status"] = "running"
        row["expires_at"] = self.clock.now() + timedelta(seconds=lease_ttl_seconds)
        return {"job_id": job_id, "status": "running"}

    def complete(self, connection, worker_id, job_id, lease_token, result=None) -> dict:
        self.require_active_lease(connection, worker_id, job_id, lease_token)
        self.rows[job_id]["status"] = "succeeded"
        return {"job_id": job_id, "status": "succeeded"}

    def fail(self, connection, worker_id, job_id, lease_token, error, retryable, retry_delay_seconds):
        self.require_active_lease(connection, worker_id, job_id, lease_token)
        self.rows[job_id]["status"] = "failed_final"
        return {"job_id": job_id, "status": "failed_final"}


class Receipts:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.by_dedupe: dict[tuple[str, str], str] = {}

    def create_or_reuse(self, connection, org_id, workspace_id, project_id, dedupe_key,
                        effect_kind, effect_scope, payload, created_by_job_id=None,
                        data_boundary=None):
        key = (org_id, dedupe_key)
        existing = self.by_dedupe.get(key)
        if existing:
            return self.rows[existing], False
        receipt_id = f"rcpt-{len(self.rows) + 1}"
        self.rows[receipt_id] = {
            "id": receipt_id,
            "org_id": org_id,
            "project_id": project_id,
            "dedupe_key": dedupe_key,
            "status": "in_progress",
            "result": {},
            "error": None,
            "created_by_job_id": created_by_job_id,
        }
        self.by_dedupe[key] = receipt_id
        return self.rows[receipt_id], True

    def list_by_dedupe_prefix(self, connection, org_id, prefix) -> list[dict]:
        return [
            row for row in self.rows.values()
            if row["org_id"] == org_id and row["dedupe_key"].startswith(prefix)
        ]

    def merge_result(self, connection, receipt_id, result) -> None:
        self.rows[receipt_id]["result"] = {**(self.rows[receipt_id].get("result") or {}), **result}

    def mark_succeeded(self, connection, receipt_id, provider_ref, result) -> None:
        row = self.rows[receipt_id]
        row["status"] = "succeeded"
        row["result"] = result

    def mark_failed(self, connection, receipt_id, error) -> None:
        row = self.rows[receipt_id]
        row["status"] = "failed"
        row["error"] = error


class Gateway:
    """Requires the worker's job lease and an active reservation before a lease exists."""

    def __init__(self, clock: Clock, jobs: Jobs, budget: Budget, receipts: Receipts) -> None:
        self.clock = clock
        self.jobs = jobs
        self.budget = budget
        self.receipts = receipts
        self.leases: dict[str, dict] = {}
        self.invocations: dict[str, dict] = {}
        self.acquire_calls = 0

    def active_lease_count(self) -> int:
        return sum(
            1 for row in self.leases.values()
            if row["status"] == "active" and row["expires_at"] > self.clock.now()
        )

    def acquire(self, connection, project_id, *, job_id, worker_id, job_lease_token,
                agent_id, skill_id, preferred_capabilities, risk_tier, consumer_owner_id,
                estimated_cost_usd_micros, max_usage, dedupe_key, ttl_seconds,
                existing_reservation_id=None, source_id=None):
        self.acquire_calls += 1
        self.acquired_for = getattr(self, "acquired_for", []) + [(source_id, consumer_owner_id)]
        self.jobs.require_active_lease(connection, worker_id, job_id, job_lease_token)
        if not existing_reservation_id:
            from boss_engine.services.model_gateway import ModelCapacityDeniedError
            raise ModelCapacityDeniedError(
                "Paid model capacity requires a positive cost estimate and reservation."
            )
        active = self.budget.assert_active_reservation(
            connection, project_id, existing_reservation_id
        )
        if not active or int(active["amount_usd_micros"]) < int(estimated_cost_usd_micros):
            from boss_engine.services.model_gateway import ModelCapacityDeniedError
            raise ModelCapacityDeniedError("Budget reservation does not cover the model estimate.")
        existing = next((row for row in self.leases.values() if row["dedupe_key"] == dedupe_key), None)
        if existing:
            return {"lease": existing, "lease_token": None, "duplicate": True}
        lease_id = f"mlease-{len(self.leases) + 1}"
        token = f"model-{lease_id}"
        row = {
            "id": lease_id,
            "project_id": project_id,
            "job_id": job_id,
            "worker_id": worker_id,
            "status": "active",
            "dedupe_key": dedupe_key,
            "budget_reservation_id": existing_reservation_id,
            "expires_at": self.clock.now() + timedelta(seconds=ttl_seconds),
            "lease_token": token,
            "estimated_cost_usd_micros": estimated_cost_usd_micros,
        }
        self.leases[lease_id] = row
        return {"lease": row, "lease_token": token, "duplicate": False}

    def _load(self, lease_id, worker_id, lease_token) -> dict:
        from boss_engine.services.model_gateway import ModelGatewayLeaseError
        row = self.leases.get(lease_id)
        if not row or row["worker_id"] != worker_id or row["lease_token"] != lease_token:
            raise ModelGatewayLeaseError("Model capacity lease token is invalid.")
        if row["status"] != "active":
            raise ModelGatewayLeaseError(f"Model capacity lease is {row['status']}.")
        if row["expires_at"] <= self.clock.now():
            raise ModelGatewayLeaseError("Model capacity lease has expired.")
        return row

    def heartbeat(self, connection, project_id, lease_id, *, worker_id, lease_token,
                  job_lease_token, ttl_seconds):
        row = self._load(lease_id, worker_id, lease_token)
        self.jobs.require_active_lease(connection, worker_id, row["job_id"], job_lease_token)
        row["expires_at"] = self.clock.now() + timedelta(seconds=ttl_seconds)
        return row

    def start_invocation(self, connection, project_id, lease_id, *, worker_id, lease_token,
                         job_lease_token, request_hash, dedupe_key, metadata):
        from boss_engine.services.typed_decision_spend import model_receipt_dedupe
        row = self._load(lease_id, worker_id, lease_token)
        self.jobs.require_active_lease(connection, worker_id, row["job_id"], job_lease_token)
        existing = next(
            (item for item in self.invocations.values() if item["dedupe_key"] == dedupe_key),
            None,
        )
        if existing:
            return {"invocation": existing, "duplicate": True}
        receipt, _created = self.receipts.create_or_reuse(
            connection, "org", "ws", project_id,
            model_receipt_dedupe(project_id, dedupe_key),
            "model.invoke", {}, {"request_hash": request_hash},
            created_by_job_id=row["job_id"],
        )
        invocation_id = f"minv-{len(self.invocations) + 1}"
        invocation = {
            "id": invocation_id,
            "status": "in_progress",
            "receipt_id": receipt["id"],
            "dedupe_key": dedupe_key,
            "lease_id": lease_id,
        }
        self.invocations[invocation_id] = invocation
        return {"invocation": invocation, "duplicate": False}

    def settle_invocation(self, connection, project_id, lease_id, invocation_id, *,
                          worker_id, lease_token, job_lease_token, succeeded, provider_ref,
                          usage_units, actual_cost_usd_micros, observed_capacity, error):
        row = self._load(lease_id, worker_id, lease_token)
        self.jobs.require_active_lease(connection, worker_id, row["job_id"], job_lease_token)
        invocation = self.invocations[invocation_id]
        if invocation["status"] != "in_progress":
            return invocation
        reservation_id = row["budget_reservation_id"]
        actual = int(actual_cost_usd_micros or 0)
        if succeeded or actual > 0:
            self.budget.consume(
                connection, project_id, reservation_id, invocation["receipt_id"],
                "model_gateway", "typed_decision", row["estimated_cost_usd_micros"],
                actual, worker_id,
            )
            self.receipts.mark_succeeded(
                connection, invocation["receipt_id"], provider_ref, {"usage_units": usage_units}
            )
            invocation["status"] = "succeeded"
            row["status"] = "completed"
        else:
            self.budget.release(
                connection, project_id, reservation_id, "Model invocation failed before billable use."
            )
            self.receipts.mark_failed(connection, invocation["receipt_id"], error or {})
            invocation["status"] = "failed"
            row["status"] = "failed"
        return invocation

    def abandon_expired_attempt(self, connection, project_id, lease_id, *, worker_id,
                                invocation_id, reason):
        row = self.leases[lease_id]
        if row["worker_id"] != worker_id:
            raise RuntimeError("owned by another worker")
        if row["status"] == "active" and row["expires_at"] > self.clock.now():
            raise RuntimeError("lease is still active")
        if row["status"] == "active":
            row["status"] = "expired"
            if row["budget_reservation_id"]:
                self.budget.release(
                    connection, project_id, row["budget_reservation_id"], reason
                )
        invocation = self.invocations.get(invocation_id)
        if invocation and invocation["status"] == "in_progress":
            self.receipts.mark_failed(
                connection, invocation["receipt_id"], {"reason": reason}
            )
            invocation["status"] = "failed"
        return {"status": row["status"]}


class Events:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def __call__(self, connection, project_id, event_type, actor, entity_type, entity_id, data):
        self.rows.append({"event_type": event_type, "entity_id": entity_id, "data": data})
        return event_type

    def types(self) -> list[str]:
        return [row["event_type"] for row in self.rows]


class Decisions:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def __call__(self, connection, project_id, question, reason, detail) -> str:
        decision_id = f"dec-spend-{len(self.rows) + 1}"
        self.rows.append({
            "id": decision_id, "question": question, "reason": reason, "detail": detail,
        })
        return decision_id


class Scripted:
    def __init__(self, budget: Budget, gateway: Gateway, events_open) -> None:
        self.budget = budget
        self.gateway = gateway
        self.events_open = events_open
        self.calls = 0
        self.fail: BaseException | None = None
        self.answer = "billing"
        self.confidence = 0.93
        self.cost = 4_000

    def decide(self, context, questions):
        assert self.events_open() is False, "provider call ran inside the uncommitted transaction"
        assert self.budget.active_reserved() >= QUOTE, "provider call without a live reservation"
        assert self.gateway.active_lease_count() >= 1, "provider call without a live model lease"
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        assert "charged twice" in str(context)
        return DecisionBatch(
            {"decision": Decision(self.answer, self.confidence, reason="refund")},
            "fake", "m", 4, cost_usd_micros=self.cost, usage={"total_tokens": 120},
        )


class World:
    def __init__(self, cap: int = 50_000) -> None:
        self.clock = Clock()
        self.budget = Budget(cap)
        self.jobs = Jobs(self.clock)
        self.receipts = Receipts()
        self.gateway = Gateway(self.clock, self.jobs, self.budget, self.receipts)
        self.events = Events()
        self.decisions = Decisions()
        self.open_transactions = 0
        self.commits = 0
        self.decider = Scripted(self.budget, self.gateway, lambda: self.open_transactions > 0)

    def commit(self, fn):
        self.open_transactions += 1
        try:
            return fn(self)
        finally:
            self.open_transactions -= 1
            self.commits += 1

    def spend(self, attempt: int = 1, *, before_provider_call=None):
        from boss_engine.services.typed_decision_spend import TypedDecisionSpend

        return TypedDecisionSpend(
            project_id=PROJECT,
            run_id=RUN,
            step_id=STEP,
            attempt=attempt,
            actor="workflow",
            worker_id="worker-1",
            budget=self.budget,
            jobs=self.jobs,
            gateway=self.gateway,
            receipts=self.receipts,
            events=self.events,
            decisions=self.decisions,
            decider_factory=lambda role, settings: self.decider,
            commit=self.commit,
            org_id="org",
            workspace_id="ws",
            before_provider_call=before_provider_call,
            ttl_seconds=120,
        )

    def execute(self, attempt: int = 1, *, before_provider_call=None):
        return self.spend(attempt, before_provider_call=before_provider_call).execute(
            _settings(), _binding(), "My card was charged twice",
        )


def _settings():
    from types import SimpleNamespace
    return SimpleNamespace(
        decision_backend="claude",
        decision_model="opus",
        decision_fallback_below=0,
    )


def test_reservation_is_refused_when_funds_are_insufficient():
    world = World(cap=1_000)
    with pytest.raises(Exception, match="Budget cap exceeded|spend refused|BOSS-451|budget"):
        world.execute()
    assert world.decider.calls == 0
    assert world.gateway.acquire_calls == 0
    assert world.budget.active_reserved() == 0
    assert world.receipts.rows == {}
    assert world.budget.decisions, "a refused reservation must leave a decision"
    assert "typed_decision.spend_refused" in world.events.types()


def test_expired_lease_does_not_call_the_model_and_releases_the_reservation():
    world = World()

    def expire() -> None:
        world.clock.advance(121)

    with pytest.raises(Exception, match="expired"):
        world.execute(before_provider_call=expire)
    assert world.decider.calls == 0
    assert world.budget.active_reserved() == 0
    assert all(row["status"] != "in_progress" for row in world.receipts.rows.values())
    assert "typed_decision.lease_expired" in world.events.types()


def test_receipt_replay_returns_the_original_receipt_without_a_second_call():
    world = World()
    first = world.execute()
    second = world.execute()
    assert first["answer"] == "billing"
    assert second["receipt_id"] == first["receipt_id"]
    assert second["answer"] == first["answer"]
    assert world.decider.calls == 1
    assert len(world.receipts.rows) == 1


def test_settlement_decrements_the_reservation_and_keeps_only_the_actual_cost():
    world = World(cap=50_000)
    output = world.execute()
    assert output["cost_usd_micros"] == 4_000
    assert world.budget.active_reserved() == 0
    assert world.budget.actual == 4_000
    assert world.budget.available() == 46_000
    reservation = next(iter(world.budget.reservations.values()))
    assert reservation["amount"] == QUOTE
    assert reservation["status"] == "consumed"
    assert reservation["actual"] == 4_000


def test_a_failed_attempt_releases_the_reservation_and_a_retry_is_visible():
    world = World()
    world.decider.fail = RuntimeError("provider rejected the prompt")
    with pytest.raises(Exception, match="provider rejected"):
        world.execute(attempt=1)
    assert world.decider.calls == 1
    assert world.budget.active_reserved() == 0
    assert len(world.receipts.rows) == 1
    assert next(iter(world.receipts.rows.values()))["status"] == "failed"
    with pytest.raises(Exception, match="already failed"):
        world.execute(attempt=1)
    assert world.decider.calls == 1, "the same attempt must not call the provider again"
    assert len(world.receipts.rows) == 1

    world.decider.fail = None
    retried = world.execute(attempt=2)
    assert retried["answer"] == "billing"
    assert world.decider.calls == 2
    assert len(world.receipts.rows) == 2
    assert "typed_decision.retry" in world.events.types()
    assert world.budget.active_reserved() == 0
    assert world.budget.actual == 4_000


def test_an_uncertain_outcome_stays_open_and_is_not_retried_silently():
    world = World()
    world.decider.fail = TimeoutError("timed out")
    from boss_engine.services.typed_decision_spend import TypedDecisionReconciliationRequired
    with pytest.raises(TypedDecisionReconciliationRequired):
        world.execute()
    assert world.budget.active_reserved() == QUOTE
    assert next(iter(world.receipts.rows.values()))["status"] == "in_progress"
    assert world.decisions.rows
    assert "typed_decision.reconciliation_required" in world.events.types()
    with pytest.raises(TypedDecisionReconciliationRequired):
        world.execute(attempt=2)
    assert world.decider.calls == 1


def test_cancellation_before_the_call_releases_the_reservation():
    from boss_engine.services.typed_decision_spend import TypedDecisionCancelled

    world = World()

    def cancel() -> None:
        raise TypedDecisionCancelled("operator cancelled the step")

    with pytest.raises(Exception, match="cancelled"):
        world.execute(before_provider_call=cancel)
    assert world.decider.calls == 0
    assert world.budget.active_reserved() == 0
    assert "typed_decision.cancelled" in world.events.types()


def test_mocked_gateway_requires_a_lease_and_a_reservation():
    world = World()
    from boss_engine.services.model_gateway import ModelCapacityDeniedError

    job = world.jobs.enqueue_job(
        world, PROJECT, "typed_decision.execute", {"agent_id": "typed_decision"},
        dedupe_key="job-gate",
    )
    leased = world.jobs.lease_owned_job(
        world, project_id=PROJECT, job_id=job["id"], worker_id="worker-1", lease_ttl_seconds=120,
    )
    with pytest.raises(ModelCapacityDeniedError, match="reservation"):
        world.gateway.acquire(
            world, PROJECT, job_id=job["id"], worker_id="worker-1",
            job_lease_token=leased["lease_token"], agent_id="typed_decision",
            skill_id="typed_decision", preferred_capabilities=["classify"], risk_tier=1,
            consumer_owner_id=PROJECT, estimated_cost_usd_micros=QUOTE,
            max_usage={"total_tokens": 100}, dedupe_key="no-reservation", ttl_seconds=120,
            existing_reservation_id=None,
        )
    reserved = world.budget.reserve(
        world, PROJECT, "model_gateway", QUOTE, "cover", 10,
    )
    with pytest.raises(RuntimeError, match="Lease token is invalid"):
        world.gateway.acquire(
            world, PROJECT, job_id=job["id"], worker_id="worker-1",
            job_lease_token="not-the-lease", agent_id="typed_decision",
            skill_id="typed_decision", preferred_capabilities=["classify"], risk_tier=1,
            consumer_owner_id=PROJECT, estimated_cost_usd_micros=QUOTE,
            max_usage={"total_tokens": 100}, dedupe_key="bad-lease", ttl_seconds=120,
            existing_reservation_id=reserved["reservation_id"],
        )


class _Named(Scripted):
    """A scripted decider that, like the real ones, names its backend and its model source."""

    def __init__(self, world: World, backend: str, source_id: str) -> None:
        super().__init__(world.budget, world.gateway, lambda: world.open_transactions > 0)
        self.backend, self.source_id = backend, source_id

    def decide(self, context, questions):
        batch = super().decide(context, questions)
        batch.backend = self.backend
        return batch


def _jev_then_claude(world: World):
    jev = _Named(world, "typesafe", "typesafe_jev")
    claude = _Named(world, "claude_code", "anthropic_claude_code")
    claude.answer = "technical"
    spend = world.spend()
    spend.decider_factory = lambda role, settings: jev if role == "primary" else claude
    return spend, jev, claude


def test_each_attempt_books_capacity_on_the_source_that_answers_for_the_actor():
    """BOSS-451: the lease was booked for the PROJECT against whatever the router chose — on a
    real install, the operator's Claude subscription, which refused it as pooled capacity."""
    world = World()
    spend, jev, _ = _jev_then_claude(world)
    output = spend.execute(_settings(), _binding(), "My card was charged twice")
    assert output["answer"] == "billing" and jev.calls == 1
    assert world.gateway.acquired_for == [("typesafe_jev", "workflow")]


def test_when_jev_fails_claude_answers_on_its_own_receipt():
    """TypeSafe is the default decider and Claude the fallback — a separate, receipted attempt
    against Claude's own source, after a visible fallback event."""
    world = World()
    spend, jev, claude = _jev_then_claude(world)
    jev.fail = RuntimeError("TypeSafe HTTP 500")
    output = spend.execute(_settings(), _binding(), "My card was charged twice")
    assert output["answer"] == "technical" and (jev.calls, claude.calls) == (1, 1)
    assert world.gateway.acquired_for == [("typesafe_jev", "workflow"),
                                          ("anthropic_claude_code", "workflow")]
    statuses = sorted(row["status"] for row in world.receipts.rows.values())
    assert statuses == ["failed", "succeeded"], "each provider attempt has its own receipt"
    assert "typed_decision.fallback" in world.events.types()
    assert world.budget.active_reserved() == 0


def test_proving_a_key_never_falls_back():
    """A Claude answer after a Jev failure would prove nothing about the Jev key."""
    world = World()
    spend, jev, claude = _jev_then_claude(world)
    jev.fail = RuntimeError("TypeSafe HTTP 401")
    with pytest.raises(Exception, match="401"):
        spend.execute(_settings(), _binding(), "My card was charged twice", fallback=False)
    assert claude.calls == 0


def test_an_uncertain_jev_outcome_is_not_answered_by_claude():
    """It may already be charged and may yet answer; it waits for reconciliation."""
    world = World()
    spend, jev, claude = _jev_then_claude(world)
    jev.fail = TimeoutError("read timed out")
    with pytest.raises(Exception, match="reconcil"):
        spend.execute(_settings(), _binding(), "My card was charged twice")
    assert claude.calls == 0
