# What BOSS does, in detail

Capability-by-capability detail on the governed runtime. This is the long form; the
[documentation home](../index.md) has the summary and
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) has what is shipped versus planned.

## Budget-governor behavior

- Spendy work should follow `quote -> reserve -> execute -> consume/release`.
- Reused or blocked spendy effects release their reservation instead of leaving budget locked.
- Spend-gated jobs without an active reservation are not leased; they create a visible decision and are cancelled explicitly rather than running silently.
- Budget allocations support parent-child envelopes for project, department, initiative, or other scoped spend. Child allocations and allocation-backed reservations are denied when they exceed the available parent allocation.
- Lease-time budget states now affect execution behavior too: `orange` defers non-critical work, and `red` halts non-`p0` jobs while freezing infrastructure provisioning plus higher-risk merge, deploy, and outbound communication effects behind visible decisions.
- Real sandbox PR proposals can now be queued as leased jobs so GitHub/OpenAI execution runs through `reservation -> job -> lease -> receipt -> artifact -> decision`.
- Real sandbox droplet provisioning now follows `reservation -> decision -> job -> lease -> receipt -> artifact -> infra_resource`, with TTL janitor queueing for cleanup.

## Audit and replay

- `GET /v1/receipts` lists receipt-backed effects as first-class API objects for a project.
- `GET /v1/audit/export` reconstructs the current command/task/job/decision/artifact/receipt/event chain, with optional task/job/decision scoping.
- `POST /v1/audit/replay-bundles` persists that export as a durable `replay_bundle` artifact so the console and external clients can capture a point-in-time run packet.
- `POST /v1/compliance/evidence-bundles` persists a durable `compliance_evidence_bundle` artifact covering project members, roles, policy records, break-glass grants, decisions, receipts, budget reservations, cost receipts, deadletters, and compliance-relevant events.
- Tasks, jobs, artifacts, and receipts now carry a `data_boundary` classification (`public`, `internal`, `confidential`, `restricted`, or `local-only`) that is visible through object APIs and audit export. Outbox and inbox messages carry the same classification through comms APIs. Job and artifact creation inherit the task boundary unless an explicit boundary is supplied.
- GitHub PR, merge, and deployment effects are blocked before receipt creation when the task boundary is `restricted` or `local-only`, producing a decision and `policy.denied` event instead of sending data to an unapproved external provider.
- Slack and email delivery effects are blocked before receipt creation when the message boundary is `restricted` or `local-only`, producing a decision and `policy.denied` event instead of sending data to an unapproved external provider.
- DigitalOcean droplet provisioning is blocked before receipt creation when the task boundary is `restricted` or `local-only`, producing a decision and `policy.denied` event instead of sending data to an unapproved external provider.
- Queued OpenAI, PR proposal, and merge work now stamps both artifacts and receipts with `created_by_job_id`, and PR-proposal reservations are attached to their worker jobs for tighter lineage.
- Approving a plan-review decision now auto-reserves the configured sandbox proposal budget and queues the next `software_factory.propose_pr` job instead of stopping at decision state only.
- Sandbox PR proposals now wait for real GitHub check-run evidence and persist that evidence into the `test_report` and `review_packet` artifacts before the PR review decision is considered mergeable.
- When local sandbox validation is enabled, sandbox PR proposals also fetch/checkout the proposal branch in an approved local clone, run allowlisted real commands, capture output into the test report, and block merge approval if any command fails or times out.
- Approving the final sandbox PR review decision creates a receipt-linked merge gate by default, and can queue a real `github.pull_request.merge` job only when `BOSS_ENABLE_SANDBOX_MERGE=true`.
- Even when merge policy is enabled, BOSS now blocks merge execution unless the linked sandbox validation report is in a passed state.
- When sandbox deploy policy is enabled, a successful merge creates a deployment review decision instead of silently treating merge as the terminal step. Approving that decision queues a real `github.deployment.create` job against the sandbox repository, backed by a receipt and a durable `deployment_result` artifact.
- If `BOSS_SANDBOX_DEPLOY_HEALTHCHECK_URL` is configured, BOSS now runs a real post-deploy HTTP health check and only treats the deploy as complete when that check passes. Failed verification creates a `deployment_health_check` artifact and a blocking follow-on decision instead of silently completing the rollout.
- Every sandbox deployment now creates a durable `deployment_rollout` artifact and queryable rollout record with staged evidence for GitHub deployment, post-deploy health, release readiness, and rollback/health-decision linkage when blocked.

## Model entitlement broker

- BOSS treats AI capacity as governed entitlement sources, not generic free compute.
- The runtime inventory now distinguishes `official_cli`, `official_coding_agent`, `official_api`, and `approved_local_model` sources.
- Public subscription sources for Codex, Claude Code, and Grok Build are modeled as official OAuth/CLI session entitlements with `auth_method`, `connection_status`, `connection_ref`, OAuth scopes, and provider-rule notes. They can be used only for owner or single-tenant work when an official session is connected; API-key sources remain the production and multi-tenant path.
- Router previews and brokered model calls write durable routing logs with selected source, selected model, denial reasons, and rejected alternatives.
- Routing decisions now include `data_boundary`; restricted and local-only work is denied for unapproved external sources, while approved local model capacity can handle local-only work when configured.
- OpenAI planning and review-packet generation now pass through the broker before execution.
- The broker fails closed when no eligible runtime-backed source is available or when provider-policy constraints disallow the workload.
- The executive console no longer labels seeded provider inventory as generically “runtime ready.” It distinguishes active capacity, credential-gated capacity, adapter-ready capacity, and registry-only inventory so the visible surface matches real backend readiness.

## Communications plane

- The comms slice now uses the canonical `outbox_messages` and `inbox_messages` tables instead of burying communication intent in chat.
- Outbound communication follows `draft -> queued -> sending -> acked` when a real provider is enabled, or transitions to `blocked`, `retrying`, or `deadlettered` with visible events when it is not.
- `comms.outbox.deliver` is a leased worker job kind, so real sends still require a job lease, a reservation, and a receipt.
- Slack is the first provider surface in this slice, but it is fail-closed by default behind `BOSS_SLACK_REAL_SEND_ALLOWED=false`. Real sends require both that flag and `BOSS_SLACK_WEBHOOK_URL`.
- Email now shares the same outbox path through a real SMTP adapter, fail-closed by default behind `BOSS_EMAIL_REAL_SEND_ALLOWED=false`. Real sends require SMTP host/from-address config and optional auth credentials.
- Decision digests are API-created `decision_digest` artifacts that group urgent decisions separately from lower-risk batched decisions. An optional channel creates an outbox draft only; sending still requires explicit queueing, reservation, lease, and receipt.
- Inbox ingestion now deterministically parses decision replies. Valid replies transition the referenced decision through the same permissioned decision service as the UI; malformed or unresolvable replies become visible `inbox_message` deadletters.
- Outbox queueing honors quiet-hours and per-channel throttling for non-urgent messages through delayed job `run_after` values. `urgent` queue requests bypass those scheduling deferrals but still require reservation, lease, and receipt before any external send.
- Outbox job failures propagate into message status and deadletters so the comms plane honors the `no silent drops` invariant.

## Self-improvement lane

- BOSS now tracks self-improvement through a durable `self_improvement_proposals` workflow instead of burying product changes in ad hoc notes or chat.
- Creating a proposal requires a change reference, diff summary, test plan, canary scope, rollback plan, and success metrics. The control plane turns that into a task, proposal artifact, and canary approval decision.
- The self-improvement API now fails closed if proposal, canary, rollout, or rollback requests omit required change-control evidence such as non-empty test plans, canary scope, rollback steps, metrics, or evidence references.
- Decision outcomes are now connected to proposal state. Approval advances the proposal, revision requests reopen it explicitly, and rejection leaves a visible rejected state instead of silent drift.
- Successful canaries automatically create the next rollout approval decision. Rollout and rollback are both recorded as first-class artifacts so BOSS can prove how it changed itself.
- The executive console now submits canary, rollout, and rollback updates with explicit metrics and evidence references instead of rendering dead-end buttons that fail validation at the API boundary.

## Health snapshots

- BOSS now has a canonical `health_snapshots` object that captures queue state, workload state, incident state, budget state, and derived top risks for a project.
- `GET /v1/status` now returns live top risks plus latest snapshot metadata, while `POST /v1/health-snapshots/refresh` persists a durable snapshot for audit and operator review.
- Snapshot risk derivation currently covers deadletters, blocked or deadlettered outbox messages, budget pressure, high-risk pending decisions, retryable jobs, and missing provider credentials.

## Role governance

- Installed roles now carry runtime governance state instead of only static pack metadata: trust score, successful-run count, violation counters, and last review or violation timestamps.
- `POST /v1/roles/{role_key}/reviews` records a durable governance review with evidence refs and metrics, updates the role trust score, and emits audit events for trust changes and detected drift.
- Review metrics can now trigger automatic autonomy downgrades for violation conditions, or create explicit autonomy-upgrade decisions when a role meets the autonomy ladder policy.
- Approved autonomy decisions now update the live role record through the control plane instead of leaving autonomy changes as spec-only intent.

## Agent modules and marketplace

- Agent logic is now a contained product surface, separate from company packs. A packaged agent module can own a role charter, prompt bundle, runtime job kinds, skill defaults, and install-time configuration.
- `GET /v1/agent-modules` exposes the marketplace catalog for a project, `POST /v1/agent-modules/install` installs a module into a project, and installed roles retain `source_module_id` and `source_module_version` lineage.
- Approved plan follow-on jobs now carry `agent_module_id`, and installed module configuration can influence runtime behavior such as proposal branch prefixes.
- OpenAI plan generation and software-factory PR proposals now resolve through installed agent modules so prompt/system behavior and capability hints are driven by the packaged module, not only hardcoded service logic.
