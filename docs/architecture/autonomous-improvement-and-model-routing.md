# Autonomous improvement and model routing

## Decision

BOSS owns the canonical improvement state machine, evidence contract, promotion
policy, job leases, budgets, receipts, and audit history.  External open-source
systems are adapters around that kernel, not alternative sources of truth.

The best immediate production stack is:

1. **Langfuse** as the framework-neutral trace, evaluation, cost, latency, and
   user-feedback sink.  Connect through OpenTelemetry/API adapters and retain
   BOSS IDs as trace attributes.  Treat the repository as open-core: the core
   is available under MIT, while separately licensed enterprise features must
   not become Community dependencies.
2. **DSPy** as the candidate optimizer.  It compiles prompt/program candidates
   against an explicit metric and is therefore a better fit for BOSS's frozen
   baseline and single-variable trial contract than an unconstrained
   self-editing agent.
3. **Temporal** as an optional durable-executor adapter when installations need
   very long-running cross-service workflows.  Postgres remains BOSS's
   canonical job and evidence ledger.
4. **LangGraph** as an optional workflow/agent adapter when teams need cyclic
   graph authoring, checkpointing, or human interrupts.  It must compile into
   BOSS workflow steps and cannot bypass BOSS leases or promotion gates.

| Need | Selected reference | BOSS integration boundary |
| --- | --- | --- |
| Trace, evaluation, feedback, cost and latency | [Langfuse](https://langfuse.com/docs) | OpenTelemetry/API sink; BOSS IDs remain canonical |
| Metric-driven candidate generation | [DSPy](https://dspy.ai/) | Offline optimizer feeding versioned trials |
| Durable cross-service execution | [Temporal](https://docs.temporal.io/) | Optional executor behind BOSS jobs and policy |
| Cyclic agent workflows and checkpoints | [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) | Optional compiler/adapter into governed workflow steps |
| Provider protocol normalization | [LiteLLM](https://docs.litellm.ai/) | Optional enterprise-gateway adapter, never the policy ledger |

Letta and Hermes are useful references for memory consolidation and offline
reflection, but their self-editing patterns are not allowed to mutate BOSS in
place.  Any learned memory, prompt, skill, or configuration becomes a versioned
candidate, is evaluated against the frozen baseline, and ships only through a
reviewable proposal/PR, canary, and rollback path.

## Closed loop

`Observe -> score -> diagnose -> generate one candidate -> isolated trial ->`
`adversarial verify -> propose -> approve -> canary -> roll out or roll back ->`
`adopt new baseline`

The analytics sink can suggest candidates, but only BOSS decides whether the
evidence is comparable and whether promotion is allowed.  This prevents a
dashboard, LLM judge, or optimizer from silently redefining success.

## Unified model gateway

The gateway separates three concerns:

- `EntitlementSource` declares provider, auth mode, ownership, tenant boundary,
  supported models, and observed limits.
- `ModelRouteDecision` explains selection and every rejection.
- `ModelGatewayLease` turns a selection into short-lived, job-scoped capacity.

The worker proves its parent `Job` lease at acquisition and on every heartbeat
or settlement.  Paid sources reserve budget first.  Source rows are locked while
concurrency and a monotonically increasing fencing token are allocated.  The
gateway returns its own opaque lease token, never a provider token.

Before an adapter call, the worker creates a `ModelGatewayInvocation`.  BOSS
creates or reuses a deduplicated `Receipt` at that point.  On settlement it
records usage, cost, provider reference, capacity state, and LLM telemetry, then
ends the capacity lease.  Failed unbilled calls release the reservation; failed
billable calls still settle their actual cost.

Subscription sessions (Codex/ChatGPT, Claude Code, Gemini/Code Assist, Grok
Build, Cursor) remain local and owner-scoped.  BOSS schedules the official CLI
surface but never extracts or proxies its OAuth cache.  Manual API/service keys
are written through to a configured secret backend and represented only by a
`SecretRef`.  The sole raw-value resolver is an internal, lease-checked adapter
method with no HTTP route.

## Scale posture

Hundreds of agents share pools by acquiring independent job and model leases.
Per-source `max_concurrent`, provider capacity probes, budget caps, worker
quotas, and cooldowns provide independent backpressure.  Additional router
implementations such as LiteLLM can sit behind an enterprise-gateway adapter,
but cannot replace these policy and accounting controls.

The lease reaper makes TTL terminal state explicit and releases any unused
BudgetReservation with an audit event. Read views also project an elapsed active
lease as expired immediately, so stale database state is never presented as
usable capacity between sweeps.
