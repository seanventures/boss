# BOSS-451 typed decision model-spend hold

**Plane:** [BOSS-451](https://plane.local.sean.network/goldeneye/projects/cc97d5cc-e8c6-4893-ac14-7f646c37f3d7/issues/621dbb33-c55a-4149-911d-119d6cf75e8e) · **Status:** In Progress · **Release:** BOSS-293 Community GA blocker until governed execution is implemented and qualified.

## Scope and observed gap

A product workflow step with `binding.typed_decision` called `typed_decisions.run_step()` inline from `workflow_capability_runner.tick()`. That path selected Claude CLI, the OpenAI API fallback in the worker, or TypeSafe Jev. Jev could retry and then ask Claude for a second opinion. The decision result reported an estimated cost, but there was no pre-spend `BudgetReservation`, model-capacity lease, `Receipt` with a dedupe key, cost settlement, or event for each provider attempt.

The delivery-workflow worker calls `POST /v1/delivery-workflows/run-capabilities`. The API wrapped the entire tick in one database transaction. The workflow step lease was claimed and the provider call happened before the transaction committed. Adding a reservation or receipt inside that same transaction would not make it durable: a crash after provider spend would roll back both records. The existing `ModelGatewayService.acquire()` requires a durable BOSS `jobs` lease and token; it cannot accept this workflow-step lease as a substitute.

TypeSafe key setup had a second spend path: `POST /v1/setup/decision-model/key` called Jev to prove the key inside its API transaction, before any reservation or receipt. A failed secret-store write or process crash could erase all evidence of that billed proof.

## Explicit release hold

- New workflow versions and new runs containing `binding.typed_decision` fail validation with `BOSS-451` before work begins. API, TUI, and MCP clients use these same server-side paths.
- A typed-decision step already running from a previously published version fails once as `deadlettered`. Its step and run keep the `BOSS-451` reason, and `workflow.step_deadlettered` plus a deadletter record make the hold visible. No model backend is constructed.
- TypeSafe key setup returns HTTP 503 before a provider call, key write, or backend-setting change. Read-only status and existing saved keys remain available; status reports `effective_backend: paused` and the hold reason. The Setup UI no longer claims that a live key proof succeeded or accepts a key during the hold.
- Ordinary human `decision` steps and other workflow capabilities are unaffected. No shipped catalogue YAML currently uses `binding.typed_decision`; the route-ticket example in tests and any customer-authored or previously published typed-decision workflow are affected.

## Restore the capability

1. Queue each typed-decision step as a durable BOSS job linked to its workflow-step lease, or add an equivalent committed model-execution record with TTL, heartbeat, fencing, and retry/deadletter semantics. Do not call a provider in the transaction that first claims the step.
2. Quote the selected source, enforce entitlement and policy, reserve the maximum authorized cost, and commit the reservation and a deduplicated in-progress `Receipt` **before** each external attempt.
3. Execute Claude CLI, OpenAI API fallback, and TypeSafe behind the same governed adapter boundary. TypeSafe retries and Claude second opinions need separate attempt identities and receipts; an uncertain network outcome must stay visible for reconciliation rather than being marked free.
4. Settle actual usage and cost, consume or release the reservation, complete or fail the receipt, and emit outcome events before publishing the typed answer to the workflow step. Make replay after a crash reuse the same attempt or require explicit reconciliation; never silently call twice.
5. Move TypeSafe key proof through the same committed pre-spend and settlement path. Restore the Setup form and workflow validation/runtime only after success, denial, timeout, crash, fallback, and replay tests pass in the Community export and clean VM.

## Validation

Focused unit tests prove no model construction or secret write during the hold. A database-backed transition test proves an already running step deadletters once and emits `workflow.step_deadlettered`; API tests prove the key endpoint returns 503 and read-only status says paused. The hold is a safety containment, not completed typed-decision support. Keep BOSS-451 In Progress and BOSS-293 GA blocked for this capability until the durable path is shipped and accepted.
