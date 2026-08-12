# Panel 19 — UX Cognitive Load / Simplification (Mission Control)

**Date:** 2026-08-12  
**Role:** Cognitive Load / Simplification Expert (not feature-cutter)  
**Scope:** Information architecture of Mission Control (`apps/web/app/ConsoleShell.tsx` AREAS + palette + topbar), role defaults, edition collapse, Focus Mode, command-palette primacy  
**Primary evidence:** live AREAS inventory (28 `AreaId`s), Panel 06 (MC UX), Panel 13 (edition packaging), shell chrome (⌘K jump palette, Run/Govern/Platform groups)  
**Non-goal:** Remove product capability, delete areas, or ship a “lite” BOSS that cannot dogfood the full control plane.

---

## 1. Verdict

| Question | Answer |
|---|---|
| Is ~28 top-level areas too many for a high-density mission console? | **Yes** as a *flat always-visible* list. Density is fine; **equal peer status for every area** is the load. |
| Must functionality be cut for GA? | **No.** Reorganize, default, role-scope, hide-until-needed, edition-collapse. |
| Hardest GA trust killers today | Design-system in production nav; Enterprise / unconfigured shells as first-class peers; no role home; palette is “jump” not “command.” |
| Recommended posture | **Progressive disclosure + command-first navigation**, with every area still reachable via palette, deep link (`?area=`), bookmarks, and a “Show all areas” / Admin unlock. |

**Panel grade for current IA:** **YELLOW — chrome works, cognitive load blocks “calm CEO morning” and confuses Community dogfood.**  
**Exit for this panel:** operators can complete their primary loop with ≤ **8 primary nav items** visible by default, without losing any area via palette/search/advanced.

---

## 2. Problem statement (load sources)

Mission Control is intentionally a **high-density control surface** (badges, live refresh, status chips). Cognitive load is not “too many pixels”; it is:

1. **Choice overload** — 28 areas presented as equal destinations (Foundations → Enterprise in one scroll).
2. **Mixed audience in one nav** — CEO approvals sit next to design tokens and SCIM.
3. **Setup / product / meta mixed** — production ops, empty shells, and DS showcase share Platform weight.
4. **Weak primary path** — ⌘K exists but only filters labels/subs; operators still *browse* the sidebar to discover work.
5. **No role memory** — actor chip shows name; nav does not adapt to CEO vs eng vs admin.
6. **Edition noise** — paid/enterprise surfaces remain visible on Community (Panel 06/13), teaching “broken settings” instead of “not in this edition.”
7. **Attention competition** — badges only on Approvals + Ops deadletters; many high-signal areas (DLQ detail, failed deploys, budget red) do not surface into a single “needs you” strip.

**Constraint from product identity:** BOSS is a financially controlled autonomous company OS. Simplification must preserve **auditability, budget gates, decisions, receipts** — not hide them behind “simple SaaS” that omits governance.

---

## 3. Current inventory (system of record for this panel)

Source: `apps/web/app/ConsoleShell.tsx` `AREAS` (28).

| Group (today) | Areas | Count |
|---|---|---|
| **Platform** | Foundations, Components, Command Center, GitHub, System Configuration, Capabilities, Agent Studio, Sandboxes, Enterprise | 9 |
| **Run** | BOSS Agent, Mission Dashboard, Approvals, Task Board, Work Allocation, Discovery, Meetings, Code Tasks, CI/CD, Monitoring, Task Shepherd, Reliability, Evidence | 13 |
| **Govern** | Budget, Coding Pipeline, Communications, Operations, Assurance, Agent Roster | 6 |

**Existing good bones (keep, extend):**

- Three groups (Run / Govern / Platform) — correct *axes*, wrong *flatness* (every item always expanded).
- ⌘K dialog, Escape, focus on open, deep link `?area=`, `localStorage` last area, `boss:navigate` events.
- Badges for pending decisions and deadletters.
- Skip link to decision inbox.
- Honest empty-state work (Panel 06) — load *after* arrival is improving; load *before* arrival (nav) is not.

**Anti-patterns to stop:**

| Anti-pattern | Why it loads users |
|---|---|
| Foundations + Components as production Platform peers | Meta tooling ≠ mission control |
| Enterprise always listed on Community | Looks broken when routes 404 |
| Command Center under Platform while “Run” is the verb | Conceptual split: home is not platform |
| Single “Operator · in control” footer for all roles | No differentiated job-to-be-done |
| Palette = area list only | Forces sidebar browsing for actions |
| Live chip tooltip “7s” vs 15s interval | Small trust tax compounds density anxiety |

---

## 4. Principles (simplification without cutting)

| # | Principle | Meaning for BOSS MC |
|---|---|---|
| P1 | **Hide-until-needed, never delete** | Every `AreaId` remains routable; deep links and palette always win over nav visibility. |
| P2 | **One primary job per role home** | Default landing + collapsed nav answer “what do I do in the next 60s?” |
| P3 | **Command palette is the full map** | Sidebar is curated; palette/search is complete + fuzzy + action-capable. |
| P4 | **Edition honesty in IA** | Community does not present paid surfaces as equal peers (badge + collapse or single Upgrades entry). |
| P5 | **Design system is dev/meta** | Not in production operator nav; still in palette under “Developer / Design”. |
| P6 | **Signal over inventory** | Nav order and badges follow *attention*, not feature marketing. |
| P7 | **Focus Mode reduces chrome, not capability** | Temporary attention tunnel; exit restores full IA. |
| P8 | **Defaults are reversible** | Role preset, pin list, “Show all” are preferences with audit-friendly local/server storage later. |
| P9 | **Group by operator mental model** | Work loop, money/risk, platform setup — not by engineering package name. |
| P10 | **Progressive density** | High-density *panels* OK; high-density *navigation* is the failure mode. |

---

## 5. Recommended patterns

### 5.1 Grouping — “hubs with drawers,” not 28 roots

Replace flat group lists with **≤ 4 hubs** in the sidebar. Each hub expands *in place* (or drills) to child areas. Children remain full product surfaces — only the *tree* changes.

#### Proposed hub model

| Hub | Operator question | Default-visible children (Community CEO/ops) | Drawer / advanced children (always in palette) |
|---|---|---|---|
| **Home** | What needs me? | Command Center (overview), Mission Dashboard | — |
| **Work** | What is moving? | Approvals, Task Board, Code Tasks, BOSS Agent | Work Allocation, Discovery, Meetings, Task Shepherd, CI/CD, Monitoring, Reliability, Evidence, Communications |
| **Money & risk** | What can we spend / prove? | Budget, Operations (DLQ/audit) | Coding Pipeline, Assurance, Agent Roster |
| **Platform** | How is the machine wired? | GitHub, System Configuration (BYOK) | Capabilities, Agent Studio, Sandboxes, Enterprise†, Foundations‡, Components‡ |

† Edition-aware: collapsed/badged on Community.  
‡ Meta: never default-visible in production operator role.

**GROUP_ORDER today:** Platform → Run → Govern.  
**Recommended visual order:** Home → Work → Money & risk → Platform (setup last).  
Rationale: mission control should open on *attention*, not design tokens / enterprise chrome.

#### Secondary grouping inside hubs (optional accordion labels)

- Work → **Intake** (Agent, Approvals, Discovery, Meetings)  
- Work → **Delivery** (Code Tasks, CI/CD, Sandboxes, Previews-via-Code Tasks)  
- Work → **Health** (Monitoring, Reliability, Shepherd, Evidence)  
- Platform → **Connect** (GitHub, System Configuration, Capabilities)  
- Platform → **Build agents** (Studio, Roster)  
- Platform → **Meta** (Foundations, Components) — role Admin/Designer only in sidebar  

No area is removed from the product map.

---

### 5.2 Defaults — land people on signal, not inventory

| Default | Recommendation | Notes |
|---|---|---|
| First visit landing | `overview` (Command Center) | Already close; ensure `initialArea` / empty `boss.area` prefers overview over foundations (AREAS[0] is currently **foundations** — fix default fallback). |
| AREAS array order | Do not use array index 0 as product default | Separate `DEFAULT_AREA` constant from design-system entries. |
| After auth / org pick | Role preset home (below) | Override only if `?area=` present. |
| Live refresh | On by default; pause in Focus Mode | Already live=true; Focus Mode should force pause or lengthen interval. |
| Sidebar | Collapsed hubs; expand hub of active area | Persist expanded hub ids in `localStorage`. |
| “Pinned” areas | Up to 5 user pins above hubs | Pins are the personalization escape hatch without showing 28. |

**Critical bug-class for load:**  
`activeDef = AREAS.find(...) ?? AREAS[0]` with Foundations first means any bad id falls into design system. **Default fallback must be `overview`.**

---

### 5.3 Roles — CEO vs eng vs admin (presets, not hard RBAC rewrite)

Treat **nav role** as a *presentation preset* layered on existing actor/auth. Do not wait for full enterprise RBAC to ship simplification. Start with local preference + optional claim from `/v1/edition` / membership role_keys when present.

#### Role presets (sidebar primary set)

| Preset | Primary (always visible) | Secondary (hub drawer) | Hidden from sidebar (palette only) | Default home |
|---|---|---|---|---|
| **CEO / Owner** | Home, Approvals, Budget, Task Board, Operations (badge), Evidence | Agent, Dashboard, Portfolio, Comms | Studio, Foundations, Components, deep infra tabs | `overview` or `decisions` if pending > 0 |
| **Engineering lead** | Home, Code Tasks, CI/CD, GitHub, Approvals, Monitoring | Shepherd, Reliability, Sandboxes, Pipeline, Task Board | Enterprise SCIM, Foundations | `code_tasks` or `delivery` |
| **Platform admin** | Home, System Configuration, GitHub, Capabilities, Operations, Sandboxes | Enterprise†, Budget, Org/Roster | Foundations/Components still palette; optional pin | `infra` |
| **Agent builder** | Home, Agent Studio, Roster, Capabilities, Meetings, Agent | Assurance, Pipeline | Foundations (palette) | `studio` |
| **Full / Power** | All hubs expanded | — | Nothing hidden | `overview` |

† Enterprise children: edition-gated (see §5.8).

**Rules:**

1. Switching preset never 404s an area — it only changes sidebar density.  
2. “Show all areas” toggle always available (footer of sidebar).  
3. Deep link `?area=assurance` opens Assurance even if hidden in sidebar (expand hub + highlight).  
4. Later: map org membership `role_keys` → preset suggestion (not forced).

---

### 5.4 Command palette as primary navigation

Elevate ⌘K from **“Jump to area”** to **Command palette (primary map of the product)**.

#### Palette information architecture

| Tier | Examples | Behavior |
|---|---|---|
| **Actions** (priority when query empty + morning) | “Review pending approvals”, “Quote budget”, “Open deadletters”, “Create code task”, “Pause live refresh” | Runs action or navigates + focuses control |
| **Needs attention** | Dynamic from status chips: N decisions, N deadletters, budget red | Same as badges but actionable |
| **Areas** | All 28 areas + synonyms | Current filter + rank by role affinity |
| **Entities** (phase 2) | Task id, decision id, provider account, PR | Deep link into panel with query params |
| **Help / meta** | “Keyboard shortcuts”, “Edition status”, “Design tokens” | Foundations/Components live here for operators |

#### Ranking

1. Exact label match  
2. Role affinity (CEO: Approvals before Components)  
3. Badge weight (pending decisions, deadletters, budget tone)  
4. Recency (`boss.area` history stack, last 8)  
5. Alphabetical fallback  

#### UX requirements (a11y)

- Full keyboard: ↑↓, Enter, Esc, Tab trap inside dialog  
- `aria-activedescendant` or roving tabindex (today: first item CSS “active” only)  
- Group headers in list: Actions / Attention / Areas / Meta  
- Empty query shows **Attention + role primaries**, not Foundations first  
- Synonyms dictionary: e.g. “DLQ” → Operations, “money” → Budget, “PR” → Code Tasks / GitHub, “DS” → Foundations  

#### Sidebar becomes secondary

- Teach once: empty Command Center tip “Press ⌘K to go anywhere.”  
- Topbar “Jump to” → rename **“Command”** / keep ⌘K.  
- Mobile: palette remains full map when hamburger is cramped.

---

### 5.5 Search — one box, two modes

| Mode | Trigger | Scope |
|---|---|---|
| **Navigate** | Palette default | Areas, actions, synonyms |
| **Find in system** | Prefix `?` or tab “Search records” | Tasks, decisions, receipts, events (API-backed, debounced) |

Do not add a second competing search in the sidebar. One command box; optional mode toggle inside the dialog.

**Honesty:** if record search is not implemented yet, show “Record search — coming; try area jump” rather than a dead field. Prefer **ship navigate well** before entity search.

---

### 5.6 Focus Mode

**Purpose:** Reduce chrome during an approval sprint, incident, or code-task watch — without removing routes.

| Focus Mode ON | Behavior |
|---|---|
| Sidebar | Collapsed to icons-only **or** hidden; current hub + pins only |
| Topbar | Minimal: title, Live/Paused, Command, budget tone |
| Live refresh | Paused by default (or 60s) to cut SSR fan-out anxiety |
| Palette | Still full map (escape hatch) |
| Banner | “Focus Mode — showing Work · Approvals context” + Exit |
| Optional scope | Pin 1–3 areas for the session (e.g. Approvals + Budget + Ops) |

| Focus Mode OFF | Full role preset restored |

**Entry points:** topbar toggle; palette action “Enter Focus Mode”; auto-suggest when pending decisions ≥ N (dismissible).

**Non-goal:** Focus Mode is not a permissions boundary.

---

### 5.7 Hide design system from production nav

| Surface | Production operator (CEO/eng/admin presets) | Power / Designer / `NODE_ENV`/flag |
|---|---|---|
| Sidebar | **Hidden** | Visible under Platform → Meta |
| Palette | Under group **Meta / Design system** | Same + boost rank if preset=Full |
| Deep link `?area=foundations` / `components` | **Works** | Works |
| Marketing / docs screenshots | May show DS; product nav defaults do not | — |

**Rationale:** Foundations + Components are internal quality tools. Listing them first in `AREAS` teaches the wrong mental model and makes default fallback dangerous.

**Implementation sketch:**

```text
visibility: "production" | "meta" | "edition:enterprise" | "always"
```

Meta areas: `foundations`, `components`.  
Still first-class in palette group “Design system”.

---

### 5.8 Edition-aware collapse

Align nav with Panel 13 packaging honesty.

| Edition | Enterprise area | SIM / self-improvement inside Ops | Marketplace / Account Connect widgets | Custom pipeline profiles |
|---|---|---|---|---|
| **Community** | Single nav row **“Enterprise & Cloud”** → upgrade/honesty panel only; **or** hide row + palette entry “Enterprise (not in Community)” | Already enterpriseOnly pattern — keep; no dead CTAs | Do not show as broken Settings | Badge “Enterprise only” (exists) |
| **Cloud / Enterprise** | Full children | Full | Full when linked | Full |

**Collapse patterns (pick one primary, support both):**

1. **Hide + palette** — cleanest Community IA.  
2. **Badge + lock row** — educates upsell without looking like outage.  
3. **Single Upgrades surface** — one Platform item absorbing SCIM/AC/marketplace chrome.

**Rule:** never show a form that will 404 as if it were local config (Panel 06 dead surfaces). Prefer one honest panel over five dead forms.

**System Configuration:** Community BYOK is real — keep visible for admin/eng; do not hide the kernel. Only hide *paid-only* subflows.

---

### 5.9 Attention model (complements grouping)

Without cutting features, **reorder and badge** for signal:

| Signal | Nav / palette treatment |
|---|---|
| `pendingDecisions > 0` | Badge Approvals; CEO home → decisions |
| `deadletters > 0` | Badge Operations (exists) |
| Budget tone red/amber | Tone chip + palette attention item |
| Control plane offline | Sticky banner; disable destructive CTAs visually |
| Unconfigured GitHub / sandbox host | Soft badge “Setup” on Platform children (not red error) |
| Empty Assurance / Studio | “Setup” or hide under Advanced until seeded — still palette-reachable |

Setup badges ≠ error badges. Language: **“Not configured”** vs **“Failing”**.

---

### 5.10 Density inside panels (secondary, same philosophy)

Nav simplification fails if every area is a wall of equal cards. Apply the same patterns *inside* large panels:

- Sticky **primary action** strip (one default verb).  
- Advanced forms behind `<details>` / “Advanced”.  
- Tables: default columns by role (CEO: status/cost; eng: logs/PR).  
- Empty states already improving (Panel 06) — keep honesty.

Out of scope for mandatory nav work, but same cognitive model.

---

## 6. Target information architecture (reference map)

All IDs remain valid deep links.

```text
HOME
  overview          Command Center
  dashboard         Mission Dashboard

WORK
  decisions         Approvals                 [badge: pending]
  work              Task Board
  agent             BOSS Agent
  code_tasks        Code Tasks
  ── drawer / advanced ──
  portfolio         Work Allocation
  discovery         Discovery
  meetings          Meetings
  delivery          CI/CD
  monitoring        Monitoring
  shepherd          Task Shepherd
  reliability       Reliability
  reports           Evidence
  comms             Communications

MONEY & RISK (Govern)
  budget            Budget
  ops               Operations                [badge: deadletters]
  ── drawer ──
  pipeline          Coding Pipeline
  assurance         Assurance
  org               Agent Roster

PLATFORM
  github            GitHub
  infra             System Configuration
  ── drawer ──
  capabilities      Capabilities
  studio            Agent Studio
  sandboxes         Sandboxes
  enterprise        Enterprise                [edition collapse]
  ── meta (palette / power only) ──
  foundations       Foundations               [hide production nav]
  components        Components                [hide production nav]
```

**Default visible count (CEO preset):** ~8–10 rows including hub labels — down from 28 peers.

---

## 7. Interaction design notes

| Element | Recommendation |
|---|---|
| Hub labels | Not clickable destinations; expand/collapse only (or click = open hub overview later) |
| Active child in collapsed hub | Auto-expand parent hub |
| Pins | Drag reorder; stored `boss.nav.pins` |
| Role preset | Stored `boss.nav.role`; switcher in sidebar foot near actor |
| Show all | Boolean `boss.nav.showAll`; forces Full map in sidebar |
| Focus Mode | `boss.nav.focus`; session-first, optional persist |
| Responsive | Hamburger already; on small screens default to palette-primary, icon rail optional |
| Performance | Collapsing nav does not by itself fix SSR fan-out; pair with **lazy panel mount** for inactive areas (separate eng task, huge load win) |

**SSR fan-out note (load adjacent):** Panel 06 notes ~full page fan-out on refresh. Cognitive load of *waiting* compounds nav load. Recommend: visible area fetch only + palette-prefetch. Not a nav IA cut — an architecture enabler for calm UX.

---

## 8. Phased delivery (no big-bang rewrite)

### Phase A — Calm defaults (P0 for Community preview honesty)

1. `DEFAULT_AREA = "overview"`; stop Foundations as `AREAS[0]` fallback.  
2. Move Foundations + Components to end; mark `meta: true`; **omit from sidebar** unless `showAll` or role Full.  
3. Edition-collapse Enterprise on Community (hide or single honesty row).  
4. Rename palette placeholder to “Command or jump…”.  
5. Empty-query palette: Attention items + role primaries (even if role is hard-coded CEO first).  
6. Fix Live tooltip 7s → 15s.

**Acceptance:** Community operator opens MC → sees ≤ ~15 sidebar rows without meta/enterprise noise; `?area=foundations` still works.

### Phase B — Hubs + role presets (P1)

1. Hub accordion model in ConsoleShell.  
2. Role preset switcher (CEO / Eng / Admin / Builder / Full).  
3. Pins (max 5).  
4. Focus Mode v1 (chrome collapse + live pause).  
5. Palette keyboard roving + group headers.

**Acceptance:** CEO preset ≤ 8 primary destinations; eng can reach Sandboxes in ≤ 2 clicks or 1 palette query.

### Phase C — Command palette actions + search (P1/P2)

1. Static actions registry (navigate + focus selectors).  
2. Synonym map.  
3. Optional entity search behind API.  
4. Command Center “Start here” using same attention feed as palette.

### Phase D — Server preferences (P2)

1. Persist role/pins per actor/org when membership APIs stable.  
2. Suggest preset from `role_keys`.  
3. Audit “nav preset changed” only if required by enterprise compliance (usually local is enough).

---

## 9. What we explicitly will not do

| Temptation | Why reject |
|---|---|
| Delete Assurance / Studio / Meetings for GA | Product map must stay complete; dogfood and enterprise paths need them |
| Merge Code Tasks + CI/CD + Sandboxes into one mega-page | Increases panel density; use hubs instead |
| Hard-remove Enterprise routes from web bundle on Community | Deep links and upgrade education; use collapse + honesty panel |
| Replace Mission Control with a single chatbot | Agent is a *Run* surface, not a replacement for decisions/budget/receipts UI |
| Auto-hide any area with empty data forever | Empty ≠ irrelevant; use Setup badge + Command Center CTAs |
| Different product URLs per role | One console; presentation presets only |

---

## 10. Metrics / acceptance (cognitive load)

Qualitative + lightweight quantitative gates:

| Metric | Target |
|---|---|
| Default sidebar leaf count (CEO, Community) | ≤ 8 primary + hub labels |
| Time-to-Approvals from cold load | ≤ 1 click or ⌘K + “ap” + Enter |
| Reach Foundations without Show all | Palette or deep link only (by design) |
| Community: Enterprise forms 404 as peer settings | **0** (honesty panel only) |
| Palette covers all AreaIds | 100% |
| Focus Mode exit restores pins/role | 100% |
| No feature flag that 404s a previously linked `?area=` | Stable deep links forever |
| A11y: palette operable keyboard-only | Required |

User research proxy for dogfood: five scripted tasks (approve, check budget, open DLQ, start code task, open design tokens) — tokens must not appear in CEO sidebar path.

---

## 11. Cross-panel dependencies

| Panel | Relationship |
|---|---|
| **06 Mission Control UX** | Empty-state honesty + hydration; this panel fixes **pre-arrival** load (nav/palette). |
| **13 Edition packaging** | Edition-aware collapse must match route mounting (404 vs soft gate). |
| **14 Ops observability** | Operations badge + Focus Mode during incidents. |
| **15 Agent Studio** | Studio stays full capability; default under Platform drawer / Builder preset. |
| **Scorecard P0-7** | Honest GTM claims pair with honest IA (no enterprise chrome as Community peers). |

---

## 12. Implementation touchpoints (for eng, not doing work here)

| File / area | Change class |
|---|---|
| `apps/web/app/ConsoleShell.tsx` | AREAS metadata (`hub`, `visibility`, `roles`, `synonyms`), DEFAULT_AREA, hub render, Focus Mode, palette groups |
| `apps/web` CSS (`.bx-nav*`) | Nested nav, pins, focus-mode shell class |
| Palette component (extract if growing) | Actions registry, ranking, a11y roving |
| Tests | `accessibility.test.ts` (palette dialog); new nav-visibility unit tests per edition + role |
| Docs | Short operator note: “⌘K is the map; sidebar is the preset” |

**Hard rules preserved:** no external calls from nav; badges only from already-fetched shell status; no silent drop of deep links.

---

## 13. Executive checklist for GA readiness (this panel)

- [ ] Design system **not** in default production sidebar  
- [ ] `DEFAULT_AREA` / fallback is Command Center, not Foundations  
- [ ] Community edition collapses Enterprise to honesty/upsell, not dead forms  
- [ ] Role preset or Show-all path documented  
- [ ] ⌘K lists **all** areas + attention actions; keyboard complete  
- [ ] Focus Mode available for approval/incident sprints  
- [ ] Deep links for all 28 areas remain valid  
- [ ] No capability removed — only reorganized / hidden-until-needed  

---

## 14. Summary judgment

BOSS Mission Control should feel like a **sentinel console**: dense when you are inside a panel, **quiet and role-shaped** when you choose where to go. Twenty-eight capabilities is a strength of the product map; twenty-eight equal sidebar peers is a cognitive failure. The fix is progressive disclosure — hubs, defaults, roles, command-first palette, Focus Mode, meta hide, edition collapse — **without deleting a single governed workflow**.
