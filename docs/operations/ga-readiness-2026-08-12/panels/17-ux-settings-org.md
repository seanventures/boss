# Panel 17 — Settings & Connections Information Architecture (GA)

**Date:** 2026-08-12  
**Role:** Settings / Configuration Information Architecture Expert  
**Scope:** Mission Control (`apps/web`) — how to reorganize **Settings & Connections** without losing function; Project vs Org vs Operator scopes; edition honesty  
**Primary evidence:** `ConsoleShell.tsx` area registry; `provider-configuration-ui.tsx` (Connections / Resources / Health / FinOps tabs); `capabilities-ui.tsx`; `pipeline-ui.tsx`; GitHub / Budget / Ops / Org / Enterprise / Sandboxes panels; Panel 03 (ProviderOps/BYOK), 06 (MC UX), 07 (Security/Auth), 13 (Edition packaging)  
**Edition under dogfood:** `community` (goldeneye)

---

## Rating scale

| Grade | Meaning |
|---|---|
| **Works** | Operator can find and complete config jobs; scope is clear; not edition-dead |
| **Partial** | Config exists but scattered, dual-homed, or scope-ambiguous |
| **Shell** | Forms look complete; primary backend unproven or empty |
| **Dead** | Surface overpromises (404 / unpaid) while looking like Settings |

---

## Executive verdict

Today Mission Control treats **configuration as a peer of work**. Eight (or more) top-level Platform/Govern areas are mostly *settings or connections*:

| Current top-level area | Config nature |
|---|---|
| System Configuration (`infra`) | Providers, SecretRefs, inventory, ChangeSets, FinOps |
| GitHub | Repo connections, bindings, merge policy |
| Capabilities | Skills inventory, MCP server registration |
| Enterprise | Edition, Account Connect, SSO/SCIM, clusters |
| Agent Roster (`org`) | Actors, packs, modules, access |
| Coding Pipeline | VAG profiles / tier policy |
| Budget | Caps, allocations, intensity, thresholds |
| Operations | Self-improvement programs, canaries (config-ish + runtime) |
| Sandboxes | Host connection, break-glass, execution-plane posture |

**Problem:** Operators hunting “where do I put my OpenAI key / GitHub app / MCP server / budget cap?” bounce across Platform + Govern. **Work surfaces** (Code Tasks, CI/CD, Approvals) compete with **setup surfaces** in the same nav density. Edition-dead Enterprise sits next to live BYOK.

**Recommendation:** Introduce a single **Settings** hub (one nav item under Platform) with **scoped sections** (Project / Org / Operator), **secondary tabs** for Connections vs Policy vs Identity, and **deep links that preserve every existing function**. Do **not** delete panels—**re-home** them. Keep **runtime** views (live Budget pressure, Sandbox fleet, Ops deadletters) where operators already act on them; link from Settings for *policy* halves.

**GA posture for IA alone:** **Partial → Works** once Settings hub ships with edition-aware tabs and no orphaned routes. Functionality is already largely present; the gap is **findability + scope honesty**.

---

## 1. Design principles

1. **One mental model: “Connect → Authorize → Govern → Run.”**  
   Settings owns Connect/Authorize/Govern *configuration*. Run stays in Run/Govern operational areas.

2. **Never lose a function.** Every form, API surface, and deep link (`?area=…`) either stays as a Settings tab/section **or** remains as a runtime panel with a Settings cross-link. Prefer `?area=settings&section=…` with **aliases** for old `area` IDs.

3. **Scope first, feature second.**  
   - **Project Settings** — default working context (project_id).  
   - **Org Settings** — shared across projects in the org.  
   - **Operator Preferences** — per-user chrome only (no tenant side effects).

4. **Edition honesty at the tab level.** Enterprise-only sections badge or hide on community; never present 404 forms as first-class Settings peers (Panel 06 / 13).

5. **Connections are first-class, not buried under “infra.”** BYOK model keys, GitHub, MCP, sandbox host, and webhooks are the community onboarding spine.

6. **Policy near the resource it gates.** Budget *policy* lives in Settings; live Budget *status* stays on Govern. Pipeline *profiles* in Settings; pipeline *run costs* can stay operational.

7. **Destructive / spendy config remains dual-control aware.** ChangeSet approve/apply, provider revoke, SCIM provisioning stay visible with approval copy—not hidden “advanced.”

8. **Boring IA over clever abstraction.** Tabs + left subnav; no multi-app shell rewrite for GA.

---

## 2. Proposed Settings hub

### 2.1 Nav placement

| Change | Detail |
|---|---|
| **Add** | Platform → **Settings** (icon: `SlidersHorizontal` or `Wrench`; sub: “Connections, identity, budgets, and policies”) |
| **Demote from top-level** (re-home into Settings) | System Configuration, GitHub (*config* half), Capabilities (*registration* half), Enterprise, Coding Pipeline (*profiles*), portions of Budget/Org/Ops/Sandboxes that are pure config |
| **Keep top-level (runtime)** | Budget (live status), Operations (DLQ/audit/replay), Sandboxes (fleet/console), Agent Roster (day-to-day staffing), Agent Studio, all Run areas |
| **Optional keep with badge** | GitHub can remain Platform peer **if** dogfood wiring is live; otherwise Settings-only until connected (Panel 06 shell risk) |

**Nav density target:** collapse 4–6 Platform/Govern config peers into **one Settings** entry → frees sidebar for Run work.

### 2.2 Hub chrome

```
┌ Settings ─────────────────────────────────────────────────────┐
│ Scope: [ Project: goldeneye ▾ ]  [ Org ]  [ You ]             │
│ Status chips: edition · API · budget tone · pending approvals │
├──────────────┬────────────────────────────────────────────────┤
│ Subnav       │ Content (existing panels re-mounted)           │
│              │                                                │
│ Connections  │                                                │
│  · Providers │                                                │
│  · GitHub    │                                                │
│  · MCP &     │                                                │
│    Skills    │                                                │
│  · Sandbox   │                                                │
│    host      │                                                │
│  · Comms     │                                                │
│    channels  │                                                │
│ Governance   │                                                │
│  · Budget    │                                                │
│  · Pipeline  │                                                │
│  · Safety &  │                                                │
│    assurance │                                                │
│  · Delivery  │                                                │
│    targets*  │                                                │
│ People       │                                                │
│  · Roster &  │                                                │
│    packs     │                                                │
│  · Access &  │                                                │
│    break-    │                                                │
│    glass     │                                                │
│ Enterprise ▸ │  (badge / hide on community)                   │
│  · Edition   │                                                │
│  · Identity  │                                                │
│  · Account   │                                                │
│    Connect   │                                                │
│  · Clusters  │                                                │
│ Operator     │  (scope = You only)                            │
│  · Console   │                                                │
│  · Defaults  │                                                │
└──────────────┴────────────────────────────────────────────────┘
```

\* Delivery **targets** registration is config; live CI/CD runs stay under Run → CI/CD.

### 2.3 URL contract (preserve deep links)

| Legacy | New | Notes |
|---|---|---|
| `?area=infra` | `?area=settings&section=providers` | Alias: `infra` → settings/providers |
| `?area=github` | `?area=settings&section=github` **or** keep runtime `github` with Settings link | Prefer dual: Settings = connect/bind; Platform GitHub = ops if connected |
| `?area=capabilities` | `?area=settings&section=capabilities` | Inventory + register MCP |
| `?area=enterprise` | `?area=settings&section=enterprise` | Edition-gated |
| `?area=pipeline` | `?area=settings&section=pipeline` | Profiles; run-cost table can remain embed or link to Evidence |
| `?area=budget` | **Keep** runtime; add `settings&section=budget` for caps/allocations/intensity | Split policy vs status |
| `?area=org` | **Keep** roster runtime; Settings section for packs/modules install | Avoid forcing staffing through Settings only |
| `?area=sandboxes` | **Keep** fleet; `settings&section=sandbox_host` for host/connect flags | Host-not-connected banner links here |
| `?area=ops` | **Keep** runtime; Settings for SIM programs if community-visible | Enterprise SIM stays gated |

**Rule:** old `area` IDs continue to work for one release via redirect map in `ConsoleShell.go` / URL sync.

---

## 3. Full mapping — every config surface → Settings (or runtime)

### 3.1 Connections (Project-primary; Org-shared where APIs allow)

| Surface today | UI source | Settings home | Scope | Community | Notes |
|---|---|---|---|---|---|
| Provider catalog + accounts (BYOK) | `provider-configuration-ui` view `connections` | **Connections → Providers** | Project (accounts); definitions may be install-global | **Yes** (kernel) | Keep existing 4-view suite as sub-tabs under Providers |
| SecretRefs / rotate / revoke | provider drawer | same | Project | Yes | Never show raw secrets |
| Resource inventory + sync | view `resources` | **Connections → Providers → Resources** | Project | Yes | ChangeSet create stays here |
| Scaling, pools, janitor | view `health` | **Connections → Providers → Scaling & Health** | Project | Yes | |
| Cloud FinOps / drift / recon | view `finops` | **Connections → Providers → Cloud FinOps** | Project | Yes | Distinct from Budget governor |
| Model credentials / entitlement sources | entitlements APIs / related forms | **Connections → Model credentials** (sub of Providers or sibling) | Project / Org | Yes (BYOK) | Panel 03 write-through secrets |
| GitHub connections, bindings, easy-connect | `github-operations-ui` | **Connections → GitHub** | Project | Yes | Merge ops can stay dual-homed under Platform when connected |
| MCP server register | `capabilities-ui` | **Connections → MCP & Skills** | Org-shared inventory (typical); project allow-list if present | Yes | |
| Native tools / skills audit | `capabilities-ui` | same | Org / install | Yes | Read-mostly |
| Sandbox host connection | `sandboxes-ui` host banner | **Connections → Sandbox host** | Operator install / Project | Yes | Fleet remains top-level Sandboxes |
| Comms channel policies | comms / policy forms | **Connections → Comms channels** | Project | Partial (send fail-closed) | External send flags are deploy config |
| Webhook secrets (GitHub/Plane/…) | ops docs + secret store | **Connections → Webhooks** (read-only pointers + status) | Project | Yes | No raw secret paste without SecretRef |
| Observability stack URLs | monitoring empty-state setup | **Connections → Observability** (optional) | Project / install | Setup-only honesty | Link from Monitoring |

### 3.2 Governance & policy

| Surface today | Settings home | Scope | Community | Runtime twin |
|---|---|---|---|---|
| Budget status card (read) | *not only Settings* | Project | Yes (status API) | **Govern → Budget** primary |
| Budget allocations, intensity, thresholds, cost governance cards | **Governance → Budget policy** | Project (policy); Org optional rollup later | Yes (UI); hard multi-tenant enforcement paid | Budget live widget |
| Coding Pipeline profiles / tiers | **Governance → Coding Pipeline** | Project / Org | Default profiles yes; **custom profiles Enterprise** (existing honesty) | Run cost table optional under Evidence |
| Deployment environment policies | **Governance → Delivery policies** | Project | Yes | Run → CI/CD |
| Approval rules / safety policy evals | **Governance → Safety & approvals** | Project | Yes | Run → Approvals |
| Assurance ownership / proof gates config | **Governance → Assurance** | Project | Shell until programs exist | Govern → Assurance |
| Self-improvement programs | **Governance → Improvement programs** | Project | Community alternative only | Ops; SIM create Enterprise-only |
| Data boundary / redaction policies | **Governance → Data boundary** | Org | Partial | — |

### 3.3 People & access

| Surface today | Settings home | Scope | Community |
|---|---|---|---|
| Agent roster, packs, modules | **People → Roster & packs** (+ keep top-level Org for daily use) | Project / Org | Yes |
| Members, role keys, permissions | **People → Access** | Org / Project | Yes |
| Break-glass grants | **People → Break-glass** | Project | Yes (audited) |
| Agent creation wizard defaults | **People → Agent defaults** | Project | Yes |

### 3.4 Enterprise (paid)

| Surface today | Settings home | Scope | Community UX |
|---|---|---|---|
| Edition / commercial status | **Enterprise → Edition** | Install / Org | Show status card only (community active) |
| Account Connect | **Enterprise → Account Connect** | Org | Hide or single “Cloud/Enterprise” upsell card—**no live 404 forms** |
| SAML / OIDC IdP metadata | **Enterprise → Identity providers** | Org / Project rollout policy | Hide or badge |
| SCIM users/groups | **Enterprise → SCIM** | Org | Hide |
| Identity rollout policies | **Enterprise → Identity rollout** | Project | Metadata-only modes may show read-only |
| Clusters / support cases | **Enterprise → Clusters & support** | Org | Hide |
| Marketplace | **Enterprise → Marketplace** | Org | Hide |

### 3.5 Operator preferences (You)

| Preference | Storage | Side effects? |
|---|---|---|
| Default area / last area | `localStorage` `boss.area` (today) | No |
| Live refresh on/off | ConsoleShell `live` state | No |
| Theme / density (if added) | local | No |
| Palette / keyboard hints | local | No |
| Default project pin | local or user prefs API | Soft only |
| Notification mute (future) | user | No external send without policy |

**Hard rule:** Operator Preferences **must not** mutate provider accounts, budgets, or membership.

### 3.6 Explicitly *not* Settings (stay operational)

| Area | Why |
|---|---|
| BOSS Agent, Command Center, Dashboard | Work entry |
| Approvals, Task Board, Work Allocation, Discovery, Meetings | Work loops |
| Code Tasks, CI/CD runs, Monitoring health, Task Shepherd, Reliability incidents | Execution / ops loops |
| Evidence / Reports | Proof consumption |
| Communications outbox (send path) | Governed runtime |
| Operations deadletters / audit / replay | Incident response |
| Sandboxes console / exec | Execution plane |
| Agent Studio workbench | Build-time creative surface |
| Foundations / Components | Design system, not product config |

---

## 4. Project vs Org vs Operator — decision matrix

| Config kind | Default scope | Promote to Org when… | Operator-only when… |
|---|---|---|---|
| Provider / model API keys | **Project** | Shared org pool + SecretRef ACL exists | Never store keys in browser prefs |
| GitHub connection | **Project** | Org-owned GitHub App serving many projects | — |
| MCP servers | **Org inventory**, project enable | Always shared catalog | — |
| Budget caps | **Project** | Org rollup / portfolio caps (future paid) | — |
| Pipeline profiles | **Project** (custom Enterprise) | Org-standard rigor packs | — |
| Agent packs / RoleSpecs | **Org** install, project assign | Marketplace / pack registry | — |
| Members / SSO | **Org** | — | — |
| Break-glass | **Project** | — | — |
| Sandbox host | **Install / operator** | Multi-project host pool | Local path overrides |
| Console chrome | — | — | **Operator** |
| Edition / Account Connect | **Org / install** | — | — |

**Scope switcher behavior**

- Changing Project reloads project-scoped sections; Org sections stay stable.  
- Attempting Org edit without permission → fail-closed 403 with audit (existing RBAC).  
- Community single-tenant dogfood: Org ≈ “the company”; still label scopes correctly so multi-project later does not rewire IA.

---

## 5. Onboarding spine (Connections-first)

Recommended empty-state order for a new project (aligns engine-first GTM + BYOK):

1. **Settings → Connections → Model credentials / Providers** — at least one usable model SecretRef.  
2. **Settings → Governance → Budget policy** — cap so spendy jobs fail closed honestly.  
3. **Settings → Connections → GitHub** — if software factory path.  
4. **Settings → Connections → Sandbox host** — if code tasks.  
5. **Settings → Connections → MCP & Skills** — optional tools.  
6. **Settings → People → Roster** — assign agents.  
7. Return to **Run** (Agent / Code Tasks).

Command Center “next actions” should deep-link into these Settings sections, not only spawn top-level areas.

---

## 6. Migration plan (no function loss)

### Phase A — IA without rewrite (GA-friendly)

1. Add `settings` AreaId + subnav shell that **embeds existing components** (`ProviderConfiguration`, capabilities, pipeline, slices of budget/org/enterprise).  
2. Alias map: `infra|capabilities|enterprise|pipeline` → settings sections.  
3. Edition filter: hide Enterprise children on community; show one Edition status card.  
4. Add cross-links: Budget runtime ↔ Budget policy; Sandboxes ↔ Sandbox host; GitHub ops ↔ GitHub connect.  
5. Tests: deep-link aliases, edition hide, a11y landmarks for Settings subnav.

### Phase B — Split policy vs runtime cleanly

1. Extract Budget policy forms into Settings-only module; Budget area becomes status + allocations summary + “Edit policy” CTA.  
2. GitHub: Settings = connect/bind; Platform GitHub = merges/activity when `connections > 0`, else redirect to Settings.  
3. Capabilities: Settings registration; optional read-only “installed tools” chip on Agent Studio.

### Phase C — Org-level packs (post-GA)

1. True Org scope switcher backed by APIs (not only project_id).  
2. Shared SecretRef pools, org pipeline packs, SCIM when enterprise live.

**Do not for GA:** rebuild all forms, invent a second settings product, or drop ChangeSet/receipt visibility from provider flows.

---

## 7. Edition honesty rules (Settings-specific)

| Edition | Visible Settings sections |
|---|---|
| **community** | Connections (full ProviderOps + GitHub + MCP + sandbox host), Governance (budget, default pipeline, safety, delivery policies), People, Operator prefs, **Edition status** (read-only) |
| **cloud** | + Account Connect required banner; hosted identity as configured |
| **enterprise\*** | + Identity, SCIM, clusters, marketplace, custom pipeline profiles, SIM automation |

If a section is unpaid: **omit from subnav** or show single locked row with edition id—**never** full forms that 404 (Panel 06 residual on Enterprise area).

---

## 8. Accessibility & UX chrome

- Settings subnav: `nav` with `aria-current` per section (mirror ConsoleShell).  
- Scope switcher: labelled radiogroup or tabs (“Project / Org / You”).  
- Preserve provider suite’s internal tabs (Connections / Resources / Health / FinOps) as nested tabs—do not flatten into 20 sibling leaves.  
- Destructive zones (revoke provider, SCIM delete) remain in-section danger zones with confirm copy.  
- Empty states: reuse honesty patterns (host not connected, zero GitHub bindings → easy-connect, zero MCP → add server).

---

## 9. Scorecard (current → target)

| Concern | Today | After Settings hub |
|---|---|---|
| Find BYOK / providers | Platform → System Configuration (easy if you know the name) | Settings → Connections → Providers |
| Find GitHub connect | Separate Platform peer | Settings → Connections → GitHub (+ optional runtime peer) |
| Find MCP | Platform → Capabilities | Settings → Connections → MCP & Skills |
| Find budget *policy* | Govern → Budget (mixed with live) | Settings → Governance → Budget + live Budget retained |
| Find enterprise identity | Platform → Enterprise (dead on community) | Settings → Enterprise (hidden/badged) |
| Scope clarity | Project in header only | Explicit Project / Org / You |
| Nav density | Config peers inflate Platform/Govern | One Settings entry |
| Function loss risk | N/A | **None** if embed + alias strategy used |

**IA grade today:** **Partial** (all functions exist; organization fights findability and edition trust).  
**IA grade target:** **Works** for community GA Settings & Connections.

---

## 10. Risks & anti-patterns

| Risk | Mitigation |
|---|---|
| Operators cannot find old areas | Alias redirects + Command Center links + one release of dual nav labels (“System Configuration” → Settings badge) |
| Settings becomes a junk drawer | Stick to Connections / Governance / People / Enterprise / You; reject runtime panels |
| Dual homes confuse (Budget, GitHub) | Primary home documented; secondary is summary + “Manage in Settings” |
| SSR fan-out worsens | Lazy-load Settings sections; do not mount all provider + enterprise feeds on every console refresh |
| Org scope without APIs | Label Org sections honestly; disable writes until API exists |
| Hiding Enterprise without status | Keep Edition card so operators know they are on community |

---

## 11. Acceptance criteria (for implementation PR)

- [ ] Single **Settings** area in ConsoleShell Platform group.  
- [ ] All of: Providers suite, GitHub connect, Capabilities/MCP, Budget policy controls, Pipeline profiles, Roster/packs config, Sandbox host config reachable under Settings without removing runtime twins where specified.  
- [ ] Legacy `?area=infra|capabilities|enterprise|pipeline` open the correct Settings section.  
- [ ] Community: no Enterprise write forms that call 404 routes; edition badge or hide.  
- [ ] Scope switcher distinguishes Project / Org / Operator; Operator cannot change tenant config.  
- [ ] Accessibility: subnav landmarks + labelled scope control.  
- [ ] Tests: alias map, edition visibility, at least one deep-link per major section.  
- [ ] Docs: this panel linked from MC UX / GA living plan; PR lists requirement IDs.

---

## 12. Relationship to other panels

| Panel | Relationship |
|---|---|
| 03 ProviderOps/BYOK | Settings **Connections → Providers** is the primary UX home for the kernel |
| 04 GitHub factory | Connect/bind in Settings; merge path remains factory runtime |
| 05 Sandbox delivery | Host config in Settings; fleet/console top-level |
| 06 Mission Control UX | Implements edition-aware nav density reduction |
| 07 Security/Auth | People → Access / Break-glass; SecretRef-only connections |
| 13 Edition packaging | Enterprise section mounting rules |
| 14 Ops observability | Observability connection optional; Monitoring stays runtime |
| 15 Agent Studio | Studio stays Platform; capabilities registration in Settings |

---

## 13. Verdict one-liner

**Re-home configuration into one Settings hub with Connections / Governance / People / Enterprise / Operator scopes; keep runtime loops top-level; alias every legacy area so no function is lost—and stop treating System Configuration, GitHub, Capabilities, and Enterprise as four competing “settings products.”**
