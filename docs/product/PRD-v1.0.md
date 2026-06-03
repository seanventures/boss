# BOSS PRD v1.0

This normalized Markdown file is the governing product-intent reference for implementation. It is derived from the June 1, 2026 PRD artifacts in the repository root and the pasted PRD transcript supplied during bootstrap.

## Product thesis

BOSS is a financially controlled autonomous company operating system that lets a CEO issue outcomes, then coordinates role-scoped AI workers, tools, approvals, budgets, verification, receipts, and continuous improvement to produce real work safely.

## Product promise

- CEOs type outcomes and receive plans, options, risks, costs, decisions, and verified work products.
- Builders turn fuzzy direction into scoped implementation workflows with strict quality gates.
- Operators see what changed, why, what it cost, what was approved, and what happens next.
- Finance retains budget control through reservations, hard caps, and cost receipts.

## North star

A CEO can say, "Build the Decision Inbox default-action system and ship it to staging," and BOSS can convert that outcome into requirements, technical design, chunked implementation tasks, PRs, tests, review packets, decision cards, receipts, cost reports, and a staged rollout without exceeding approved budgets or bypassing approval gates.

## Core loop

`CEO command -> intake and clarify -> plan and budget quote -> task graph and role staffing -> execution by bounded workers -> artifacts, tests, and review packets -> decision cards and approvals -> receipts and audit trail -> learn from results -> propose improvements`

## Product principles

- Accountability before autonomy
- Artifacts over memory
- Bounded loops, not infinite agents
- Human company analogs with machine-grade controls
- Financial control shapes routing and execution
- Self-improvement requires change management
- Optimize across limits, never by bypassing them

## Primary persona

The first user is a founder/CEO/product builder using AI to create software and operate a company with limited engineering bandwidth. BOSS is founder-first and dogfoods on itself through the `boss-dogfood` workspace.

## Surface areas

- Executive Console and Metatron
- Decision Inbox
- Canonical work system
- Budget Control Center
- Durable execution runtime
- Role system and company packs
- Software Factory
- Skills Registry and model routing
- Model Entitlement Broker
- Communications and presence
- Audit, replay, and observability
- Self-improvement and Agent Factory

## Core P0 requirements

### Executive Console

- Accept natural-language CEO outcomes plus constraints and approval mode.
- Show what changed, top risks, blockers, costs, and next actions.
- Back every status response with fresh references to tasks, decisions, receipts, events, and artifacts.

### Decision Inbox

- Decision cards must show recommendation, options, risks, cost impact, deadline, default action, and evidence links.
- Supported flows include approve, reject, revise, delegate, expire, and execute.
- Tier-2+ actions require explicit approvals.
- High-risk defaults must be safe by default, typically `HOLD`.

### Canonical work system

- Stable IDs and lifecycles for tasks, plans, decisions, policies, roles, meetings, receipts, artifacts, events, and health snapshots.
- Every generated artifact must be versioned and linked.
- Every material mutation must emit an event.

### Budget governor

- Every spendy action follows quote, reserve, execute, consume/release, cost receipt.
- Budget states are `green`, `yellow`, `orange`, and `red`.
- Hard caps must apply at job leasing, model/tool invocation, and infra provisioning.

### Durable runtime

- Jobs move through `queued`, `leased`, `running`, `succeeded`, `failed_retryable`, `failed_final`, `deadlettered`, and `cancelled`.
- Workers are stateless and pull jobs through leases.
- Side effects are idempotent through receipts and dedupe keys.

### Roles and packs

- Every installed role has a valid `RoleSpec`.
- Core Pack v1 includes Metatron, COO, CAO, CTO, PM, Designer, QA, Release, Program Manager, and VP Agents.
- Roles cannot operate outside scope without policy escalation.

### Software Factory

- The dogfood path is spec -> plan -> chunked implementation -> tests -> review packet -> merge decision.
- Real software changes require tests or an explicit test-gap note.
- Merge and deploy remain approval gated.

### Model Entitlement Broker

- BOSS must treat OpenAI, Claude, Gemini, Cursor, Grok, and approved local models as entitlement pools rather than "free unlimited compute".
- BOSS must support four source kinds:
  - official CLI
  - official coding agent
  - official API
  - approved local model
- BOSS must route tasks across these sources based on task fit, observed capacity, provider rules, cost, quality, and risk.
- BOSS must maintain explicit source records that include:
  - provider
  - access method
  - entitlement kind
  - owner
  - tenant suitability
  - approved workload classes
  - quota or rate-limit shape
  - cost mode
  - risk ceiling
  - policy notes
- Subscription-backed tools authenticated by a human operator are allowed only for owner-run or explicitly approved single-tenant sandbox workflows unless provider terms clearly permit broader backend use.
- API-backed sources are the default route for production, customer-facing, multi-tenant, or higher-reliability workloads.
- Local model sources are allowed only when they are explicitly approved, policy-scoped, and observable through the same receipt, reservation, and audit system.
- The broker must fail closed when a source is out of capacity, disallowed for the workload class, missing credentials, or blocked by provider rules.

## Operating invariants

- No external effect without a `Receipt`.
- No spendy action without a `BudgetReservation`.
- No worker execution without a lease.
- No silent drops.
- No self-modification without PR/config diff, tests, approval, canary, and rollback.
- No high-risk default action unless policy explicitly allows it.
- No important work exists only in chat.
- No consumer-surface scraping, private-endpoint use, credential sharing, or rate-limit evasion.
- No personal subscription source may be used for multi-tenant customer workloads unless the provider explicitly permits that usage mode.

## Release framing

### 1.0 Core

- Accountable command loop
- Durable runtime
- Budget governor
- Decision Inbox
- Real GitHub and OpenAI/Codex integrations against safe sandboxes
- Dogfood Software Factory with PR proposals, tests, review packets, and merge decisions

### 1.0 Full

- Role Pack Manager
- Skills Registry
- Audit and Replay Center
- Self-improvement workflow
- Multi-user approvals and enterprise hardening
- Advanced FinOps and pack expansion

## Acceptance catalog anchors

- Command creates task, job, artifact, and visible console update.
- Budget denial prevents execution and creates a decision card.
- Reservation lifecycle updates reserved and actual spend correctly.
- Decision approval updates state and emits events.
- Receipt dedupe prevents duplicate effects.
- Lease recovery reclaims expired work.
- Deadletters are visible through UI and API.
- Self-improvement cannot bypass tests, approval, canary, or rollback.

## Current bootstrap interpretation

This repository starts with real-only sandbox integrations:

- Real GitHub operations only against `seanheiney/boss-sandbox`
- Real OpenAI model calls only when an active reservation exists
- No fake adapters, no synthetic PR receipts, and no automatic merge or deploy in the initial wave

## Model Entitlement Broker notes verified on June 1, 2026

- OpenAI Codex usage depends on the ChatGPT plan, counts toward agentic usage limits, and OpenAI states that API billing is managed separately from ChatGPT.
- Anthropic states that Claude Pro and Max usage limits are shared across Claude and Claude Code.
- Google states that Gemini Code Assist agent-mode and Gemini CLI request quotas are combined and vary by edition or license.
- Cursor states that its CLI is intended for scripts and automation workflows and can run multiple agents in parallel as part of a Cursor subscription.
- xAI documents per-model RPM and TPM limits for its API, and its consumer terms state that rate limitations may be imposed at xAI's discretion.

## Reference links

- [OpenAI Codex plan usage](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan)
- [OpenAI API billing separate from ChatGPT](https://help.openai.com/en/articles/8156019)
- [OpenAI Terms of Use](https://openai.com/policies/terms-of-use/)
- [Anthropic Claude Code with Pro or Max](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan)
- [Google Gemini Code Assist quotas](https://developers.google.com/gemini-code-assist/resources/quotas)
- [Cursor Agent CLI blog](https://cursor.com/blog/cli)
- [Cursor CLI product page](https://cursor.com/en-US/cli)
- [xAI API rate limits](https://docs.x.ai/developers/rate-limits)
- [xAI consumer terms](https://x.ai/legal/terms-of-service)
