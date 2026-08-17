# BOSS vs Emergent — gap analysis

**Date:** 2026-08-17 · **Status:** for review, nothing implemented
**Sources:** help.emergent.sh, emergent.sh/enterprise, MongoDB customer case study, published
pricing round-ups (Aug 2026). BOSS side read from this repository, not from memory.

---

## 1. What Emergent actually is

A **consumer/prosumer app factory**. One prompt in, a deployed full-stack app out, in about an
hour. Y Combinator-backed, $23M from Lightspeed, claims $50M ARR and 5M+ users across 190+
countries, 50,000+ apps deployed on MongoDB Atlas.

Its shape, from the docs:

| Area | What Emergent has |
|---|---|
| Entry point | Natural-language prompt; **Voice Mode**; mobile app building |
| Generation | Coordinated agents (architect, designer, developer, integration, PM) |
| Stack (opinionated) | React/Next + Tailwind front end, FastAPI/Node back end, **MongoDB** |
| Iteration | Live preview, **Forking**, **Rollback**, **Context Limits** management |
| Integrations | Catalogue: Stripe, Razorpay, Supabase, Airtable, Calendly, Slack, Twilio, SendGrid, Resend, ElevenLabs, Giphy, Google Suite, OpenAI/Claude/Gemini |
| Keys | **Universal Key** — one credential that fans out to model providers |
| Extensibility | **Custom agent building**: main agents, sub-agents, custom tools; **MCP** support in both directions ("Emergent as MCP") |
| Ship | One-click **managed hosting**, **custom domains**, **pre-deployment health check**, deployment types |
| Source | GitHub export/integration |
| Commercial | Credits. Free 10 · Standard $20/100 · Pro $200/750 · Team $300/1,250 shared. A single deploy can eat ~50% of a Standard month |
| Enterprise | SOC 2 + ISO 27001, SSO, RBAC, audit logs, shared workspaces, pooled credits, managed uptime |

**What it is not:** there is no evidence of governed approval gates, spend policy engines,
evidence bundles, incident/oncall response, or self-hosting/VPC. Governance is "built in" as
platform hygiene (SSO/RBAC/audit), not as a control plane the customer operates.

---

## 2. What BOSS actually is

An **autonomous software factory with a governance spine**. 220 engine service modules, 30
console areas. Its differentiators are the things Emergent does not attempt:

- **Governed pipeline** — standards → spec → plan → write_tests → implement → run_tests →
  review panel → gated PR, with evidence recorded at each step.
- **Agent panels (meetings)** that debate to a PRD before code exists, with human or
  auto-approval, and — as of today — can spec a *feature* onto an app that already exists.
- **Decision queue / approval gates** with risk tiers, autonomy thresholds, per-capability
  approval rules (42 of 80 capabilities require a human today).
- **Budget as policy** — reservations, daily/monthly caps, soft-cap warn, lite mode, freeze
  provisioning, freeze tier-2. Spend is a gate, not a receipt.
- **Task Shepherd, Reliability, AI SRE, Assurance, Evidence** — failure diagnosis, auto-repair,
  incident paths.
- **Discovery** — web-researched, cited dossiers before committing to build.
- **A real operator agent** — as of today with `run_shell` in a disposable checkout, read-only
  `query_db`, `describe_schema`, plus governed write tools.
- **Deploys to infrastructure you own** — ssh/docker targets, health checks, secrets store.
- **MCP server** — BOSS is drivable by other agents.

**Generated stack:** Vite + React + TS + Tailwind, Express + better-sqlite3 (single process,
`npm start`). Deliberately lean and buildable.

---

## 3. The honest gap

Two products aimed at different buyers. The gap that matters is **not** "BOSS lacks features" —
it is that BOSS's path from *idea* to *someone else's URL* has holes Emergent has paved.

### 3.1 Gaps that block a user finishing a job

| # | Gap | Emergent | BOSS today | Severity |
|---|---|---|---|---|
| G1 | **Managed hosting for built apps** | One click, app is live on their infra | Previews expire in 60 min; real deploy needs *your own* ssh host + secrets | **Critical** |
| G2 | **Custom domains** | Built-in | None (`custom_domain`: 0 hits in engine) | **Critical** |
| G3 | **Rollback of a deployed app** | First-class feature | Deployment rollout exists; no user-facing "go back one version" | High |
| G4 | **Integration catalogue** | ~15 named providers with guides | None. Agent must hand-write each integration | High |
| G5 | **Provider keys (Universal Key)** | One key fans out to all models | Secrets store exists; no BYO-key fan-out for *generated apps* | High |
| G6 | **Auth/user management in generated apps** | Role-based auth generated in | Scaffold has none | High |
| G7 | **Database for generated apps** | Managed MongoDB | better-sqlite3 in-process; nothing managed | High |
| G8 | **Forking an app** | First-class | None | Medium |
| G9 | **Mobile apps** | Web ↔ mobile | Web only | Medium |
| G10 | **Voice input** | Voice Mode | None | Low |

### 3.2 Gaps in commercial readiness

| # | Gap | Emergent | BOSS today | Severity |
|---|---|---|---|---|
| G11 | **Credits/metering as a product** | Credits, top-ups, pooled team credits, visible burn | Budget engine is richer *internally*, but there is no sellable unit, no top-up, no per-seat/pooled model | **Critical for launch** |
| G12 | **Self-serve onboarding** | Sign up → prompt → app | Design-partner onboarding, ops tokens, manual project setup | **Critical for launch** |
| G13 | **SSO / RBAC / audit as a sold feature** | SOC 2 + ISO 27001, SSO, RBAC, audit | SSO area exists (SAML/OIDC modules); RBAC is role-governance internally; no certifications | High |
| G14 | **Shared workspaces** | Multi-user, pooled billing | Single-tenant, actor-based | High |

### 3.3 Where BOSS is ahead (do not trade these away)

- Governance: approval gates, risk tiers, evidence, audit as *mechanism* not just logging.
- Budget as an enforcing policy engine.
- Panels/PRD before code, and now feature-level specs onto existing apps.
- Failure autonomy: shepherd, SRE, reliability, assurance.
- Deploy to infrastructure the customer owns — the thing enterprises actually ask for and
  Emergent does not offer.
- An operator agent with a shell and database access over the *factory itself*.

---

## 4. Strategic read

Copying Emergent feature-for-feature is the wrong move: they have 5M users, $50M ARR and a
two-year head start on a consumer funnel. BOSS's defensible position is **the governed factory
for teams who cannot use a black box** — regulated, self-hosted, audit-bound.

But three of their capabilities are not "consumer polish", they are **table stakes for anyone
shipping software**, and BOSS cannot be evaluated without them: **hosting (G1), domains (G2),
and a way to pay (G11/G12)**. Today a BOSS app either lives in a 60-minute preview or requires
the customer to bring an ssh host. That is a demo, not a product.

---

## 5. Phased plan

### Phase 1 — "The app has a URL" (largest single gap)
*Goal: a built app is reachable at a stable address without the user owning infrastructure.*

1. **Managed app hosting** — promote a preview into a durable deployment on goldeneye (or a
   customer's target) with a persistent address, not a 60-minute TTL.
2. **Stable subdomain per app** — `<app>.<tenant>.boss.<domain>`, issued at first deploy.
3. **Custom domain binding** — CNAME + TLS, with a verification step.
4. **User-facing rollback** — "go back to the previous version" on the app card, using the
   deployment history already recorded.
5. **Pre-deploy health check surfaced** — BOSS already runs health checks; show the result as a
   gate the user can read before promoting.

*Why first:* G1–G3. Nothing else matters if the output cannot be visited.

### Phase 2 — "The app can do real work"
*Goal: generated apps stop being toys.*

1. **Auth in the scaffold** — sessions, users, roles, guarded routes.
2. **A managed database option** — Postgres per app (BOSS already runs Postgres) alongside
   better-sqlite3 for the lean path.
3. **Integration catalogue, top 5 first** — Stripe, Slack, Resend/SendGrid, Supabase, Google.
   Each as a *recipe the agent applies*, plus a secret binding, not hand-written each time.
4. **BYO provider keys for generated apps** — the Universal Key equivalent, reusing the
   existing secret store.

### Phase 3 — "Somebody can buy it"
*Goal: self-serve, metered, multi-seat.*

1. **Sellable metering** — express the existing budget engine as credits: balance, burn,
   top-ups, pooled team pool. The enforcement engine exists; the *product* does not.
2. **Self-serve onboarding** — sign up, create a project, first build with no ops token.
3. **Workspaces and seats** — multi-user tenants with pooled spend.
4. **Billing integration** — Stripe, subscription + top-up.

### Phase 4 — "Enterprise says yes"
*Goal: convert the governance lead into a sold advantage.*

1. **SSO/RBAC productised** — finish the SSO area, roles as a customer-facing concept.
2. **Audit export** — signed evidence bundles per app/decision.
3. **SOC 2 readiness pack** — controls mapped to what BOSS already records.
4. **Self-host/VPC install** — the thing Emergent does not offer. Community edition is the
   beachhead.

### Phase 5 — "Reach parity where it is cheap"
*Only after 1–4.*

1. **Forking** an app and its lineage.
2. **Mobile targets.**
3. **Voice input** to the agent dock.
4. **Emergent-style templates/quick starts.**

---

## 6. What I would cut

- **Do not** chase 15 integrations. Five, done as reusable recipes, beats fifteen listed.
- **Do not** copy the credits *pricing*; copy the *legibility*. BOSS's budget engine is more
  capable and completely invisible to a buyer.
- **Do not** build mobile before hosting. An unreachable mobile app is worth less than a
  reachable web one.

---

## 7. Open questions for you

1. **Who is the buyer?** Phases 1–3 chase Emergent's user. Phase 4 chases the buyer BOSS is
   already better for. Resource order should follow that answer.
2. **Is managed hosting on goldeneye acceptable long-term**, or is customer-infrastructure the
   product and hosting only a trial convenience?
3. **Community edition scope** — is self-serve hosting part of it, or is that the commercial
   line?
4. **MongoDB vs Postgres** for generated apps. Emergent standardised on Mongo; BOSS runs
   Postgres. Cheaper to reuse.

---

## 8. Confidence and limits

- Emergent's *public* docs are thin on architecture; several pages returned navigation only.
  Feature names are quoted from their docs; internal behaviour is inferred from reviews and the
  MongoDB case study, not verified.
- I did not have access to `app.emergent.sh` (authenticated), so the running product's UX is
  not assessed first-hand.
- BOSS's side was read from this repository. Where a capability is claimed absent, it was
  checked by search (e.g. `custom_domain`, `rollback`, `stripe`, `mobile` — no engine modules).
- Pricing is from third-party round-ups dated 2026; treat as approximate.
