# BOSS Community Edition — product specification

**Status:** Active product baseline (not a trial tier)  
**Audience:** Open-source operators, founders, platform teams, design partners  
**Companion:** [BOSS Cloud & Enterprise](./BOSS_CLOUD_ENTERPRISE_SPEC.md) · Marketing [community site](https://boss.sean.network/community/)  
**Pack:** `packages/specpack/packs/community_edition.yaml`  
**Runtime:** `BOSS_EDITION=community` (default)

---

## 1. Product thesis

**BOSS Community is a fully functional, self-hosted Business Operating System for governed AI work.**

It is the open foundation: inspectable, extensible, and useful on day one without BOSS Cloud, Account Connect, or any hosted dependency. Cloud and Enterprise are optional scale paths — not unlock keys for the core loop.

> Community is not a crippled demo. It is the product you operate.  
> Cloud is the easy button when you want BOSS to operate it for you.

### One sentence

BOSS turns an outcome into durable tasks, budgets, decisions, and receipts — via CLI, MCP, or Mission Control — with BYO keys, fail-closed effects, and no silent drops.

### The loop (invariant)

```
Outcome → Plan → Work → Evidence → Decision
```

Every spendy or external effect is budgeted and receipted. Failures retry, deadletter, or escalate. Models may propose; evidence and humans complete.

---

## 2. Who Community is for

| Persona | Why Community fits |
|---|---|
| **Founder / solo operator** | Run an AI workforce with real control, not chat theater |
| **Platform / DevEx** | Self-host governed codegen, PRs, and deploy gates |
| **Internal tools team** | Clear backlogs with receipts and approvals |
| **Security-conscious org** | Keys stay local; no mandatory SaaS control plane |
| **OSS contributor** | Full kernel to read, fork, and extend |

---

## 3. Independence contract (hard rules)

Community **must** work with:

| Requirement | Meaning |
|---|---|
| `BOSS_EDITION=community` | Default packaging |
| `BOSS_ACCOUNT_CONNECT_URL` **unset** | No hosted dependency |
| Local Postgres | Durable control plane |
| BYOK / SecretRefs | Model + infra credentials you own |
| Local Mission Control | Full self-host console |

Community **must not**:

- Block CEO commands, code tasks, budgets, GitHub, or ProviderOps because Cloud is unlinked  
- Mount broken Enterprise forms as required settings  
- Soft-depend on Account Connect, marketplace, or managed fleets (self-hosted SSO is Community)  

Community **may** opt into Cloud later via **Easy Connect** (`/v1/cloud/*`) without losing the local runtime.

---

## 4. Full feature surface (what ships)

Status legend: **shipped** · **partial** · **planned** · **Cloud/Enterprise**

### 4.1 Control plane kernel — **shipped**

| Capability | Status | Notes |
|---|---|---|
| Durable tasks, plans, jobs | shipped | Postgres-backed; survive restarts |
| Leases, heartbeat, fencing | shipped | No silent job drops |
| Deadletters + remediations | shipped | Visible failure path |
| Events / audit trail | shipped | Why something happened |
| Decisions (approve / reject / escalate) | shipped | Decision inbox |
| Receipts + dedupe keys | shipped | Every external effect |
| Budget quote / reserve / consume / release | shipped | Soft ports in Community; real BudgetPort on paid editions |
| Artifacts & evidence objects | shipped | Tests, traces, review packets |

### 4.2 Interfaces — **shipped**

| Capability | Status | Notes |
|---|---|---|
| Control plane HTTP API | shipped | OpenAPI contract |
| CLI | shipped | Operator + automation |
| MCP server | shipped | Agent clients use governed tools |
| Mission Control (self-host web) | shipped | Full local console |
| API-first architecture | shipped | Web is a client, not the source of truth |

### 4.3 Models & skills — **shipped / partial**

| Capability | Status | Notes |
|---|---|---|
| BYOK model providers | shipped | OpenAI-compatible, Anthropic, etc. via SecretRefs |
| Local / private base URLs | partial | OpenAI-compatible endpoints (Ollama/vLLM-class) |
| Model entitlement broker (local) | shipped | Route under policy; Community uses allow-all port by design |
| Skill packs & role packs | shipped | Specpack roles/workflows/skills |
| Managed model gateway | Cloud | Optional after Easy Connect |

### 4.4 Software Factory — **shipped**

| Capability | Status | Notes |
|---|---|---|
| Outcome → code task | shipped | Governed worker path |
| Spec / architecture / implement / test | shipped | Evidence-backed phases |
| Sandbox execution | shipped | Isolated worker substrate |
| PR / review packets | shipped | GitHub-backed when connected |
| Browser / test evidence | shipped | Completion needs proof |
| Deploy targets + runs | shipped | Community delivery path (not Cloud fleets) |

### 4.5 GitHub project sync — **shipped**

| Capability | Status | Notes |
|---|---|---|
| Project connections + bindings | shipped | Easy connect patterns |
| Branch policies & snapshots | shipped | Immutable topology |
| Fenced branch leases | shipped | Concurrent safety |
| Safe sync + conflict resolution | shipped | Receipted |
| Merge authorizations | shipped | Protected targets |
| Reconciler | shipped | Webhook/poll/repair |

### 4.6 ProviderOps (Resource Plane kernel) — **shipped**

| Capability | Status | Notes |
|---|---|---|
| Provider accounts (model + infra keys) | shipped | Write-only secret slots |
| Inventory sync (read-only) | shipped | DO, CF, Supabase, OpenAI, Anthropic (official APIs) |
| ChangeSets plan / approve / apply | shipped | Dual-control, receipts |
| Worker pools + autoscaler proposals | shipped | Local capacity |
| Cost events / FinOps views | partial | When billing-readable keys present |
| Hosted connector marketplace | Cloud | Optional after link |

### 4.7 Workforce & orchestration — **shipped / partial**

| Capability | Status | Notes |
|---|---|---|
| Role-scoped agents (Metatron, COO, PM, …) | shipped | Specpack roles |
| Focused always-on agents | partial | Builder + `/v1/focused-agents` duty loop. Validated apply only. Not GA. |
| Metatron captain (session / goals / watch / drive / overwatch) | partial | Always-on overwatch validates every action then may apply preview restart or a governed `code.task`. Decisions/DLQ stay human. No live dogfood. Not GA. |
| Work orchestration / portfolio | shipped | Priorities, quotas, leases |
| Meetings / multi-agent panels | partial | Runtime present; expand packs |
| Assurance / ground truth | partial | Runtime present; depth growing |
| Self-improvement automation | Enterprise mount | Fail-closed on Community routes |

### 4.8 Open-source packaging — **shipped / policy**

| Capability | Status | Notes |
|---|---|---|
| Self-host Docker / Compose / k8s paths | shipped | Deploy docs |
| Edition router (Community vs paid mounts) | shipped | Paid paths absent → 404 |
| Optional Easy Connect to BOSS Cloud | shipped | Community-mounted `/v1/cloud/*` |
| Self-hosted SSO / SCIM | Community (shipped) | OIDC + SAML SP + SCIM 2.0; opt-in flags |
| HA fleets | Enterprise / planned | Not required for Community |

---

## 5. What is deliberately *not* Community

These remain Cloud or Enterprise so Community stays honest and sharp:

| Capability | Home | Why |
|---|---|---|
| Hosted multi-tenant control plane | Cloud | BOSS operates the plane |
| Managed elastic worker fabric | Cloud (planned) | Capacity as a service |
| Hosted Account Connect + marketplace | Cloud | Org identity & catalog |
| Seat billing / commercial portal | Cloud | Commercial ops |
| Hosted multi-tenant IdP / Account Connect | Cloud / Enterprise | Self-hosted SSO is Community |
| HA clustering / multi-region | Enterprise / planned | Ops profiles |
| Premium support SLAs | Enterprise | Design partnership |

---

## 6. Architecture (operator view)

```
┌─────────────────────────────────────────────────────────────┐
│  Interfaces:  CLI  ·  MCP  ·  Mission Control  ·  HTTP API  │
└────────────────────────────┬────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────┐
│  Control plane (Postgres)                                     │
│  commands · tasks · jobs · leases · decisions · events        │
│  budgets · receipts · artifacts · policies                    │
└───────┬───────────────────────────────┬─────────────────────┘
        │                               │
┌───────▼──────────┐          ┌─────────▼────────────────────┐
│  Workers         │          │  Adapters (fail-closed)       │
│  leased jobs     │          │  models · GitHub · providers  │
│  Software Factory│          │  SecretRefs only              │
└──────────────────┘          └───────────────────────────────┘
```

**Hard technical rules** (also in `AGENTS.md`):

1. No external effect without a `Receipt` + `dedupe_key`  
2. No spendy action without a `BudgetReservation`  
3. No worker execution without a lease + heartbeat  
4. Provider writes via `ChangeSet` + `ResourceReceipt`  
5. No silent drops — retry, deadletter, or decision  

---

## 7. Install surfaces (story)

### Minimum path

1. Postgres  
2. API + worker  
3. BYO model key  
4. CLI or MCP client  

### Full Community path

1. Above + Mission Control web  
2. GitHub connection for Software Factory  
3. ProviderOps keys for infra you own  
4. Optional: Easy Connect when you want Cloud later  

Detailed operator docs: `docs/operations/`, `Makefile` targets (`make dev`, `make smoke`), and the community site quickstart.

---

## 8. Acceptance criteria (Community is “real”)

1. **No Cloud:** With Account Connect unset, CEO command → task → job → receipt path works.  
2. **Mission Control:** Self-host console loads work, decisions, budget, and ProviderOps.  
3. **BYOK:** Model call uses operator keys; credentials never returned to agents.  
4. **GitHub:** Project can connect and produce a receipted PR path (when GH configured).  
5. **ProviderOps:** Inventory + ChangeSet gates work for configured providers.  
6. **Fail closed:** Missing keys create visible decisions/events, not silent mocks.  
7. **Easy Connect optional:** `/v1/cloud/catalog` and `/status` work; start without AC URL returns blocked with clear reason; core paths still 200.  
8. **Edition honesty:** Paid routes (marketplace, Account Connect, self-improvement, cluster packs) are unmounted (404), not half-gated. Self-hosted SSO (SAML/SCIM/IdP registry) is Community.

---

## 9. Spec pack composition

From `community_edition.yaml`:

**Includes:** local auth, single org/workspace, local control plane, local runner, canonical objects, Metatron, Mission Control, decision inbox, receipts, budget governor, software factory, model entitlement broker, ProviderOps kernel, local model routing, role/skill packs, import/export, Cloud Easy Connect opt-in.

**Installs (defaults):** roles metatron/coo/cto/pm/qa/release_manager; workflows ceo_command_to_pr, decision_inbox, budget_gated_job, model_entitlement_routing; core safety + budget + entitlement + work orchestration policies.

**License posture:** `license_candidate: AGPLv3` with `legal_review_required: true` — treat as policy-in-progress for public flip; engine packaging may carry separate notices. Do not invent a final license claim beyond what legal has approved.

---

## 10. Narrative for open source (positioning)

Use this language publicly:

- **Product, not preview:** “BOSS Community is a complete self-hosted product.”  
- **Governed AI work:** “Not another chat window — durable work with evidence.”  
- **You own the keys:** “BYOK models and infrastructure.”  
- **You operate the system:** “Cloud is optional managed ops, not a requirement.”  
- **Fail closed:** “Missing config surfaces as decisions and events.”  
- **Scale path:** “Start Community. Connect Cloud when you want BOSS to run the plane.”

Avoid:

- “Free tier / trial of Cloud”  
- Claiming managed fleets or **hosted** multi-tenant IdP as Community features (self-hosted SSO is Community)  
- Claiming unattended production autonomy  

---

## 11. Source of truth map

| Artifact | Role |
|---|---|
| This document | Community product + feature contract |
| `packages/specpack/packs/community_edition.yaml` | Installable pack |
| `apps/api/app/routers/enterprise.py` | What is paid-only |
| `docs/product/BOSS_CLOUD_ENTERPRISE_SPEC.md` | Upmarket path |
| `api/openapi.yaml` | API surface |
| `https://boss.sean.network/community/` | Public Community story |
| `AGENTS.md` | Implementation hard rules |

---

## 12. Changelog

| Date | Change |
|---|---|
| 2026-08-12 | Initial full Community Edition product + feature specification |
