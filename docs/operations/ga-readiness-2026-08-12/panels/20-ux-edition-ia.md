# Panel 20 — Edition-aware Information Architecture (UX)

**Date:** 2026-08-12  
**Role:** Edition Packaging UX Expert  
**Scope:** Mission Control navigation IA — what Community dogfood shows by default, what is Advanced, what is Enterprise-only (visible upsell vs hidden), **without deleting routes**  
**Companion panels:** [06 Mission Control UX](./06-mission-control-ux.md), [13 Edition Packaging](./13-edition-packaging.md)  
**Primary evidence:** `apps/web/app/ConsoleShell.tsx` AREAS; `GET /v1/edition`; edition routers (`community` vs paid mounts); existing honesty patterns in self-improvement, pipeline custom profiles, provider-configuration

---

## 1. Problem statement

Community dogfood (`BOSS_EDITION=community`) still presents a **full commercial console**:

- **Enterprise** sits in Platform nav as a first-class peer (“Editions, identity, and account connect”).
- Paid HTTP surfaces (`/v1/account-connect`, `/v1/enterprise`, `/v1/marketplace`, `/v1/self-improvement`, …) are **unmounted → 404**.
- Operators experience **enterprise-looking chrome + dead forms** as product bugs, not SKU boundaries.

Panel 13 rule still stands: paid surfaces must be **hidden**, **single durable Enterprise/Cloud-only state**, or both — never empty/error that looks like outage.

This panel specifies **information architecture only**. Routes, `?area=` deep links, and panel modules **stay**; visibility and chrome change by edition.

---

## 2. Design principles (non-negotiable)

| # | Principle | Implication |
|---|---|---|
| P1 | **No route deletion** | Every current `AreaId` remains routable via `?area=`, ⌘K (with labels), and bookmarks. Hidden ≠ removed. |
| P2 | **SKU honesty over feature density** | Community default nav answers “can I run governed work today?” not “what might Enterprise sell later?” |
| P3 | **Whole workflows, not half-gates** | ProviderOps BYOK + delivery remain Community-capable end-to-end (Panel 13). Do not demote ChangeSets to Enterprise. |
| P4 | **Three chrome layers** | **Default** (always shown) · **Advanced** (collapsed / “More”) · **Enterprise** (upsell or gated panel) |
| P5 | **404 is never a primary CTA** | On Community, no button whose happy path is a paid mount. Prefer badge + explain + optional upgrade CTA. |
| P6 | **One upgrade story** | Prefer a single **Editions / Upgrade** surface over scattering broken SSO/SCIM/cluster forms. |
| P7 | **Deep-link safe** | Visiting `?area=enterprise` on Community always lands on an **honest locked or upsell page**, never a blank error. |
| P8 | **Edition source of truth** | Nav/filter from `GET /v1/edition` (`edition_id`, `status`, capabilities). Fail closed to Community-safe chrome if edition fetch fails. |

---

## 3. Visibility model (implementation contract)

Extend each area (and, where needed, **in-panel sections**) with:

```text
navVisibility:
  community: default | advanced | upsell | hide_nav
  cloud:     default | advanced | upsell | hide_nav
  enterprise_*: default | advanced | upsell | hide_nav

routePolicy: always_mounted_ui   # panels stay in the SPA bundle
deepLink: always_resolves        # ?area= never 404s the shell
```

| Chrome mode | Sidebar | ⌘K palette | Deep link `?area=` | In-panel behavior |
|---|---|---|---|---|
| **default** | Shown in primary group | Ranked high | Full working UI | Full product loop |
| **advanced** | Under **Advanced / More** (collapsed by default) | Shown with “Advanced” hint | Full working UI | Same; optional “why advanced” tip once |
| **upsell** | Shown with **Enterprise** (or **Cloud**) badge | Shown with badge | **Upsell / locked shell** for paid subfeatures; community-safe subsets still work if any | Primary CTAs = learn / contact / set `BOSS_EDITION` docs — not live paid APIs |
| **hide_nav** | Not listed | Optional low-rank “hidden areas” or omitted | Still resolves | Honest panel (setup / locked / dev-only) |

**Important:** `hide_nav` is **nav chrome only**. It does **not** delete the route or the React panel.

---

## 4. Recommended IA — Community dogfood (default edition)

### 4.1 Mental model for Community

Community Mission Control is:

> **Governed local company OS loop:** command → plan/work → approve → budget-aware job → receipt/evidence → (optional) PR/deploy with BYOK.

It is **not** hosted SSO, marketplace catalog, managed fleets, SIM automation, or multi-node enterprise identity.

### 4.2 Default nav (always expanded) — Community

Group order for Community: **Run → Govern → Platform** (product work first; Platform chrome last).  
Keep Platform first only for design-system internal builds; **ship Community dogfood with Run first.**

| Area ID | Label | Group | Why default |
|---|---|---|---|
| `overview` | Command Center | Run* | Home: what changed, risks, next actions |
| `agent` | BOSS Agent | Run | Primary conversational entry into governed workflows |
| `dashboard` | Mission Dashboard | Run | Snapshot: queue, budget tone, health |
| `decisions` | Approvals | Run | Explicit approvals before effects |
| `work` | Task Board | Run | Tasks, artifacts, activity |
| `code_tasks` | Code Tasks | Run | Core Software Factory path |
| `delivery` | CI/CD | Run | Community-mounted whole workflow (targets → deploy) |
| `budget` | Budget | Govern | Visibility + quote/reserve UX; **copy must not claim Enterprise hard multi-tenant enforcement** (`NoopBudgetPort`) |
| `reports` | Evidence | Govern | Receipts, throughput, audit-adjacent proof |
| `ops` | Operations | Govern | **Default for deadletters + audit tail**; SIM subsection is Enterprise-locked (see §6) |
| `github` | GitHub | Platform | Factory binding; empty → easy-connect (shell is OK; not edition-dead) |
| `capabilities` | Capabilities | Platform | Skills / MCP inventory — Community useful |

\* Implement as: Command Center remains area id `overview` but **visually leads Run** (or stays Platform only if engineering prefers; product recommendation is **Run-first** for dogfood).

**Default count target:** ~10–12 items. Today’s 26+ peers feel like Enterprise console on a free self-host SKU.

### 4.3 Advanced (collapsed “Advanced”) — Community

Still fully functional where APIs exist; demoted so founders are not lost.

| Area ID | Label | Notes |
|---|---|---|
| `portfolio` | Work Allocation | Quotas/leases — power operators |
| `discovery` | Discovery | Research dossiers |
| `meetings` | Meetings | Multi-agent panels |
| `monitoring` | Monitoring | Honest empty when no targets/observability |
| `shepherd` | Task Shepherd | Stuck-work reasoning |
| `reliability` | Reliability | AI SRE / incidents |
| `pipeline` | Coding Pipeline | Presets Community; **custom profiles** Enterprise-locked (existing pattern) |
| `comms` | Communications | Outbox discipline; external send fail-closed |
| `assurance` | Assurance | Proof programs — keep advanced until demo seed exists |
| `org` | Agent Roster | Actors/packs/modules; marketplace widgets use upsell, not dead sync |
| `infra` | System Configuration | **BYOK ProviderOps kernel is Community** (Panel 13) — advanced because dense FinOps/ChangeSet suite, not because paid |
| `studio` | Agent Studio | Seed/setup first; advanced until catalog seeded |
| `sandboxes` | Sandboxes | Host-plane dependent; advanced until host connected |

### 4.4 Hide from Community primary nav (routes retained)

| Area ID | Label | Deep-link behavior |
|---|---|---|
| `foundations` | Foundations | Dev/design system — `hide_nav` on Community dogfood; still `?area=foundations` for design QA |
| `components` | Components | Same |
| `enterprise` | Enterprise | **Do not hide_nav** — use **upsell** (see §5). If product insists on quieter chrome, `hide_nav` + single “Edition status” chip that deep-links is acceptable fallback |

---

## 5. Enterprise-only surfaces — upsell vs hidden

### 5.1 Decision rule

| If the surface… | Then |
|---|---|
| Is a **SKU differentiator** operators should know exists (SSO, Account Connect, marketplace, clusters, SIM automation, hard budget/entitlements, support SLA) | **Visible upsell** — badge in nav **or** section in Editions page |
| Is an **operator form that only works on paid mounts** (live SCIM provision, cluster validation run, billing portal session, marketplace sync, self-improvement canary/rollout) | **Hidden controls** inside the panel (or disabled with one lock card) — **no primary button that 404s** |
| Is **Community-capable** but dense | Advanced, not Enterprise |
| Is **dev-only** | `hide_nav` |

### 5.2 Visible upsell (recommended)

**A. Nav item: `enterprise` → relabel for Community**

| Edition | Nav label | Subtitle | Badge |
|---|---|---|---|
| Community | **Editions** (or “Upgrade”) | “Community active · Enterprise & Cloud capabilities” | `Enterprise` / `Cloud` |
| Cloud (linked) | Enterprise / Commercial | Identity, marketplace, support | none or `Cloud` |
| Enterprise * | Enterprise | Identity, clusters, compliance | none |

**B. Community landing for `?area=enterprise` (upsell shell)**

Single page sections (all static/API-safe on Community):

1. **Your edition** — from `GET /v1/edition` (community / active / local auth).  
2. **Included now** — control plane, decisions, receipts, BYOK ProviderOps, delivery, local auth.  
3. **Enterprise & Cloud** — capability list from packs (SSO/OIDC/SAML, SCIM, marketplace, Account Connect, HA/clusters, SIM automation, enforced budget/entitlements, support SLA) with status **“Requires Enterprise or Cloud edition”**.  
4. **How to run paid edition** — operator docs (`BOSS_EDITION=…`), not fake “Start trial” against missing hosted services unless Account Connect URL exists.  
5. **Optional secondary:** link to marketing Cloud beta — external, not in-app broken billing.

**C. In-panel upsell chips (keep community workflows intact)**

| Host area | Community shows | Upsell chip |
|---|---|---|
| `ops` | Deadletters, audit, health, improvement **programs** (if community API) | Self-improvement **proposals / canary / rollout** → Enterprise |
| `pipeline` | Lean/balanced/strict presets | Custom profiles → Enterprise (already) |
| `org` | Local roles/packs | Hosted marketplace sync / private modules → Cloud/Enterprise |
| `budget` | Soft visibility / local numbers | “Hard multi-tenant caps & entitlement broker” → Enterprise |
| `infra` | Full BYOK ProviderOps | Hosted managed fleets / Account Connect provider bridge → Cloud |
| `reports` | Local evidence | Enterprise audit-export governance / retention modes → Enterprise |

### 5.3 Hidden on Community (controls & sub-nav only; routes stay)

Hide or replace with lock card — **do not mount fake success**:

| Surface | Why hidden (not upsell-primary) |
|---|---|
| Live Account Connect link/unlink **forms** that POST paid routes | 404 / fail; show status “not available on Community” once |
| Marketplace **sync / install from hosted catalog** buttons | Paid mount |
| SCIM user/group **provision** actions | Paid + policy |
| SAML/OIDC **enable** wizards that call enterprise APIs | Paid |
| Enterprise **cluster register / validation run** CTAs | Paid + external verification |
| Support case **hosted escalation** | Paid |
| Self-improvement **create proposal / canary / rollout** CTAs | Already partially honest — keep hidden when `enterpriseOnly` |
| Billing portal session | Hosted-only |

**Pattern to copy:** `self-improvement` + pipeline custom profiles: badge, edition id, no 404 CTA.

### 5.4 Explicitly **not** Enterprise-only (do not upsell-lock)

These are Community product (advanced OK):

- ProviderOps: accounts, resources, ChangeSets, receipts, worker pools, janitor, drift, costs (BYOK)
- Delivery / deploy targets / deployment runs
- Code tasks, decisions, events, skills, MCP
- Sandboxes (when host connected)
- GitHub MC (when credentials present)
- Local auth, local control plane

Locking these behind Enterprise would **break open-core usefulness** and reintroduce the half-workflow bug class Panel 13 forbids.

---

## 6. Area-by-area matrix (all current ConsoleShell areas)

| Area | Community nav | Community deep-link | Cloud (linked) | Enterprise * | Notes |
|---|---|---|---|---|---|
| `overview` | **default** | full | default | default | Home |
| `agent` | **default** | full | default | default | |
| `dashboard` | **default** | full | default | default | |
| `decisions` | **default** | full | default | default | |
| `work` | **default** | full | default | default | |
| `portfolio` | advanced | full | default | default | |
| `discovery` | advanced | full | advanced | advanced | |
| `meetings` | advanced | full | advanced | advanced | |
| `code_tasks` | **default** | full | default | default | |
| `delivery` | **default** | full | default | default | Community whole workflow |
| `monitoring` | advanced | full | default | default | Empty-state honesty |
| `shepherd` | advanced | full | advanced | advanced | |
| `reliability` | advanced | full | advanced | default | |
| `reports` | **default** | full | default | default | Audit-export **modes** upsell |
| `budget` | **default** | full + soft-copy | default | default + hard caps | Honesty on Noop port |
| `pipeline` | advanced | presets; custom **locked** | advanced | default | |
| `comms` | advanced | full (send gated) | advanced | advanced | |
| `ops` | **default** | community ops; SIM **locked** | default | default | |
| `assurance` | advanced | full/shell | advanced | default | |
| `org` | advanced | full; marketplace **upsell** | default | default | |
| `github` | **default** | full/shell | default | default | |
| `infra` | advanced | full BYOK | default | default | Not paid-lock |
| `capabilities` | **default** | full | default | default | |
| `studio` | advanced | setup/seed | advanced | advanced | |
| `sandboxes` | advanced | host honesty | advanced | advanced | |
| `enterprise` | **upsell** | upsell shell | default | default | Relabel “Editions” on Community |
| `foundations` | hide_nav | full | hide_nav | hide_nav | Design QA |
| `components` | hide_nav | full | hide_nav | hide_nav | Design QA |

---

## 7. In-area IA (Enterprise payloads inside Community panels)

### 7.1 Operations (`ops`)

```
Operations
├── Health / deadletters / audit tail     [Community default]
├── Replay / canaries (if community API)  [Advanced subsection]
└── Self-improvement automation           [Enterprise lock card]
    └── Improvement programs (read)       [Community alternative if API exists]
```

### 7.2 Enterprise area (Community)

```
Editions (upsell shell)
├── Edition status card                   [GET /v1/edition]
├── What you can do now                   [links to default areas]
├── Enterprise & Cloud capabilities       [static/pack list, no live forms]
└── Operator upgrade path                 [docs / edition env]
# Hidden until paid edition:
# Account Connect forms, SCIM, clusters, marketplace sync, support escalation
```

### 7.3 System Configuration (`infra`)

```
System Configuration
├── BYOK providers / ChangeSets / receipts / pools   [Community advanced]
├── Community banner (already pattern)               [keep]
└── Managed / hosted ProviderOps bridge              [Cloud upsell chip only]
```

Do **not** put the whole ProviderOps suite behind Enterprise.

### 7.4 Agent Roster (`org`)

```
Agent Roster
├── Local actors / roles / pack installs   [Community]
└── Hosted marketplace catalog             [upsell / lock — no sync CTA]
```

---

## 8. Command palette & badges

| Element | Community behavior |
|---|---|
| ⌘K list | Default areas first; Advanced under separator; Editions with badge; foundations/components omitted or “Design” footer |
| Nav badge `Enterprise` | On `enterprise` item and any locked subsection title |
| Nav badge `Advanced` | Optional subtle; prefer group collapse over per-item noise |
| Status chip in header | `Community · local` from edition status (builds trust) |
| Pending decision / DLQ badges | Unchanged on default areas |

---

## 9. Anti-patterns (do not ship)

1. **Delete** `enterprise` route or panel module “to clean Community.”  
2. Leave **full SCIM/cluster forms** interactive when APIs 404.  
3. Treat **ProviderOps** as Enterprise-only after moving kernel to Community.  
4. **Silent** empty Enterprise page with only network errors.  
5. Different **deep-link 404** vs sidebar hide (always resolve shell).  
6. Market **hard budget enforcement** on Community chrome.  
7. Show **Foundations/Components** as peer product areas in dogfood nav.  
8. Use overall `make release-blockers` red as reason to hide Community-default work areas.

---

## 10. Implementation sketch (guidance only; not this panel’s code change)

1. Add `navVisibility` (or `tier: default|advanced|upsell|dev`) to `AREAS` in `ConsoleShell.tsx`.  
2. Fetch edition once per shell session; map `edition_id` → filter + group collapse state (`localStorage` key `boss.nav.advancedOpen`).  
3. Extract Community **Editions upsell** view from current enterprise feed when `edition_id === community` or paid routes absent.  
4. Reuse `enterpriseOnly` / lock-card components from self-improvement + pipeline.  
5. Tests:  
   - Community: default nav length ≤ 12; `enterprise` badge present; deep link `?area=enterprise` has no paid POST buttons.  
   - Community: `?area=infra` still offers ChangeSet paths (not locked).  
   - Community: foundations not in sidebar.  
   - Enterprise edition: full enterprise forms when routes mounted.  
6. Docs: link this panel from Panel 06 GA rule and Panel 13 UI honesty checklist.

---

## 11. Acceptance criteria (UX GA for edition IA)

Community dogfood operators can:

- [ ] Complete CEO → task → decision → receipt path using **only default nav** items  
- [ ] Open **Advanced** to reach ProviderOps, Studio, Sandboxes without hunting Enterprise  
- [ ] See **Editions** upsell that names capabilities without 404 CTAs  
- [ ] Deep-link any legacy `?area=` without shell crash  
- [ ] Never confuse “Enterprise not in SKU” with “API is down”  
- [ ] Still run BYOK ProviderOps + CI/CD without upgrading edition  

Enterprise/Cloud operators can:

- [ ] See Enterprise area as **default** Platform peer with live identity/marketplace/cluster UI when mounts and config allow  
- [ ] Fail closed with honest blocked states when Account Connect / SSO not configured (Panel 13)

---

## 12. Summary recommendation

| Layer | Community sees | Purpose |
|---|---|---|
| **Default** | Command Center, Agent, Dashboard, Approvals, Task Board, Code Tasks, CI/CD, Budget, Evidence, Operations (core), GitHub, Capabilities | Ship the governed loop |
| **Advanced** | Portfolio, Discovery, Meetings, Monitoring, Shepherd, Reliability, Pipeline, Comms, Assurance, Org, System Config (BYOK), Studio, Sandboxes | Power features without SKU confusion |
| **Enterprise upsell (visible)** | Editions nav + lock chips (SIM, custom pipeline profiles, marketplace, SSO story, hard caps, clusters) | Honest commercial boundary |
| **Hidden controls (not routes)** | Paid POST/forms (SCIM, cluster runs, Account Connect link, marketplace sync, SIM automation CTAs) | Prevent dead-button trust loss |
| **Dev hide_nav** | Foundations, Components | Design system out of product chrome |

**Ship Community GA chrome with this IA before marketing “Mission Control complete.”** Runtime edition mounts already partition APIs; **nav still lies.** Fix chrome, keep routes.

---

## 13. Traceability

| Source | Use |
|---|---|
| Panel 06 | Dead Enterprise area; edition honesty GA rule |
| Panel 13 | Paid prefixes; Community whole workflows; UI honesty |
| `community_edition.yaml` | Local auth, basic MC, budget governor (soft), factory |
| `enterprise_edition.yaml` | SSO/SCIM, HA, audit export, enterprise budget, support |
| `cloud_edition.yaml` | Account Connect, hosted control plane, managed workers |
| `ConsoleShell.tsx` AREAS | Current unfiltered nav inventory |
| Self-improvement / pipeline UI | Reference lock patterns |

---

**Panel verdict:** Edition-aware IA is a **Community GA blocker for trust**, not for backend capability. Implement visibility tiers without deleting routes; convert Enterprise from “always-on Platform peer with dead forms” into **visible Editions upsell + hidden paid controls**, and collapse power tooling under **Advanced**.
