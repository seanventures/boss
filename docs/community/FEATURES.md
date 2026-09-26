# BOSS Community — full feature list

Authoritative status lives in [BOSS_COMMUNITY_EDITION_SPEC.md](../product/BOSS_COMMUNITY_EDITION_SPEC.md). This page is the public-facing checklist.

**v1 lock:** [V1_FEATURE_LOCK.md](../product/V1_FEATURE_LOCK.md) — Community v1 in / out / Cloud-Enterprise only. ✦ rows stay Cloud/Enterprise (never Community shipped). **Not public GA.**

**Legend:** ✅ shipped in Community · ◐ partial · ○ planned · ✦ Cloud / Enterprise only

## Control plane

| Feature | Status |
|---|---|
| Durable tasks, plans, jobs (Postgres) | ✅ |
| Job leases, heartbeat, fencing | ✅ |
| Deadletters and remediations | ✅ First-class remediations (`retry` / `requeue` / `escalate` / `hold`) on `/v1/deadletters/*` and Mission Control. Retry never silently drops. **Not public GA.** |
| Event / audit feed | ✅ |
| Go event + webhook dispatcher (S3) | ◐ Optional Enterprise sidecar in `services/webhook-dispatcher`. Python event SoR + leased `webhook.outbound.deliver` stay canonical. Unset URL → honest `not_configured`. Default dry-run, no live mount, **not GA.** |
| Global activity log | ◐ Searchable/sortable Mission Control area (status, kind, actor, stuck banner). **Not GA.** |
| Decision inbox (approve / reject / escalate) | ✅ Hold `reason` + `default_action` on the card; GET `/v1/decisions/{id}`. **Not GA.** |
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
| MCP server for agent clients | ✅ |
| Self-hosted Mission Control console | ✅ |
| API-first clients (`packages/api-client`) | ✅ |

The [terminal API guide](TERMINAL_API.md) covers Metatron commands, direct
`/v1/` calls, and one-shot JSON for scripts. Server-side policy and audit
checks apply to terminal requests as they do to other API clients.

## Models & skills

| Feature | Status |
|---|---|
| Bring-your-own API keys (SecretRefs) | ✅ |
| SecretRef consumption (factory / OpenAI / GitHub / Plane) | ◐ Fail-closed: bound usable ref wins; env is last-resort only when no ref is bound. Unresolvable bound refs do not fall back. Live `local_vault` bind `sref_6b00a2d6a9424abba8a0` on tag `3af9c64` (BOSS-369 Done). **Not GA.** |
| Official QA /reports v1.1 | ◐ Honesty helper + inspect gate + Mission Control scorecard island. Missing UI/UX stays `not_scored`. Never invent scores. Live official POSTs may still be in flight. **Not GA.** |
| Local / OpenAI-compatible base URLs | ✅ |
| Local model entitlement broker | ✅ |
| Role packs and skill packs (specpack) | ✅ |
| Agent Studio starter catalog | ◐ First-run seed installs 13 agents, 3 panels, and specpack workflows. A2A reports `not_configured` when Redis is unset (not a false-red degraded). **Not GA.** |
| Pack YAML import / export in Mission Control | ✅ |
| Managed multi-tenant model gateway | ✦ Cloud |
| Hosted skill marketplace | ✦ Cloud |

## Software Factory

| Feature | Status |
|---|---|
| Outcome → governed code task | ✅ |
| Spec / architecture / implement / test phases | ✅ |
| Sandbox execution plane | ◐ Local subprocess is the Community coding path. Firecracker/microVM claims are **frozen** (`BOSS_MICROVM_CLAIMS_FROZEN=true`). In-repo Go S1 is a fail-closed scaffold — not live, not GA. |
| Evidence-backed completion | ✅ |
| Factory quality pass after green verify | ◐ One improve pass if UI/UX evidence is thin. Frozen tests stay frozen; failed pass restores last green impl. Unknown JTBD / `a11y_violations` stay `not_scored`. Stays partial. **Not public GA.** |
| PR / review packets (with GitHub) | ✅ |
| Browser / automated test evidence | ✅ |
| Deployment targets and runs | ✅ |
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
| Resource receipts and cost events | ◐ Honest empty/unknown/unavailable labels; exact $ needs billing-readable keys |
| Worker pools and scale proposals | ✅ |
| Hosted connector marketplace | ✦ Cloud |

## Workforce

| Feature | Status |
|---|---|
| Role-scoped agents (Metatron, COO, PM, …) | ✅ |
| Focused always-on agents | ◐ Builder + duty loop in code (`/v1/focused-agents`). Worker ticks due always-on/hybrid agents every 60s; Wake now for on-demand. Same validate-then-apply gates as captain. **Not GA** until live dogfood. |
| Metatron captain (persistent session / goals / watch) | ◐ Always-on overwatch on shared tag `3f1d2cf`. Worker every 60s: observe → measure → validate → apply-safe → verify. Live dogfood: first tick applied a preview restart (`app_preview.started` 17:32:16Z, verify ok). Decisions/DLQ stay human. **Not public GA.** |
| AI SRE kernel (awesome-ai-sre map) | ◐ Capability ledger at `GET /v1/ai-sre/capabilities` + Mission Control island. MCP adds list/diagnose/capabilities/verify/page. Observe-only worker tick at `POST /v1/ai-sre/overwatch` (scan / diagnose / dry-run page). Runbooks at `/v1/ai-sre/runbooks` (validate + Decision hold; `page_oncall` dry-run only). Noise view `GET /v1/ai-sre/noise` (clustered vs raw, no invented %). Ask `POST /v1/ai-sre/ask` returns a query plan only. Evidence-only incident postmortem at `POST/GET /v1/reliability/incidents/{id}/postmortem` (existing evidence only; never invents RCA; does not auto-resolve). Deterministic `GET /v1/ai-sre/bench` (InvestigationEngine fixtures). Never remediates, never auto-applies Decisions, never merges PRs. **Not public GA.** |
| Work orchestration / portfolio | ✅ |
| Meetings / multi-agent panels | ✅ |
| Agent process handoffs | ◐ Authorable role sequences (NL compile, manual builder, Metatron/chat). Default: PRD → architect/CTO/VP-PM review → improve → captain build → QA → UI/UX → captain verify → panel certify → captain accept. Simulated completes without spend. **Not GA.** |
| Project Manager steward | ◐ Operate / test / validate / propose over process runs (`/v1/process-manager`, worker 60s). Metrics are evidence-only; templates are never silently rewritten. **Not GA.** |
| Assurance / ground-truth runtime | ✅ |
| Self-improvement automation routes | ✦ Enterprise mount |

Metatron captain: one-shot `POST /v1/metatron/query` plus `/v1/metatron/session|goals|watch|drive|overwatch`. Worker overwatch is always on. Every action (chat, goal, drive, apply) runs `validate_action` / `validate_move` first. Decisions and deadletters never auto-apply. BOSS self-change opens a PR — never a silent write. **Not GA** until live dogfood of a validated apply on the shared tag.

## Scale (optional, not Community GA)

| Feature | Status |
|---|---|
| Go tenant gateway (S2) | ◐ Library + binary in `go/tenant-gateway/`. Tenant/project routing, auth header pass-through, fail-closed unknown tenant, `/health`. **Not mounted.** Production edge remains Traefik. Engine runs without it. Not a Community GA claim. **Not GA.** |

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

A task is **not** done because a model said so. It is done when:

1. Evidence exists (tests, artifacts, traces).  
2. Required receipts exist for side effects.  
3. Budget reservations settled correctly.  
4. Any sensitive step has a recorded decision.  
