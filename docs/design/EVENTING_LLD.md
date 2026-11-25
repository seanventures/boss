# P9: autonomous agents woken by SaaS and messaging events: low-level design (for the build agents)

**Status:** draft for review. Nothing in it is built.
**Date:** 14 November 2026
**Code baseline:** `seanventures/boss` `origin/main` at `275a3f556` (Merge PR #100, `feat/ap-p7-subagents`). Every `file:line` below is against that commit. Paths are relative to the repo root; `services/x.py` means `engine/boss_engine/services/x.py`.
**Depends on:** [`AGENT_HOSTS_LLD.md`](AGENT_HOSTS_LLD.md) (P8; cited as "P8 §x"), [`AGENT_PLATFORM.md`](AGENT_PLATFORM.md) (AP-P1..P7, shipped).
**Plain-English version:** to be written from this document by someone else.

**Binding operator decisions (14 Nov 2026).**
1. Durable events live in BOSS on Postgres. No Kafka backbone.
2. An event intake layer with adapters, on the same connector model as `engine/boss_engine/connectors/tickets/`.
3. Priority: SaaS events and messaging. Messaging = Slack and Microsoft Teams, two-way. SaaS = ServiceNow, Salesforce, GitHub, Jira/Plane, email (Gmail/Outlook), and a generic signed webhook.
4. Stream sources (Kafka, SQS, Pub/Sub, Service Bus) are not built now; the contract must take them later.
5. Agents are both triggered automatically and callable by people.
6. Agents run on `runtime: local` (the worker) or `runtime: microvm`. `replica` VMs on; `per_run` off by default.
7. VMs have no outbound network. Everything goes vsock → `boss-hostd` → gateways or the run plane. Replicas pull work from the run plane (`next`, then a lease).

---

## How to read this document

- §0 is the summary and the ten findings that shape the design. Read it first.
- §1 to §3 are requirements, the architecture, and the adapter contract.
- §4 to §10 are the low-level design: data, routing, identity, guards, dispatch, replies, security.
- §11 to §15 are console, observability, failure modes, phases and open decisions.
- Appendix A is the external research per source, with URLs. Appendix B is the code inventory (reused / extended / new).
- **Labels.** `[code]` = verified in the repo at the commit above. `[doc]` = verified against the vendor's published docs (URL given). `[P8]` = from the P8 LLD. `[UNVERIFIED]` = believed true, not confirmed in a primary source; each one has a named check in §14. `[estimate]` = a design number to be measured in §14.

---

## 0. Executive summary

**What P9 adds.** One intake layer that turns anything that happens in Slack, Teams, ServiceNow, Salesforce, GitHub, Jira/Plane, a mailbox or a signed webhook into a durable, deduplicated **intake event** in Postgres; a router that matches it to an agent deployment (declarative filter, optionally a cheap model triage under a cost cap); and one compile step to the primitive BOSS already has: **admit a run on a thread** (`thread_runs.admit`, `services/thread_runs.py:110`) with an on-behalf-of identity. The run is then executed by whoever claims it: the local worker, or a microVM replica pulling from the P8 run plane. Replies to Slack/Teams leave through BOSS's chat outbox, so the bot token never enters a VM.

**The path in one line.**
`source → adapter.verify → ack (≤ 3 s, target p99 ≤ 300 ms) → intake_events row (dedupe) → router → agent_triggers kind 'intake' → thread_runs.admit (OBO) → NOTIFY → claim (local worker | replica via run-plane next + lease) → agent → reply tool → chat_outbox → Slack/Teams.`

**The dispatch model the operator proposed is right, with five refinements** (each justified in §8):
1. **The wake signal already exists.** `thread_runs.admit` emits `agent.run_admitted` (`thread_runs.py:150-152`) and every committed `events` row is `pg_notify`'d on `boss_events` by a trigger (`db/schema.sql:12979-12998`), consumed by a `PgListener` per API process (`services/event_stream.py:302-330`). The run plane's `next` long-poll subscribes to that hub. No new NOTIFY channel is needed for the claim; the `boss-hostd` desired-state hint is used only for **scale-from-zero**, not for ordinary wake-ups.
2. **The claim must become deployment-aware.** `JobLeaseService.claim_job` selects by job kind only (`services/jobs.py:595-601`). Today any worker with `agent.run` would steal a run meant for a microVM deployment. P9 adds a `jobs.target` column (`local` | `deployment:<id>`) and both claim paths filter on it.
3. **The local worker sleeps 15 s when idle** (`apps/worker/boss_worker/config.py:23`, `BOSS_POLL_INTERVAL_SECONDS=15`). On `runtime: local` an event would wait up to 15 s for its run to start. P9 makes `/v1/worker/poll` a long-poll woken by the same hub, so local and microVM have the same sub-second claim.
4. **Each trigger owns one thread with `skip_if_active` by default** (`agent_triggers.py:315-320`; `db/schema.sql:14423`). That is right for cron and wrong for conversations and SaaS records: the second Slack message while the first run is going would be **skipped**. P9 routes by a **thread key** (Slack channel + `thread_ts`, Teams conversation id, ServiceNow `sys_id`, ...) with `enqueue` semantics, so one Slack thread = one BOSS thread, and unrelated conversations run in parallel.
5. **Replies go through a control-plane tool, not the MCP gateway.** A `reply` tool invoked over the run plane (`POST /tools/{name}/invoke`, P8 §3.13.3) writes a fenced, deduplicated `chat_outbox` row; the outbox sender (`services/notify.py:832-883`) holds the bot token. The MCP gateway stays the path for everything else an agent does in Slack/ServiceNow/Salesforce (search, read, update records).

**Latency targets** (all measured in §14): intake ack p99 ≤ 300 ms (Slack's hard limit is 3 s); intake → run `queued` p95 ≤ 250 ms; `queued` → claimed on a warm replica or a woken local worker p95 ≤ 300 ms, p99 ≤ 1 s; first visible sign in Slack/Teams (status line or reaction) p95 ≤ 1.5 s after the person sends; scale-from-zero first claim ≤ 3 s [P8 cold start target].

### 0.1 Ten findings from the code that shape the design

| # | Finding | Evidence | Consequence in P9 |
|---|---|---|---|
| F1 | All four trigger kinds already compile to `admit a run on a thread` with `on_behalf_of` + `grant_id`, re-checked per firing, and dedupe per firing. | `agent_triggers.py:13-20, 360-395, 507-620`; `db/schema.sql:14459-14476` | Reuse. Intake routes become a fifth kind, `intake`, on the same table and the same `fire`. |
| F2 | One thread per trigger, `skip_if_active` default. | `agent_triggers.py:315-320, 565-575`; `db/schema.sql:14423-14424` | Add a per-event **thread key** and `enqueue`; skip/coalesce stay for clocks. |
| F3 | Event dispatch runs inside the writer's transaction, synchronously, and **fails open**: a dispatcher exception is logged and lost, with no retry and no dead letter. | `services/core.py:280-310`; `event_dispatch.py:279-463` | External events never go through `emit_event` dispatch. They get their own durable inbox with retry, dead-letter and replay (§4). |
| F4 | `DISPATCHABLE_EVENT_TYPES` is a closed set of 15 internal types; trigger filters are exact string equality on top-level keys. | `event_dispatch.py:23-44`; `agent_triggers.py:718-720` | A real filter language over the envelope (§5). Keep the BOSS-internal path as is. |
| F5 | Slack verify/parse exist, ignore bot messages and subtypes, and parse `app_mention`, but `handle_inbound` **drops mentions**. There is no `event_id` dedupe and no `X-Slack-Retry-Num` handling. | `chat_adapters.py:124-165` (bot filter `:153-154`, mention `:160-164`); `chat_channels.py:669-679` | Reuse verify/parse/outbox; add conversation intake, dedupe and retry handling. People cannot talk to agents in Slack today. |
| F6 | Identity links exist per chat endpoint, verified by a one-time code confirmed in the console. No SSO/email auto-match. `users.by_email` exists. | `chat_channels.py:297-405`; `db/schema.sql:12905-12921`; `identity/users.py:56-59` | Keep code-linking; add an SSO-attested email match as an opt-in (§6). |
| F7 | Local worker idles 15 s between polls; the claim filters only by kind. | `apps/worker/boss_worker/config.py:23`; `services/jobs.py:595-601`; `apps/api/app/main.py:24209-24222` | Long-poll + `jobs.target` (§8). |
| F8 | A durable, deduplicated inbound table already exists for GitHub/Bitbucket (`inbound_webhooks`, `UNIQUE(source, delivery_key)`), with own-bot loop prevention. | `db/schema.sql:2788-2803`; `services/webhooks.py:806, 1027-1039` | The pattern to generalise. P9b moves GitHub onto the intake layer; `inbound_webhooks` stays as the GitHub reconciler's audit until then. |
| F9 | The ticket webhook **fetches the ticket from the provider inside the request** before answering. The agent-trigger webhook route is `async def` but does blocking DB work on the event loop. | `ticket_sync.py:1286-1318`; `apps/api/app/routers/agent_platform.py:348-368` | Intake never calls out before acking; all routes run verify + insert only, in the threadpool. |
| F10 | The chat outbox is durable, deduped, leased (2 min, `SKIP LOCKED`), retried with backoff and dead-lettered, and sent outside the claiming transaction. Ops: post, update, ephemeral, dm. | `services/notify.py:812-883`; `chat_adapters.py:250-274`; `db/schema.sql:12961-12977` | Reuse for all agent replies; add `stream_*`, `react`, `status` ops and Teams. |

Two naming clashes to avoid: `inbox_items` / `services/inbox.py` is the **personal attention inbox** (BOSSAPP-875), and `inbox_messages` (`db/schema.sql:2613`; `services/comms.py:873-944`) is the comms command inbox. The new table is **`intake_events`**; the layer is called **intake**, never "inbox", in code and console.

---

## 1. Requirements

### 1.1 Functional requirements

| ID | Requirement |
|---|---|
| FR-1 | **Intake connections.** An operator connects a source (Slack workspace, Teams bot, ServiceNow instance, Salesforce org, GitHub App/repo, Jira/Plane connection, Gmail/Outlook mailbox, generic webhook) to a project. One connection row, secrets in the secret backend, never returned. |
| FR-2 | **Verify every inbound request** with the source's own proof (HMAC, JWT, client state, OIDC push token) before reading anything else. |
| FR-3 | **Ack fast.** Push sources are answered after verify + one durable insert, never after routing, a model call or a provider call. |
| FR-4 | **Durable and deduplicated.** Every accepted event is one `intake_events` row keyed by `(connection, dedupe_key)`; a redelivery is answered 200 and recorded as a duplicate. |
| FR-5 | **Route.** Declarative filters choose zero, one or several agent deployments; optional model triage picks among candidates under a cost cap. |
| FR-6 | **Compile to a run.** A match fires an `intake` trigger: `thread_runs.admit` on a thread chosen by a thread key, as a named person (OBO) or under the trigger owner's standing grant. |
| FR-7 | **People talk to agents.** In Slack (DM, `@mention`, thread reply, slash command) and Teams (1:1, channel `@mention`, group chat), a linked person addresses an agent and gets the reply in the same thread. The conversation continues on the same BOSS thread. |
| FR-8 | **Agents reply.** In-thread replies, edit-in-place progress, a status line, reactions, decision cards (existing). Proactive messages to a person or channel when policy allows. |
| FR-9 | **Both runtimes.** The same intake path works for `runtime: local` and `runtime: microvm` (`replica`), with no event pushed into a VM. |
| FR-10 | **Pull and push.** Sources that cannot push (ServiceNow without outbound REST, mailboxes, Salesforce via Pub/Sub API) are polled or subscribed on a per-connection cursor, on a DB lease so exactly one process does it. |
| FR-11 | **Subscriptions are kept alive.** Graph subscriptions and Gmail `watch` are renewed before expiry and recreated after loss, with catch-up from the cursor. |
| FR-12 | **Dead-letter and replay.** Failed events park with the reason; a person can replay one or a filtered set; replay never re-runs a firing that already admitted a run unless asked. |
| FR-13 | **Guards.** Loop prevention, per-source and per-route rate limits, storm circuit breakers, per-route spend caps, quiet hours, the existing kill switch. |
| FR-14 | **Screening.** Event text written outside BOSS is screened for prompt injection before an agent reads it, with the verdict on the envelope. |
| FR-15 | **Stream-ready contract.** The adapter contract has a `stream` mode (long-lived consumer with offsets, ack/commit, back-pressure) so Kafka/SQS/Pub/Sub/Service Bus plug in later with no schema change. Not built in P9. |
| FR-16 | **Console.** Connect, route, watch, replay, link identities (§11, screen list only). |
| FR-17 | **Audit.** Every accepted, refused, deduplicated, routed, fired and replied event is traceable from the source id to the run and back. |

### 1.2 Non-functional requirements

| ID | Target | Why |
|---|---|---|
| NFR-1 | Intake ack p99 ≤ 300 ms, p99.9 ≤ 1 s at 100 events/s per API instance | Slack requires a response within 3 s [doc, Appendix A.1]; Teams/Bot Framework callers time out at 15 s [doc, A.2]; GitHub at 10 s [doc, A.5]. 10× headroom. |
| NFR-2 | Zero accepted-event loss across API crash, deploy, Postgres failover (P8d) | Ack only after commit. |
| NFR-3 | Zero duplicate runs from redelivery (Slack retries, Graph redelivery, GitHub redeliver, poll overlap) | `UNIQUE(connection_id, dedupe_key)` + `agent_trigger_fires UNIQUE(trigger_id, dedupe_key)`. |
| NFR-4 | Event → run claimed: p95 ≤ 550 ms, p99 ≤ 1.2 s end to end on a warm replica or woken local worker | The operator's "sub-second claim on a warm replica". |
| NFR-5 | First visible response in Slack/Teams p95 ≤ 1.5 s | People judge a bot by its first sign of life; Slack's own AI-app guidance uses a status line for this [doc, A.1]. |
| NFR-6 | Per-conversation ordering: messages in one Slack thread / Teams conversation are processed in received order | Double-text policy works only on ordered input. |
| NFR-7 | Intake survives a 50× burst from one source without delaying other sources by more than 1 s | Per-connection queues and fair claiming (§8.5). |
| NFR-8 | No real credential (bot token, client secret, signing secret) ever in a VM, a log, an event or a response | P8 G4/AP-D11; `chat_adapters.py:23-25`. |
| NFR-9 | Polled sources: detection lag ≤ poll interval + 30 s, default interval 60 s | ServiceNow Table API, mail fallback. |
| NFR-10 | Postgres write cost ≤ 4 small writes per event on the hot path | §2.4 budget. |

### 1.3 Non-goals

- Kafka/SQS/Pub/Sub/Service Bus consumers (contract only, §3.6).
- Pushing events into a VM through `boss-hostd`.
- A new message broker of any kind.
- Replacing BOSS-internal event dispatch (`event_dispatch.py`); it keeps serving workflow and webhook-out sinks.
- Marketplace-listed Slack app / AppSource-listed Teams app. Single-workspace / single-tenant installs first (§15).
- Voice, Telegram/Discord conversation (Telegram keeps decisions only).

---

## 2. Architecture

### 2.1 The whole path

```
  SOURCES                         BOSS CONTROL PLANE (API ×2, Postgres)                              EXECUTION
  ───────                         ──────────────────────────────────────                              ─────────
  Slack Events API ─┐
  Teams Bot Framework┤  HTTPS    ┌──────────────────────────────┐
  ServiceNow REST ──┤ ────────► │ (1) INTAKE ROUTE             │  verify → normalise-lite →
  GitHub webhook ───┤  public   │  /v1/intake/{kind}/{conn}    │  INSERT intake_events
  Jira/Plane hook ──┤           │  @public(HMAC_SIGNED_WEBHOOK)│  ON CONFLICT DO NOTHING → COMMIT → 200
  Generic signed ───┤           └──────────────┬───────────────┘           (≤300 ms p99)
  Gmail Pub/Sub push┤                          │ NOTIFY boss_intake (at commit)
  Graph notifs ─────┘                          ▼
                                 ┌──────────────────────────────┐
  ServiceNow Table API ◄─poll──  │ (2) INTAKE PROCESSOR         │  claim (SKIP LOCKED, per-conversation
  Gmail history.list   ◄─poll──  │  threads in each API process │  order) → full parse → screen →
  Graph delta          ◄─poll──  │  + pollers/renewers on       │  identity → loop/storm guards →
  Salesforce Pub/Sub   ◄─sub───  │  extension_job_leases        │  ROUTER (filter, optional triage)
  (Kafka/SQS… later)   ◄─stream─ └──────────────┬───────────────┘
                                                │ agent_triggers.fire(kind='intake', thread_key, OBO)
                                                ▼
                                 ┌──────────────────────────────┐
                                 │ (3) ADMIT                    │  thread_runs.admit: run + job (target)
                                 │  one transaction             │  in one txn; emit agent.run_admitted
                                 └──────────────┬───────────────┘  → pg_notify boss_events at commit
                                                │
                     ┌──────────────────────────┴──────────────────────────────┐
                     ▼                                                         ▼
      (4a) LOCAL: worker long-polls /v1/worker/poll           (4b) MICROVM: replica long-polls
           (woken by the hub) → claim_job(target=local)            run-plane next via vsock → boss-hostd
           → agent_run_worker.execute                              → API (woken by the hub) → claim
                                                                   (target=deployment:<id>) + lease epoch
                     │                                                         │
                     └────────────────────────┬────────────────────────────────┘
                                              ▼
                                 ┌──────────────────────────────┐
                                 │ (5) AGENT RUN (kernel)       │ model → model gateway; tools → MCP gateway;
                                 │                              │ reply → run-plane tool 'chat.reply'
                                 └──────────────┬───────────────┘ (fenced by epoch)
                                                ▼
                                 ┌──────────────────────────────┐
                                 │ (6) CHAT OUTBOX              │ chat_outbox row (dedupe) → NOTIFY →
                                 │  notify.deliver_chat_due     │ sender resolves bot token from vault →
                                 └──────────────────────────────┘ chat.postMessage / Bot Connector
```

### 2.2 Components

| Component | Where it runs | New / extended | Responsibility |
|---|---|---|---|
| Intake routes | BOSS API, public, `@public(reason=HMAC_SIGNED_WEBHOOK, verifier=...)` (`identity/declarations.py:43-64`) | New router `apps/api/app/routers/intake.py` | Verify, insert, ack. Nothing else. |
| Adapter registry | `engine/boss_engine/connectors/intake/<key>/{manifest.yaml, adapter.py}` | New, modelled on `connectors/tickets/registry.py` | Load, validate manifest vs implemented hooks (as `registry.py` does for tickets). |
| Intake processor | Thread pool in each API process, registered in `system_services` (`db/schema.sql:14494`) | New `services/intake.py` | Claim, parse, screen, identity, guards, route, fire. |
| Pollers / renewers / subscribers | Same processes, one DB lease per job on `extension_job_leases` (pattern: `ticket_sync.py:1437-1495`) | New `services/intake_pollers.py` | Cursor-based polling, Graph/Gmail subscription renewal, Salesforce Pub/Sub subscriber (P9c). |
| Router | In the processor | New `services/intake_router.py` | Filter DSL, triage, thread key, fan-out. |
| `intake` trigger kind | `agent_triggers` | Extended (`KINDS`, `agent_triggers.py:40`) | Pause, authority, fires, describe, health: all reused. |
| Admission | `thread_runs.admit`, `agent_deployments.admit_run` | Extended (`target`) | Unchanged semantics. |
| Claim | `/v1/worker/poll` (local) and `/v1/run-plane/next` (P8) | Extended: long-poll, `jobs.target` | Sub-second claim. |
| Conversation map | `chat_threads` (`db/schema.sql:12926-12941`) | Extended | Slack/Teams thread ↔ BOSS thread, Teams conversation reference. |
| Identity links | `chat_identity_links` (`db/schema.sql:12905-12921`) | Extended | Link method, SSO-attested email. |
| Reply tool | Control-plane tool behind the run plane | New tool `chat.reply` (+ `chat.status`, `chat.react`) | Fenced write into `chat_outbox`. |
| Outbox | `chat_outbox`, `notify.deliver_chat_due` | Extended (ops, NOTIFY kick, Teams) | Deliver with retries; holds no secret at rest. |
| Teams adapter | `services/chat_adapters.py` | New class `TeamsChat` | Verify JWT, parse activities, send via Bot Connector. |

### 2.3 Why intake runs in the BOSS API processes, not a new service

- **Problem.** Push endpoints must be public, highly available and fast; pollers must be singletons.
- **Alternatives.** (a) A separate `boss-intake` service (Go or Python); (b) Lambda-style functions; (c) the API.
- **Choice: (c).** The API already serves the public signed routes (`/v1/webhooks/*`, `apps/api/app/main.py:6911-7004`), already has the `@public` verifier discipline, already runs singletons on DB leases (`ticket_sync.py:1437-1495`, `extension_job_leases`), and P8c runs two API instances behind keepalived (P8 §3.14.5). A separate service would add one more pair to make HA and one more holder of signing secrets.
- **What it costs.** Intake competes with console traffic for the API's threadpool. Mitigation: a dedicated bounded executor for intake routes (§8.5) and the 300 ms budget.
- **How it grows.** The processor is a plain loop over a table; it can be lifted into a `boss-intake` process with no schema change when volume demands (§15 D10).

### 2.4 Postgres cost per event (hot path)

| Write | Rows | When |
|---|---|---|
| `intake_events` insert | 1 | Intake route |
| `intake_events` claim + finish | 1 update ×2 | Processor |
| `agent_trigger_fires` insert | 1 per matched route | Fire |
| `agent_runs` + `jobs` + `agent_threads` + `events` | 4 | `thread_runs.admit` (existing) |
| `chat_threads` upsert | 1 (first message of a conversation) | Fire |

About 9 small writes per routed event, 3 per ignored event. At 100 events/s that is ~900 writes/s, within the P8 §2.15.6 budget for one tuned Postgres [estimate → P9a-T6]. NFR-10's "≤ 4" applies to the intake-owned writes only; the admit writes are the existing per-run cost.

---

## 3. The adapter contract

### 3.1 Shape: one folder per source, a manifest and a class

Mirrors `connectors/tickets/` exactly (`registry.py:1-40`, `contract.py:37-46`): the manifest is the truth the console shows; the registry refuses a manifest that claims a capability the adapter does not implement, or the reverse.

```
engine/boss_engine/connectors/intake/
  contract.py          # IntakeEvent envelope, Protocol, errors, capability table
  registry.py          # load + validate manifests (same rules as tickets/registry.py)
  base.py              # IntakeAdapterBase: refusals for unclaimed capabilities
  slack/      manifest.yaml  adapter.py
  teams/      manifest.yaml  adapter.py
  servicenow/ manifest.yaml  adapter.py
  salesforce/ manifest.yaml  adapter.py      (P9c)
  github/     manifest.yaml  adapter.py
  jira/       manifest.yaml  adapter.py      # thin: delegates verify/parse to tickets/jira
  plane/      manifest.yaml  adapter.py      # thin: delegates to tickets/plane
  gmail/      manifest.yaml  adapter.py      (P9c)
  outlook/    manifest.yaml  adapter.py      (P9c)
  webhook/    manifest.yaml  adapter.py      # generic signed webhook (Standard Webhooks + BOSS v1)
```

### 3.2 Manifest

```yaml
key: slack
label: Slack
icon: slack
description: Messages, mentions and DMs from a Slack workspace; agents reply in the thread.
docs_url: https://docs.slack.dev/apis/events-api/
adapter: SlackIntakeAdapter
modes: [push, socket]            # push | poll | subscribe | socket | stream
capabilities:
  verify: true                    # verify(request) -> Verified
  handshake: true                 # url_verification / validationToken / CloudEvents OPTIONS
  parse: true                     # parse(raw) -> list[IntakeEvent]
  poll: false                     # poll(cursor) -> (events, next_cursor)
  subscribe: false                # subscribe/renew/unsubscribe provider-side subscriptions
  stream: false                   # open/consume/commit (later: Kafka, SQS, ...)
  reply: true                     # send(op) for the outbox
  stream_reply: true              # incremental reply (chat.startStream/appendStream/stopStream)
  identity: true                  # resolve_person(external_user) -> email/subject
  conversation: true              # events carry a conversation key; replies thread
  health: true
ack:
  deadline_ms: 3000               # the source's own limit; the route asserts ≤ 10 % of it
  success_status: 200
  retry_signal_headers: [X-Slack-Retry-Num, X-Slack-Retry-Reason]
dedupe: [event_id]                # envelope fields that form dedupe_key, in order
auth_fields:
  - {name: team_id, label: Workspace ID}
  - {name: signing_secret, label: Signing secret, secret: true}
  - {name: bot_token, label: Bot token (xoxb-…), secret: true}
  - {name: app_token, label: App-level token (xapp-…), secret: true, required: false, help: Socket Mode only}
default_hosts: [slack.com, "*.slack.com"]
scopes: [app_mentions:read, chat:write, im:history, channels:history, groups:history, users:read, users:read.email, reactions:write, assistant:write]
self_identity: [bot_user_id, bot_id, app_id]     # used for loop prevention (§7.1)
```

Validation rules (registry): `modes` ⊆ the five words; each claimed capability has its hooks implemented (table in `contract.py`, as `CAPABILITIES` at `connectors/tickets/contract.py:37-46`); `ack.deadline_ms` present for `push`; `dedupe` names envelope fields; `default_hosts` registered with the outbound credential guard exactly as tickets do (`registry.py` docstring, lines 1-9).

### 3.3 The Protocol

```python
class IntakeAdapter(Protocol):
    key: str

    # PUSH (route thread; must be pure CPU, no I/O except secret resolution)
    def verify(self, conn: IntakeConnection, req: RawRequest, now: float) -> Verified: ...
    def handshake(self, conn: IntakeConnection, req: RawRequest) -> Optional[RawResponse]: ...
    def dedupe_key(self, conn: IntakeConnection, req: RawRequest) -> str: ...      # cheap, from headers/body
    def ack(self, conn: IntakeConnection, req: RawRequest, outcome: AckOutcome) -> RawResponse: ...

    # PROCESSOR (claimed row; may do I/O through the gateways)
    def parse(self, conn: IntakeConnection, raw: StoredRaw) -> list[IntakeEvent]: ...
    def enrich(self, conn: IntakeConnection, ev: IntakeEvent) -> IntakeEvent: ...  # e.g. fetch the ticket
    def resolve_person(self, conn: IntakeConnection, actor: ExternalActor) -> PersonHint: ...
    def is_self(self, conn: IntakeConnection, ev: IntakeEvent) -> bool: ...        # loop prevention

    # PULL
    def poll(self, conn: IntakeConnection, cursor: Cursor, limit: int) -> PollResult: ...
    def subscribe(self, conn: IntakeConnection, spec: SubscriptionSpec) -> Subscription: ...
    def renew(self, conn: IntakeConnection, sub: Subscription) -> Subscription: ...
    def unsubscribe(self, conn: IntakeConnection, sub: Subscription) -> None: ...

    # STREAM (contract only in P9)
    def open_stream(self, conn: IntakeConnection, cursor: Cursor) -> StreamHandle: ...
    def consume(self, h: StreamHandle, max_events: int, max_wait_ms: int) -> list[StreamRecord]: ...
    def commit(self, h: StreamHandle, upto: Cursor) -> None: ...

    # REPLY (outbox sender only; never called from a run)
    def send(self, conn: IntakeConnection, op: OutboxOp, secrets: SecretView) -> Receipt: ...

    def health(self, conn: IntakeConnection) -> Health: ...
```

Errors reuse the ticket family's shape (`contract.py:54-107`): `AuthFailed`, `RateLimited(retry_after)`, `HostNotAllowed`, `CapabilityNotSupported`, plus `VerifyRefused(reason)` (the `ChatVerifyRefused` pattern, `chat_adapters.py:49-54`) and `CursorExpired` (Gmail `historyId` too old, Salesforce replay id beyond retention, Graph delta token expired) which triggers a resync (§13).

### 3.4 The envelope: CloudEvents 1.0-shaped

Every adapter's `parse` returns one or more `IntakeEvent`s. Field names follow CloudEvents 1.0 [doc, A.7] so a later stream source can carry CloudEvents natively and an outbound bridge can emit them.

| Field | CloudEvents | Example (Slack) | Notes |
|---|---|---|---|
| `id` | `id` (required) | `Ev08MFMKH6` | The source's own event id. With `source` it is the dedupe identity. |
| `source` | `source` (required) | `slack://T024BE7LD` | `<kind>://<tenant>`: Slack team, Teams tenant, SN instance host, SF org id, GitHub installation, mailbox. |
| `specversion` | required | `1.0` | |
| `type` | `type` (required) | `com.slack.message.app_mention` | Reverse-DNS, BOSS-normalised (table in A.8). |
| `subject` | `subject` | `C123/1700000000.000100` | The **entity or conversation** the event is about. Feeds the thread key. |
| `time` | `time` | event time from the source | Not receive time. |
| `datacontenttype` | yes | `application/json` | |
| `data` | `data` | normalised payload (below) | Bounded to 64 KB; larger raw bodies go to object storage with a hash. |
| `bossconnection` | extension | `ic_…` | Intake connection id. |
| `bossconversation` | extension | `{channel, thread_ts}` / Teams `conversation.id` | Present when `conversation` capability. |
| `bossactor` | extension | `{kind: user|bot|service|unknown, external_id, display, email?}` | |
| `bosscausality` | extension | `{run_id, deployment_id, hops}` | Read from BOSS markers on our own writes (§7.1). |
| `bossretry` | extension | `{num: 1, reason: http_timeout}` | From `X-Slack-Retry-*` etc. |
| `bossscreen` | extension | `{verdict, rules, mode}` | From `tool_result_screen.screen_result` (`tool_result_screen.py:365`). |
| `bossdedupe` | extension | `slack:T024BE7LD:Ev08MFMKH6` | The key actually used. |

`data` for conversational sources is normalised to: `{text, text_blocks?, mentions: [external ids], is_dm, files: [name,url,size] (never downloaded at intake; same rule as tickets, contract.py:140-145), reply_to}`. For record sources: `{record_type, record_id, record_url, operation: created|updated|deleted|commented, changed_fields: [...], fields: {...whitelisted...}}`, which is a superset of `TicketEvent` (`contract.py:199-208`).

### 3.5 Ack semantics

| Outcome at the route | HTTP answer | Row |
|---|---|---|
| Verified, inserted | Source's success code (Slack 200, Teams 200/201, Graph 202, GitHub 202, Gmail push 204, generic 202) | `received` |
| Verified, duplicate (conflict on `dedupe_key`) | Same success code | `duplicates` counter +1 on the existing row, `last_retry_num` updated |
| Verified, connection paused/disabled | Success code | none; counter only. A source that retries on non-2xx would otherwise hammer us. |
| Verified, intake over its per-connection hard cap (§7.3) | Success code | `received` with `status='shed'` (kept for replay) |
| Not verified | 401 (Slack/generic/GitHub), 401/403 Teams, 202 with no effect for Graph `clientState` mismatch | none; `intake_refusals` counter + a sampled audit row |
| Handshake (Slack `url_verification`, Graph `validationToken`, CloudEvents `OPTIONS`) | The exact echo the source requires | none |
| Database unavailable | 503 | none. The source retries. This is the only case we let the source's retry carry durability. |

**Never** answer success before the insert has committed (NFR-2). **Never** call a provider, a model or the router before answering (F9).

### 3.6 Fit for later stream sources

- `modes: [stream]` + `open_stream/consume/commit` + an `intake_cursors` row per (connection, partition). A stream consumer is a leased job like a poller; it commits the source offset **after** the `intake_events` insert commits, so at-least-once from the stream becomes exactly-once at the inbox via `dedupe_key = <topic>:<partition>:<offset>` or the CloudEvents `id`.
- Back-pressure: `consume(max_events)` is driven by the same intake queue-depth signal that sheds push traffic (§8.5); a stream simply stops consuming instead of shedding.
- Kafka consumer groups, SQS visibility timeouts and Service Bus peek-lock map onto `commit` (offset commit / DeleteMessage / Complete). No schema change; only an adapter and a credential kind. Not built in P9.

### 3.7 Per-source adapter summary

| Source | Modes (P9) | Verify | Dedupe key | Ack | Conversation | Reply | Phase |
|---|---|---|---|---|---|---|---|
| Slack | push (Events API), socket (opt-in) | `v0` HMAC-SHA256 over `v0:ts:body`, 5-min window (existing, `chat_adapters.py:109-140`) | `event_id` | 200 ≤ 3 s | channel + `thread_ts` | chat.postMessage / update / stream APIs | P9a |
| Teams | push (Bot Framework) | JWT (Bot Framework OpenID metadata, `aud` = app id, `serviceUrl` claim) | `activity.id` + conversation id | 200 | `conversation.id` (+ reply chain) | Bot Connector `POST /v3/conversations/{id}/activities` | P9b |
| ServiceNow | push (Business Rule/Flow outbound REST to generic route), poll (Table API on `sys_updated_on`) | HMAC header set by the outbound REST message, or OAuth client credential to a BOSS token [design choice] | `sys_id` + `sys_mod_count` | 202 | `sys_id` (record) | through MCP gateway (work notes), not the outbox | P9b |
| Salesforce | subscribe (Pub/Sub API gRPC, CDC / Platform Events), push (Outbound Messages, Flow HTTP callout) | OAuth on our side (subscribe); Outbound Message: org id in the SOAP body + TLS + allow-listed Salesforce IP ranges [UNVERIFIED → P9c-T1] | `replayId` / CDC `transactionKey`+`sequenceNumber` | Pub/Sub flow control (`num_requested` ≤ 100); Outbound Message answers `<Ack>true</Ack>` [UNVERIFIED, A.4] | record id | MCP gateway | P9c |
| GitHub | push | `X-Hub-Signature-256` (existing, `webhooks.py`) | `X-GitHub-Delivery` | 202 ≤ 10 s | issue/PR number | MCP gateway / egress | P9b (migration) |
| Jira / Plane | push (existing connectors), poll (existing) | connector's `verify_webhook` (`contract.py:420`) | webhook delivery id or (issue key, updated) | 202 | issue key | ticket connector write-back | P9b (re-plumb) |
| Gmail | subscribe (`users.watch` → Pub/Sub push), poll (`history.list`) | Pub/Sub push OIDC token (`aud`, service account email) | `historyId` → message id | 204 | `threadId` | send through the MCP/egress gateway as the mailbox | P9c |
| Outlook | subscribe (Graph change notifications), poll (delta) | `validationToken` handshake + `clientState` | `resourceData.id` + `changeType` | 202 ≤ 3 s [doc, A.6] | `conversationId` | Graph `sendMail`/reply through MCP gateway | P9c |
| Generic webhook | push | Standard Webhooks (`webhook-id/timestamp/signature`) **and** BOSS v1 (`X-BOSS-Signature v1=` over `<ts>.<body>`, `agent_triggers.py:728-784`) | `webhook-id` / `X-BOSS-Delivery` / body hash | 202 | sender-provided `subject` | none (or callback URL) | P9a |

---

## 4. Data model (Postgres, additive)

All changes are additive and idempotent (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`), the house style of `db/schema.sql`. Nothing existing is renamed or dropped.

### 4.1 New tables

**`intake_connections`**: one row per connected source install.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | `icn_…` |
| `project_id` | TEXT FK projects | Org-wide connections (one Slack workspace for many projects) are a P9b option: `project_id` NULL + `org_id`, routes still per project. |
| `org_id` | TEXT | |
| `kind` | TEXT | Adapter key (`slack`, `teams`, …), validated against the registry |
| `name` | TEXT | |
| `mode` | TEXT | `push` / `poll` / `subscribe` / `socket` / `stream` (one of the manifest's `modes`) |
| `config` | JSONB | Non-secret: team id, tenant id, instance URL, tables, poll interval, self identity (bot user id), SSO attestation |
| `secret_refs` | JSONB | `{"signing_secret": "sref_…"}` through `secrets_service.put_secret`, exactly as `chat_endpoints.secret_refs` (`db/schema.sql:12893`) |
| `path_token` | TEXT UNIQUE | Unguessable segment of the public URL (same idea as `agent_triggers.path_token`, `agent_triggers.py:321`) |
| `chat_endpoint_id` | TEXT FK chat_endpoints NULL | Slack/Teams: the chat endpoint this connection owns, so decision cards, identity links and the outbox keep working (§4.2) |
| `status` | TEXT | `active` / `paused` / `disabled` / `auth_failed` / `rate_limited` |
| `limits` | JSONB | `{rps, burst, hard_cap_per_min, max_backlog}` (§7.3) |
| `health` | JSONB | Last verify ok/refused, last event at, last poll, last renew, lag |
| `created_by`, `on_behalf_of`, `grant_id` | TEXT | Owner and standing grant (the rule `event_subscriptions` follows, `db/schema.sql:2744-2749`) |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

**`intake_events`**: the durable intake inbox. Partitioned by month on `received_at` (P9b; plain table in P9a).

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | `iev_…` |
| `connection_id` | TEXT FK | |
| `project_id` | TEXT | Denormalised for scoping and authz |
| `dedupe_key` | TEXT NOT NULL | From the adapter (`slack:T…:Ev…`); `UNIQUE (connection_id, dedupe_key)` |
| `source_event_id` | TEXT | Raw id for search |
| `type`, `subject` | TEXT | Envelope fields, filled by `parse` (NULL at insert) |
| `conversation_key` | TEXT | Ordering key (§8.4): Slack `channel/thread_ts`, Teams conversation id, record id |
| `status` | TEXT | `received` → `processing` → `routed` / `ignored` / `filtered` / `refused` / `failed` → `dead`; plus `shed`, `duplicate_only` |
| `raw` | BYTEA or JSONB | Verified raw body ≤ 256 KB; larger → object storage, `raw_ref` + `raw_sha256` |
| `headers` | JSONB | Allow-listed headers only (retry headers, delivery id); **never** `Authorization` or signatures |
| `envelope` | JSONB | The parsed `IntakeEvent` (§3.4) |
| `actor_kind`, `actor_external_id`, `principal_id` | TEXT | Resolved identity (§6) |
| `screen` | JSONB | Screening verdict |
| `attempts`, `next_attempt_at`, `lease_until`, `claimed_by`, `last_error` | | The `chat_outbox` lease pattern (`notify.py:832-848`) |
| `retry_count_seen`, `duplicates` | INT | How many times the source redelivered |
| `outcome` | JSONB | `[{trigger_id, fire_outcome, run_id}]` |
| `received_at`, `event_time`, `processed_at` | TIMESTAMPTZ | |
| `trace_id` | TEXT | Carried to the run |
| `replay_of` | TEXT NULL | For replays (§4.4) |

Indexes: `UNIQUE (connection_id, dedupe_key)`; `(status, next_attempt_at)` partial `WHERE status IN ('received','failed')`; `(conversation_key, received_at)`; `(project_id, received_at DESC)`.

Trigger: `AFTER INSERT … pg_notify('boss_intake', json_build_object('id',NEW.id,'connection_id',NEW.connection_id)::text)`, same shape as `boss_events_notify` (`db/schema.sql:12984-12998`). Delivered at commit, so the processor never sees an uncommitted row.

**`intake_cursors`**: per-source position for poll / subscribe / stream.

| Column | Notes |
|---|---|
| `connection_id`, `stream_key` (PK pair) | `stream_key`: `incident` table, `mailbox:inbox`, `/data/AccountChangeEvent`, later `topic:partition` |
| `cursor` TEXT | `sys_updated_on|sys_id` keyset, Gmail `historyId`, Graph `deltaLink`, Salesforce `replay_id` (base64), offset |
| `cursor_time` | Source time at the cursor (lag = now − cursor_time) |
| `updated_at`, `last_error`, `resync_required` BOOL | |

The cursor is advanced **in the same transaction** as the `intake_events` inserts for that page, so a crash re-reads at most one page, and duplicates of it hit `UNIQUE (connection_id, dedupe_key)`.

**`intake_subscriptions`**: provider-side subscriptions BOSS must keep alive.

| Column | Notes |
|---|---|
| `id`, `connection_id`, `provider_subscription_id`, `resource` | Graph subscription id + resource; Gmail watch (topic, label ids) |
| `client_state_hash` | sha256 of the random `clientState` (≤ 128 chars, [doc] Graph); the value lives in the secret backend |
| `expires_at`, `renew_at` | `renew_at = expires_at − max(25 % of lifetime, 6 h)` for Graph; Gmail daily (Google recommends daily, 7-day hard limit) |
| `status` | `active` / `renewing` / `reauthorization_required` / `removed` / `failed` |
| `last_lifecycle` | Last lifecycle notification (`reauthorizationRequired`, `subscriptionRemoved`, `missed`) |

### 4.2 Extended tables

| Table | Change | Why |
|---|---|---|
| `agent_triggers` (`db/schema.sql:14416`) | `kind` CHECK adds `'intake'`; new columns `connection_id TEXT`, `priority INT DEFAULT 100`, `thread_key TEXT` (template), `identity_mode TEXT`, `reply_mode TEXT`, `limits JSONB`, `quiet_hours JSONB`; config holds the filter DSL and triage spec | Routes **are** triggers: pause, authority (`agent_triggers.py:387-395`), fires, health, failure streak, describe and the console row come free (F1). |
| `agent_trigger_fires` (`:14465`) | `intake_event_id TEXT`; `outcome` CHECK adds `'filtered'`, `'deferred'`, `'shed'` | One fire per (route, event); dedupe key `intake:<intake_event_id>` (replay: `intake:<id>:r<n>`). |
| `agent_threads` (`:2843`) | none; `external_ref = 'intake:<route>:<thread_key_value>'` via `thread_runs.ensure_thread` (`thread_runs.py:59-87`) | One BOSS thread per conversation/entity per agent. |
| `chat_endpoints` (`:12887`) | `kind` CHECK adds `'teams'`; `intake_connection_id` | A Slack/Teams intake connection owns one endpoint; decision cards keep flowing (`chat_channels.py:534-557`). |
| `chat_threads` (`:12926`) | `agent_thread_id TEXT`, `deployment_id TEXT`, `conversation_ref JSONB` (Teams `{serviceUrl, conversationId, tenantId, activityId, botId}`), `last_inbound_at`, `bot_message_refs JSONB` (our posted ts/activity ids, for edit-in-place and loop checks) | **The conversation ↔ thread map.** Today it maps a posted message to a decision/run/work item (`bind_thread`, `chat_channels.py:408-421`); it now also maps a human conversation to the agent thread. |
| `chat_identity_links` (`:12905`) | `method TEXT` (`code` / `sso_email` / `admin` / `aad_oid`), `external_email`, `idp_subject`, `attested_by` | §6. |
| `chat_outbox` (`:12961`) | `run_id`, `fence_epoch`, `op_seq INT`, `priority INT`; `kind` adds `agent_reply`; NOTIFY trigger `boss_chat_outbox` | Fenced replies (§9.4), sub-second first send. |
| `jobs` (`:942`) | `target TEXT NOT NULL DEFAULT 'local'` (`local` / `deployment:<id>`); index `(kind, target, status, priority_score)` | The deployment-aware claim (§8.2). |
| `agent_deployments` (`:14359`) | P8 adds `runtime`, `replicas`, `vm_mode`; P9 adds `min_replicas`, `max_replicas`, `scale_to_zero BOOL DEFAULT false`, `max_queued_runs INT DEFAULT 200`, `queue_slo_ms INT DEFAULT 1000` | Back-pressure and scale-from-zero (§8.6). |
| `scoped_budgets` scopes (`services/scoped_budgets.py:45`) | `SCOPES` adds `'deployment'` and `'trigger'` | Per-route and per-agent spend caps (§7.4). |

### 4.3 Dead letter

No separate table. `intake_events.status = 'dead'` after `MAX_INTAKE_ATTEMPTS = 8` (backoff 1 s, 5 s, 30 s, 2 m, 10 m, 30 m, 1 h, 2 h), or immediately for a permanent error (`parse` refused the schema, connection deleted). Each dead row emits `intake.event_dead` (an `events` row, so it reaches the console stream and alert rules). A dead row keeps raw + envelope + `last_error` for 30 days (§15 D9).

### 4.4 Replay

`POST /v1/intake/events/replay {ids | filter, mode}`:
- `mode = reprocess` (default): sets `status='received'`, `attempts=0`, `replay_of=id` bookkeeping in `outcome`. Firings use dedupe key `intake:<id>`, so **a route that already admitted a run for this event is not fired again** (`agent_trigger_fires UNIQUE(trigger_id, dedupe_key)`, `db/schema.sql:14475`). Only routes that failed, were filtered by a since-changed rule, or were added later fire.
- `mode = force`: dedupe key `intake:<id>:r<n>` (n = replay count): runs again. Requires `integration:manage` and a typed reason; audited.
- Replay re-runs identity and guards **as of now** (a revoked link refuses).
- Poll-mode resync: `POST /v1/intake/connections/{id}/resync {from}` rewinds the cursor; dedupe makes the overlap harmless.

### 4.5 Retention

| Data | Kept | Then |
|---|---|---|
| `intake_events.raw` | 14 days | Nulled; `raw_sha256` stays |
| `intake_events.envelope` | 30 days (`routed`/`ignored`), 90 days (`dead`/`refused`) | Row deleted; `agent_trigger_fires` and the run keep the link |
| Duplicates counters | Lifetime of the row | |
| Refusal samples (bad signatures) | 7 days, 1 per minute per connection | |

---

## 5. Routing and filters

### 5.1 Where a route lives

A route is an `agent_triggers` row with `kind='intake'`, a `connection_id`, a `priority`, and in `config`:

```json
{
  "match": {"all": [
     {"field": "type", "op": "in", "value": ["com.slack.message.app_mention", "com.slack.message.im"]},
     {"field": "data.channel", "op": "in", "value": ["C0SUPPORT"]},
     {"not": {"field": "data.text", "op": "matches", "value": "(?i)^\\s*ignore"}}
  ]},
  "addressing": "mention_or_dm",          // conversational sources only: mention_or_dm | channel_all | thread_followups
  "fanout": "first_match",                // first_match (default) | all_matches
  "triage": {"mode": "off"},              // or {"mode":"model", ...} §5.3
  "thread_key": "{bossconversation.channel}/{bossconversation.thread_ts}",
  "double_text": "enqueue",
  "goal": "Answer support questions from the runbooks; open a ServiceNow incident when asked."
}
```

### 5.2 The filter language

- Boolean: `all`, `any`, `not`. Depth ≤ 6, ≤ 64 leaves.
- Leaf ops: `eq`, `ne`, `in`, `nin`, `prefix`, `suffix`, `contains` (case-insensitive), `exists`, `gt/gte/lt/lte` (numbers, ISO times), `matches` (RE2 syntax only via `google-re2`, or a 2 ms timeout + 2 KB pattern cap if RE2 is unavailable; never Python `re` backtracking on untrusted text), `changed` (record sources: field in `data.changed_fields`).
- Fields: any envelope path (`type`, `source`, `subject`, `data.*`, `bossactor.kind`, `bossactor.email_domain`, `bossscreen.verdict`).
- Compiled once per trigger version and cached with the subscription-cache pattern (5 s TTL + explicit invalidation, `event_dispatch.py:53, 82-103`).
- Evaluated in the processor, never in the intake route.
- Replaces the current exact-equality filter (`agent_triggers.py:718-720`) for `intake` routes. The existing `event` kind keeps its filter for compatibility; a later migration can compile it into the DSL (`{"all":[{"field":k,"op":"eq","value":v}...]}`).

**Order.** Routes of a connection are evaluated by `(priority, created_at)`. `first_match` stops at the first route whose filter matches and whose guards admit; `all_matches` fans out to every matching route (each its own fire and run). A conversational event never fans out to two agents on one thread: the thread key includes the route.

**Addressing in chat.** A message in a channel the bot is in is not, by default, addressed to any agent. `mention_or_dm` (default) fires on `app_mention`, DMs to the bot, and **follow-ups in a thread the agent already owns** (lookup in `chat_threads` by `(endpoint, channel, thread_ts)`), so a person need not `@mention` again inside an ongoing conversation. `channel_all` fires on every human message in listed channels (support-desk channels), always behind triage or a tight filter and a rate limit.

**Which agent in a shared workspace.** One Slack app per BOSS install fronts many agents (§15 D1). The agent is chosen by: (1) the thread already being owned (follow-up); (2) a channel → route binding; (3) a leading agent name in a mention or DM (`@BOSS triage: …`), matched against deployed agent keys/aliases; (4) the connection's default DM agent; (5) triage, if on; else a short reply listing the agents the person may use.

### 5.3 Optional model triage, with a cost cap

Used only when a route set says `triage.mode = "model"`, typically for `channel_all` support channels and email.

```json
"triage": {"mode": "model", "model_tier": "small", "candidates": ["atrg_support", "atrg_billing", "none"],
           "max_input_chars": 4000, "max_usd_per_event": 0.002, "daily_usd_cap": 5.00,
           "min_confidence": 0.6, "on_cap": "default_route", "default_route": "atrg_support",
           "cache_seconds": 600}
```

- Input: the **screened** envelope summary (type, subject, first 4,000 chars of text, actor kind) plus each candidate route's one-line description. Never the raw body, never secrets.
- Output: JSON `{route, confidence, reason}` parsed strictly; anything else counts as "no answer".
- Governance: the call goes through the existing governed model path used by the screening cascade (`tool_result_screen._governed_ask`, `tool_result_screen.py:460`), so it is metered, routed by tier and budget-checked (`BudgetGovernor.reserve`, `services/budget.py:1307`). Ledger tag `intake_triage`, scope = the connection's project.
- Cap: per event (`max_usd_per_event`, refused before the call by an estimate from input length) and per day per connection (`daily_usd_cap`). At the cap: `on_cap` = `default_route` | `drop` | `ask` (a decision card to the connection owner, rate-limited to one per hour).
- Cache: `(connection, sha256(normalised text))` for `cache_seconds`, so a storm of identical alerts is triaged once.
- Latency: triage adds one small-model call (~300–800 ms [estimate]); it runs after the ack, so it never threatens the source deadline, but it does count against NFR-4. Conversational routes with explicit addressing skip triage.

### 5.4 Thread key and double-text

| Source | Default thread key | Default double-text |
|---|---|---|
| Slack mention/DM | `channel/(thread_ts or ts)`; a DM without a thread uses `channel/day` (one thread per DM per day) [§15 D11] | `enqueue` |
| Teams | `conversation.id` (the channel thread's id includes `;messageid=`) | `enqueue` |
| ServiceNow / Salesforce / Jira / GitHub | `record_type/record_id` | `enqueue` (a later update waits for the run on the earlier one, then sees it in history) |
| Mail | `threadId` / `conversationId` | `enqueue` |
| Generic webhook | `subject` if present, else one thread per route (today's behaviour) | route's `concurrency` (`skip_if_active` default, as now) |

`thread_runs.admit` already implements `enqueue / reject / interrupt / rollback` under the thread row lock (`thread_runs.py:15-25, 110-159`). `interrupt` is a good choice for "the person corrected themselves" in chat; we keep `enqueue` as the default and expose the option per route.

### 5.5 Firing

`intake_router.fire_route(route, event)` calls the existing `agent_triggers.fire` (`agent_triggers.py:522-620`) extended with:
- `thread_id` override from the thread key (via `ensure_thread`, setting `agent_threads.deployment_id`),
- `acting` from identity resolution (§6) instead of only the trigger's standing grant,
- `payload` = the envelope (screened text included), so `_message` (`agent_triggers.py:494-504`) gives the agent "This run was started by a trigger…" plus what arrived. For conversational events, the user message is the person's text verbatim, and the trigger preamble moves into the system context, so the agent sees a normal chat turn.
- `extra.reply_to = {chat_thread_id, endpoint_id}` written into `finalize_ctx`, which the reply tool reads (§9.1).

Everything else (project archived, paused, not deployed, authority lost, savepoint on refused admission) is the existing code path.

---

## 6. Identity

### 6.1 Rule

A run has a **subject** (the person it acts for) and an **actor** (the agent), per P8 §5.2/§3.12.2: the run token is an RFC 8693 OBO token, effective permissions = person ∩ agent ceiling ∩ grant ∩ task. P9 decides the subject per event:

| `identity_mode` (per route) | Subject | Used for |
|---|---|---|
| `person` | The linked human who sent the message / made the change. Refuse if not linked. | Chat conversations (default for Slack/Teams) |
| `person_or_owner` | The linked human if any; else the route owner's standing grant | Record events from people (Jira/GitHub comments) |
| `owner` | The route owner's standing grant (today's trigger rule, `agent_triggers.py:360-395`) | Service events with no person: ServiceNow business rules, Salesforce CDC, alerts, generic webhooks |
| `owner_readonly` | Owner's grant, but the run's task scope drops write-class tools | Unknown senders allowed to ask read-only questions (opt-in) |

**Interactive chat runs act as the person**, so "what the agent can do for me in Slack" = what I can do in BOSS ∩ what the agent may do. The first time a linked person addresses an agent they have no standing grant for, the agent does not run; the person gets a private message with a console link to grant it (the existing consent flow that triggers use, `identity/consent.py:959`), and the event is kept `deferred` for 30 minutes and fires when the grant lands.

### 6.2 Resolving the person, per source

| Source | External identity | Resolution order |
|---|---|---|
| Slack | `team_id` + `event.user` | 1. `chat_identity_links` verified row for (endpoint, user) (`chat_channels.py:301-307`). 2. If the connection has `sso_attested: true` (§6.3): `users.info` → `profile.email` (scope `users:read.email`) → `identity/users.by_email` (`identity/users.py:56-59`) with `email_verified_at` set and the email domain in the org's verified domains → create a link with `method='sso_email'`. 3. Unknown. |
| Teams | `from.aadObjectId` + `conversation.tenantId` | 1. Link row. 2. If the org's BOSS SSO provider is Entra for the same tenant: match `idp_subject` (`oid`) to the principal's federated identity → `method='aad_oid'` (strongest; no email involved). 3. Graph `GET /users/{oid}` → `userPrincipalName`/`mail` → `by_email` under the same attestation rule. 4. Unknown. |
| ServiceNow | `sys_updated_by` / `opened_by` sys_id | Usually `owner`. If `person_or_owner`: `sys_user` lookup (through the MCP gateway) → email → `by_email`, attestation required. |
| Salesforce | `LastModifiedById` / `CreatedById` (CDC header `commitUser`) | Same as ServiceNow, via `User.Email`. |
| GitHub | `sender.login`, `sender.id` | The existing per-person provider identities (`ticket_sync.provider_identity/remember_identity/confirm_identity`, `ticket_sync.py:1534-1598`) and the GitHub connection identities. `sender.type == 'Bot'` → not a person. |
| Jira/Plane | `user.accountId` / actor id | Ticket provider identities as above (`ticket_sync.py:1534-1598`). |
| Mail | `From` | Person only when `Authentication-Results` shows DMARC pass for the From domain **and** the domain is an org verified domain; then `by_email`. Otherwise unknown. `From` alone is spoofable and is never trusted. |
| Generic webhook | none | `owner` only. A sender may pass `subject_hint`; it is data, never identity. |

### 6.3 SSO attestation (Okta and friends)

The operator's ask is "the human behind a Slack/Teams user, linked through SSO/Okta email + `chat_identity_links`". An email address in a Slack profile is only as trustworthy as the workspace's sign-in: in a workspace without SSO, a user can set any email they can verify, and a guest may share the org's domain. So:

- **Auto-link by email is opt-in per connection** (`config.sso_attested = {idp: "okta", attested_by, attested_at, domains: [...]}`), set by an org admin who states that the workspace/tenant enforces SSO through the same IdP BOSS uses. The console shows the risk in one line.
- Guests and external (Slack Connect) users are never auto-linked: Slack `is_restricted` / `is_ultra_restricted` / `team_id ≠ connection team`; Teams `tenantId ≠ connection tenant` or guest `userType`.
- An auto-link is a normal `chat_identity_links` row (`method='sso_email'`), revocable by the person (`revoke_link`, `chat_channels.py:393-405`) and re-checked at every use: principal still active, still in the org, still SSO-sourced. Deactivation through Okta (SCIM) ends the link at the next event because `users.by_email` returns no active user.
- **Without attestation**, the existing one-time code link stands (`start_link` / `confirm_link`, `chat_channels.py:309-381`): the person DMs "link" or is offered the link on first contact.

### 6.4 Unknown users

| Route `identity_mode` | Unknown sender gets |
|---|---|
| `person` (chat default) | No run. A private reply (ephemeral in a channel, DM otherwise) with the link instructions (`_link_reply`, `chat_channels.py:682-696`), at most once per user per hour. Event `refused(reason=unlinked)`. |
| `owner_readonly` | A run under the owner's grant, write tools removed, and the reply says it answers as a read-only assistant. |
| `person_or_owner` | Owner's grant; the run's context names the external actor as data. |
| `owner` | N/A (service events). |

### 6.5 Service events (no person)

Run under the route owner's standing grant, checked at every firing; a lost grant pauses the route with "Renew authority" (`agent_triggers.py:582-592`). The run token's subject is the owner, the actor is the agent, and the `intake_event_id` is recorded as the cause, so the audit reads "agent X, for Alice (route owner), because ServiceNow INC0012345 was updated by integration user svc_boss".

---

## 7. Loop prevention, rate limits, storms, spend, quiet hours

### 7.1 Loop prevention (three layers)

1. **Self filter at parse** (`adapter.is_self`), per source:
   - Slack: `event.bot_id` present, `subtype` in (`bot_message`, `message_changed`, `message_deleted`, `message_replied`), or `event.user == connection.config.bot_user_id`. The existing parser already drops `bot_id`/`subtype` (`chat_adapters.py:153-154`); P9 keeps that and adds the user check. **Other bots** (`bot_id` ≠ ours) are dropped by default too; a route may opt in to bot authors (`allow_bots: [B0…]`) for agent-to-agent hand-offs, with the hop counter below.
   - Teams: `from.id == recipient.id` (the bot), `from.role == 'bot'`.
   - GitHub: `sender.login == settings.github_app_bot_login` (existing, `services/webhooks.py:1027-1039`) **or** `sender.type == 'Bot'` unless allow-listed.
   - ServiceNow/Salesforce: modifier = the connection's integration user (`sys_updated_by`, `LastModifiedById`). This only works if BOSS writes **as** that user; the MCP gateway's ServiceNow/Salesforce credential must be that integration user (§10).
   - Mail: our own sending address, or `Auto-Submitted` ≠ `no` (RFC 3834), or `X-BOSS-Run` present.
2. **Causality markers on our own writes**: Slack `metadata: {event_type: "boss_run", event_payload: {run, deployment, hops}}` on `chat.postMessage`; Teams `channelData.boss`; ServiceNow/Jira/Salesforce comments prefixed with an invisible marker or a dedicated field when available; mail headers `X-BOSS-Run`, `X-BOSS-Hops`, `Auto-Submitted: auto-generated`. Intake reads them into `bosscausality`; an event caused by the **same deployment** is dropped; any event with `hops ≥ 3` is dropped and alerted.
3. **Rate guard per entity**: at most `limits.per_entity` runs per (route, subject) per window (default 5 per 24 h for record sources, the ServiceNow AI Agent kill-switch default [doc, A.3]; 30 per hour for a chat thread). The sixth is `deferred` and the route owner gets one card; with `auto_pause` the route pauses (the existing failure-streak mechanism, `agent_triggers.py:50-52, 690-705`).

### 7.2 Source rate limits we must respect on the way out

The outbox sender keeps a token bucket per (endpoint, channel/conversation):
- Slack `chat.postMessage` ≈ 1/s per channel with short bursts; `chat.update` Tier 3 (50+/min) [doc, A.1]; streaming methods per their tiers.
- Teams per bot per thread: 7/1 s, 8/2 s, 60/30 s, 1,800/h; 50 RPS per app per tenant [doc, A.2].
- On 429: honour `Retry-After`, set `next_attempt_at`, do not count as a failed attempt (today every failure counts, `notify.py:855-873`; P9 distinguishes `RateLimited`).

### 7.3 Inbound rate limits and storm guards

| Guard | Where | Default | At the limit |
|---|---|---|---|
| Per-connection token bucket | Intake route (in memory per API instance, refilled from config) | 20 rps, burst 100 | Still verify + insert, but `status='shed'` beyond `hard_cap_per_min` (default 3,000/min); never answer non-2xx to Slack/Graph (they would retry and Slack auto-disables at > 95 % failures in 60 min [doc, A.1]) |
| Per-connection backlog | Processor | `max_backlog` 10,000 `received` rows | New events → `shed`; alert `intake.backlog_high` |
| Storm detector | Processor, per route | rate > 10× the route's 7-day p95 per minute, or > `limits.storm_per_min` (default 60) | Route switches to **digest mode** for 15 min: events still recorded, routed as one `coalesce` run per 5 minutes with a summary of N events (the trigger's existing `coalesce` policy, `agent_triggers.py:18-20, 567-568`); owner alerted |
| Per-deployment queue | Admission | `max_queued_runs` 200 | Fire outcome `deferred`; event back to `failed` with backoff; alert |
| Kill switch | Admission | existing `ambient.policy.stop_state` (`services/ambient/policy.py:327`) and deployment pause | `refused(stopped)` |

### 7.4 Spend guards

- **Per-run:** the existing budget reserve at run start (`BudgetGovernor.reserve`, `services/budget.py:1307`) and `budget_wait` (`thread_runs.py:43`); the project's `at_limit` choice (pause / cheaper / stop, `services/spend_limits.py:1-20`).
- **Per route / per deployment:** new scoped-budget scopes `trigger` and `deployment` (§4.2) with daily and monthly caps. At the cap the route pauses with the reason; chat users get one line ("This assistant has reached today's budget").
- **Per triage:** `daily_usd_cap` (§5.3).
- **Estimate shown in the console** before turning on `channel_all` or an email route: events/day (from the last 7 days of `intake_events` if the connection already exists) × the agent's mean cost per run (`agent_deployments.cost_per_run`, `services/agent_deployments.py:296-310`).

### 7.5 Quiet hours

Per route `quiet_hours {start, end, timezone, mode}` evaluated with `notify.quiet_now` (`services/notify.py:311-329`):
- `mode=defer` (record sources): the fire becomes `deferred` with `next_attempt_at` = quiet end.
- `mode=ack_and_defer` (chat): the person gets "I'll pick this up at 08:00", the run is admitted then.
- `mode=answer` (default for chat): no quiet hours for conversations a person starts.
Outbound quiet hours for proactive messages reuse comms' policy (`services/comms.py:1287-1360`).

---

## 8. Dispatch to `local` and `microvm` replicas

### 8.1 Verdict on the proposed model

The operator's model (§ brief, steps 1-7) is **confirmed**, with the refinements below. The key property is P8's rule "execution belongs to whoever holds the lease, and every write is fenced" (P8 §2.7): intake only has to produce a durable admitted run; who executes it, and where, is the claim's business. Nothing about an event needs to reach a VM except through `next`.

**Why not push events into VMs through `boss-hostd`.**
- `boss-hostd` would need routing knowledge (which VM has a free slot for which deployment), duplicating the scheduler; Postgres is the only truth (P8 §2.5).
- A pushed event has no durable owner between hand-off and checkpoint; a VM that dies after accepting it loses it, unless we re-invent the lease.
- Back-pressure is natural with pull: a replica with no free slot simply does not ask.
- The VM gets an inbound path from the host it otherwise does not have (only vsock port 5000 control, P8 §3.8), widening its attack surface.

### 8.2 The claim path

**Admission (unchanged + `target`).** `agent_deployments.admit_run` (`services/agent_deployments.py:268-291`) passes `target` into `thread_runs._start` (`thread_runs.py:162-176`) → `enqueue_job(..., target=…)`:
- `runtime: local` → `target='local'`.
- `runtime: microvm` → `target='deployment:<deployment_id>'`.

**Why `jobs.target` is required.** `claim_job` filters on `kind IN capabilities` only (`services/jobs.py:595-650`). A local worker advertising `agent.run` would claim a microVM deployment's run and execute it in the worker container, defeating the isolation the operator chose. With `target`:
- `/v1/worker/poll` (`apps/api/app/main.py:24209-24222`) claims only `target='local'`.
- `/v1/run-plane/next` (P8 §3.13.3) claims only `target='deployment:<the replica's deployment>'`, where the deployment is taken from the **replica token** that `boss-hostd` attached (P8 §3.2.2), never from the request body.

**Local (4a).**
```
worker → POST /v1/worker/poll {capabilities, wait_ms: 20000}
API:  claim_job(target='local') → found? return leased
      else subscribe(hub, kinds={'agent.run_admitted','job.enqueued'}) and wait ≤ wait_ms
      on wake → claim again (SKIP LOCKED; several waiters race, one wins, others re-wait)
```
The worker's `poll_interval_seconds=15` (`apps/worker/boss_worker/config.py:23`) becomes the **fallback** sleep only when the API does not support `wait_ms` (version skew). The API's wait runs off the request thread (`asyncio.Event` from the hub, the pattern of `identity/change_feed.py:103-150`).

**MicroVM replica (4b). Superseded by P8 LLD §3.13.0, which is binding: ONE long-poll per host, not per replica.** `boss-hostd` calls `POST /v1/run-plane/hosts/next` with every local replica's `free_slots`. The API claims runs for the host's deployments, assigns each to a replica and returns it, and the host hands it to that VM over vsock. The per-replica description below is kept only for the claim and filter details, which carry over unchanged.

**MicroVM replica (4b), original per-replica text, for reference.**
```
boss-runtime (VM) ─vsock 7104─► boss-hostd ─mTLS+replica token─► POST /v1/run-plane/next
   {free_slots: n, wait_ms: 25000}
API: claim up to n jobs WHERE kind='agent.run' AND target='deployment:<dep>' (SKIP LOCKED),
     grant lease to replica:<id>, epoch+1, in one txn; return [{run_id, epoch, handle binding}]
     none → wait on the hub for 'agent.run_admitted' with deployment_id=<dep>, ≤ wait_ms
```
- **One long-poll per replica, not per slot**: `free_slots` lets one request claim several runs (fewer connections; 350 replicas × 2 API = 700 open waits per host-pool, cheap as asyncio waits).
- `boss-hostd` passes the request through; it adds nothing but the token and the DPoP proof (P8 §3.8 steps 1-5).
- The hub event must carry `deployment_id` so only that deployment's waiters wake. `agent.run_admitted` today carries `thread_id, policy, busy, job_id, trigger` (`thread_runs.py:150-152`) and the NOTIFY payload carries only the row identity (`db/schema.sql:12984-12990`). P9 adds `deployment_id` to the event data and a `target` key to the NOTIFY JSON (still ≤ 8000 bytes), so the API can filter without a read.

**Why the existing `boss_events` NOTIFY and not a new channel.** It is already emitted at commit for every admit (`thread_runs.py:150` → `events` insert → trigger `db/schema.sql:12993-12997`), already listened to by one `PgListener` per API process with reconnect and resync (`services/event_stream.py:302-330`), and already filtered per subscriber by the `Hub` (`event_stream.py:268-300`). A dedicated `boss_runs` channel is a P9c option if `boss_events` volume makes the hub's filtering measurable (§12 metric `hub_fanout_ms`).

**Missed-notification safety.** NOTIFY is not durable: a listener reconnect loses notifications (`event_stream.py:303-305` resyncs). Waiters re-claim on (a) a wake and (b) a hub resync.

The safety net is **one sweep per API process, not one tick per waiter**:
- Every 5 s the process runs a single query, `SELECT DISTINCT target FROM jobs WHERE status='queued' AND target = ANY($targets_with_waiters) AND created_at < now() - interval '1 second'`, and wakes only the waiters for the targets it returns.
- A per-waiter 2 s tick would cost 2,500 queries/s at 5,000 idle replicas. The sweep costs 0.2 queries/s per API process, whatever the fleet size.
- Worst-case added latency after a lost NOTIFY: 5 s. This is rare, because it needs a listener reconnect.

**Idle cost at fleet scale (operator question, 2026-11-14).** A long-poll is not repeated polling. Each replica holds one request open and sends a new one only after work arrives, or after `wait_ms` (25 s) with no work.

| Item | Cost at 5,000 idle replicas |
|---|---|
| Requests | 5,000 / 25 s = 200 req/s of ~1 KB (less with HTTP/2 header compression), about 1.6 Mbit/s fleet-wide |
| Postgres | Zero while idle. Waiters wait on the in-memory hub, which one `LISTEN` per API process feeds. Only a wake (a real run) causes a claim query, plus the 5 s sweep above. |
| API memory | One asyncio wait per replica: kilobytes each |
| TCP connections and requests | **One connection and ONE outstanding request per host** (P8 §3.13.0). 20 hosts = 20 requests, whatever the number of VMs. Earlier text in this row, about one HTTP/2 stream per replica, is superseded. |
| (superseded) | **One per host, not per VM.** `boss-hostd` already keeps a pooled HTTP/2 mTLS connection to the API (P8 §3.8). Each replica's `next` long-poll is one HTTP/2 stream multiplexed on it: 20 hosts × 350 replicas = 20 connections, 7,000 streams. API instances set `SETTINGS_MAX_CONCURRENT_STREAMS` ≥ 1,000 per connection. |

- **Option for later (only if measured stream counts warrant it):** `boss-hostd` aggregates its replicas into one host-level `next` call carrying `{deployment → free_slots}` and fans the claimed runs out over vsock. That keeps one stream per host. It is not needed for P9: the host would have to learn per-run assignment traffic, and HTTP/2 streams already make per-replica polls cheap.
- **P9b-T11 (new):** with 5,000 simulated idle replicas on 2 API instances, show:
  - idle Postgres queries attributable to waiters ≤ 1/s;
  - API CPU < 5 %;
  - claim p95 unchanged versus 50 replicas.

**The `boss-hostd` desired-state hint** (P8 §3.2.1 watch) is **not** used for ordinary wake-ups: a replica already long-polling wakes faster through the API than through a host round-trip, and the host should not learn per-run traffic. It **is** used for scale-from-zero (§8.6) and for "drain this replica", which are placement events.

### 8.3 Latency budget (event → run start, warm replica)

| Step | Budget p95 | Basis |
|---|---|---|
| Source → BOSS edge (network, TLS) | outside our control | |
| Intake route: verify + insert + commit | 40 ms | HMAC/JWT (JWKS cached) < 1 ms; one insert + commit ~5-20 ms [estimate → P9a-T2] |
| NOTIFY `boss_intake` → processor wake | 10 ms | |
| Processor claim + parse + screen (patterns) + identity (cached link) + route | 60 ms | Screen in `patterns` mode is regex-only (`tool_result_screen.py:1-20`) |
| Identity miss (Slack `users.info` via gateway) | +150 ms first time per user, cached 1 h | |
| Optional model triage | +300-800 ms [estimate] | skipped for addressed chat |
| `fire` → `admit` txn + commit | 30 ms | 4 writes (§2.4) |
| NOTIFY `boss_events` → API hub → waiting `next` | 15 ms | |
| Claim txn + lease grant + response through `boss-hostd` → VM | 25 ms | +1-2 ms host hop (P8 §2.15.3) |
| Kernel start in the replica (thread history load over the run plane) | 100 ms | [estimate → P9b-T4] |
| **Total, intake ack → run executing** | **≈ 280 ms p95** (≤ 550 ms NFR-4 with margin; + triage when on) | |
| First visible sign in Slack (status / reaction, §9.2) | posted by the processor in parallel with fire: +≈ 300 ms | |

Local worker: identical with the long-poll; without it (version skew) p50 +7.5 s, p100 +15 s.

### 8.4 Ordering

- **Per conversation / entity**: the processor claims with `FOR UPDATE SKIP LOCKED` **and** a transaction-scoped advisory lock on `hashtext(conversation_key)`: a second event of the same conversation waits until the first is fired, so admits happen in `received_at` order; `thread_runs.admit` then serialises runs on the thread (the row lock, `thread_runs.py:121`).
- **Across conversations**: parallel, no ordering promise.
- **Source reordering** (Slack retry after 1 min, Graph retries up to 4 h, Salesforce outbound messages out of order [doc, A]): record events carry `event_time` / version (`sys_mod_count`, CDC `commitNumber`); an event older than the last processed version for the same `record_id` is `ignored(stale)` unless the route asks for every change.

### 8.5 Back-pressure (end to end)

```
source ──(never refused; shed beyond hard cap)──► intake_events ──(processor: N threads per API,
fair round-robin across connections by claim query ORDER BY connection_id rotation)──► fire
──(per-deployment max_queued_runs → 'deferred')──► jobs(target) ──(claimed only when a slot is free)──►
replica slots (default 4 per VM, P8 §2.6) ──(budget reserve → budget_wait)──► model gateway
```
- The intake route never blocks on downstream state; it only inserts. Bounded executor: 32 threads per API instance for intake routes, separate from console traffic.
- The processor claims at most `processor_batch` (default 50) rows per connection per pass, so one stormy connection cannot starve others (NFR-7).
- The queue between admission and execution is `jobs`/`agent_runs`, already durable; queued runs wait there with no timeout unless the route sets `max_wait` (chat default 10 min, then the person is told "the assistant is busy, try later" and the run is cancelled).
- Queue age per deployment (`now − oldest queued run`) is the autoscaling signal (§8.6) and an alert.

### 8.6 No replica free, and scale-to-zero

| Situation | Behaviour |
|---|---|
| All slots busy, replicas < `max_replicas` | Run stays `queued`. When queue age > `queue_slo_ms` (default 1 s) for 10 s, the scheduler raises the deployment's desired replica count by one (desired-state generation bump → `boss-hostd` restores a VM from snapshot, ~10-40 ms + runtime ready [P8 §2.10, estimate]); the new replica's first `next` claims the run. Scale-down after 10 min idle, never below `min_replicas`. |
| All slots busy, replicas = `max_replicas` | Run waits. Chat: after 5 s the processor posts "Queued: N ahead of you" via `chat.status`. Queue age alert at 60 s. |
| `replicas = 0` and `scale_to_zero = true` | Admission sees zero live replicas (`agent_replicas` observed state, P8 §3.1) and bumps desired to `max(1, min_replicas)` **in the same transaction** as the admit. The host restores a VM (cold start of a new VM type ≤ 3 s [P8 §8.1 target]); the replica's first `next` claims the run. Chat users see the status line meanwhile. |
| Deployment has no host capacity anywhere | Run waits; Fleet doctor alerted (P8 §3.16.2); chat user told after 30 s. |
| **Fallback to `runtime: local`?** | **No, by default.** A microVM deployment was put there for isolation; silently running it in the worker container breaks that promise. A deployment may opt in (`fallback_runtime: local`, refused for deployments that handle `untrusted` sources such as public email or `channel_all`) (§15 D7). |

Recommendation: **chat-facing deployments keep `min_replicas ≥ 1` per production environment** (two for HA, P8 requirement 5); scale-to-zero only for record/mail agents where a 3 s start is invisible.

### 8.7 HA and fencing

- Intake: two API instances (P8c); any instance accepts any event; the processor is a competing consumer (`SKIP LOCKED`), pollers and renewers are singletons on `extension_job_leases` (`ticket_sync.py:1437-1495` pattern). Processor lease on a row: 60 s; a crashed processor's rows are re-claimed after expiry.
- Runs: P8 lease + `fence_epoch`; a dead replica's run resumes elsewhere from its checkpoint in ~35 s (P8 Flow 5).
- **Replies under failover**: a reply is a fenced run-plane write (§9.4); a stale replica's reply is refused (`409 fenced`), so a person never sees two answers from two executors.
- A reply already handed to Slack before the VM died is in `chat_outbox` with its dedupe key; the resumed run regenerates the same `op_seq` for the same step and the insert is a no-op.

---

## 9. Replies and two-way conversation

### 9.1 The reply tool (control plane)

Tools exposed to every agent whose run has `finalize_ctx.reply_to`:

| Tool | Effect |
|---|---|
| `chat.reply(text, blocks?, final=true)` | Post in the originating thread (Slack `thread_ts`, Teams reply chain). |
| `chat.stream(text_delta)` / `chat.stream_end()` | Incremental answer (§9.2). |
| `chat.status(text)` | Status line / typing indicator. |
| `chat.react(emoji)` | Slack reaction on the triggering message. |
| `chat.ask(question, options)` | A decision card in the thread (reuses `decision.created` → `_queue_chat_cards`, `services/core.py:278-279`, `chat_channels.py:534-557`). |
| `chat.send(to, text)` | Proactive message to a person/channel; **write-class**, governed by tool policy and quiet hours. |

Execution:
- `runtime: local`: an ordinary BOSS-state tool in the kernel.
- `runtime: microvm`: `POST /v1/run-plane/tools/chat.reply/invoke` (P8 §3.13.3 "Tools that touch BOSS state run on the control plane through `tool_policy_engine`"). The VM sees only a tool; it never holds an endpoint id it could re-target, a bot token or a Slack URL.
- The tool resolves the target **from the run** (`finalize_ctx.reply_to` → `chat_threads` row), not from arguments. A run can only reply into the conversation that started it, unless `chat.send` is granted.
- It writes `chat_outbox (kind='agent_reply', run_id, fence_epoch, op_seq, dedupe_key='reply:<run>:<step>:<seq>')` inside the fenced transaction (`require_active_lease`, `jobs.py:1873`), then commits. A NOTIFY on `chat_outbox` insert wakes the sender in < 50 ms, instead of the outbox tick.

### 9.2 Streaming and edit-in-place

| | Slack | Teams |
|---|---|---|
| First sign of life (processor, before the run starts) | `assistant.threads.setStatus` "Thinking…" (auto-clears on reply or after 2 min) [doc, A.1]; migrate to `agents.sessions.setStatus` (Slack changelog 2026-08-20, compatibility bridge) [doc, A.1]; fallback: 👀 reaction | `typing` activity |
| Streaming | `chat.startStream` → `chat.appendStream` → `chat.stopStream` (2025-10-07 changelog; streams ≤ 10 min, ≤ 12,000 chars; `recipient_user_id`/`recipient_team_id` required in channels) [doc, A.1] | Bot streaming via `typing` activities with `streaminfo` (`informative`/`streaming`, sequence numbers, full accumulated text each time, ≤ 1 request/s, ≤ 10 min, one stream per conversation; **not** for proactive messages) [doc, A.2] |
| Fallback | Post once, then `chat.update` the same `ts` at most every 1.5 s (Tier 3), final update at end | Post, then `PUT …/activities/{id}` (updateActivity) at most 1/s |
| Long answers | Split at 3,000 chars per block (existing `render_*` caps, `chat_adapters.py:206-236`) | Adaptive Card / text ≤ 28 KB [UNVERIFIED → P9b-T1] |

The outbox coalesces stream deltas: deltas for one `(run, stream)` arriving faster than the channel's allowed rate are merged into one append/update. Ordering per stream is by `op_seq`; the sender holds a per-stream advisory lock so two API instances never interleave appends.

### 9.3 Teams specifics

- **Azure Bot registration is required in practice.** Inbound activities come from the Bot Connector service and are signed for the bot's Microsoft App ID; outbound calls need a Bot Framework token from Entra client credentials (`scope https://api.botframework.com/.default`) [doc, A.2]. New **multi-tenant** Azure Bot resources cannot be created after 31 July 2025; use **single-tenant** (in the customer's tenant) or user-assigned managed identity [doc, A.2]. A Developer-Portal-only ("Teams-managed") bot exists without an Azure subscription, but OAuth/SSO need an Azure Bot resource and outbound sends reportedly fail in production tenants [UNVERIFIED, A.2]. **Decision D5: require an Azure Bot (single-tenant) registered in the customer's tenant.**
- **Inbound** `POST /v1/intake/teams/{path_token}`: validate the JWT against `https://login.botframework.com/v1/.well-known/openidconfiguration` (JWKS cached ≤ 24 h), `iss = https://api.botframework.com`, `aud = app id`, `nbf/exp` ± 5 min, and `serviceUrl` claim = `activity.serviceUrl` [doc, A.2]. Ack 200 after insert. Teams re-sends an activity not processed in 15 s [doc, A.2]; dedupe on `activity.id`.
- **Conversation reference** stored in `chat_threads.conversation_ref`: `serviceUrl`, `conversation.id`, `tenantId`, bot id, last `activityId`. Proactive messages use it; `serviceUrl` from the latest inbound activity wins (it can change).
- **Install scope**: proactive messages need the app installed in that scope (personal/team/chat) [doc, A.2]; 403 `MessageWritesBlocked` → mark the link `blocked`, stop retrying.
- **SDK**: the Bot Framework SDK is archived (support ended 31 Dec 2025); successor is the Microsoft 365 Agents SDK [doc, A.2]. BOSS needs only JWT validation + three REST calls, so implement them directly in `TeamsChat` (no SDK dependency), with the protocol pinned by contract tests.
- **Graph change notifications for chat messages** (reading channel traffic without an @mention) are a later option: subscriptions ≤ 4,320 min with lifecycle notifications required beyond 1 h and encrypted resource data [doc, A.2]. Not needed for "people talk to agents". P9c optional.

### 9.4 The outbox and its guarantees

`chat_outbox` (`db/schema.sql:12961-12977`) + `deliver_chat_due` (`notify.py:832-883`) already give: a row before send, dedupe, claim with a 2-minute lease and `SKIP LOCKED`, send outside the claiming transaction, backoff (30 s … 1 h, `notify.py:827-829`), dead after 6 attempts with an event. P9 adds:
1. NOTIFY-driven immediate send (first attempt ≤ 50 ms after commit) instead of relying on the tick; `chat_channels` already kicks immediately after a press via a background task (`apps/api/app/routers/chat_channels.py:246-268`).
2. `RateLimited` → reschedule without spending an attempt.
3. Ops `stream_start/append/stop`, `status`, `react`, Teams `post/update/typing/stream`.
4. Receipts written back to `chat_threads.bot_message_refs` (for edits and loop checks).
5. A fenced insert: the run-plane tool verifies `(run, epoch)` before inserting (§8.7).

**Bot token handling.** The sender resolves `bot_token` from the secret backend at send time (`endpoint_secret`, `chat_channels.py:198-206`); it lives in the API process memory for one call. In P8 terms the API is the "chat gateway": a central process that holds the real key, the same role as the MCP gateway (P8 §3.10). The VM never sees it, and neither does `boss-hostd`.

**Why the outbox and not the MCP gateway for replies.** (a) Replies need conversation state BOSS owns (thread map, our message ts, stream ids), (b) they need to be fenced to the run epoch, (c) they need rate shaping per channel across all runs, and (d) decision cards already go this way. The MCP gateway remains the path for an agent that wants to *use* Slack/Teams as a tool (search messages, read a channel, post to an arbitrary channel), with the same policy engine and audit (P8 §3.10).

---

## 10. Security

### 10.1 Public routes

- Each intake route is declared `@public(reason=PublicReason.HMAC_SIGNED_WEBHOOK, verifier=<adapter verify>)` (`identity/declarations.py:43-64`), like the chat and ticket routes (`routers/chat_channels.py:255-256`; `routers/ticket_sync.py:175-177`). Teams JWT and Gmail Pub/Sub OIDC are not HMAC; P9 adds `PublicReason.SIGNED_JWT_WEBHOOK` ("a JWT signed by the source's published keys, issuer + audience pinned"), an amendment the enum's docstring requires (`declarations.py:44-45`).
- URL: `/v1/intake/{kind}/{path_token}`; `path_token` is 24+ random bytes (unguessable, rotatable) but **not** the authenticator.
- Verify on the raw bytes before parsing (the existing rule, `chat_adapters.py:8-11`); bounded body read (1 MB default; Slack/Teams bodies are small) with 413 beyond, as `_read_bounded` (`routers/chat_channels.py:196-208`).
- Replay window: Slack 5 min (existing), Standard Webhooks timestamp 5 min, Teams `exp`, Pub/Sub token ≤ 1 h; plus dedupe.
- Graph: `clientState` compared in constant time; handshake echo only for subscriptions BOSS created (a `validationToken` request for an unknown connection gets 404).
- Generic webhook: Standard Webhooks `whsec_` secrets, multiple signatures for rotation (`v1,<sig> v1,<sig2>`) [doc, A.7]; BOSS v1 scheme kept for existing triggers.
- CloudEvents webhook `OPTIONS` abuse-protection handshake answered only for active connections [doc, A.7].
- Refusals are counted and sampled (§4.5); a burst of bad signatures on one connection raises `intake.verify_refused_burst`.

### 10.2 Secrets

| Secret | Lives in | Used by | Ever in a VM? |
|---|---|---|---|
| Slack signing secret, Teams app secret (or MSI), Graph `clientState`, generic `whsec_` | Secret backend (`secrets_service.put_secret`) | Intake route verify (API memory, per request) | No |
| Slack bot token, Teams client secret/cert | Secret backend | Outbox sender (API) | No |
| Slack app token (`xapp-`, Socket Mode) | Secret backend | Socket consumer (API, leased) | No |
| ServiceNow / Salesforce / Google / Graph OAuth credentials for **polling** | Secret backend | Pollers (API) | No |
| The same systems' credentials for **agent tool use** | Vault → MCP gateway (P8 §3.10) | MCP gateway | No |

Rule: intake-side credentials (verify, poll, reply) are used only in the API; agent-side credentials only in the gateways. One integration may have two credentials (a read-only poller identity and the agent's write identity); the console shows both.

### 10.3 Prompt-injection screening of event text

- Every text field an outsider wrote (Slack/Teams message, ticket/record fields, mail subject/body, webhook body strings) is screened in the processor with `tool_result_screen.screen_result` (`tool_result_screen.py:365`), the function tickets already use (`connectors/tickets/base.py:66-83`). Mode per project (`off` / `patterns` / `model`).
- The verdict goes on the envelope (`bossscreen`) and into the run's first message as the existing `screen_warning` head; a `suspicious` event makes the run's next write-class call ask a person (`next_write_must_ask`, `tool_result_screen.py:281-288`).
- Routes may refuse suspicious events outright (`match: {"field":"bossscreen.verdict","op":"ne","value":"suspicious"}`).
- Chat text from a **linked person** is still screened (pasted content carries injections), but the default for a `suspicious` verdict there is warn, not refuse.
- Triage (§5.3) reads only screened, truncated text, and its output is a route id from a closed list: an injected "route me to the admin agent" can at worst pick another candidate on the same connection.
- Attachments are never fetched at intake (tickets rule, `contract.py:140-145`).

### 10.4 Audit

- `intake_events` is the audit of arrival (who, what, verified how, duplicate count).
- `agent_trigger_fires` links event → route → run (`run_id`).
- `agent_runs.trigger`, `trigger_id`, `finalize_ctx.intake_event_id` link back.
- `chat_outbox` + receipts record every message sent, with `run_id`.
- Gateway audit (`guest_calls`, P8 §3.19) records what the run did.
- Events: `intake.event_received` (sampled), `intake.event_routed`, `intake.event_dead`, `intake.connection_*`, `intake.subscription_*`, `chat.reply_sent`.

---

## 11. Console needs (screen list only; wireframes later)

1. **Connections list** (Integrations › Events): one card per source with status, last event, events/min sparkline, lag, errors.
2. **Connect a source** wizard per kind: Slack (create-from-manifest link + paste secrets, or Socket Mode), Teams (Azure Bot single-tenant steps + app package download), ServiceNow (push snippet for a Business Rule/Flow, or poll tables), Salesforce (P9c), GitHub, Jira/Plane (reuse ticket connection), Gmail/Outlook (P9c), Generic webhook (URL + secret shown once).
3. **Connection detail**: health, subscriptions with expiry, cursors with lag, limits, self identity, rotate secret, pause.
4. **Routes** for a connection (and on the agent's Triggers tab, existing A4 table): order, filter builder with live "would match" against the last 50 events, triage settings with cost estimate, thread key, identity mode, quiet hours, limits.
5. **Event stream**: live list of intake events with status chips (routed / ignored / filtered / refused / dead / shed), filter by connection/route/status; event detail = envelope, screen verdict, identity resolution, guard decisions, fires → runs, replies.
6. **Dead letters and replay**: bulk select, reprocess / force (reason required).
7. **People links**: per connection, linked/unlinked external users, method (code / SSO email / Entra oid), revoke; SSO attestation setting.
8. **My chat links** (exists: `/v1/chat/links`, `routers/chat_channels.py:138-195`) gains Teams.
9. **Agent conversation view**: a Slack/Teams thread rendered beside its BOSS thread and runs.
10. **Govern › Limits**: per-route and per-deployment budgets (new scopes), storm/digest state.

---

## 12. Observability

**Metrics** (Prometheus names, labels `connection`, `kind`, `route`, `deployment`):

| Metric | Type | Alert |
|---|---|---|
| `intake_ack_seconds` | histogram | p99 > 1 s for 5 min (Slack deadline 3 s) |
| `intake_events_total{status}` | counter | `refused` burst; `shed` > 0 |
| `intake_duplicates_total` | counter | rising = source retrying → we are slow or erroring |
| `intake_backlog{connection}` | gauge | > 1,000 for 2 min |
| `intake_processing_seconds{stage}` | histogram | parse / screen / identity / route / fire |
| `intake_to_admit_seconds` | histogram | p95 > 500 ms |
| `admit_to_claim_seconds{runtime}` | histogram | p95 > 1 s warm; per deployment |
| `run_queue_age_seconds{deployment}` | gauge | > `queue_slo_ms` × 10 |
| `first_visible_reply_seconds{kind}` | histogram | p95 > 2 s |
| `outbox_send_seconds`, `outbox_rate_limited_total`, `outbox_dead_total` | | dead > 0 |
| `intake_poll_lag_seconds{connection,stream}` | gauge | > 3 × interval |
| `intake_subscription_expiry_seconds` | gauge | < 6 h and not renewing |
| `intake_triage_usd_total`, `route_spend_usd` | counter | at 80 % of cap |
| `loop_drops_total{layer}` | counter | spike = misconfigured self identity |
| `hub_fanout_ms` | histogram | P9c decision on a dedicated channel |

**Tracing.** `trace_id` minted at the intake route, stored on the event, passed as `finalize_ctx.trace_id`, joined to P4 spans and P8 `guest_calls`; one trace from "Slack sent" to "reply delivered".

**Health.** Each connection's `health` JSON feeds `system_services` beats (`db/schema.sql:14494`) for the processor, each poller and each renewer, so the system doctor sees a dead poller.

---

## 13. Failure modes

| Failure | Detection | Effect | Recovery |
|---|---|---|---|
| API instance dies mid-request before commit | Source gets no 2xx | None stored | Source retries (Slack ×3 within ~6 min; Graph ≤ 4 h; Teams re-send after 15 s); GitHub does **not** retry [doc, A.5] → the GitHub poller reconciliation / redelivery API sweep (P9b) |
| API dies after commit, before reply | Source retries | Duplicate → `duplicates+1` | None needed |
| Postgres down | Insert fails | 503 to source | Source retry; Slack auto-disable risk if > 95 % failures for 60 min and ≥ 1,000 events/h [doc, A.1] → alert at 5 min |
| Processor crash | Row lease expires (60 s) | Delay ≤ 60 s | Another instance claims |
| Lost NOTIFY | 2 s safety tick | +≤ 2 s | — |
| Router bug / filter error | Exception per event | `failed` + backoff → `dead` | Fix, replay |
| Identity lookup (Slack/Graph API) fails | Adapter error | Event `failed`, retried; after 3 tries → treated as unknown user (never as the owner) | — |
| No live replica | Queue age | Run waits; scale-up / scale-from-zero | §8.6 |
| Replica dies mid-run | P8 watchdog | Resume elsewhere ~35 s; replies fenced | P8 Flow 5 |
| Bot token revoked / app uninstalled | Outbox `invalid_auth` / Teams 403 | Replies dead; connection `auth_failed` | Console asks to reconnect; events still recorded |
| Slack `app_rate_limited` (30,000/h/workspace) [doc, A.1] | Event from Slack | Events dropped by Slack | Alert; reduce `channel_all` subscriptions |
| Graph subscription expired / removed | Renewer / lifecycle `subscriptionRemoved` | Gap | Recreate + delta sync from cursor |
| Graph `missed` lifecycle | Lifecycle notification | Gap | Delta sync |
| Gmail `historyId` too old (404) | Poller | Gap | Full sync bounded to last 7 days, dedupe absorbs overlap [doc, A.6] |
| Salesforce replay id beyond 72 h | Subscribe error | Gap | Resubscribe from `LATEST` + a Table/SOQL catch-up query on `LastModifiedDate` [doc, A.4] |
| ServiceNow outbound REST fails (instance side) | Missing events | Gap | Poll fallback on `sys_updated_on` every 5 min for push connections ("push with poll safety net", the Zapier pattern) |
| Agent replies to itself | Loop layers | Dropped | §7.1 |
| Storm | Detector | Digest mode | §7.3 |
| Injected text | Screen | Warning + write held | §10.3 |
| Spend cap reached | Budget | Route paused / `budget_wait` | §7.4 |

---

## 14. Phases, tests and exit criteria

Each phase passes on a laptop (local runtime) and on boss-1 before the next starts, as P8 requires. Targeted tests during development; full suites once before merge.

### P9a: intake core, Slack conversations, generic webhook (M-L)

**Build**
- `connectors/intake/` contract, registry, base; adapters `slack` (push; socket opt-in), `webhook` (Standard Webhooks + BOSS v1).
- Tables `intake_connections`, `intake_events`, `intake_cursors` (empty use), extensions to `agent_triggers`, `agent_trigger_fires`, `chat_threads`, `chat_identity_links`, `chat_outbox`, `jobs.target`.
- Intake route, processor, router (filter DSL, no triage), thread keys, `intake` trigger kind, identity (code links + SSO-attested email), loop layers 1-3, per-connection limits, dead letter + replay.
- Worker long-poll wake (`/v1/worker/poll wait_ms`), `jobs.target` on local claims.
- Reply tools on the local runtime; outbox NOTIFY kick, rate-limit aware retry; Slack status + `chat.update` edit-in-place; `chat.startStream` behind a flag.
- Console screens 1-3, 4 (basic), 5, 6, 7.

**Tests**
- T1 Contract: registry refuses manifest/adapter mismatch (port of the ticket registry tests).
- T2 Ack latency: 100 rps for 10 min on one API instance, Slack-signed payloads; p99 ≤ 300 ms, zero 5xx.
- T3 Exactly-once: replay every Slack delivery 1-3 times with `X-Slack-Retry-Num`; runs created = distinct `event_id`s.
- T4 Crash: `kill -9` the API at random during a 1,000-event run; accepted (2xx) events lost = 0; duplicate runs = 0.
- T5 Ordering: 20 messages in one thread within 1 s → admitted in order, run inputs in order.
- T6 Loop: agent replying in a channel it listens to (`channel_all`) → 0 self-triggered runs over 100 replies; bot-to-bot ping-pong stops at hop 3.
- T7 Identity: unlinked user → link DM, no run; SSO-attested auto-link; guest user never auto-linked; revoked link refused on next message.
- T8 Storm: 3,000 events/min on one connection → other connection's p95 intake→admit unaffected (± 100 ms); digest mode engages.
- T9 Latency (local): event → run executing p95 ≤ 550 ms with the long-poll; first status visible in Slack p95 ≤ 1.5 s (measured with a real Slack workspace on boss-1).
- T10 Injection: seeded injection strings in Slack text → verdict `suspicious`, write held.
- T11 Replay: dead → reprocess fires only failed routes; force with reason creates one new run.

**Exit:** T2-T11 green; zero lost and zero duplicate runs in T3/T4; a person holds a 10-turn conversation with an agent in a Slack thread on boss-1 with the agent's replies in the thread.

### P9b: Teams, microVM claim path, SaaS push/poll (L)

**Build**
- `teams` adapter (JWT verify, activities, conversation references, proactive, streaming, update), `chat_endpoints.kind='teams'`, Entra oid linking.
- Run-plane `next` with `free_slots`, `jobs.target='deployment:<id>'`, hub filtering by deployment, `chat.*` tools via run-plane invoke, fenced outbox inserts.
- Autoscale on queue age, scale-from-zero via desired state, `min/max_replicas`.
- Model triage with caps; storm digest; scoped budgets `trigger`/`deployment`; quiet hours.
- `servicenow` adapter: push (snippet for an async Business Rule / Flow REST step posting Standard-Webhooks-signed JSON to the intake URL) + Table API poll (keyset on `sys_updated_on`+`sys_id`).
- `github` adapter on intake (migrate `/v1/webhooks/github` routing; keep `inbound_webhooks` as the reconciler's audit until P9c); `jira`/`plane` adapters delegating to ticket connectors; ticket pickup re-plumbed so the provider fetch happens in the processor, not the request (fixes F9).
- Console 4 (full), 8, 9, 10.

**Tests**
- T1 Teams contract tests against recorded activities; JWT negative tests (wrong `aud`, wrong `iss`, expired, `serviceUrl` mismatch).
- T2 Teams re-send after 15 s → one run.
- T3 Claim latency, microVM: warm replica, 1,000 events → admit→claim p95 ≤ 300 ms, p99 ≤ 1 s; event → run executing p95 ≤ 550 ms.
- T4 Isolation: a local worker never claims a `deployment:` job; a replica never claims another deployment's job (token-derived).
- T5 Failover: kill the replica holding 4 live chat runs → runs resume on the other replica within 40 s; each person receives exactly one final answer (fenced replies).
- T6 Scale-from-zero: first event to a zero-replica deployment → run executing ≤ 3 s p95.
- T7 Back-pressure: 500 queued runs, `max_queued_runs=200` → 300 deferred, retried, no loss.
- T8 ServiceNow: push + poll safety net together → each change one run; poll alone lag ≤ interval + 30 s.
- T9 Triage cap: daily cap reached → default route, spend never exceeds cap + one event's estimate.
- T10 Teams rate limits: 20 messages/s into one conversation → no 429 storm; sender shapes to 7/s.

**Exit:** all green on boss-1 with two hosts; Slack and Teams conversations with a `runtime: microvm` replica agent; no secret found in any VM (P8 scan extended to Slack/Teams tokens).

### P9c: subscriptions, mail, Salesforce, polish (M-L)

**Build**
- `intake_subscriptions` + renewer; `outlook` (Graph change notifications + delta), `gmail` (`users.watch` → Pub/Sub push with OIDC verify; daily renew; `history.list`), lifecycle handling.
- `salesforce`: Pub/Sub API subscriber (gRPC, `num_requested` ≤ 100, replay ids, 72 h retention) as a leased long-lived consumer: the first user of the `subscribe/stream` half of the contract; optional Outbound Message endpoint.
- Optional Teams Graph channel-message subscriptions.
- Partitioning of `intake_events`; retention jobs; dedicated NOTIFY channel if `hub_fanout_ms` warrants.
- Fleet doctor playbooks: subscription lost, poller dead, connection auth failed, outbox dead letters.

**Tests**
- T1 Graph: validation handshake ≤ 10 s; renew before expiry for 7 simulated days; `subscriptionRemoved`/`missed` → recreate + delta, no gaps (compare with a full listing).
- T2 Gmail: watch renewal; `historyId` 404 → bounded full sync; DMARC-failing From never resolves to a person.
- T3 Salesforce: kill the subscriber mid-stream → resume from stored replay id, 0 gaps, 0 duplicate runs; replay id older than 72 h → resubscribe + catch-up query.
- T4 Soak: 72 h, all adapters, mixed load (10 events/s), zero lost, zero duplicates, p99 ack ≤ 300 ms throughout.

**Exit:** T1-T4 green; operator walk-through of every console screen on boss-1.

### Checks for `[UNVERIFIED]` items

| Check | Where |
|---|---|
| Salesforce Outbound Message `Ack` format and retry cadence | P9c-T3 with a dev org |
| Teams message size limit and Adaptive Card limits | P9b-T1 |
| Teams Developer-Portal-only bot outbound behaviour | P9b-T1 (one attempt, documented) |
| Slack `appendStream`/`stopStream` tiers | P9a-T9 measured |
| ServiceNow Flow Designer REST step auth options | P9b-T8 with a PDI |
| Graph 410 on expired delta token | P9c-T1 |

---

## 15. Open decisions (with recommendations)

| # | Decision | Recommendation |
|---|---|---|
| D1 | One Slack app per BOSS install fronting many agents, or one Slack app per agent | **One per install** (connection) in P9; agent chosen by thread ownership, channel binding, leading name or default (§5.2). Per-agent branded apps later via the Slack manifest API. |
| D2 | Socket Mode for installs without a public URL (laptops) | **Support as an opt-in mode in P9a**, Slack only. Not allowed for Marketplace apps [doc, A.1]; fine for single-workspace installs. Leased singleton consumer, ≤ 10 connections per app. |
| D3 | Auto-link by SSO email | **Opt-in per connection with an admin attestation**, guests/external never; code link otherwise (§6.3). |
| D4 | Unknown chat users | **No run; link prompt** (≤ 1/h). `owner_readonly` available per route. |
| D5 | Teams registration | **Azure Bot, single-tenant, in the customer's tenant** (multi-tenant creation deprecated after 31 Jul 2025 [doc, A.2]); BOSS implements the REST protocol directly (Bot Framework SDK archived). |
| D6 | Scale to zero | **Off for chat-facing deployments** (`min_replicas ≥ 1`, 2 in production); on allowed for record/mail agents (≤ 3 s start). |
| D7 | Fallback from microVM to local when no replica | **No** by default; opt-in per deployment, refused for untrusted sources. |
| D8 | Model triage | **Off by default**; on per route with a per-event and daily cap; never for explicitly addressed chat. |
| D9 | Raw payload retention | **14 days raw, 30/90 days envelope** (§4.5). Confirm against customers' data policies (mail bodies, PII). |
| D10 | Intake inside the API or a separate `boss-intake` service | **Inside the API** (two instances from P8c); lift out later with no schema change. |
| D11 | Slack DM threading | **One BOSS thread per DM per day** unless the person replies in a Slack thread (then per thread). Alternative: one thread forever per DM (long context costs). |
| D12 | Replies via outbox vs MCP gateway | **Outbox via a run-plane tool** for conversational replies; MCP gateway for Slack/Teams as a general tool (§9.4). |
| D13 | ServiceNow push auth | **Standard Webhooks HMAC header computed in the Business Rule script**, secret in a ServiceNow system property encrypted (password2) field; mutual TLS where the customer prefers. Poll safety net always on. |
| D14 | Salesforce in P9c via Pub/Sub API (stream-like, long-lived gRPC) | **Yes**; it is the first consumer of the `subscribe/stream` contract and validates it for Kafka later. Outbound Messages only as a fallback for orgs without CDC/Platform Event allocation. |
| D15 | Org-wide connections (one Slack workspace across many projects) | **P9b**, with routes still per project and the project chosen by channel binding. |
| D16 | Copilot-Studio-style "maker credentials" risk | BOSS already avoids it: service events run on the owner's **standing grant re-checked per firing**, and person events run as the person (§6). Confirm the operator accepts owner-grant semantics for service events. |

---

## Appendix A. External research (per source), with URLs

All retrieved 14 Nov 2026. Salesforce developer pages returned 403 to direct fetches; Salesforce figures come from the official pages' search snippets and are marked accordingly.

### A.1 Slack
- Events API: respond with HTTP 2xx within 3 s; retries up to 3 times (near-immediate, 1 min, 5 min); `X-Slack-Retry-Num` (1-3), `X-Slack-Retry-Reason` (`http_timeout`, `too_many_redirects`, `connection_failed`, `ssl_error`, `http_error`, `unknown_error`); `x-slack-no-retry: 1` on a non-200 stops redelivery of that event; `event_id` unique across workspaces; subscriptions auto-disabled when > 95 % of deliveries fail within 60 min (apps with < 1,000 events/h exempt); 30,000 deliveries per workspace per app per 60 min, then `app_rate_limited` events. https://docs.slack.dev/apis/events-api/
- Socket Mode: not allowed in the public Slack Marketplace; up to 10 open WebSockets; `apps.connections.open` with an app-level token; each envelope acked by `envelope_id`; connections refresh every few hours. https://docs.slack.dev/apis/events-api/using-socket-mode/
- Signing: `v0:<timestamp>:<raw body>`, HMAC-SHA256 with the signing secret, `v0=` prefix, 5-minute window, constant-time compare. https://docs.slack.dev/authentication/verifying-requests-from-slack/
- `url_verification` challenge echo. https://docs.slack.dev/reference/events/url_verification/
- `app_mention` (`app_mentions:read`; not sent for DMs → `message.im`). https://docs.slack.dev/reference/events/app_mention/ ; message subtypes (`bot_message`, `message_changed`, `message_deleted`, `message_replied`, `thread_broadcast`) and scopes per channel type. https://docs.slack.dev/reference/events/message/
- Dropping events with `bot_id`/`bot_message`/own user id is common practice, not a documented Slack rule [UNVERIFIED].
- Rate limits: tiers 1-4; `chat.postMessage` ≈ 1 message/s per channel with a workspace limit; 429 + `Retry-After`. https://docs.slack.dev/apis/web-api/rate-limits/ ; `chat.update` Tier 3. https://docs.slack.dev/reference/methods/chat.update/ ; 2025-05-29 `conversations.history` limits for non-Marketplace commercial apps (internal apps unaffected). https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps
- Streaming: `chat.startStream`/`appendStream`/`stopStream` (2025-10-07). https://docs.slack.dev/changelog/2025/10/7/chat-streaming ; `chat.startStream` Tier 2, ≤ 10 min, ≤ 12,000 chars, `recipient_user_id`/`recipient_team_id` required in channels. https://docs.slack.dev/reference/methods/chat.startStream/ ; append/stop tiers [UNVERIFIED].
- `assistant.threads.setStatus` (600/min/app/team; clears on reply or after 2 min). https://docs.slack.dev/reference/methods/assistant.threads.setStatus/ ; replaced by `agents.sessions.setStatus` with a compatibility bridge (2026-08-20). https://docs.slack.dev/changelog/2026/08/20/agent-updates
- Interactivity: 3 s ack; `response_url` up to 5 times in 30 min; `trigger_id` 3 s, single use. https://docs.slack.dev/interactivity/handling-user-interaction/
- Agentforce in Slack: https://slack.com/blog/news/ai-for-employees-agentforce-slack

### A.2 Microsoft Teams
- Bot Connector → bot JWT validation (OpenID metadata `https://login.botframework.com/v1/.well-known/openidconfiguration`; `iss https://api.botframework.com`; `aud` = app id; 5 min skew; `serviceUrl` claim match; key cache refresh ≤ 24 h) and bot → Connector client-credentials tokens (`https://api.botframework.com/.default`, 3600 s). https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-authentication
- Multi-tenant Azure Bot creation deprecated after 2025-07-31; single-tenant or user-assigned managed identity. https://learn.microsoft.com/azure/bot-service/abs-quickstart
- Teams-managed (Developer Portal) bots vs Azure Bot. https://microsoft.github.io/teams-sdk/cli/concepts/bot-locations ; outbound 401 reports [UNVERIFIED]. https://learn.microsoft.com/en-us/answers/questions/5650695/
- 15 s processing → Teams retries (duplicates). https://learn.microsoft.com/en-us/microsoftteams/platform/bots/bot-concepts
- Proactive messages: install scope, conversation reference (`tenantId`, `conversationId`, `serviceUrl`), `MessageWritesBlocked`, update via `PUT …/activities/{id}`. https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages
- Rate limits per bot per thread 7/1 s, 8/2 s, 60/30 s, 1,800/h; 50 RPS per app per tenant. https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/rate-limit
- Streaming (`streaminfo`, ≤ 1 req/s, ≤ 10 min, one stream per conversation, not proactive). https://learn.microsoft.com/en-us/microsoftteams/platform/bots/streaming-ux
- Bot Framework SDK archived; Microsoft 365 Agents SDK successor. https://learn.microsoft.com/en-us/azure/bot-service/bot-service-overview
- Graph subscriptions: chatMessage ≤ 4,320 min; Outlook message ≤ 10,080 min (1,440 with resource data); `lifecycleNotificationUrl` required for Teams > 1 h; `clientState` ≤ 128 chars. https://learn.microsoft.com/en-us/graph/api/resources/subscription ; validation (`validationToken`, 10 s, text/plain), ack 2xx ≤ 3 s, retries ≤ 4 h, slow/drop states. https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks ; lifecycle events. https://learn.microsoft.com/en-us/graph/change-notifications-lifecycle-events
- Teams protected-API metering change [UNVERIFIED wording]. https://learn.microsoft.com/id-id/graph/teams-licenses
- Outgoing webhooks: HMAC, @mention only, 5 s. https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-outgoing-webhook

### A.3 ServiceNow
- Outbound REST auth: basic, OAuth 2.0, mutual TLS (constraints with MID server). https://docs.servicenow.com/bundle/sandiego-api-reference/page/integrate/outbound-rest/concept/c_OutboundRESTAuth.html
- RESTMessageV2 async/MID example. https://docs.servicenow.com/bundle/washingtondc-api-reference/page/app-store/dev_portal/API_reference/RESTMessageV2/reference/r_RESTMessageV2MIDServerExample.html
- Use async business rules for outbound calls; `executeAsync` via `ecc_queue` [UNVERIFIED, community]. https://www.servicenow.com/community/developer-articles/outbound-integrations-using-soap-rest-performance-best-practices/ta-p/2301503
- Table API paging (`sysparm_limit`, `sysparm_offset`, Link header), keyset on `sys_updated_on` [UNVERIFIED as an official recipe]. https://www.servicenow.com/community/developer-forum/what-is-the-limit-for-results-on-rest-table-api/m-p/1587156
- Inbound REST rate limiting. https://docs.servicenow.com/bundle/tokyo-api-reference/page/integrate/inbound-rest/concept/inbound-REST-API-rate-limiting.html
- AI Agent triggers (table + condition, run-as user, per-run record cap). https://www.servicenow.com/docs/r/intelligent-experiences/add-trigger-aia-new.html ; kill switch (5 fires per record per 24 h, 25 records) via search snippet. https://www.servicenow.com/docs/r/zurich/intelligent-experiences/aia-kill-switch.html ; loop advice. https://www.servicenow.com/community/now-assist-articles/limit-assist-consumption-by-designing-ai-agents-which-avoid/ta-p/3450013

### A.4 Salesforce
- Pub/Sub API recommended for Platform Events and CDC. https://developer.salesforce.com/docs/platform/api-streaming/guide/intro-stream.html
- Flow control: `num_requested` ≤ 100; new FetchRequest within 60 s when nothing pending (snippet). https://developer.salesforce.com/docs/platform/pub-sub-api/guide/flow-control.html
- 72 h retention for platform events and CDC. https://developer.salesforce.com/docs/platform/pub-sub-api/guide/event-message-durability.html
- ManagedSubscribe (beta). https://developer.salesforce.com/docs/platform/pub-sub-api/references/methods/managedsubscribe-rpc.html
- Streaming API (CometD) PushTopic/generic events legacy. https://developer.salesforce.com/docs/atlas.en-us.api_streaming.meta/api_streaming/event_comparison.htm
- Event allocations (snippet). https://developer.salesforce.com/docs/atlas.en-us.platform_events.meta/platform_events/platform_events_limits_hourly.htm ; https://developer.salesforce.com/docs/platform/pub-sub-api/guide/allocations.html
- Outbound Messages: queued up to 24 h; may duplicate and arrive out of order. https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_om_outboundmessaging_notifications.htm ; `<Ack>true</Ack>` [UNVERIFIED, third party].
- Client credentials with a run-as integration user. https://developer.salesforce.com/blogs/2024/02/invoke-rest-apis-with-the-salesforce-integration-user-and-oauth-client-credentials ; JWT bearer [UNVERIFIED].
- Agentforce from flows/Apex and platform events. https://developer.salesforce.com/blogs/2025/04/invoke-agentforce-agents-with-apex-and-flow ; https://developer.salesforce.com/blogs/2025/07/integrate-agents-with-platform-events

### A.5 GitHub
- 2xx within 10 s; queue and process asynchronously; `X-GitHub-Delivery` unique, kept on redelivery. https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks
- No automatic redelivery. https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries ; manual/API redelivery for 3 days. https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks
- `X-Hub-Signature-256`, constant-time compare. https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
- Bot sender filtering example (`sender.type == "Bot"`). https://www.mintlify.com/github/safe-settings/operations/webhooks

### A.6 Gmail and Outlook
- Gmail push: `watch` at least every 7 days (daily recommended); ≤ 1 notification/s per user; publisher `gmail-api-push@system.gserviceaccount.com`; `historyId` in the message. https://developers.google.com/workspace/gmail/api/guides/push ; `history.list`, 404 → full sync. https://developers.google.com/workspace/gmail/api/guides/sync
- Pub/Sub push OIDC token verification (`aud`, `email`, `email_verified`, `iss`). https://docs.cloud.google.com/pubsub/docs/authenticate-push-subscriptions
- Graph message delta. https://learn.microsoft.com/en-us/graph/api/message-delta ; subscription lifetimes and lifecycle: A.2.

### A.7 Generic signed webhooks
- Standard Webhooks: `webhook-id`, `webhook-timestamp`, `webhook-signature` (`v1,<base64>`; `v1a` ed25519), signed `id.timestamp.payload`, `whsec_` secrets, multiple signatures for rotation. https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md
- CloudEvents 1.0 required attributes and `source`+`id` uniqueness. https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md ; HTTP webhook abuse protection (`OPTIONS`, `WebHook-Request-Origin`). https://github.com/cloudevents/spec/blob/main/cloudevents/http-webhook.md

### A.8 How agent products wire events to agents
- **Copilot Studio**: event triggers are Power Automate connector triggers; payload processed like a user message and billed; triggers run on the **maker's** credentials. https://learn.microsoft.com/en-us/microsoft-copilot-studio/authoring-triggers-about
- **Agentforce**: agents invoked from record-triggered / platform-event-triggered flows and Apex; agents publish platform events back. (A.4 links.)
- **ServiceNow AI Agents**: table + condition triggers, run-as identity, per-run record cap, kill switch. (A.3 links.)
- **Zapier**: polling every 1-15 min by plan, dedupe by `id`/primary keys; REST hooks with polling fallback. https://docs.zapier.com/platform/build/deduplication ; https://docs.zapier.com/platform/build/trigger ; https://platform.zapier.com/publish/integration-checks-reference
- **n8n**: webhook vs polling triggers; respond immediately vs after the workflow. https://docs.n8n.io/nodes/n8n-nodes-base.webhook

**Patterns BOSS adopts:** verify on raw bytes before parsing; ack within the shortest deadline (Slack/Graph 3 s) after a durable insert only; dedupe on the source's own id (`event_id`, `X-GitHub-Delivery`, `webhook-id`, CloudEvents `source+id`, `replay_id`); assume at-least-once and out-of-order, order per entity by version; renew subscriptions early and resync from a cursor after any gap; map identities explicitly and never trust a free-text email; drop own-bot events and make our writes non-triggering; cap fires per record (ServiceNow's 5/record/24 h), per route and per spend. BOSS improves on Copilot Studio's maker-credential model with per-firing grant checks and person-scoped chat runs.

**Normalised `type` names (CloudEvents `type`):** `com.slack.message.app_mention`, `com.slack.message.im`, `com.slack.message.channel`, `com.slack.message.thread_reply`, `com.microsoft.teams.message`, `com.microsoft.teams.conversation_update`, `com.servicenow.record.{created|updated|deleted}`, `com.salesforce.cdc.{create|update|delete|undelete}`, `com.salesforce.platform_event`, `com.github.{issues|issue_comment|pull_request|…}.{action}`, `com.atlassian.jira.issue.{created|updated|commented}`, `so.plane.issue.{…}`, `com.google.gmail.message.received`, `com.microsoft.outlook.message.{created|updated}`, `boss.webhook.{route-defined}`.

---

## Appendix B. Inventory: reused, extended, new

| Item | `file:line` | P9 |
|---|---|---|
| Trigger kinds, compile to admit, OBO link, per-firing authority, fires dedupe | `services/agent_triggers.py:1-24, 40, 298-395, 507-620` | **Extended**: kind `intake`, thread key, acting from identity |
| Bot clock tick (cron/monitor) | `agent_triggers.py:628-687`; `services/bots.py:605` | Reused as is |
| Event sink + signed inbound webhook (BOSS v1 HMAC) | `agent_triggers.py:710-784`; `routers/agent_platform.py:348-368` | **Reused** as the `webhook` adapter's legacy scheme; route moved to the threadpool |
| Internal event dispatch, subscriptions | `services/event_dispatch.py:23-44, 279-463`; `services/core.py:63, 278-310`; `db/schema.sql:2726-2749` | Reused unchanged for internal events (fails open; not used for external events) |
| `boss_events` NOTIFY + PgListener + Hub | `db/schema.sql:12979-12998`; `services/event_stream.py:268-330` | **Reused** as the claim wake; payload gains `target` |
| Thread admission, double-text, active run | `services/thread_runs.py:15-25, 43, 59-87, 110-176` | Reused; `_start` passes `target` |
| `admit_run` | `services/agent_deployments.py:268-291` | **Extended** (`target`) |
| Job claim / lease / fence | `services/jobs.py:595-650, 1873-1885`; `/v1/worker/poll` `apps/api/app/main.py:24209-24222` | **Extended**: `target` filter, long-poll |
| Worker loop and idle poll | `apps/worker/boss_worker/config.py:23,27`; `apps/worker/boss_worker/main.py:784-799` | **Extended**: `wait_ms`, 15 s only as fallback |
| Agent run execution, lease fence | `services/agent_run_worker.py:46-90` | Reused |
| Slack verify/parse/render/send | `services/chat_adapters.py:37-46, 109-274` | **Extended**: conversation events, retries, streaming/status ops |
| Chat endpoints, actions, identity links, threads, inbound handling | `services/chat_channels.py:56-65, 297-421, 669-716`; `db/schema.sql:12887-12959` | **Extended**: Teams kind, conversation map, link methods |
| Chat public route, bounded read, immediate send | `apps/api/app/routers/chat_channels.py:196-288` | Reused pattern |
| Chat outbox | `services/notify.py:809-883`; `db/schema.sql:12961-12977` | **Extended**: NOTIFY kick, rate-limit aware, fenced agent replies, new ops |
| Quiet hours | `services/notify.py:311-329`; `services/comms.py:1287-1360` | Reused |
| Comms inbox/outbox (email/slack commands) | `services/comms.py:163-454, 855-944`; `db/schema.sql:2613` | Not reused for agent events (different purpose); name clash avoided |
| Personal attention inbox | `services/inbox.py:1-30` | Name clash avoided ("intake") |
| Ticket connector contract, registry, webhook schemes, screening | `connectors/tickets/contract.py:37-46, 54-107, 199-208, 380-422`; `registry.py:1-40`; `base.py:66-83` | **Pattern reused** for `connectors/intake/`; Jira/Plane adapters delegate |
| Ticket inbound webhook + poller on DB leases, provider identities | `services/ticket_sync.py:36-42, 1085-1318, 1418-1510, 1534-1598`; `routers/ticket_sync.py:175-196` | **Extended**: provider fetch moves to the processor (F9) |
| GitHub/Bitbucket inbound store + own-sender loop check | `db/schema.sql:2788-2803`; `services/webhooks.py:806-1060` (own sender `:1027-1039`); `apps/api/app/main.py:6911-7004` | Pattern generalised; migrated in P9b |
| Public route declarations | `identity/declarations.py:43-68` | **Extended**: `SIGNED_JWT_WEBHOOK` |
| Prompt-injection screen | `services/tool_result_screen.py:1-20, 281-288, 365, 460` | Reused |
| Budget, spend limits, scoped budgets | `services/budget.py:1307`; `services/spend_limits.py:1-20`; `services/scoped_budgets.py:45` | **Extended**: scopes `trigger`, `deployment` |
| Kill switch | `services/ambient/policy.py:327` | Reused |
| Users by email | `identity/users.py:56-59` | Reused for SSO-attested linking |
| Consent / schedule acting | `identity/consent.py:959` | Reused |
| LISTEN pattern | `identity/change_feed.py:103-150` | Reused for long-poll waits |
| System services registry | `db/schema.sql:14494` | Reused for processor/poller health |
| MCP gateway | `services/mcp_gateway.py:1-25` | Reused for agents' Slack/Teams/SaaS tool use (P8 §3.10) |
| **New** | — | `connectors/intake/*`, `services/intake.py`, `services/intake_router.py`, `services/intake_pollers.py`, `routers/intake.py`, tables `intake_connections`, `intake_events`, `intake_cursors`, `intake_subscriptions`, Teams adapter, reply tools, run-plane `next` deployment filter |
