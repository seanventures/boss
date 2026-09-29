# BOSS-451 typed decision model-spend hold

**Plane:** [BOSS-451](https://plane.local.sean.network/goldeneye/projects/cc97d5cc-e8c6-4893-ac14-7f646c37f3d7/issues/621dbb33-c55a-4149-911d-119d6cf75e8e) · **Status:** In Progress · **Release:** workflow attempts spend through the existing budget, lease, and receipt path. Key proof and Community/VM qualification are still open.

## Scope and observed gap

A product workflow step with `binding.typed_decision` called `typed_decisions.run_step()` inline from `workflow_capability_runner.tick()`. That path selected Claude CLI, the OpenAI API fallback in the worker, or TypeSafe Jev. Jev could retry and then ask Claude for a second opinion. The decision result reported an estimated cost, but there was no pre-spend `BudgetReservation`, model-capacity lease, `Receipt` with a dedupe key, cost settlement, or event for each provider attempt.

The delivery-workflow worker calls `POST /v1/delivery-workflows/run-capabilities`. The API wrapped the entire tick in one database transaction. The workflow step lease was claimed and the provider call happened before the transaction committed. Adding a reservation or receipt inside that same transaction would not make it durable: a crash after provider spend would roll back both records. The existing `ModelGatewayService.acquire()` requires a durable BOSS `jobs` lease and token; it cannot accept this workflow-step lease as a substitute.

TypeSafe key setup had a second spend path: `POST /v1/setup/decision-model/key` called Jev to prove the key inside its API transaction, before any reservation or receipt. A failed secret-store write or process crash could erase all evidence of that billed proof.

## What is in this release

A workflow `binding.typed_decision` can run when the attempt has a spend authority (`settings`, a database engine, project, run, and step). `typed_decisions.run_step` still raises `TypedDecisionGovernanceHold` when that authority is missing, and the runner records that as one deadletter. No model client is constructed on that path.

When the authority is present, `TypedDecisionSpend` commits, on a second connection, before any provider call:

- a `BudgetGovernor` reservation for the attempt quote
- a project-scoped job lease (`lease_owned_job`) with the same TTL, token, and heartbeat rules as other worker leases
- a model-gateway lease that accepts only that job lease and that reservation
- one in-progress receipt per attempt (`model-gateway:{project}:{dedupe}`)

Settlement consumes the reservation down to the actual cost. A definite failure or a cancellation before the call releases the unused reservation. An expired lease before the call is abandoned and released, and the model is not called. A timeout or a lease expiry after the call stays `in_progress` with a blocking decision and `typed_decision.reconciliation_required`. Replaying a succeeded receipt returns that receipt. The same attempt is not called or receipted twice. A later attempt emits `typed_decision.retry` and then uses a new dedupe key.

Publishing and starting a typed-decision workflow is no longer rejected at validation. The attempt is the hold boundary.

## What stays held

- TypeSafe key setup returns HTTP 503 before a provider call, key write, or backend-setting change. Read-only status still reports `effective_backend: paused` and `execution_available: false`. The Setup UI does not treat that as a live key proof.
- Community export and clean-VM qualification of this path are not done.
- Key proof is not on the spend path. Restore step 5 below is still open.
- Ordinary human `decision` steps and other workflow capabilities are unchanged.

## Restore the capability

Steps 1–4 are the workflow path in this branch. Step 5 is not.

1. Queue each typed-decision step as a durable BOSS job linked to its workflow-step lease, or add an equivalent committed model-execution record with TTL, heartbeat, fencing, and retry/deadletter semantics. Do not call a provider in the transaction that first claims the step.
2. Quote the selected source, enforce entitlement and policy, reserve the maximum authorized cost, and commit the reservation and a deduplicated in-progress `Receipt` **before** each external attempt.
3. Execute Claude CLI, OpenAI API fallback, and TypeSafe behind the same governed adapter boundary. TypeSafe retries and Claude second opinions need separate attempt identities and receipts; an uncertain network outcome must stay visible for reconciliation rather than being marked free.
4. Settle actual usage and cost, consume or release the reservation, complete or fail the receipt, and emit outcome events before publishing the typed answer to the workflow step. Make replay after a crash reuse the same attempt or require explicit reconciliation; never silently call twice.
5. Move TypeSafe key proof through the same committed pre-spend and settlement path. Restore the Setup form and workflow validation/runtime only after success, denial, timeout, crash, fallback, and replay tests pass in the Community export and clean VM.

## Validation

`engine/tests/test_typed_decision_spend.py` covers insufficient funds, lease expiry before the call, receipt replay, settlement against the reservation, a visible retry, an uncertain outcome that is not retried, cancellation, and a gateway double that still requires the lease and the reservation. The doubles are not a live model API.

`engine/tests/test_a_typed_decision_never_guesses.py` still fails closed when `run_step` has no spend authority. `engine/tests/test_a_jev_key_is_proved_before_it_is_kept.py` still refuses key proof. The API hold tests keep the key endpoint at 503 and the ungoverned deadletter, and they allow publish onto the governed path.

Keep BOSS-451 In Progress until key proof uses the same committed spend path and the Community export and clean VM qualify it. Do not treat this branch as that qualification.
