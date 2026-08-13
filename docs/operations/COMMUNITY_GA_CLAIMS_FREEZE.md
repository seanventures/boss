# Community GA — Claims Freeze

**Status:** Frozen for private Community preview / Community GA language  
**Date:** 2026-08-12  
**Branch reference:** `ai/grok/community-ga`  
**Living plan:** [`GA_LIVING_PLAN.md`](GA_LIVING_PLAN.md)  
**Edition source of truth:** `BOSS_EDITION` / `GET /v1/edition` → `edition_id`  
**UI honesty:** Mission Control hides or badges paid CTAs on Community (U2)

> **Rule:** Marketing, release notes, README hero copy, sales decks, and operator
> docs must use **only** the Allowed list below until a claim moves here with
> live dogfood evidence linked from Plane (umbrella **BOSS-337**).

---

## Allowed claims (Community)

Claims that are true for a self-hosted Community install without Account Connect,
marketplace, or Enterprise cluster packs.

| Claim | Evidence / notes |
|---|---|
| Self-hosted governed control plane (tasks, plans, jobs, leases) | Kernel + `qa-live` |
| Budgets, decisions inbox, receipts with dedupe keys | Gate + PRD core |
| Event / audit feed and deadletter visibility | Live ops surfaces |
| Community BYOK ProviderOps kernel (env SecretRefs, providers, inventory, ChangeSets, local worker pools) | `#157` / W0 Done |
| Project GitHub connect + link + test receipts | Live MC bindings |
| **Self-hosted SSO** — OIDC bearer, SAML SP, SCIM 2.0, IdP registry/rollout packs | Community mount; not hosted multi-tenant IdP |
| Local auth / API keys | Default Community auth |
| Metatron status Q&A | Optional gate PASS |
| Honest empty CI/CD and monitoring when undeployed | BOSS-344 |
| Preview list `open_url` is an API-gateway URL (absolute when `BOSS_PREVIEW_PUBLIC_BASE` is set); loopback is `internal_url` only | Live list 2026-08-13; `PREVIEW_GA_CHECKLIST.md` |
| Automated `make qa-live` control-plane gate | 10/10 PASS |
| Optional BOSS Cloud Easy Connect **without** requiring Cloud for core loops | Community independence enforced |
| Software Factory / code-task **APIs and UI exist** (not “always reliable unattended”) | Partial substrate — see Forbidden |

### SSO clarification (do not regress)

- **Allowed:** “Community includes **self-hosted** SSO (OIDC / SAML / SCIM) against your IdP.”
- **Forbidden:** “Community includes hosted multi-tenant IdP / Account Connect SSO as a managed Cloud feature.”

---

## Forbidden claims (until proven + reclassified)

Do **not** say these for Community GA, private preview, or public “autonomous company OS” language.

| Forbidden claim | Why |
|---|---|
| Autonomous unattended company / fully reliable factory unattended | DLQ + substrate gaps |
| Agent Studio workforce out of the box | Empty catalog / seed lag on live |
| Earned autonomy economy complete (G1–G3 “done” product) | Not a Community GA claim |
| Sandboxes / previews “always work” | Rootfs unproven; **0 running** previews; expired View is 409 |
| “Preview View opens a live app” | List `open_url` is a public-base gateway URL (proven); browser 200 HTML is **not** proven |
| **Hosted** multi-tenant IdP / Account Connect as Community features | Paid Cloud surface |
| Hosted agent marketplace / billing portal as Community | Paid Cloud surface |
| Self-improvement proposal canary/rollout as Community | Enterprise-only routes |
| Enterprise cluster packs / HA / managed fleets as Community | Paid Enterprise surface |
| Zero open deadletters / production-hardened private main + Actions green | Ops residual |
| Public multi-tenant SaaS GA | Separate Cloud track |
| “Managed elastic workers available now” without catalog honesty | Catalog must say planned when planned |

---

## Edition surface map (operator honesty)

| Surface | Community | Cloud / Enterprise |
|---|---|---|
| Core control plane | ✅ | ✅ |
| BYOK ProviderOps | ✅ | ✅ |
| Self-hosted SSO (OIDC/SAML/SCIM/IdP) | ✅ | ✅ |
| Hosted Account Connect | ✦ hidden/badge | ✅ |
| Hosted marketplace | ✦ hidden/badge | ✅ |
| Self-improvement proposals | ✦ hidden/badge | ✅ Enterprise |
| Enterprise clusters / support packs | ✦ hidden/badge | ✅ Enterprise |
| Cloud Easy Connect (optional link) | ✅ honest CTA | ✅ |

Mission Control implements this map when `edition_id === community` (or
`BOSS_EDITION=community`): paid CTAs are replaced with honesty cards; SSO/identity
panels stay visible and badged **Included in Community**.

---

## How to promote a claim

1. Live dogfood evidence on a shared deploy tag (`make qa-live` + feature proof).  
2. Plane issue comment with evidence links; move residual to Done.  
3. Update this file (move row Allowed ← Forbidden) **and** `GA_LIVING_PLAN.md` DoD.  
4. Only then change public marketing copy / release notes.

---

## Related docs

- [`GA_LIVING_PLAN.md`](GA_LIVING_PLAN.md) — living DoD and phase tracker  
- [`ga-readiness-2026-08-12/GA_READINESS_SCORECARD.md`](ga-readiness-2026-08-12/GA_READINESS_SCORECARD.md) — assessment snapshot  
- [`docs/product/BOSS_COMMUNITY_EDITION_SPEC.md`](../product/BOSS_COMMUNITY_EDITION_SPEC.md) — product scope  
- [`docs/community/FEATURES.md`](../community/FEATURES.md) — public feature checklist  
