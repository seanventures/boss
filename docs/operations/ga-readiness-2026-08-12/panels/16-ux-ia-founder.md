# Panel 16 — UX Information Architecture (Founder / CEO Persona)

**Date:** 2026-08-12  
**Role:** Information Architecture + Founder/CEO Persona Expert  
**Scope:** Mission Control shell (`apps/web/app/ConsoleShell.tsx`) — reorganize ~28 areas for comprehension without removing functionality  
**Primary evidence:** `ConsoleShell.tsx` AREAS list; panel 06 Mission Control UX; panel 13 Edition packaging; public site loop `Outcome → Plan → Work → Evidence → Decide`  
**Edition under dogfood:** `community` (goldeneye) — Enterprise is secondary, not peer-equal in primary nav

---

## 0. Executive verdict

Today’s nav is a **flat inventory of product surfaces** grouped as Platform / Run / Govern. That is accurate for builders who already know BOSS, and wrong for a **daily founder job**:

> “What outcome am I driving → what needs my decision → is work moving → what proof do I have → what did it cost?”

The marketing simple loop is already right. Mission Control should **mirror that loop in primary chrome**, and push power tooling (ProviderOps, Studio, Sandboxes, design system, Enterprise identity) into **Settings / Advanced** with progressive disclosure.

**Constraint honored:** every current `AreaId` remains reachable. This panel is **labels, grouping, nesting, defaults, and disclosure rules** — not feature deletion.

| Metric | Today | Recommended |
|---|---|---|
| Top-level nav items | ~28 flat | **≤8 primary** + nested secondary |
| Group metaphor | Platform / Run / Govern (build-system) | **Loop + Control + Configure** (operator job) |
| Enterprise placement | Peer Platform item | **Secondary under Settings**, edition-badged |
| Design system (Foundations/Components) | Same height as Approvals | **Dev / Design lab** under Advanced |
| Default land | Command Center (`overview`) | Keep **Home** (`overview`); Mission Dashboard becomes nested / widget source |

---

## 1. Founder mental model (why this IA)

### Daily CEO jobs-to-be-done

| Rank | Job | Frequency | Today’s closest area(s) |
|---|---|---|---|
| 1 | See what needs me now | Every open | Command Center, Approvals badge |
| 2 | Decide (approve / reject / escalate) | Multiple / day | Approvals |
| 3 | Direct work (outcome, chat, priority) | Daily | BOSS Agent, Work Allocation, Discovery |
| 4 | Confirm work is moving / stuck | Daily | Task Board, Task Shepherd, Mission Dashboard |
| 5 | Ship software safely | Daily (builders) | Code Tasks, CI/CD, GitHub, Sandboxes |
| 6 | Verify proof & spend | Daily / weekly | Evidence, Budget, Assurance |
| 7 | Configure agents, tools, providers | Occasional | Roster, Capabilities, Studio, System Configuration |
| 8 | Enterprise identity / commercial | Rare (community) or ops-admin | Enterprise |

### Loop alignment (product simple loop)

| Loop step | Founder language | Primary home | Nested power |
|---|---|---|---|
| **Outcome** | “What are we trying to achieve?” | Home signals + Outcomes hub | Discovery, Work Allocation (portfolio) |
| **Plan** | “How do we staff and sequence?” | Work (allocation tab) | Meetings, Coding Pipeline profiles |
| **Work** | “What is running / blocked?” | Work hub | Task Board, Code Tasks, Shepherd, Comms |
| **Evidence** | “What proof exists?” | Evidence hub | Reports, Assurance, Reliability traces |
| **Decide** | “What requires my signature?” | **Decide** (first-class) | Approvals + decision history filters |

**Anti-pattern today:** Approvals sit mid-Run list; Foundations sit above Command Center; Enterprise sits peer with Command Center. Founder attention is diluted by design-system and commercial chrome.

---

## 2. Primary nav (≤8 items) — daily CEO use

**Rule:** Primary items are **jobs**, not every panel. Each primary may expand to a secondary rail or in-page subnav. Unread/action badges only on **Decide**, **Work** (stuck), and **Ops health** (deadletters) when nested under Settings.

### Recommended primary set (8)

| # | Primary label | Purpose (one line) | Default land `AreaId` | Badge |
|---|---|---|---|---|
| 1 | **Home** | What changed · top risks · next actions · KPI snapshot | `overview` | Optional: combined “needs attention” count |
| 2 | **Ask BOSS** | Natural-language intake into governed workflows | `agent` | — |
| 3 | **Decide** | Explicit approvals before real effects | `decisions` | `pendingDecisions` (existing) |
| 4 | **Work** | Outcomes, tasks, allocation, stuck work | `work` | Optional: deadletter/stuck count |
| 5 | **Ship** | Code → PR → deploy → health | `code_tasks` | — |
| 6 | **Evidence** | Receipts, proof, scorecards, incidents | `reports` | — |
| 7 | **Budget** | Live spend, reservations, cap pressure | `budget` | Tone chip (green/amber/red) already in chrome |
| 8 | **Settings** | Connect, agents, tools, pipeline, org, enterprise | `infra` (or Settings landing) | Edition badge if unpaid surfaces available |

### Why these eight (and not others)

- **Home** absorbs “Command Center + Mission Dashboard” as one executive surface (dashboard metrics remain widgets / secondary tab — not a peer).
- **Ask BOSS** is the product thesis intake path; keep it primary so the console is not “forms only.”
- **Decide** is the CEO’s highest-leverage action; elevate above Task Board.
- **Work** is the durable control-plane queue (not just Kanban chrome).
- **Ship** is the software-factory path founders buy for; collapses Code Tasks / CI/CD / Monitoring / Sandboxes / GitHub under one mental bucket.
- **Evidence** is the audit moat; separates “proof” from “run.”
- **Budget** stays primary — money is governance, not a buried Govern item.
- **Settings** is the honest place for Platform/Enterprise/design-system density.

### Explicitly **not** primary (still fully reachable)

Foundations, Components, Mission Dashboard (standalone), Work Allocation, Discovery, Meetings, CI/CD, Monitoring, Task Shepherd, Reliability, Coding Pipeline, Communications, Operations, Assurance, Agent Roster, GitHub, Capabilities, Agent Studio, Sandboxes, Enterprise — all nested (see §3–4).

---

## 3. Secondary / nested groups (power features)

Secondary nav appears as:

1. **Subnav under the active primary** (desktop left inset or horizontal tabs), and  
2. **⌘K palette** (already present — keep; index by old + new labels), and  
3. **Settings sections** for configure-time surfaces.

### 3.1 Home

| Nested label | `AreaId` | Notes |
|---|---|---|
| Command Center | `overview` | Default |
| Mission snapshot | `dashboard` | Rename from Mission Dashboard; same data, secondary tab or Home widgets |
| (Future) Attention queue | — | Composite of pending decisions + deadletters + red budget — not a new area |

### 3.2 Ask BOSS

| Nested label | `AreaId` | Notes |
|---|---|---|
| Conversation | `agent` | Default; single primary surface |

Optional later: deep-link chips into Work / Decide from agent responses (already partially via AreaLink) — no new AreaId.

### 3.3 Decide

| Nested label | `AreaId` | Notes |
|---|---|---|
| Inbox | `decisions` | Default; filter “needs action” vs history in-panel |

Power: approval rules remain inside the panel / Settings → Policies when present — no nav split.

### 3.4 Work (Outcome → Plan → Work)

| Nested label (recommended) | `AreaId` | Loop step | Notes |
|---|---|---|---|
| Task board | `work` | Work | Default |
| Priorities & allocation | `portfolio` | Plan | Today “Work Allocation” |
| Discover outcomes | `discovery` | Outcome | Research → candidate outcomes |
| Agent meetings | `meetings` | Plan | Debate → PRD |
| Task shepherd | `shepherd` | Work | Stuck-work reasoning |
| Outbox / communications | `comms` | Work | External sends discipline |

**Order in subnav:** Task board → Priorities → Discover → Meetings → Shepherd → Communications  
(Founder hits board first; discovery is intentional, not equal-noise.)

### 3.5 Ship (software factory)

| Nested label (recommended) | `AreaId` | Notes |
|---|---|---|
| Code tasks | `code_tasks` | Default — agent edits in sandbox |
| Delivery (CI/CD) | `delivery` | PR → deploy path |
| Runtime health | `monitoring` | Deployed app health / Grafana |
| Reliability (AI SRE) | `reliability` | Incidents & governed fixes |
| Repositories | `github` | Bind / sync / merge truth |
| Sandboxes | `sandboxes` | Execution plane console |

**Order:** Code tasks → Delivery → Repositories → Sandboxes → Runtime health → Reliability  
(Build path first; ops health after something is deployed.)

### 3.6 Evidence

| Nested label (recommended) | `AreaId` | Notes |
|---|---|---|
| Evidence & reports | `reports` | Default — throughput, receipts, scorecards |
| Assurance / proof gates | `assurance` | Ground truth & adversarial verification |
| (Cross-link) Reliability | `reliability` | Also under Ship; dual parent OK via same AreaId |

Dual-parent note: `reliability` lives under **Ship** for operators; Evidence subnav may **link** to it without duplicating route state. Prefer single parent **Ship → Reliability**, with Evidence showing summary cards that deep-link.

### 3.7 Budget

| Nested label | `AreaId` | Notes |
|---|---|---|
| Budget control | `budget` | Default |

Coding pipeline **run costs** stay visible as a cross-link to Settings → Pipeline (do not fork Budget into two primaries).

### 3.8 Settings (configure + admin + advanced)

Settings is a **sectioned secondary tree**, not one mega-page.

#### Settings → Agents & tools

| Nested label | `AreaId` |
|---|---|
| Agent roster | `org` |
| Capabilities (tools & MCP) | `capabilities` |
| Agent studio | `studio` |
| Coding pipeline (VAG profiles) | `pipeline` |

#### Settings → Infrastructure & providers

| Nested label | `AreaId` |
|---|---|
| System configuration (ProviderOps / FinOps) | `infra` |
| Operations (health, audit, replay, self-improvement) | `ops` |

#### Settings → Organization & edition

| Nested label | `AreaId` |
|---|---|
| Enterprise & identity | `enterprise` |

#### Settings → Design lab (developer / internal)

| Nested label | `AreaId` |
|---|---|
| Foundations (design tokens) | `foundations` |
| Components (UI library) | `components` |

**Default Settings land:** `infra` for connected ops, or a lightweight Settings index that lists sections with one-line purpose + setup status (Connected / Not set up / Enterprise).

---

## 4. Where Settings / content org goes

### Placement map (configure-time vs run-time)

| Concern | Primary | Nested under | Rationale |
|---|---|---|---|
| **Infra / ProviderOps / connections / FinOps** | Settings | Infrastructure → System configuration (`infra`) | Connect once; reference often via Budget & Ship |
| **GitHub** | Ship | Repositories (`github`) | Day-to-day factory path; *connection wizard* can also appear in Settings index as “Connect GitHub” shortcut → same `AreaId` |
| **Org / actors / packs / modules** | Settings | Agents → Agent roster (`org`) | Staffing is configure + govern, not every-hour CEO nav |
| **Enterprise (identity, Account Connect, SCIM, clusters)** | Settings | Organization & edition → Enterprise | **Never primary** on Community; secondary always |
| **Pipeline (VAG profiles, tiers, run costs)** | Settings | Agents & tools → Coding pipeline | Policy/config; run costs surface also on Budget as read-only link |
| **Capabilities (skills, MCP)** | Settings | Agents & tools → Capabilities | Extension surface, not daily loop |
| **Communications** | Work | Outbox / communications | Sending is operational work, not “settings” |
| **Operations (DLQ, audit, canaries, SIM)** | Settings | Infrastructure → Operations | Power operator; deadletter badge may promote a **Home** attention chip linking here |
| **Sandboxes** | Ship | Sandboxes | Runtime of code work; host-not-connected honesty stays |
| **Studio** | Settings | Agents & tools | Build agents occasionally; not CEO primary |
| **Design system** | Settings | Design lab | Internal / design QA only |

### Settings information scent (index page content outline)

No new AreaId required; implement as `infra` landing variant or `?area=settings` alias → first section:

1. **Connect** — GitHub, providers, sandbox host (status pills)  
2. **Govern money & models** — Budget (link), Pipeline profiles  
3. **Staff agents** — Roster, Capabilities, Studio  
4. **Trust & edition** — Enterprise (badged)  
5. **Lab** — Foundations, Components (collapsed by default)

---

## 5. Rename recommendations (clarity)

Renames are **labels only**; `AreaId` stays stable for deep links (`?area=`), localStorage, and tests.

| `AreaId` | Current label | Recommended label | Why |
|---|---|---|---|
| `overview` | Command Center | **Home** (page title may remain “Command Center”) | Founder-default; “command center” is fine as H1 inside |
| `agent` | BOSS Agent | **Ask BOSS** | Action-oriented; reduces “another agent product” confusion |
| `dashboard` | Mission Dashboard | **Mission snapshot** | Nested under Home; not a second home |
| `decisions` | Approvals | **Decide** (nav) / **Decision inbox** (H1) | Verb matches loop; “Approvals” still OK as synonym in palette |
| `work` | Task Board | **Task board** | Keep; sentence case consistency |
| `portfolio` | Work Allocation | **Priorities & allocation** | “Allocation” alone is HR/cloud jargon |
| `discovery` | Discovery | **Discover outcomes** | Ties to Outcome step |
| `meetings` | Meetings | **Agent meetings** | Distinguishes from human calendar |
| `code_tasks` | Code Tasks | **Code tasks** | Keep concept; sentence case |
| `delivery` | CI/CD | **Delivery** | Less toolchain; more outcome (“where did the ship stop?”) — subtitle retains CI/CD |
| `monitoring` | Monitoring | **Runtime health** | Not generic observability brand |
| `shepherd` | Task Shepherd | **Stuck work** | Founder-readable; subtitle can keep “Shepherd” |
| `reliability` | Reliability | **Incidents & fixes** | AI SRE is feature; incident is job |
| `reports` | Evidence | **Evidence** | Keep — already correct for moat |
| `budget` | Budget | **Budget** | Keep |
| `pipeline` | Coding Pipeline | **Coding pipeline** | Keep; Settings only |
| `comms` | Communications | **Outbox** | Matches “discipline before external sends” |
| `ops` | Operations | **Control plane ops** | Distinguishes from runtime Monitoring |
| `assurance` | Assurance | **Proof gates** | Less abstract than “Assurance” for GA |
| `org` | Agent Roster | **Agent roster** | Keep |
| `github` | GitHub | **Repositories** | Product-neutral; GitHub remains in subtitle until multi-forge |
| `infra` | System Configuration | **Providers & resources** | Says what you configure |
| `capabilities` | Capabilities | **Tools & MCP** | Concrete |
| `studio` | Agent Studio | **Agent studio** | Keep |
| `sandboxes` | Sandboxes | **Sandboxes** | Keep |
| `enterprise` | Enterprise | **Enterprise & identity** | Signals commercial/SSO, not “more product” |
| `foundations` | Foundations | **Design tokens** | Lab clarity |
| `components` | Components | **UI components** | Lab clarity |

### Group renames (replace Platform / Run / Govern)

| Today | Recommended top-level metaphor |
|---|---|
| Platform | Fold into **Settings** + **Ship** |
| Run | Fold into **Work** + **Ship** + **Ask BOSS** |
| Govern | Fold into **Decide** + **Budget** + **Evidence** + Settings policies |

Do **not** keep Platform/Run/Govern as peer headers if primary becomes the 8 jobs — dual taxonomy confuses. Palette can still tag items with legacy group for power users.

---

## 6. Progressive disclosure rules

### 6.1 Edition (Community first)

| Rule | Behavior |
|---|---|
| **E1** | Primary nav is **identical** for Community and Enterprise (same 8 jobs). |
| **E2** | Enterprise-only surfaces (`enterprise`, SIM/self-improvement inside `ops`, custom pipeline profiles, Account Connect/SCIM) show **badge `Enterprise`** in secondary Settings, never as unbadged primary peers. |
| **E3** | If `GET /v1/edition` (or equivalent) is `community`, Enterprise nav item remains **reachable** but opens with **honest empty / upgrade / self-host identity story** — no fake cluster fleet. Prefer collapse under Settings → “Organization & edition” with lock icon. |
| **E4** | Community BYOK ProviderOps (`infra`) is **first-class Settings**, not Enterprise-only — matches packaging panel 13. |
| **E5** | Never hide an AreaId entirely if deep links / dogfood depend on it; use **visually deprioritized** + empty honesty instead of 404 chrome. |

### 6.2 Empty state (setup vs quiet)

| Rule | Behavior |
|---|---|
| **S1** | Distinguish **Not set up** (no connection, 0 targets, host down) from **Quiet** (configured, zero incidents). |
| **S2** | Nested items with Not set up show a **dim label + setup dot** in subnav (e.g. Repositories, Sandboxes, Runtime health). |
| **S3** | Primary **Ship** remains visible even if all factory children are unconfigured — landing explains “Connect a repo → run a code task.” |
| **S4** | **Evidence / Proof gates** with zero cases use “No proof program yet” not green-zero dashboard cosplay (align panel 06). |
| **S5** | **Outbox** empty must not look like a full messaging product; CTA = draft governed send / connect channel later. |

### 6.3 Advanced / power-user

| Rule | Behavior |
|---|---|
| **A1** | Design lab (`foundations`, `components`) **collapsed** by default; expand “Show design lab” or `?advanced=1`. |
| **A2** | Control plane ops (`ops`) nested under Settings; **deadletter count** may surface on Home attention chip without promoting Ops to primary. |
| **A3** | Agent studio, Coding pipeline, Tools & MCP default under Settings; power users pin via local preference (optional later) — not required for GA IA. |
| **A4** | ⌘K always searches **all** areas by old and new labels (synonym table). |
| **A5** | Mobile: primary 8 as bottom bar or top scroll chips; secondary as in-page tabs; no 28-item drawer dump without grouping. |

### 6.4 Density / GA honesty

| Rule | Behavior |
|---|---|
| **D1** | Default Community session shows **Home, Decide, Work, Budget** as the “first hour” story; Ship/Evidence/Ask BOSS still primary but onboarding highlights the four. |
| **D2** | Do not lead with Enterprise, Foundations, or full ProviderOps graph on first empty org. |
| **D3** | Mission snapshot metrics preferred as **Home widgets** over a competing top-level dashboard. |

---

## 7. Old → new reachability map (complete)

Every current area remains addressable by the same `AreaId` (`?area=<id>`).  
**Primary** = one of the 8. **Secondary path** = human navigation path.

| `AreaId` | Current group / label | New primary | New secondary path | Recommended label |
|---|---|---|---|---|
| `overview` | Platform / Command Center | **Home** | Home → Command Center | Home / Command Center |
| `dashboard` | Run / Mission Dashboard | **Home** | Home → Mission snapshot | Mission snapshot |
| `agent` | Run / BOSS Agent | **Ask BOSS** | Ask BOSS → Conversation | Ask BOSS |
| `decisions` | Run / Approvals | **Decide** | Decide → Inbox | Decide / Decision inbox |
| `work` | Run / Task Board | **Work** | Work → Task board | Task board |
| `portfolio` | Run / Work Allocation | **Work** | Work → Priorities & allocation | Priorities & allocation |
| `discovery` | Run / Discovery | **Work** | Work → Discover outcomes | Discover outcomes |
| `meetings` | Run / Meetings | **Work** | Work → Agent meetings | Agent meetings |
| `shepherd` | Run / Task Shepherd | **Work** | Work → Stuck work | Stuck work |
| `comms` | Govern / Communications | **Work** | Work → Outbox | Outbox |
| `code_tasks` | Run / Code Tasks | **Ship** | Ship → Code tasks | Code tasks |
| `delivery` | Run / CI/CD | **Ship** | Ship → Delivery | Delivery |
| `monitoring` | Run / Monitoring | **Ship** | Ship → Runtime health | Runtime health |
| `reliability` | Run / Reliability | **Ship** | Ship → Incidents & fixes | Incidents & fixes |
| `github` | Platform / GitHub | **Ship** | Ship → Repositories (+ Settings Connect shortcut) | Repositories |
| `sandboxes` | Platform / Sandboxes | **Ship** | Ship → Sandboxes | Sandboxes |
| `reports` | Run / Evidence | **Evidence** | Evidence → Evidence & reports | Evidence |
| `assurance` | Govern / Assurance | **Evidence** | Evidence → Proof gates | Proof gates |
| `budget` | Govern / Budget | **Budget** | Budget → Budget control | Budget |
| `infra` | Platform / System Configuration | **Settings** | Settings → Providers & resources | Providers & resources |
| `ops` | Govern / Operations | **Settings** | Settings → Control plane ops | Control plane ops |
| `org` | Govern / Agent Roster | **Settings** | Settings → Agent roster | Agent roster |
| `capabilities` | Platform / Capabilities | **Settings** | Settings → Tools & MCP | Tools & MCP |
| `studio` | Platform / Agent Studio | **Settings** | Settings → Agent studio | Agent studio |
| `pipeline` | Govern / Coding Pipeline | **Settings** | Settings → Coding pipeline | Coding pipeline |
| `enterprise` | Platform / Enterprise | **Settings** | Settings → Enterprise & identity | Enterprise & identity |
| `foundations` | Platform / Foundations | **Settings** | Settings → Design lab → Design tokens | Design tokens |
| `components` | Platform / Components | **Settings** | Settings → Design lab → UI components | UI components |

**Count check:** 28 AreaIds in ConsoleShell → 28 rows above. None removed.

### Deep link & compatibility

| Mechanism | Policy |
|---|---|
| `?area=<AreaId>` | Unchanged IDs |
| `localStorage boss.area` | Unchanged IDs |
| ⌘K | Match old labels, new labels, subtitles, group tags |
| AreaLink / `boss:navigate` | Unchanged IDs |
| Docs / demos saying “Approvals” | Still valid synonym for Decide |
| Tests asserting labels | Update when implementing; this panel does not change code |

---

## 8. Target IA tree (reference)

```
Home                         → overview [default app land]
  ├─ Command Center          → overview
  └─ Mission snapshot        → dashboard

Ask BOSS                     → agent

Decide                       → decisions  [badge: pending]

Work
  ├─ Task board              → work [default]
  ├─ Priorities & allocation → portfolio
  ├─ Discover outcomes       → discovery
  ├─ Agent meetings          → meetings
  ├─ Stuck work              → shepherd
  └─ Outbox                  → comms

Ship
  ├─ Code tasks              → code_tasks [default]
  ├─ Delivery                → delivery
  ├─ Repositories            → github
  ├─ Sandboxes               → sandboxes
  ├─ Runtime health          → monitoring
  └─ Incidents & fixes       → reliability

Evidence
  ├─ Evidence & reports      → reports [default]
  └─ Proof gates             → assurance

Budget                       → budget  [tone in chrome]

Settings
  ├─ Providers & resources   → infra [default settings land]
  ├─ Control plane ops       → ops
  ├─ Agent roster            → org
  ├─ Tools & MCP             → capabilities
  ├─ Agent studio            → studio
  ├─ Coding pipeline         → pipeline
  ├─ Enterprise & identity   → enterprise  [badge: Enterprise]
  └─ Design lab (collapsed)
        ├─ Design tokens     → foundations
        └─ UI components     → components
```

---

## 9. Implementation guidance (for a later PR — not this panel)

Non-binding notes for whoever implements ConsoleShell changes:

1. Extend `AreaDef` with `primary: PrimaryId`, `parent?: AreaId`, `navLabel`, `synonyms[]`, `disclosure: 'default' | 'setup' | 'enterprise' | 'advanced'`.
2. Keep `group: Platform|Run|Govern` temporarily as deprecated tags for palette until tests migrate.
3. Render `GROUP_ORDER` replacement as `PRIMARY_ORDER` (8) + `childrenByPrimary`.
4. Preserve badge hooks on `decisions` and `ops` deadletters; optionally aggregate on Home.
5. Add Settings index section component (can live inside `infra` or a thin wrapper without new backend).
6. Update empty-state copy only where labels change mid-sentence.
7. Ship IA change as **one reviewable PR** with visual snapshot + nav unit test for full AreaId coverage.

**Out of scope for IA panel:** backend routes, edition mounting, deleting panels, merging React trees.

---

## 10. Risks & open choices

| Risk / choice | Recommendation |
|---|---|
| Power users lose “I can see everything” | ⌘K + Settings expanded tree + optional “Classic flat nav” preference (P2) |
| `reliability` dual home | Single parent Ship; Evidence deep-links |
| GitHub under Ship vs Settings | Ship primary; Settings only as Connect shortcut |
| Home vs Mission Dashboard duplication | Prefer widgets on Home; keep `dashboard` tab for density |
| “Decide” vs “Approvals” brand | Nav = Decide; H1/palette synonyms include Approvals |
| Communications under Work | Correct for send path; policy knobs later under Settings if needed |
| Design lab discovery for designers | Bookmark + advanced flag; not GA primary |

---

## 11. Success criteria (IA)

- Founder can answer in ≤3 clicks: **What needs my decision? What is stuck? What did we spend? What shipped?**
- Community first-run does not present Enterprise/Design system as equal peers to Decide/Work.
- All 28 `AreaId`s remain deep-linkable and palette-findable.
- Nav story matches marketing loop: **Outcome → Plan → Work → Evidence → Decide** (with Budget + Settings as control planes).
- No functionality removed; only **information scent** and **progressive disclosure** change.

---

## 12. Relationship to other GA panels

| Panel | Relationship |
|---|---|
| 06 Mission Control UX | Edition-aware nav + empty honesty → this IA operationalizes structure |
| 13 Edition packaging | Enterprise secondary; Community ProviderOps stays Settings-first-class |
| Public site UX audit | Same simple loop language in product chrome |
| 15 Agent Studio A2A | Studio remains Settings power feature until catalog is non-empty by default |

---

## 13. Summary recommendation (one paragraph)

Re-anchor Mission Control on **eight founder jobs**—Home, Ask BOSS, Decide, Work, Ship, Evidence, Budget, Settings—mapped to the product loop, and nest all twenty-eight existing areas under those jobs without dropping a single `AreaId`. Rename for verbs and outcomes (Decide, Discover outcomes, Delivery, Proof gates, Providers & resources), demote Enterprise and the design system to badged/collapsed Settings, and use edition, empty-state, and advanced disclosure so Community GA reads as a governed work OS rather than an inventory of shells.
