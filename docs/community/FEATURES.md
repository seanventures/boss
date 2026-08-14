# BOSS Community — full feature list

Authoritative status lives in [BOSS_COMMUNITY_EDITION_SPEC.md](../product/BOSS_COMMUNITY_EDITION_SPEC.md). This page is the public-facing checklist.

**Legend:** ✅ shipped in Community · ◐ partial · ○ planned · ✦ Cloud / Enterprise only

## Control plane

| Feature | Status |
|---|---|
| Durable tasks, plans, jobs (Postgres) | ✅ |
| Job leases, heartbeat, fencing | ✅ |
| Deadletters and remediations | ✅ |
| Event / audit feed | ✅ |
| Decision inbox (approve / reject / escalate) | ✅ |
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

## Models & skills

| Feature | Status |
|---|---|
| Bring-your-own API keys (SecretRefs) | ✅ |
| Local / OpenAI-compatible base URLs | ✅ |
| Local model entitlement broker | ✅ |
| Role packs and skill packs (specpack) | ✅ |
| Pack YAML import / export in Mission Control | ✅ |
| Managed multi-tenant model gateway | ✦ Cloud |
| Hosted skill marketplace | ✦ Cloud |

## Software Factory

| Feature | Status |
|---|---|
| Outcome → governed code task | ✅ |
| Spec / architecture / implement / test phases | ✅ |
| Sandbox execution plane | ✅ |
| Evidence-backed completion | ✅ |
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
| Metatron captain (persistent session / goals / watch) | ◐ Always-on overwatch (`POST /v1/metatron/overwatch`, worker every 60s): observe → measure → validate → apply-safe → verify. Every action is validated. Decisions/DLQ stay human. BOSS self-change is a PR. Live dogfood residual until shared-tag deploy. **Not GA.** |
| Work orchestration / portfolio | ✅ |
| Meetings / multi-agent panels | ✅ |
| Assurance / ground-truth runtime | ✅ |
| Self-improvement automation routes | ✦ Enterprise mount |

Metatron captain: one-shot `POST /v1/metatron/query` plus `/v1/metatron/session|goals|watch|drive|overwatch`. Worker overwatch is always on. Every action (chat, goal, drive, apply) runs `validate_action` / `validate_move` first. Decisions and deadletters never auto-apply. BOSS self-change opens a PR — never a silent write. **Not GA** until live dogfood of a validated apply on the shared tag.

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
