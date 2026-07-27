# Work Orchestration and Delivery Presence

Status: Implemented vertical slice
Requirement family: BOSS-270 / WOS-01 through WOS-08
Governing specs: `PRD-v2.0.md`, `TECHNICAL_SPEC-v2.0.md`

## Customer problem

Teams using autonomous agents routinely hit the same operational failures:

- everything appears urgent, so capacity follows whichever request is newest or loudest;
- project, team, and global resource limits are configured in separate places and cannot be rebalanced coherently;
- agents start from chat or ad hoc queues, leaving no reliable owner, ticket, retry state, or delivery trail;
- ticketing, execution, pull requests, deployment status, and team chat show different slices of reality;
- Product Management work becomes stale because PRDs, acceptance criteria, priorities, and delivery evidence have no accountable owner;
- operators must keep Plane, GitHub, deployment tools, and Mattermost open just to answer “what is happening?”

BOSS solves these as one governed system: externalize work in a tracker, rank it with inspectable inputs, allocate bounded capacity, execute it through leased jobs, and project delivery status back into one operator view.

## Product outcomes

1. An operator can explain why one item runs before another.
2. Capacity can be reserved and capped across global, org, workspace, project, agent, and workload scopes.
3. All Agent Studio work has a canonical work item, external tracker binding, allocation, and durable job.
4. Plane is useful out of the box for open-source installations; alternate trackers use the same plugin contract.
5. The Product Manager agent keeps PRDs and tickets current and cannot close work without evidence.
6. Routine ticket, queue, PR, deployment, and channel monitoring happens inside BOSS.
7. GitHub and Mattermost activity is attributable to governed bot identities and visible in an audit feed.

## Functional requirements

### WOS-01 — Explainable portfolio ranking

Every work item records strategic value, customer impact, urgency, cost of delay, risk reduction, dependency pressure, confidence, optional manual adjustment, and rationale. BOSS computes and stores the score and `P0..P4` band. Manual intervention changes the score but never erases the inputs that produced it.

### WOS-02 — Hierarchical quotas and dynamic allocation

Quota envelopes support `global`, `org`, `workspace`, `project`, `agent`, and `workload` scopes and declare reserved minimum, soft limit, hard limit, optional burst, allocation weight, and explicit borrowing policy. A claim locks and evaluates every applicable envelope from global through agent/workload scope before it consumes the work item's allocation. Rebalancing retires unused allocations and grants ranked capacity in score order; quota edits trigger a fresh plan.

Budget and policy are higher authority than allocation. A P0 item can move to the front of the eligible queue; it cannot spend money, exceed a hard limit, cross a data boundary, or perform an unapproved effect.

### WOS-03 — Durable job intake

Plane is not used as a distributed worker queue. The tracker owns intake, discussion, ownership, and human-visible state. BOSS Postgres jobs own priority ordering, leases, TTL, heartbeat, retry, deadletter, dedupe, and receipts.

Tracked jobs carry:

- `work_item_id`
- `tracker_binding_id`
- `allocation_id`
- `priority_score`
- `workload_class`
- `resource_units`

A worker claims the highest-scoring eligible job and atomically consumes its allocation. Every terminal path releases capacity. Denied claims remain queued with an operator-visible reason.

### WOS-04 — WorkTracker plugin contract

Plane is seeded as the default provider with issue create/read/update/comment/link and webhook capabilities. GitHub Issues, Linear, and Jira are seeded as alternate plugin descriptors. A provider registration stores only non-secret configuration and credential references.

Creating BOSS work produces a pending binding and a `work_tracker.issue.sync` job. The sync job:

1. acquires a normal fenced lease;
2. creates or reuses a deduplicated external-effect receipt;
3. invokes the provider adapter;
4. persists the external issue ID, key, URL, and state;
5. retries or deadletters visibly on failure.

When Plane credentials are configured, its provider is `required` and ordinary agent jobs wait for the binding to synchronize. An unconfigured Community installation runs in advisory migration mode while keeping the missing sync visible.

### WOS-05 — Embedded operator surface

Mission Control includes a Work Allocation area with:

- ranked portfolio and score bands;
- quota envelope and utilization;
- durable job queue and lease state;
- tracker providers and sync status;
- bot identities and status channels;
- a live ticket/PR/deploy/message rail;
- an inspector explaining score, allocation, tracker state, ownership, and evidence.

Operators can add or reprioritize work, change the project quota, rebalance capacity, link a ticket, and configure delivery bots/channels without leaving BOSS.

### WOS-06 — Product Manager agent

Agent Studio seeds a Product Manager persona that owns:

- PRD creation and currency;
- customer problem and outcome clarity;
- acceptance criteria;
- task decomposition and dependency hygiene;
- priority recommendations with evidence;
- ticket state and shipment-link hygiene.

It uses the canonical agent job system and the WorkTracker capabilities. Each
completed run persists the AgentVersion's declared `product_delivery_brief`
artifact contract, including PRD changes, ticket changes, acceptance criteria,
and evidence gaps. It may not mark work done solely from its own claim.

### WOS-07 — GitHub, Mattermost, and bot presence

GitHub PR, merge, and deployment job completions create delivery-presence events. Mattermost sends through the official v4 API using a bot token only when the real-send rollout flag is enabled. Bot records refer to SecretRefs; raw credentials are never returned to agents or UI.

Status channels declare purpose, provider channel identity, subscribed event kinds, allowed data boundaries, and bot identity. Presence writes are deduplicated and retained in Postgres for troubleshooting.
GitHub deployment events are projected automatically into every matching
active status channel and attributed to the channel's bot identity. Projection
updates BOSS immediately; an external Mattermost post remains a separate
leased, budgeted, policy-gated, Receipt-backed communications effect.

### WOS-08 — Evidence and end-to-end proof

The minimum proof covers:

- confidence-weighted score ordering;
- higher-ranked capacity grant and lower-ranked throttle;
- allocation consumption, denial, and release across job transitions;
- mandatory tracker binding and queued Plane synchronization;
- provider, bot, channel, and delivery-presence projection;
- Product Manager catalog and entitlement resolution;
- Mattermost fail-closed configuration and bot request shape;
- frontend type-check, interaction contract, and production build.

## State models

`WorkTrackerBinding`

`pending_create -> synced`
`pending_create|failed -> queued retry -> synced|deadlettered`

`ResourceAllocation`

`planned -> active|throttled -> released|expired`

`Job`

`queued -> leased -> running -> succeeded`
`queued|leased|running -> failed_retryable -> queued`
`queued|leased|running -> deadlettered|cancelled`

## Security and authority

- Tracker, GitHub, and Mattermost credentials are settings or SecretRefs, never model context.
- Tracker sync, messaging, pull requests, merges, and deploys cross adapter boundaries and produce receipts.
- Spendy work still requires a BudgetReservation.
- Provider infrastructure changes still require ChangeSets and ResourceReceipts.
- Delivery channel data-boundary policy is evaluated before publication.
- Required tracker synchronization gates agent leasing; ticket priority never grants additional authority.

## Rollout and compatibility

The canonical schema retains legacy unscoped jobs so existing installations can migrate without losing work. New Agent Studio runs use the tracked path. The overview reports tracked-job coverage and pending tracker sync so remaining policy debt is measurable. A later hardening release can reject new unscoped jobs once every producer has migrated.

GitHub branch automation and repository synchronization remain governed by the existing Software Factory/GitHub integration feature family. This feature consumes their PR, merge, and deployment evidence rather than duplicating branch-management authority.
