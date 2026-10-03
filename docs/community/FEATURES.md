# Features

Everything in BOSS Community as of `v1.0-rc4`, grouped by area. Cloud and Enterprise capabilities are marked so the open-core boundary is explicit; the full v1 scope is tracked in [V1_FEATURE_LOCK.md](https://github.com/seanventures/boss/blob/main/docs/product/V1_FEATURE_LOCK.md).

**Legend:** ✅ Stable · ◐ Preview: works today, still being hardened · ○ Planned · ✦ Cloud / Enterprise only

## Control plane

| Feature | Status |
|---|---|
| Durable tasks, plans, jobs (Postgres) | ✅ |
| Job leases, heartbeat, fencing | ✅ |
| Deadletters and remediations | ✅ First-class remediations (`retry` / `requeue` / `escalate` / `hold`) on `/v1/deadletters/*` and Mission Control. Retry never silently drops. |
| Event / audit feed | ✅ |
| Global activity log | ◐ Searchable, sortable Mission Control area (status, kind, actor, stuck banner). |
| Decision inbox (approve / reject / escalate) | ✅ Hold `reason` and `default_action` on the card; GET `/v1/decisions/{id}`. |
| Receipts with unique dedupe keys | ✅ |
| Budget quote → reserve → consume / release | ✅ |
| Artifacts and evidence packets | ✅ |
| Single org / workspace / project model | ✅ |
| Local auth / API keys | ✅ |

## Interfaces

| Feature | Status |
|---|---|
| HTTP control-plane API (OpenAPI) | ✅ |
| CLI | ✅ |
| MCP server for agent clients | ◐ All 983 mounted Community operations are mapped: 851 use API policy, 64 need separate exact-request human confirmation, none remain held, and 68 protocol/internal routes are excluded. See [MCP coverage](MCP.md). |
| Self-hosted Mission Control console | ✅ |
| API-first clients (`packages/api-client`) | ✅ |

Every interface goes through the same HTTP API, so server-side policy and audit checks apply to terminal requests exactly as they do to other API clients.

- [Terminal API guide](TERMINAL_API.md): Metatron commands, direct `/v1/` calls, and one-shot JSON for scripts.
- [Terminal capability matrix](TERMINAL_CAPABILITY_MATRIX.md) and [MCP operation matrix](mcp-api-capability-matrix.json): how each mounted operation maps to each surface.

## Models & skills

| Feature | Status |
|---|---|
| Bring-your-own API keys (SecretRefs) | ✅ |
| SecretRef consumption (factory / OpenAI / GitHub / Plane) | ◐ Fail-closed: a bound, usable ref wins; env is a last resort only when no ref is bound. Unresolvable bound refs never fall back. |
| Official QA /reports v1.1 | ◐ Honesty helper, inspect gate, and Mission Control scorecard island. Missing UI/UX stays `not_scored`; scores are never invented. Live official POSTs may still be in flight. |
| Local / OpenAI-compatible base URLs | ✅ |
| Local model entitlement broker | ✅ |
| Role packs and skill packs (specpack) | ✅ |
| Starter agent catalog | ◐ First-run seed installs 34 agents (including the mobile template), 3 panels, and specpack workflows. Templates default to side-effect-free briefs; mobile device execution requires a separate governed runner. A2A reports `not_configured` when Redis is unset rather than a false-red degraded state. |
| Pack YAML import / export in Mission Control | ✅ |
| Managed multi-tenant model gateway | ✦ Cloud |
| Hosted skill marketplace | ✦ Cloud |

### Plane integration (optional)

Community installs and runs without Plane, an ops token, or a private registry. If you connect a Plane instance you run, HTTPS certificate verification stays on by default.

- For a certificate authority you operate, set `PLANE_TLS_CA_FILE` to a PEM file readable by the API and worker.
- `PLANE_TLS_INSECURE_SKIP_VERIFY=true` is an explicit local-only fallback for when no CA you trust is available.

!!! warning
    Only use `PLANE_TLS_INSECURE_SKIP_VERIFY` against a host you operate.

## Software Factory

| Feature | Status |
|---|---|
| Outcome → governed code task | ✅ |
| Spec / architecture / implement / test phases | ✅ |
| Sandbox execution plane | ◐ Local subprocess is the Community coding path. Firecracker/microVM claims are **frozen** (`BOSS_MICROVM_CLAIMS_FROZEN=true`). The in-repo Go S1 sandbox is a fail-closed scaffold: not live, not GA. Retained sandbox inspection requires project access; hold and delete controls stay disabled until their budget, approval, rollback, and receipt paths are complete. |
| Evidence-backed completion | ✅ |
| Discovery web research | ◐ Runs as a governed worker job: each research stage is budget-reserved, leased and receipted, and a retry replays stages already paid for instead of calling them again. Uses the Claude Code subscription; a local model cannot do web research. A workflow's research step runs it when that subscription can be used, and otherwise is recorded as skipped with the reason and a `workflow.research_skipped` event, and the run goes on to the panel. |
| Live debugging investigations | ◐ Past results remain readable. New Claude CLI investigations and legacy queued execution are paused until leased, budgeted model calls and governed host/database effects are complete. |
| Factory quality pass after green verify | ◐ One improve pass when UI/UX evidence is thin. Frozen tests stay frozen, and a failed pass restores the last green implementation. Unknown JTBD / `a11y_violations` stay `not_scored`. Stays partial. |
| PR / review packets (with GitHub) | ✅ |
| Browser / automated test evidence | ✅ |
| Deployment target metadata and recorded history | ✅ |
| SSH workload planning and deployment | ◐ Paused in Community until its governed effect and recovery path is complete. SSH installation and console tunnels remain available. |
| Agent host shell and host-backed preview sandboxes | ◐ Paused in Community until host changes have approved, budgeted, receipted execution. Simulated preview records remain available. |
| Managed elastic worker fleets | ✦ Cloud (planned) |

## GitHub

| Feature | Status |
|---|---|
| Project connection + repo binding | ✅ |
| Easy connect patterns | ✅ |
| Branch policies and immutable snapshots | ✅ |
| Fenced branch leases | ✅ |
| Safe sync and conflict resolution | ✅ |
| Merge authorizations | ✅ |
| Durable reconciler | ✅ |

## ProviderOps (your infra)

| Feature | Status |
|---|---|
| Provider accounts (model + infra keys) | ✅ |
| Read-only inventory sync | ✅ |
| ChangeSets (plan / approve / apply) | ✅ |
| Resource receipts and cost events | ◐ Explicit empty / unknown / unavailable labels; exact dollar amounts need billing-readable keys. |
| Worker pools and scale proposals | ✅ |
| Hosted connector marketplace | ✦ Cloud |

## Workforce

| Feature | Status |
|---|---|
| Role-scoped agents (Metatron, COO, PM, …) | ✅ |
| Focused always-on agents | ◐ Builder and duty loop (`/v1/focused-agents`). The worker ticks due always-on and hybrid agents every 60s; **Wake now** runs on-demand agents. Same validate-then-apply gates as the captain. until proven in live operation. |
| Metatron captain (persistent session / goals / watch) | ◐ Worker overwatch follows observe → measure → validate → apply-safe → verify. Decisions and deadletters stay human. |
| AI SRE kernel (awesome-ai-sre map) | ◐ Capability ledger at `GET /v1/ai-sre/capabilities` plus a Mission Control island. MCP adds list / diagnose / capabilities / verify / page. Observe-only worker tick at `POST /v1/ai-sre/overwatch` (scan / diagnose / dry-run page). Runbooks at `/v1/ai-sre/runbooks` (validate + Decision hold; `page_oncall` is dry-run only). Noise view at `GET /v1/ai-sre/noise` (clustered vs. raw, no invented percentages). `POST /v1/ai-sre/ask` returns a query plan only. Evidence-only incident postmortem at `POST/GET /v1/reliability/incidents/{id}/postmortem` (uses existing evidence only, never invents an RCA, never auto-resolves). Deterministic `GET /v1/ai-sre/bench` (InvestigationEngine fixtures). Never remediates, auto-applies Decisions, or merges PRs. **Preview.** |
| Work orchestration / portfolio | ✅ |
| Meetings / multi-agent panels | ✅ |
| Agent process handoffs | ◐ Authorable role sequences (natural-language compile, manual builder, Metatron/chat). Default sequence: PRD → architect/CTO/VP-PM review → improve → captain build → QA → UI/UX → captain verify → panel certify → captain accept. Simulated runs complete without spend. |
| Project Manager steward | ◐ Operates, tests, validates, and proposes changes over process runs (`/v1/process-manager`, worker every 60s). Metrics are evidence-only; templates are never silently rewritten. |
| Assurance / ground-truth runtime | ✅ |
| Self-improvement automation routes | ✦ Enterprise mount |

### Metatron captain

Endpoints: one-shot `POST /v1/metatron/query`, plus `/v1/metatron/session|goals|watch|drive|overwatch`.

| Aspect | Behavior |
|---|---|
| Overwatch | Always on in the worker |
| Validation | Every action (chat, goal, drive, apply) runs `validate_action` / `validate_move` first |
| Human gates | Decisions and deadletters never auto-apply |
| Changes to BOSS itself | Opened as a PR, never written silently |

!!! note
    The captain stays in preview until a validated apply has been proven in live operation on a release tag.

## Open-core boundary

| Feature | Status |
|---|---|
| Full Community product without Cloud | ✅ |
| Optional Easy Connect to BOSS Cloud | ✅ |
| Hosted Account Connect & seats | ✦ Cloud |
| Self-hosted SSO (OIDC / SAML / SCIM) | ✅ Community |
| IdP catalog + registry (Okta, Entra, …) | ✅ Community |
| HA / multi-region clustering | ✦ planned Enterprise |
| Premium support SLAs | ✦ Enterprise |

## What “done” means in Community

A model saying a task is finished does not complete it. A task is done when:

1. Evidence exists (tests, artifacts, traces).
2. Required receipts exist for side effects.
3. Budget reservations settled correctly.
4. Any sensitive step has a recorded decision.
