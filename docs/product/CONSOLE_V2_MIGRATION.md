# Console v2 — migration plan

Status: **draft for review**. Nothing is built yet.
Branch: `feat/console-v2` (from `main` @ `09ecd89`, all current fixes merged).
Reference: `docs/product/console-v2-reference/` — the artifact's saved HTML and extracted
`app.js`, read directly rather than inferred from screenshots.

---

## 1. The headline

The artifact is **not a restyle**. The paint is close to today's console — Sean is right that
style, font and format look similar. What changes is the **subject of the product**.

Today's console is organised around **stages of work**: 33 areas, one per phase or system —
Discovery, Meetings, Code Tasks, CI/CD, Observability, Budget…

The artifact is organised around **a company that employs agents**: you pick a company, you
see its projects, and everything else is a view onto the *workforce* doing them — a live hive,
a roster, an org chart, the panels where they argue, the gates where they ask permission.

Three consequences, and they are the whole migration:

1. **Most current areas stop being destinations.** Discovery, Build, Review, Ship become
   *phases of a project*, not places in a menu.
2. **Agents become first-class.** Three of the nine nav items (Hive, Workforce, Org Chart) are
   views of the agent population. Today that is one area. `Metatron` stops being a screen and
   becomes an *agent* with the `WATCHDOG` role.
3. **Meetings and Approvals merge.** `Decisions` holds both the panel debate (FOR/AGAINST) and
   the gate you approve.

---

## 2. The design system (exact, from the source)

### Colour

| Token | Hex | Use |
|---|---|---|
| canvas | `#090D17` | page background |
| panel | `#0E1422` | active nav, cards, chat bubbles (them) |
| surface | `#141B2B` | toggles, chat bubbles (you) |
| border | `#1E2940` | card and control borders |
| border-strong | `#2C3A55` | toggle borders |
| edge | `#253350` | org-graph edges |
| **mint** | **`#4FE3C1`** | primary accent, RUNNING, active nav rail, focus |
| green | `#58D68A` | DONE / APPROVED / stance FOR / integration ok |
| amber | `#F5B14C` | GATED / AT GATE / load >80% / budget >75% |
| red | `#F26D6D` | STUCK / REJECTED / stance AGAINST |
| purple | `#A78BFA` | panel LIVE, stage kind `PANEL` |
| ink | `#E6EDF5` | primary text |
| muted | `#8D9AB5` | secondary text |
| dim | `#5A6785` | tertiary text, inactive tabs |
| idle | `#3E4D6B` | IDLE dot |

### Type
- Display / UI: **Space Grotesk**
- Metadata, numbers, IDs, status: **IBM Plex Mono** — used heavily and deliberately
  (`MISSION CONTROL`, `BUILD 64%`, `lead Atlas (CEO)`, `RUNNING`). The mono/sans split is
  a large part of why it reads differently from today's console.

### Motion
- `pulse 2s infinite` — a running agent or project
- `blink 1s infinite` — a stuck agent
- Nothing else animates.

### Structural rules
- Active nav: left rail in mint, background `#0E1422`, text `#E6EDF5`.
- Active tab: mint underline, mint text; inactive `#5A6785`, no underline.
- Status is a **coloured dot plus a mono label**, never colour alone.

---

## 3. The nine screens, in detail

Internal ids in brackets — they reveal intent.

### 3.1 Projects `[missions]`
Filters `ALL / RUNNING / GATED / DONE`. Per project: status chip, title, one-line
description, `lead · N agents · N steps · $N spent`, and a phase bar
(`DISCOVERY | BUILD | REVIEW | SHIPPED` with %).

### 3.2 Hive `[hive]`
A radial graph of the whole workforce. `Atlas (CEO)` at the centre; inner ring
`Rhea, Forge, Ledger, Arc, Metatron`; outer ring `Vector, Pixel, Nova, Muse, Scout, Quill,
Sentry`. Edges are **org structure** (grey) plus **live handoffs** (coloured, labelled with
what was handed over). Node colour = status; click a node to chat with that agent.

**This screen has no equivalent today.** It is the clearest expression of "a company that
employs agents", and it is new build, not migration.

### 3.3 Workforce `[factory]`
Roster with filters `ALL / WORKING / IN PANEL / AT GATE / IDLE`, scoped by project. Each
agent: name, role, model, current task, status dot, chat.

### 3.4 Org Chart `[org]`
Manager columns: `Rhea → Nova, Scout` · `Forge → Vector, Pixel` · `Ledger → Quill, Sentry` ·
`Arc → Muse`. Shows name, role, **model**, current task.

Roles seen: `CEO, COO, CFO, CTO, ARCHITECT, WATCHDOG, BACKEND, FRONTEND, PM, PMM, ANALYST,
LEGAL, COMPLIANCE`.

### 3.5 Decisions `[panels]` — merges Meetings + Approvals
Two things on one screen:
- **Panel sessions** — `LIVE / VERDICT / CLOSED`, with debate messages each carrying a stance
  `FOR` (green) or `AGAINST` (red), and a verdict that can be ratified.
- **Gates** — the pending approvals, each with title, detail, requesting agent, timestamp, and
  Approve/Reject. Resolution reads `APPROVED BY YOU · LOGGED` or
  `REJECTED BY YOU · AGENTS NOTIFIED`.

### 3.6 Workflows `[boss]`
Workflow **definitions**, scoped `GLOBAL / <project>`. Each is an ordered list of stages;
each stage has a number, a **kind** (`PANEL` purple, `GATE` amber, work mint), owners (agents
with status dots), and a `gated` flag. Stages can be **removed and customised**, then deployed.

**Editing the pipeline is new capability** — today's pipeline is code, not configuration.

### 3.7 Receipts `[timeline]`
The audit trail: filter by project, full-text search across actor, text, kind and artifact.

### 3.8 Infra & Models `[infra]`
Scoped `GLOBAL / <project>`. Three blocks:
- **Fleet** — hosts with load bars, amber above 80%
- **Budgets** — spent/cap bars, amber above 75%
- **Integrations** — connected services with ok/warn dots

### 3.9 Settings `[settings]`
Tabs `GitHub` · `SSO & Security` · `Members & Access`.
- **GitHub**: repos with CI status (`PASSING/RUNNING/FAILING`), and four permission toggles —
  *Merge to main*, *Production deploy*, *Manage repo secrets*, *Create branches & PRs*.
- **SSO**: policy toggles — *Enforce SSO*, *Require MFA at IdP*, *8-hour session limit*,
  *Just-in-time provisioning* — plus ACS URL, Entity ID, IdP issuer, certificate expiry.
- **Members**: member list with roles.

### 3.10 Chrome present on every screen
- **Top bar**: `BOSS · MISSION CONTROL`, company switcher, **New project**, and right-aligned
  `BURN $spent / $cap`, `N GATES`, `SSO OK`, avatar.
- **Project strip**: one chip per project — dot, name, stage, %, `GATED` flag.
- **Sidebar footer**: `AUTONOMY · Supervised · "Spend & deploys pause for you."`
- **New project modal**: brief, workflow choice, budget (`$1k / $5k / $20k`).
- **Agent chat drawer**: per-agent conversation with typing state.
- **Company switcher**: multiple companies (`GeneVault Inc`, `Driftwake Labs`, `Copperline`)
  with live/idle status.

---

## 4. Every current area, accounted for

33 areas today. Nothing disappears silently.

### Becomes a project phase (no longer a menu item)
| Today | v2 |
|---|---|
| Discovery | Project ▸ DISCOVERY |
| Meetings | **Decisions** (panel sessions) |
| Code Tasks | Project ▸ BUILD |
| CI/CD | Project ▸ SHIP + Settings ▸ GitHub |
| Observability | Project ▸ SHIP + Infra & Models (fleet) |
| Coding Pipeline | **Workflows** (as an editable definition) |
| Agent processes | **Workflows** |
| Task Shepherd | Project ▸ BUILD (stuck work) + `STUCK` status in Hive/Workforce |
| Mission Dashboard, Task Board, Work Allocation | **Projects** |

### Maps to a named destination
| Today | v2 |
|---|---|
| Approvals | **Decisions** (gates) |
| Evidence, Activity log | **Receipts** |
| Agent Roster, Agent Studio | **Workforce** + **Org Chart** |
| Budget | top-bar `BURN` + **Infra & Models** (budgets) |
| System Configuration, Capabilities, Foundations | **Infra & Models** |
| GitHub | **Settings ▸ GitHub** |
| Operations, Assurance, Communications | **Settings** |
| Reliability, AI SRE | **Infra & Models** (fleet health) |
| BOSS Agent | agent chat drawer, reachable from any agent |
| Sandboxes | Project ▸ BUILD (preview) |

### Changes shape or retires — needs your ruling
| Today | Proposal | Why |
|---|---|---|
| **Metatron** | becomes an **agent** (`WATCHDOG`), not a screen | that is what the artifact does |
| **Components** | **retire** | design-system page, not a product feature |
| **Command Center** | **retire as a page** | Projects + top bar replace it |

---

## 5. What v2 needs that BOSS does not have yet

Being honest about this now prevents Phase 2 quietly becoming a platform project.

| Capability | Today | Needed |
|---|---|---|
| **Multiple companies** | one org per session | company switcher, cross-company burn |
| **Agent org hierarchy** | roles exist; no manager→report | a reporting structure |
| **Live handoffs** | events exist | agent→agent handoff as a first-class record, for Hive edges |
| **Per-agent chat** | one agent conversation | address a *named* agent |
| **Editable workflows** | pipeline is code | stage definitions as data, with kinds and owners |
| **Autonomy modes** | approvals always on | `Supervised` vs autonomous, and what changes |
| **Agent status vocabulary** | run states | `WORKING / IN PANEL / AT GATE / IDLE / STUCK` |
| **Panel stances** | meetings produce minutes | per-message `FOR`/`AGAINST` |
| **SSO configuration UI** | env config | ACS/Entity/IdP/cert + policy toggles |

Each becomes an API and schema change, specified before the phase that needs it.

---

## 6. Architecture — both consoles at once

**v2 is a new presentation over the same data layer.** Feeds, actions and the API are shared;
only the shell and components fork.

```
apps/web/app/
  (v1 pages)          ← untouched
  v2/
    layout.tsx        ← top bar · project strip · sidebar · autonomy footer
    missions/  hive/  factory/  org/  panels/
    boss/  timeline/  infra/  settings/
  api/                ← route handlers, shared
lib/                  ← feeds, SHARED
components/v2/        ← tokens.css + the v2 design system
```

**The toggle** lives in the top bar, persists per user, and accepts `?console=v1|v2`.

**Why the data layer must not fork:** this week's worst bug — one endpoint costing 13s of a
20s page — was findable only because every feed passes through one place.

**Performance budget, carried from this week's measurements:**
- rendering ~50 areas measured **1ms**; fetching is everything;
- `Promise.all` waits for its slowest member, so one slow feed holds the page;
- **TTFB ≤ 2s per v2 screen, enforced by a test**, not by hope.

---

## 7. Phases

Each ends shippable behind the toggle.

**Phase 0 — Shell.** `tokens.css` from §2, top bar, project strip, sidebar, autonomy footer,
toggle. *Exit:* v2 shell renders with real company and project data.

**Phase 1 — Projects.** List, filters, phase bars, spend. *Exit:* every project appears with
the correct phase; numbers reconcile with v1.

**Phase 2 — Project workspace (the core five).** Discovery, Spec, Build, Ship, Observability
as phases. Highest risk of feature loss; gated by the parity checklist.

**Phase 3 — Decisions.** Gates first (they exist today), then panel sessions with stances.

**Phase 4 — Workforce, Org Chart, Receipts.** Mostly re-presentation, plus the reporting
structure from §5.

**Phase 5 — Hive.** New build: handoff records, then the graph.

**Phase 6 — Workflows, Infra & Models, Settings.** Editable workflows are the platform work.

**Phase 7 — Autonomy modes and cutover.** v2 default; v1 reachable one release, then removed.

---

## 8. Parity — the gate on every phase

`docs/product/console_v2_parity.yaml` records, per migrated area: every **action**, every
**fact displayed**, every **navigation**, every **empty and error state**. A phase cannot
close with an unaccounted row. `retired: <reason>` is allowed; silently missing is not.

This exists because of a real miss: the deployment card had **no Unbind control at all**,
while `DELETE /v1/deployment-targets/{id}` had existed the whole time.

---

## 9. Testing, and the console test agent

### 9.1 Conventional
- Component tests per v2 component: default, empty, error, loading.
- Parity tests reading the YAML: every declared action exists and is wired.
- Performance tests: TTFB budget per screen; fail if a screen adds a feed slower than 1s.
- Guards carried from this week: no Server Action on a hot path (they re-render the route to
  reply — the 115s "Saving…"), no invented actor, no metric computed from a limited page.

### 9.2 The Console Test Agent

**Mode A — Learn (runs against v1, before migration).** Drives today's console in Chrome and
records, per area: the DOM, every interactive element, what each does, the network calls it
issues, and the resulting state. Output: a behavioural baseline —
*"on Code Tasks, Set target issues POST …/setup-deployment and the row then shows a host."*

**Mode B — Verify (runs against v2).** Replays each behaviour, locating the equivalent control
through the parity file, asserting the same **outcome** — same API call, same resulting state
— not the same pixels.

Reports three categories: **MISSING** (no v2 equivalent), **BROKEN** (different outcome),
**MOVED** (same outcome, new location — informational).

**Rules**
- Deterministic assertions on network and state. The model *locates* controls and *summarises*
  differences; it never decides pass/fail. A model that decides pass/fail will eventually tell
  us what we want to hear.
- Runs in CI per PR against a seeded project, and on demand against goldeneye.
- **Acceptance for the agent itself:** it must first find known v1 gaps — the missing Unbind
  control, the audit export costing 13s — or it is not a baseline.

**Honest limit:** it will catch missing and broken behaviour. It will not tell you whether the
UI vibes. That judgement stays with you and Sean; the agent exists so your time goes there
instead of into checking whether buttons still work.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| 33 → 9 hides features | parity file + test agent; `retired` must be explicit |
| Phase 2 becomes a platform project | §5 capabilities specified before the phase needing them |
| v2 inherits the slow load | TTFB budget enforced from Phase 0 |
| Two consoles, double the bugs | shared `lib/`; only presentation forks |
| Migration stalls half-done | every phase ships behind the toggle; v1 always works |
| Test agent gives false confidence | deterministic assertions; must first find known v1 gaps |

---

## 11. Open questions

1. **Autonomy modes** — what exactly changes between `Supervised` and autonomous? Just
   spend/deploy gating, or panel and merge behaviour too?
2. **Multiple companies** — is this real for v1 of the console, or aspirational in the mock?
   It is the largest schema change in §5.
3. **Metatron as an agent** — agreed?
4. **`Components` retired** — agreed?
5. **Workflow editing** — can an operator really re-order and delete pipeline stages, or is
   the artifact showing an aspiration? This decides whether Phase 6 is UI or platform work.
6. **Agent identity** — the artifact names thirteen agents with roles and models. Is that the
   intended roster, or illustrative?

---

## 12. What I need to start

Answers to §11.1–11.6. Phases 0 and 1 need only Q3/Q4 and can begin immediately; the rest
depend on the autonomy and multi-company rulings.
