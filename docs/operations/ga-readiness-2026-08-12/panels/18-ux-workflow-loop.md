# Panel 18 — UX Workflow Loop: Journey Hubs, Deep-Links, Current-Work Rail

**Date:** 2026-08-12  
**Role:** Workflow UX Expert  
**Scope:** Reorganize Mission Control so one job can be followed end-to-end without hunting ~14 Run nav items  
**Product loop:** **Outcome → Plan → Work → Evidence → Decide**  
**Substrate (always available, never the main story):** Factory / GitHub / Sandbox  
**Primary code:** `apps/web/app/ConsoleShell.tsx`, `area-link.tsx`, area `*-ui.tsx` modules  
**Related panels:** [06-mission-control-ux](06-mission-control-ux.md), [04-github-factory](04-github-factory.md), [05-sandbox-delivery](05-sandbox-delivery.md), public GTM simple-loop framing  

---

## Executive verdict

Mission Control today is a **flat subsystem catalog** (14+ Run areas plus Govern/Platform). Operators who want to finish *one job* must leave the job context, scan labels, and re-orient on each panel.

**Recommendation:** keep every existing area (no deletes), but reframe primary navigation around **five journey hubs** that mirror the product loop, plus a persistent **Current Work rail** and **entity deep-links** that carry job identity across hubs. Flat area IDs remain deep-link destinations and ⌘K targets; they stop competing as equal top-level peers.

| Grade | Meaning for this panel |
|---|---|
| **Ship UX intent** | Operators follow Outcome→…→Decide without learning 14 product names first |
| **Do not** | Delete areas, hide substrate permanently, or invent a second orchestration UI |

---

## Problem (why 14 Run items fail the loop)

| Symptom | Why it hurts |
|---|---|
| Run group is a laundry list | Discovery, Meetings, Code Tasks, CI/CD, Shepherd, Reliability, Evidence… are *stages of the same job*, not parallel products |
| No “where am I in the loop?” | User lands on Code Tasks with no Outcome/Plan/Decision crumbs |
| Job identity does not travel | `?area=` switches panels; `task_id` exists for Metatron but is not a console-wide focus contract |
| Evidence and Decide sit far from Work | Approvals and Evidence are easy to miss mid-job; operators bounce to Command Center instead |
| Substrate competes with loop | GitHub / Sandboxes / Pipeline feel like peer destinations rather than “execution plane under Work” |
| Command Center + Dashboard + Agent overlap | Three “start here” surfaces without a single job spine |

This is a **information architecture** problem, not a missing feature set. Panel 06 already grades chrome as Works; the gap is **journey continuity**.

---

## Design principles

1. **Navigate the loop, not the subsystem.** Primary nav = stages; secondary = tools inside the stage.
2. **One focused job at a time.** The Current Work rail always shows the active Outcome / Task / Decision / Code task / Deploy chain.
3. **Deep-link is the system of record for focus.** URL carries `area` + entity ids; shareable, back-button safe, ⌘K addressable.
4. **Nothing is deleted.** Every current `AreaId` remains a route target, palette hit, and optional pin.
5. **Substrate is subordinate.** Factory / GitHub / Sandbox appear as *attachments to Work*, not as peer chapters of the company OS story.
6. **Decide is a first-class stage.** Approvals are not a side inbox; they are the gate between Work and durable Evidence.
7. **Honest empty stages.** A hub with no configured substrate shows setup cards (align Panel 06), not a dead fleet.

---

## Product loop → journey hubs

### Hub map (primary sidebar: 5 hubs + 2 always-on anchors)

```
┌─────────────────────────────────────────────────────────────────┐
│  ANCHORS (always)                                               │
│  · Pulse          overview + dashboard (situation, not a job)   │
│  · Direct         agent (chat entry that *opens* jobs)          │
├─────────────────────────────────────────────────────────────────┤
│  JOURNEY HUBS (product loop)                                    │
│  1. Outcome       discovery · meetings                          │
│  2. Plan          portfolio (+ plan artifacts on task)          │
│  3. Work          work · code_tasks · shepherd                  │
│     └ substrate   delivery · github · sandboxes · pipeline      │
│  4. Evidence      reports · assurance · (receipts strip)        │
│  5. Decide        decisions  (and return-to-Work CTAs)          │
├─────────────────────────────────────────────────────────────────┤
│  OPERATE (post-ship / keep-alive — not mid-build default)       │
│  · monitoring · reliability · ops (govern)                      │
├─────────────────────────────────────────────────────────────────┤
│  GOVERN / PLATFORM (collapsed drawers — unchanged inventory)    │
│  · budget · comms · org · infra · capabilities · studio · …     │
└─────────────────────────────────────────────────────────────────┘
```

### Stage definitions (operator language)

| Hub | Stage question | Success looks like | Primary CTAs |
|---|---|---|---|
| **Pulse** | What changed / what needs me? | Risks, pending decisions, budget tone, next actions | Open pending Decision; Resume focused job |
| **Direct** | What should we do? | Chat / command becomes Task + lineage | Propose command; “Open in Work” |
| **Outcome** | What are we trying to achieve? | Discovery dossier or Meeting → candidate outcomes | Promote outcome; Start meeting panel |
| **Plan** | What is ranked, funded, and bound? | Portfolio rank, quota, tracker ticket, plan steps | Allocate; open Work item |
| **Work** | What is moving right now? | Task board card + jobs + code task + shepherd notes | Advance task; open Code Task; unblock |
| **Evidence** | What proof do we have? | Receipts, scorecards, assurance case links | Open receipt; attach to Decision |
| **Decide** | What human gate is open? | Approve / reject with evidence refs | Approve; Request changes; Reject |
| **Operate** | Is the shipped thing healthy? | Monitoring targets, incidents, DLQ burn-down | Open incident; remediate |

**Loop order is default reading order**, not a hard linear FSM. Jobs re-enter Decide mid-Work, return to Plan when re-ranked, and drop into Operate after delivery.

---

## Area inventory — nothing deleted

Every current `AreaId` from `ConsoleShell.tsx` maps to a hub (or substrate drawer). **No area is removed**; labels may gain a stage eyebrow in UI.

### Run-facing areas (task focus set)

| AreaId | Today label | Hub | Role in loop | Deep-link entity keys (propose) |
|---|---|---|---|---|
| `overview` | Command Center | **Pulse** (default home) | Situation awareness; jump into loop | `risk`, `event_id` |
| `dashboard` | Mission Dashboard | **Pulse** (tab / KPI strip) | Cross-phase snapshot | (project scope only) |
| `agent` | BOSS Agent | **Direct** | Conversational intake → durable work | `thread_id`, `task_id` |
| `discovery` | Discovery | **Outcome** | Signal → cited dossier → candidate outcomes | `discovery_id`, `outcome_index` |
| `meetings` | Meetings | **Outcome** | Multi-agent debate → PRD / outcomes | `meeting_id`, `room_id` |
| `portfolio` | Work Allocation | **Plan** | Rank, quota, leases, tickets, delivery presence | `work_item_id`, `allocation_id` |
| `work` | Task Board | **Work** (spine) | Tasks, artifacts, activity — canonical job board | `task_id`, `job_id` |
| `code_tasks` | Code Tasks | **Work** → Factory lane | Sandboxed code changes → gated PR | `code_task_id`, `task_id` |
| `shepherd` | Task Shepherd | **Work** → Unblock lane | Reasoning about stuck work | `task_id`, `shepherd_run_id` |
| `delivery` | CI/CD | **Work** → **Ship substrate** | Code task → PR → deploy stop-points | `deploy_target_id`, `delivery_run_id`, `task_id` |
| `monitoring` | Monitoring | **Operate** | Health of deployed apps | `target_id`, `alert_id` |
| `reliability` | Reliability | **Operate** | Incidents, RCA, governed fixes | `incident_id`, `task_id` |
| `reports` | Evidence | **Evidence** | Throughput, receipts, cost, scorecards | `receipt_id`, `report_period` |
| `decisions` | Approvals | **Decide** | Explicit approvals before effects | `decision_id`, `task_id` |

### Govern areas (stay Govern drawer; linked from loop when relevant)

| AreaId | Label | Loop attachment | Notes |
|---|---|---|---|
| `budget` | Budget | Side of **Plan** + **Work** | Cap pressure chip always in shell; full panel in Govern |
| `pipeline` | Coding Pipeline | **Work** substrate (VAG profiles) | Profiles/tiers for code factory; not a Run peer |
| `comms` | Communications | After **Decide** / external effect | Outbox discipline before send |
| `ops` | Operations | **Operate** | Deadletters, audit, replay; SIM enterprise-gated |
| `assurance` | Assurance | **Evidence** secondary | Proof gates / cases — pair with reports |
| `org` | Agent Roster | **Plan** / staffing context | “Who is on this job” from Current Work rail |

### Platform / substrate / chrome (stay Platform drawer)

| AreaId | Label | Loop attachment | Notes |
|---|---|---|---|
| `github` | GitHub | **Work** substrate | Branch truth, sync, merge — open from Code Task / Delivery |
| `sandboxes` | Sandboxes | **Work** substrate | Execution plane under Code Tasks |
| `infra` | System Configuration | Setup, not daily loop | ProviderOps / FinOps |
| `capabilities` | Capabilities | Setup / Direct tool audit | Skills & MCP |
| `studio` | Agent Studio | Authoring plane | Build agents/panels/workflows; not mid-job default |
| `enterprise` | Enterprise | Edition / identity | Badge/gate per Panel 06 |
| `foundations` | Foundations | Design system | Dev/design only; collapse under Platform → Design |
| `components` | Components | Design system | Same |

### Substrate rule (Factory / GitHub / Sandbox)

Treat as a **single “Ship & Execute plane” strip** under the Work hub:

```
Work hub
├── Board          (work)
├── Code           (code_tasks)
├── Unblock        (shepherd)
└── Ship plane     [ CI/CD · GitHub · Sandboxes · Pipeline ]
```

Operators mid-build click **Ship plane** tabs without leaving the focused `task_id` / `code_task_id`. Platform nav still lists full GitHub MC / Sandboxes for admin setup.

---

## Current Work rail (persistent, job-centric)

### Placement

- **Desktop:** right rail (or left of main content under the hub header), ~280–320px, collapsible.
- **Mobile:** bottom sheet “This job” when a focus entity is set; otherwise hidden.
- **Always visible** when `focus` query (below) is non-empty; on Pulse, show “Needs you” shortlist instead.

### Rail contents (ordered)

1. **Focus identity** — Outcome title or Task title · stable mono id · state text (not pill soup)
2. **Loop stepper** — compact Outcome → Plan → Work → Evidence → Decide with current stage filled; clickable to hub with same focus
3. **Now** — current job/code-task/deploy line + owner agent orb
4. **Blockers** — pending Decision count, shepherd note, DLQ/deadletter link, budget hold
5. **Proof strip** — last N receipts / verification checks (link into Evidence)
6. **Substrate chips** — GitHub PR, sandbox, deploy target (only if bound)
7. **Actions** — Approve (if decision open), Open board card, Open code task, View evidence pack

### Focus contract (URL)

Extend existing `?area=` deep-links (already in `ConsoleShell` + `AreaLink`) with a shared focus bag. **Proposed query keys** (additive; ignore unknown for back-compat):

| Param | Meaning |
|---|---|
| `area` | Hub destination area (existing) |
| `task_id` | Canonical job spine (already used by Metatron) |
| `decision_id` | Open approval |
| `code_task_id` | Factory instance |
| `discovery_id` / `meeting_id` | Outcome sources |
| `work_item_id` | Portfolio row |
| `receipt_id` | Evidence anchor |
| `incident_id` | Operate path |
| `focus` | Optional alias: `task:<id>` when only one id is needed |

**Rules:**

- Changing hub **preserves** focus params (only `area` changes).
- `AreaLink` gains optional focus props → encoded in `href` + `boss:navigate` detail payload `{ area, focus }`.
- ⌘K results include “Resume: {task title}” when focus is set.
- Clearing focus returns to Pulse “Needs you” mode.

### Example end-to-end without hunting

1. Pulse: “3 high-risk decisions · 1 stuck code task” → **Resume task_abc**
2. Rail loads task_abc; stepper shows **Work** active, **Decide** badge = 1
3. Work hub Board highlights task_abc; jobs list filtered
4. Open Code lane → same task_abc code task; sandbox chip live
5. Ship plane → CI/CD row for that PR; still task_abc
6. Evidence hub → receipts for task_abc
7. Decide hub → decision_id filtered to task_abc → Approve
8. Stepper advances; Operate only if deploy target exists

No need to scan 14 sidebar labels mid-flight.

---

## Deep-link matrix (stage transitions)

Every primary object must offer **“Continue loop”** links, not only “Open area”.

| From | Next links (with focus preserved) |
|---|---|
| Discovery promote | → Plan (`portfolio`) + Work (`work&task_id=`) |
| Meeting conclusion | → Outcome summary + Plan + open PRD artifact |
| Portfolio allocate | → Work board card |
| Task blocked on approval | → Decide (`decisions&decision_id=`) |
| Task ready for code | → Work / Code (`code_tasks&code_task_id=`) |
| Code task PR opened | → Ship / GitHub + Delivery |
| Delivery stopped | → Work / Shepherd or Decide (policy gate) |
| Receipt written | → Evidence (`reports&receipt_id=`) |
| Decision approved | → Work (resume) or Evidence (pack) |
| Deploy healthy | → Operate / Monitoring |
| Incident opened | → Operate / Reliability → may spawn Task (back to Work) |

Implement with shared `buildConsoleHref({ area, ...focus })` used by Overview, rail, and panel rows (replace ad-hoc strings).

---

## Hub page composition (IA, not full redesign)

Each hub is a **thin shell** over existing panels (reuse `*-ui.tsx`), not a rewrite:

| Hub | Default view | Secondary tabs (existing areas) |
|---|---|---|
| Pulse | Command Center primary; Dashboard KPI strip pinned top | Overview · Dashboard |
| Direct | Agent chat full height; “created tasks” side list | Agent only |
| Outcome | Discovery list default | Discovery · Meetings |
| Plan | Portfolio / allocation | Portfolio · (Budget chip → Govern) |
| Work | Task Board default | Board · Code · Unblock · Ship plane |
| Evidence | Reports / receipts | Evidence · Assurance |
| Decide | Approvals inbox **actionable-only default** | Decisions |
| Operate | Monitoring if targets else Reliability/DLQ | Monitoring · Reliability · Ops |

**Nav badge policy (reuse shell badges):**

- Decide: pending decisions (existing)
- Work: stuck/shepherd or open jobs (optional)
- Operate: open incidents + deadletters (merge ops badge)

---

## Sidebar information architecture (proposed)

```
PULSE
  Command Center          area=overview
  Mission Dashboard       area=dashboard     [secondary]

DIRECT
  BOSS Agent              area=agent

OUTCOME
  Discovery               area=discovery
  Meetings                area=meetings

PLAN
  Work Allocation         area=portfolio

WORK
  Task Board              area=work
  Code Tasks              area=code_tasks
  Task Shepherd           area=shepherd
  ── Ship plane ──
  CI/CD                   area=delivery
  GitHub                  area=github        [also Platform]
  Sandboxes               area=sandboxes     [also Platform]
  Coding Pipeline         area=pipeline      [also Govern]

EVIDENCE
  Evidence                area=reports
  Assurance               area=assurance     [also Govern]

DECIDE
  Approvals               area=decisions

OPERATE
  Monitoring              area=monitoring
  Reliability             area=reliability
  Operations              area=ops           [Govern full panel]

GOVERN (collapsed)
  Budget, Communications, Agent Roster, …

PLATFORM (collapsed)
  System Configuration, Capabilities, Agent Studio,
  Enterprise, Foundations, Components
```

**Count comparison:** primary visible journey destinations ≈ **8–10** (with Ship plane nested), vs **14 flat Run peers**. Full inventory remains reachable.

---

## Interaction patterns

### Loop stepper (hub header)

- Horizontal five-step control under hub title.
- Current stage = solid; completed = check text; blocked = coral status text.
- Clicking a stage navigates hub **with focus preserved**.
- Optional sixth “Operate” only when deploy/incident context exists.

### Cross-links in panels

- Every task/job/decision row: **Open in Work**, **Evidence**, **Decide** as text links (AreaLink + focus).
- Overview “next actions” must deep-link into focused hubs, not only switch `area`.

### ⌘K palette

- Group results: **Resume job** · **Journey hubs** · **All areas** · **Entities** (task/decision ids).
- Keep every AreaId searchable by today’s labels for power users.

### Edition honesty (align Panel 06)

- Enterprise / Studio / Assurance empty → setup or edition badge **inside hub**, not fake fleet.
- Community: Ship plane shows GitHub easy-connect / sandbox host-down honesty without leaving Work focus.

---

## What not to do

| Anti-pattern | Why |
|---|---|
| Delete “low use” areas (Shepherd, Reliability, Meetings) | Breaks PRD loops; hide under hub instead |
| One infinite scroll mega-page of all 14 | Perf and cognitive load already SSR-heavy (Panel 06) |
| Chat as system of record | PRD: durable state wins; Direct only *creates* focus |
| Separate “Workflow” product parallel to Work | Duplicates Task Board / Workflow Runtime Studio |
| Force linear wizard for every job | Re-entry, incidents, and approvals are non-linear |
| Lose `?area=` ids | Breaks bookmarks, tests, and agent-written links |

---

## Implementation phasing (UX engineering order)

### Phase A — Focus + deep-links (highest leverage, small chrome)

1. `buildConsoleHref` + extend `AreaLink` / `boss:navigate` with focus payload  
2. Preserve query params on `go(area)` in `ConsoleShell`  
3. Honor `task_id` / `decision_id` / `code_task_id` filters in Work, Decide, Code Tasks UIs  
4. Overview next-actions emit focused links  

**Exit:** share `/?area=decisions&task_id=…` opens filtered inbox; switching to Work keeps task_id.

### Phase B — Current Work rail

1. Rail component fed by existing task/decision/code-task/receipt reads (lazy, not full SSR fan-out)  
2. Loop stepper wired to hubs  
3. Mobile bottom sheet  

**Exit:** one task can be stepped Outcome→…→Decide without losing identity.

### Phase C — Journey hub nav regroup

1. Regroup `AREAS` metadata: `hub`, `stageOrder`, `primary` flags (keep `id` stable)  
2. Sidebar renders hubs; secondary tabs inside hub shell  
3. Ship plane nested under Work  
4. Palette grouping  

**Exit:** default sidebar shows ≤10 primary entries; all AreaIds still in palette + “All tools”.

### Phase D — Panel continuity polish

1. Receipt → Decision evidence refs  
2. Shepherd unblock → same task board highlight  
3. Delivery stop → Decide or Shepherd CTA  
4. Approvals default = actionable pending only (Panel 06)  

### Phase E — Operate separation

1. Monitoring/Reliability leave the mid-build default path  
2. Post-deploy “handoff to Operate” from Delivery success  

---

## Acceptance criteria (workflow UX)

- [ ] Operator can start from Pulse or Direct and complete **Outcome → Plan → Work → Evidence → Decide** for one task using only hub nav + rail (no hunting secondary labels).  
- [ ] All 14 Run areas (plus Govern/Platform inventory) remain reachable by id, palette, and URL.  
- [ ] Focus params survive hub switches; Back button restores prior hub + focus.  
- [ ] Ship substrate (CI/CD, GitHub, Sandboxes, Pipeline) is nested under Work by default.  
- [ ] Decide and Evidence are primary hubs, not buried below Reliability.  
- [ ] Empty/unconfigured substrate shows honest setup (Panel 06), still inside Work focus.  
- [ ] No new autonomous self-modifying UX; approvals and budget gates remain explicit.  
- [ ] Tests: deep-link preserve; AreaLink focus; rail renders with fixture task; nav hub grouping snapshot.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Power users miss flat list | ⌘K + “All areas” drawer; remember last hub |
| SSR fan-out worsens with rail | Rail client-fetch by focus id only; don’t add full feeds to every page load |
| Hub rename confuses docs/GTM | Keep area labels; hubs are grouping only |
| Dual placement (GitHub in Work + Platform) | Same `area=github`; entry context differs (focused vs admin) |
| Meetings/Discovery unused on dogfood | Hub still valid; empty state → “Start discovery” CTA |

---

## Relationship to GTM “simple loop”

Public framing (Outcome → Plan → Work → Evidence → Decide) must match **Mission Control primary nav**.  
Company OS vision (roster, studio, enterprise, provider fleet) lives in **Govern/Platform drawers**, not the day-one sidebar. This panel is the console embodiment of that GTM path B promise without deleting enterprise-grade surfaces.

---

## Sources

- `apps/web/app/ConsoleShell.tsx` — `AreaId`, `AREAS`, `?area=`, palette, badges  
- `apps/web/app/area-link.tsx` — client area navigation  
- `docs/product/PRD-v2.0.md` — durable state, factory, orchestration, assurance  
- `docs/product/UI_REDESIGN_PLAN.md` — board / evidence / approvals patterns  
- Panel 06 Mission Control UX — empty states, edition honesty, chrome grade  
- `docs/operations/ga-readiness-2026-08-12/PUBLIC_SITE_UX_AUDIT.md` — simple loop GTM  

---

## Panel summary line

**Re-group, don’t delete:** five loop hubs + Pulse/Direct anchors, a persistent Current Work rail, and focus-preserving deep-links let operators finish one job end-to-end; all existing areas remain addressable under Work substrate or Govern/Platform drawers.**
