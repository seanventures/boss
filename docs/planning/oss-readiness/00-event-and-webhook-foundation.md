# Event & Webhook Foundation

> A reactive event dispatcher plus outbound and signed-inbound webhooks, built on the existing lease job queue · unblocks asks 1 (docs auto-update), 2 (orchestration agent), 3 (github agent) · module: Platform / Event Foundation · priority: P0-foundation

## 1. Context & Goal

Three of the OSS-readiness asks — auto-updating docs (ask 1), an orchestration agent that reacts to work state (ask 2), and a GitHub agent that triages/reviews (ask 3) — all require the same missing primitive: **BOSS must be able to *react* to something happening**, and to *exchange webhooks* with the outside world. Today BOSS can only *record* that things happened.

Concretely, none of these work without a shared foundation:

- **Ask 1** needs "a feature completed → regenerate/refresh its docs." That is a subscriber on `code_task.pr_opened` / `code_task.completed` / `plan.dispatched` events that enqueues a docs job.
- **Ask 2** needs an orchestration agent that wakes on state transitions (task completed, plan dispatched, decision resolved) and decides the next move. That is a subscriber on an allowlist of event types.
- **Ask 3** needs BOSS to receive GitHub webhooks (PR opened, review requested, check_suite completed, issue comment) — an *inbound* signed receiver — and to notify external systems — an *outbound* webhook.

Rather than build three bespoke event loops, this spec defines **one** load-bearing primitive: a post-commit **event dispatcher** on the existing lease job queue, an **outbound webhook delivery** capability, and a **signed inbound webhook receiver**. All three asks then become thin consumers.

**Goal:** ship an OSS-engine-safe (no dependency on hosted/commercial code) reactive-event + webhook foundation that turns the append-only `events` ledger into a pub/sub-style reactive system with durable, at-least-once delivery and exactly-once effect semantics, reusing the mechanisms BOSS already trusts (the lease queue, receipts, deadletters, `data_boundary`).

## 2. Current State (ground truth)

**Events are a poll-only ledger.** `emit_event()` at `apps/api/app/services/core.py:41` does exactly one thing — `INSERT INTO events (...)` — inside the caller's DB transaction. There is no pub/sub, no subscriber, no fan-out, no side effect. The `events` table (`db/schema.sql:1569`) is a flat append-only log: `id, project_id, event_type, actor, entity_type, entity_id, data (JSONB), created_at`. No consumer offset, no subscription, no delivery tracking.

**~292 call sites emit, nothing consumes.** `grep -c "emit_event(" apps/api/app` ≈ **292** in-tree call sites (the digest's ~343 counts worker + tests). Rich event types already flow through it: `code_task.pr_opened`, `code_task.completed_no_pr`, `discovery.completed`, `meeting.completed`, `plan.dispatched`, `plan.created`, plus the entire job lifecycle (`job.created`, `job.leased`, `job.succeeded`, `job.failed_final`, `job.deadlettered` — see `apps/api/app/services/jobs.py:146,317,427,519,557`). **Every one of these is a reaction trigger we are currently throwing away.**

**The only read surface is a poll.** `GET /v1/events` at `apps/api/app/main.py:9052` (`event_feed`) — `SELECT ... FROM events WHERE project_id = :project_id ORDER BY created_at DESC LIMIT/OFFSET`. Clients must poll. Nothing server-side wakes on an event.

**There is no inbound webhook receiver anywhere.** GitHub integration is **outbound-only**: `apps/api/app/services/github_adapter.py` has `create_pull_request` (:183), `merge_pull_request` (:223), `wait_for_commit_checks` (:319, polls check-runs — the *only* way BOSS learns external CI state). `github_app_adapter.py` mints an installation token to *author* as the app (`github_app_id`, `config.py:455`). **No route accepts a webhook, no `X-Hub-Signature` verification exists, and there is no webhook secret in config** — `apps/api/app/config.py` has `slack_webhook_url` (:62) and GitHub *tokens* (:206) but no inbound-webhook secret field. The nearest inbound path, `POST /v1/comms/inbox/ingest` (`main.py:7871` → `comms.py:762`), is unauthenticated-payload / regex-parsed (`_parse_inbound_message`, `comms.py:1254`) and only recognizes `decision_reply`/`message` — no signature check, no free-form intent, no GitHub semantics.

**What already exists that we should reuse (not rebuild):**

- **A durable, exactly-once lease job queue.** `JobLeaseService` (`apps/api/app/services/jobs.py`): `create_job` (:104) inserts a `queued` row; `claim_job` (:189) leases with `FOR UPDATE SKIP LOCKED`; `heartbeat`/`complete`/`fail` (:347/:396/:438) with retry → `failed_retryable` → `deadlettered` and a `deadletters` row (`db/schema.sql:1543`). The `jobs` table (`db/schema.sql:736`) already carries `data_boundary`, `max_attempts`, `run_after`, backoff. **This is our delivery substrate.**
- **A worker dispatch-by-kind.** `apps/worker/boss_worker/runner.py:543-681` is a straight `if job.kind == "..."` ladder; `capabilities()` (:247) is the allowlist of kinds a worker will lease. Adding a reactive consumer = adding a `kind` here.
- **An outbound-delivery precedent.** `comms.outbox.deliver` (job kind → handler at `runner.py:668`, service `comms.deliver_outbox_message` at `comms.py:357`) already does "durable message row → leased job → external send → receipt." An outbound *webhook* is the same shape.
- **A per-project registry precedent.** `mcp_servers` (`db/schema.sql:1581`, service `mcp_registry.py`) is exactly the shape our subscription/endpoint config needs: `project_id, url, transport, risk_tier, data_boundary, enabled`.
- **Schema management.** `db/schema.sql` applied idempotently by `ensure_schema` (`apps/api/app/db.py:132`) with additive column reconcile. New tables are `CREATE TABLE IF NOT EXISTS` appends.

## 3. Target Design & Approach

### 3.1 Principle: transactional outbox, not a new bus

`emit_event()` already runs *inside* the caller's transaction (same `Connection`). We exploit that: the dispatcher enqueues reactive jobs **in the same transaction as the emit**. If the business transaction rolls back, the reaction never fires; if it commits, the reaction job is durably queued. This gives us exactly-once *triggering* aligned with the write, and at-least-once *delivery* via the lease queue's existing retry/deadletter machinery. No Kafka, no Redis, no new infra — Postgres + the queue we already trust.

```
emit_event(conn, ...) ──► INSERT events (unchanged)
                     └──► dispatch_event(conn, event)   # NEW, same txn
                              ├─ match event_type against subscriptions (allowlist)
                              └─ for each match: enqueue_reactive_job(conn, kind, payload)
                                     └─ INSERT jobs (queued)   # reuses lease queue
   ... txn commits ...
worker leases job.kind ──► reactive consumer runs (docs / orchestrator / github / webhook.outbound)
```

### 3.2 Event dispatcher (the core seam)

Add `apps/api/app/services/event_dispatch.py` with a single free function:

```python
def dispatch_event(connection, settings, event: EmittedEvent) -> None
```

`emit_event` gets one added line at the end of its body (`core.py:41`): after the `INSERT`, call `dispatch_event(connection, settings, event)`. To keep `emit_event`'s signature source-compatible across ~292 call sites, `dispatch_event` reads its inputs from the values already passed in; `settings` is resolved lazily (module-level `get_settings()`), so **no call site changes**.

`dispatch_event`:

1. Loads the **subscription allowlist** for `(project_id, event_type)` — see §3.3. Kept tiny and cached per-process with a short TTL (the set changes rarely); cache is best-effort, correctness comes from the DB read.
2. For each matching subscription, builds a compact reactive payload `{event_id, event_type, entity_type, entity_id, subscription_id, data}` and enqueues a durable job.
3. **Dedupe:** the enqueued job carries a deterministic `dedupe_key = hash(subscription_id + event_id)` so a retried emit or an at-least-once inbound webhook cannot double-fire a reaction (mirrors the outbox `dedupe_key` UNIQUE constraint pattern).
4. **Never raises into the caller.** Dispatch failures are caught, logged, and recorded as a `job.dispatch_failed` event; a failed reaction must not roll back the business write that emitted it. (An unroutable event is a monitoring signal, not a transaction abort.)

**Enqueue without the budget coupling.** `JobLeaseService.create_job` (`jobs.py:104`) is the natural enqueue path but `JobLeaseService.__init__` constructs `BudgetGovernor`/`CommsService`/`AgentRuntimeService` (`jobs.py:31-35`) — an OSS-engine no-go for a hot primitive. **Extract the INSERT** in `create_job` into a free function `enqueue_job(connection, project_id, kind, payload, *, task_id=None, priority, max_attempts, run_after_seconds, data_boundary, dedupe_key=None)` in `jobs.py` (or `core.py`), and have both `create_job` and `dispatch_event` call it. This is a pure `INSERT jobs` + `emit_event("job.created")` with zero budget/entitlement imports. Budget gating already happens later at *lease* time (`claim_job` → `_budget_state_policy_for_job`, `jobs.py:891`), so reactive jobs inherit budget backpressure for free without the dispatcher importing budget code.

### 3.3 Subscription / allowlist config

New per-project table (mirrors `mcp_servers`), appended to `db/schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS event_subscriptions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,               -- exact match or 'prefix.*' glob (e.g. 'code_task.*')
  sink_kind TEXT NOT NULL,                -- 'job' | 'webhook_out'
  sink_ref TEXT NOT NULL,                 -- job kind to enqueue, OR webhook_endpoint id
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  enabled BOOLEAN NOT NULL DEFAULT true,
  config JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_event_subscriptions_match
  ON event_subscriptions(project_id, enabled, event_type);
```

- **Allowlist is enforced two ways.** (a) A subscription must exist and be `enabled`. (b) A process-level `DISPATCHABLE_EVENT_TYPES` constant in `event_dispatch.py` bounds what a subscription is *permitted* to match — the initial set is exactly the reaction triggers the three asks need: `{code_task.pr_opened, code_task.completed, code_task.completed_no_pr, plan.dispatched, plan.created, discovery.completed, meeting.completed, webhook.inbound.received}`. This double gate stops a mis-seeded subscription on, say, `job.heartbeat` from melting the queue.
- **Boundary honored.** `data_boundary` on the subscription flows onto the enqueued job (jobs already carry and gate on `data_boundary`), so a `restricted` event never fans out to a `public` sink.
- **Built-ins seeded per project** the way MCP servers are (`bootstrap.py`): ask 1/2/3 register their default subscriptions at project creation; operators add on-demand ones via API.
- **CRUD API** mirroring `/v1/mcp/servers`: `GET/POST/PATCH/DELETE /v1/event-subscriptions` (project-scoped, actor-authorized like every other route).

### 3.4 Outbound webhook delivery

Two new tables + one new job kind, modeled on `outbox_messages` + `comms.outbox.deliver`:

```sql
CREATE TABLE IF NOT EXISTS webhook_endpoints (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  secret_ref TEXT,                        -- HMAC signing key (secret store ref, NOT the raw key)
  enabled BOOLEAN NOT NULL DEFAULT true,
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  endpoint_id TEXT NOT NULL REFERENCES webhook_endpoints(id),
  event_id TEXT,                          -- source event, if triggered by dispatcher
  status TEXT NOT NULL DEFAULT 'pending', -- pending|delivered|failed|deadlettered
  dedupe_key TEXT NOT NULL,
  request JSONB NOT NULL DEFAULT '{}'::jsonb,
  response JSONB,
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
```

- **Delivery path.** A subscription with `sink_kind='webhook_out'` (or a direct API call) inserts a `webhook_deliveries` row (`pending`) and enqueues a `webhook.outbound.deliver` job. Worker handler (new arm in `runner.py`, alongside `comms.outbox.deliver` at :668) POSTs the JSON body to `endpoint.url` with headers: `X-Boss-Event`, `X-Boss-Delivery` (delivery id), and `X-Hub-Signature-256: sha256=<hmac(secret, raw_body)>` so *we* can be verified by the receiver exactly as GitHub signs us. On 2xx → `delivered`; on non-2xx / timeout → raise → lease queue retries with backoff → `deadlettered` after `max_attempts`.
- **Retries are free.** No custom retry loop — the lease queue already does `failed_retryable` → `run_after` backoff → `deadletters` (`jobs.py:438-572`).
- **Service** `webhook_service.py` in `apps/api/app/services/` (engine layer; imports only `core`, `db`, `config`, an HTTP client). No budget/entitlement/commercial imports.

### 3.5 Signed inbound webhook receiver

New auth-exempt route (like the preview proxy is auth-exempt) `POST /v1/webhooks/inbound/{source}` where `{source}` ∈ configured providers (`github`, ...):

1. **Verify signature before anything else.** Read raw body, compute `HMAC-SHA256(secret, raw_body)`, constant-time-compare against `X-Hub-Signature-256`. Secret comes from new config `BOSS_WEBHOOK_INBOUND_SECRET` (or a per-source `inbound_webhook_secrets` map / secret-store ref). Missing/invalid signature → `401`, no side effects, record a `webhook.inbound.rejected` event for audit.
2. **Persist + idempotency.** Insert into `webhook_deliveries` (or a dedicated `inbound_webhooks` table) keyed on the provider delivery id (GitHub sends `X-GitHub-Delivery`) as `dedupe_key` — a redelivered webhook is a no-op.
3. **Normalize + emit.** Map the provider payload to a canonical event and `emit_event(conn, project_id, "webhook.inbound.received", actor="github", entity_type=..., entity_id=..., data={source, github_event, action, ...})`. Because `webhook.inbound.received` is in the dispatchable allowlist (§3.3), the dispatcher immediately fans it out to whatever subscribers ask 3 registered (e.g. `github.triage`, `github.review`). **The inbound receiver reuses the exact same reactive path as internal events** — that is the whole point of one foundation.
4. **Project routing.** `{source}` + payload (repo → project mapping, reusing `github_owner`/`github_repo` config, `config.py:206`) resolves `project_id`. Unmapped repo → `202` accepted + `webhook.inbound.unrouted` event (don't 500 a provider; they disable failing endpoints).

### 3.6 How consumers register (the three asks become thin)

- **Ask 1 (docs):** register subscription `code_task.completed → job:docs.refresh`; worker gains a `docs.refresh` kind that regenerates docs for the changed surface.
- **Ask 2 (orchestrator):** register `plan.dispatched`, `code_task.completed`, `discovery.completed` → `job:agent.orchestrator.next_move`; reuses the existing agent job machinery (`agent.*` kinds already flow through `runner.py` and `_finalize_success`, `jobs.py:694`).
- **Ask 3 (github agent):** register inbound webhook endpoint + subscription `webhook.inbound.received → job:github.triage`; adds `create_review`/`create_comment` to `github_adapter.py` (currently outbound-only) for the reaction to act.

None of them touch the dispatcher — they add a subscription row + a worker `kind`. That is the success criterion for this foundation.

### 3.7 OSS-engine safety

Everything above lives in the **engine layer** and imports only `core`, `db`, `config`, `jobs` (the extracted `enqueue_job`), and an HTTP client. **No imports of `budget`, `entitlements`, `commercial`, `saml`, `oidc`, `enterprise`.** Budget backpressure is inherited at lease time, not compiled into the dispatcher. Secrets are config/secret-store refs with `None` defaults (no committed secrets — consistent with the repo's existing posture). The receiver, dispatcher, outbound delivery, and subscription registry are all things a self-hosted OSS operator can run with nothing but Postgres and the worker.

## 4. Implementation Tickets

### Ticket 1 — Extract `enqueue_job` + wire the event dispatcher — size M
**Scope:** Break the budget coupling on enqueue and add the reactive dispatch seam. Extract the pure `INSERT jobs` (+`job.created` emit) from `JobLeaseService.create_job` (`jobs.py:104`) into a free `enqueue_job(...)`; refactor `create_job` to call it. Add `event_dispatch.py` with `dispatch_event()`, `DISPATCHABLE_EVENT_TYPES`, and per-process subscription cache. Add the single dispatch call at the end of `emit_event` (`core.py:41`), reading `settings` lazily so no call site changes.
**Key files:** `apps/api/app/services/core.py`, `apps/api/app/services/jobs.py`, new `apps/api/app/services/event_dispatch.py`.
- [ ] `enqueue_job` free function exists; `create_job` delegates to it; no behavior change to existing job creation (existing job tests green).
- [ ] `emit_event` calls `dispatch_event` and **never** propagates a dispatch exception to the caller (unit test: dispatcher raises → business `INSERT events` still commits).
- [ ] Only event types in `DISPATCHABLE_EVENT_TYPES` are eligible; a high-volume type (`job.heartbeat`) is proven un-dispatchable by test.
- [ ] Dispatch enqueues at most one job per `(subscription_id, event_id)` (dedupe test with a replayed event).
- [ ] `event_dispatch.py` imports none of `budget`, `entitlements`, `commercial`.

### Ticket 2 — `event_subscriptions` table + registry + CRUD API — size S
**Scope:** Subscription config surface mirroring MCP servers. Add `event_subscriptions` DDL to `db/schema.sql`; add `event_subscription_service.py` (list/create/update/delete, glob match); add `/v1/event-subscriptions` routes; seed ask-1/2/3 defaults in `bootstrap.py`.
**Key files:** `db/schema.sql`, new `apps/api/app/services/event_subscription_service.py`, `apps/api/app/main.py`, `apps/api/app/services/bootstrap.py`.
- [ ] `ensure_schema` creates `event_subscriptions` idempotently (fresh + existing DB).
- [ ] Exact and `prefix.*` glob matching both covered by tests.
- [ ] CRUD routes are project- + actor-authorized like `/v1/mcp/servers`; disabled subscriptions never fire.
- [ ] New project bootstraps with default subscriptions for docs/orchestrator/github; dispatcher fans out to them end-to-end (integration test: emit `code_task.completed` → a `docs.refresh` job appears `queued`).

### Ticket 3 — Outbound webhook delivery — size M
**Scope:** Signed outbound webhooks over the lease queue. Add `webhook_endpoints` + `webhook_deliveries` DDL; add `webhooks.py` (create endpoint, enqueue delivery, HMAC sign); add `webhook.outbound.deliver` worker arm + `capabilities()` entry; add `/v1/webhook-endpoints` CRUD and a manual `POST /v1/webhook-endpoints/{id}/deliver`.
**Key files:** `db/schema.sql`, `apps/api/app/services/webhooks.py`, `apps/worker/boss_worker/runner.py`, `apps/worker/boss_worker/control_plane.py`, `apps/api/app/main.py`.
- [x] Delivery signs body with `X-Hub-Signature-256: sha256=<hmac>` using the endpoint secret ref.
- [x] 2xx → `delivered`; non-2xx/timeout → job `fail(retryable=True)` → backoff → `deadlettered` after `max_attempts` (reuses existing queue retry, no bespoke loop).
- [x] Redelivery of the same `dedupe_key` is a no-op (UNIQUE-constraint enforced).
- [x] `data_boundary` on endpoint gates which events may target it; a `restricted` event cannot deliver to a `public` endpoint (test).
- [x] `webhooks.py` imports none of `budget`, `entitlements`, `commercial`.

### Ticket 4 — Signed inbound webhook receiver — size M
**Scope:** `POST /v1/webhooks/inbound/{source}` with HMAC verification, idempotent persistence, GitHub payload normalization, and re-emit into the dispatcher. Add `BOSS_WEBHOOK_INBOUND_SECRET` (+ per-source map) to config; add auth-exempt route; add GitHub event → canonical event mapping; emit `webhook.inbound.received`.
**Key files:** `apps/api/app/config.py`, `apps/api/app/main.py`, `apps/api/app/services/webhooks.py`, `db/schema.sql`.
- [x] Invalid/missing signature → `401`, zero business effects, `webhook.inbound.rejected` event recorded (constant-time compare test).
- [x] Valid GitHub `pull_request`/`check_suite`/`issue_comment` payloads normalize and emit `webhook.inbound.received` with `github_event`+`action` in `data`.
- [x] Duplicate `X-GitHub-Delivery` is idempotent (second POST creates no new event/job).
- [x] Unmapped repo → `202` + `webhook.inbound.unrouted` (never 5xx to the provider).
- [x] End-to-end: a signed inbound PR-opened webhook triggers a subscribed reactive job (proves inbound reuses the §3.2 path).

### Ticket 5 — Docs, canary & operability — size S
**Scope:** Make the foundation observable and prove it under BOSS's own invariant harness. Add a delivery/dispatch view to `GET /v1/events` neighbors (or a `GET /v1/webhook-deliveries` list); add an exactly-once dispatch canary alongside the existing outbox canary; write operator docs (register a subscription, add an endpoint, verify a GitHub webhook).
**Key files:** `apps/api/app/main.py`, `apps/api/app/services/canaries.py`, `docs/` (this folder).
- [x] Canary asserts: one emit of a dispatchable event → exactly one reactive job per enabled subscription (no dupes, no drops), consistent with the existing exactly-once/outbox canaries.
- [x] Deadlettered webhook deliveries surface in the existing `GET /v1/deadletters` view.
- [x] Operator doc covers inbound secret setup + subscription registration for all three asks.

## 5. Dependencies, Sequencing & Priority

**Priority: P0-foundation.** Asks 1, 2, and 3 are blocked until this lands. Nothing else in the OSS-readiness set has a higher unblock ratio (one primitive, three asks).

**Upstream dependencies:** none new — this is built entirely on primitives that already exist (lease queue `jobs.py`, `emit_event` `core.py:41`, `ensure_schema` `db.py:132`, worker dispatch `runner.py`, secret-store refs). No new infra (no Kafka/Redis/broker).

**Sequencing:**
1. **Ticket 1** (dispatcher + `enqueue_job` extraction) — the load-bearing seam; everything else needs it. Also delivers the OSS-safety win of decoupling enqueue from budget.
2. **Ticket 2** (subscriptions) — required to route anything; enables the first end-to-end internal reaction.
3. **Tickets 3 and 4** can proceed in **parallel** after 2 (outbound and inbound are independent). Ask 3 (github agent) needs *both* 4 (receive) and 3 (respond via new `create_review`/`create_comment`).
4. **Ticket 5** last — hardens and documents.

**Downstream (separate specs, this foundation unblocks them):** docs-refresh consumer (ask 1), orchestration agent (ask 2), github triage/review agent + `github_adapter` inbound methods (ask 3).

**Suggested cut for a first vertical slice:** Tickets 1 + 2 + a trivial `log.reaction` consumer prove the reactive loop internally in days; 3 + 4 open the external surface.

## 6. Open Decisions

1. **Dispatcher placement — inside `emit_event` vs. an explicit call.** Wiring into `emit_event` (`core.py:41`) makes *every* current and future emit reactive for free (best coverage, zero call-site churn) but couples the hot `emit_event` path to subscription reads. Alternative: an explicit `emit_and_dispatch()` used only at the ~8 trigger sites we care about. **Recommendation:** wire into `emit_event` with the `DISPATCHABLE_EVENT_TYPES` gate short-circuiting *before* any DB read for non-dispatchable types (which is >95% of the ~292 emits), so the common path stays a cheap set-membership check.

2. **Post-commit vs. same-transaction enqueue.** Same-transaction (transactional outbox) is proposed — simplest, atomic with the write, no post-commit hook infra. But a reactive job becomes leasable the instant the txn commits, and the dispatcher runs while holding the business txn open (adds one `INSERT jobs` to that txn's critical section). Alternative: a true post-commit hook (SQLAlchemy `after_commit`) or a sweeper that polls `events` by offset. **Recommendation:** same-transaction now (matches how `comms.outbox` already works); revisit a sweeper only if emit-path latency regresses.

3. **`webhook_deliveries` reuse for inbound vs. dedicated `inbound_webhooks` table.** One table with a `direction` column is fewer objects; two tables keep inbound audit (raw payload, signature status) cleanly separate. **Recommendation:** dedicated `inbound_webhooks` for clarity of the security-sensitive inbound audit trail.

4. **Secret storage granularity.** Single `BOSS_WEBHOOK_INBOUND_SECRET` (simple, one GitHub App) vs. per-endpoint/per-project secret refs (multi-tenant, rotate-able). **Recommendation:** per-project/per-source secret *ref* stored on `webhook_endpoints.secret_ref` and a config fallback for single-tenant OSS installs.

5. **Subscription authoring surface.** DB rows + REST CRUD (proposed, matches MCP) vs. declarative YAML in `packages/specpack` (matches SkillSpec/PackSpec) vs. both. **Recommendation:** DB + REST as source of truth; optionally seed from a YAML default set at bootstrap so OSS installs get sane defaults without API calls.

6. **Fan-out ordering / storms.** A single event can match many subscriptions and enqueue many jobs; a burst of `code_task.completed` could flood the queue. Do we need per-subscription rate limiting or a max fan-out guard beyond `DISPATCHABLE_EVENT_TYPES`? **Recommendation:** rely on the queue's existing budget-state backpressure (`jobs.py:891`) for v1; add a per-subscription concurrency cap only if a canary shows storm risk.

7. **Open-core boundary alignment.** PRD-v2.0 (:496-554) documents whole-control-plane-AGPL, which diverges from the engine-only stance. This foundation is deliberately engine-layer and hosted-free, but the *console* surfaces for subscriptions/endpoints (`apps/web`) sit on the paid side. **Confirm** whether the subscription/webhook CRUD APIs are OSS or paid; the design keeps them OSS-runnable regardless.
