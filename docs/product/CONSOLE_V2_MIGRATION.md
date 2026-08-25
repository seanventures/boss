# Console v2 — migration plan

Status: **draft for review**. Nothing in here is built yet.
Branch: `feat/console-v2` (based on `main` @ `09ecd89`, all current fixes merged).

---

## 0. What this document is for

Sean's feedback was *"the UI still isn't vibing yet"*, and the answer is the
`Autonomous Company Builder` artifact. This plan says how we get there without losing a
single working feature, how we run both consoles side by side while we do it, and how we
stop the operator being the test harness.

Three constraints drive every decision below:

1. **No feature may be lost.** Discovery, Meetings, Code Tasks, CI/CD and Observability are
   named as core; the other 28 areas still have to land somewhere or be explicitly retired
   with a reason.
2. **Both consoles run at once**, switchable, so bug fixes to today's console keep shipping
   while v2 is built.
3. **An AI agent tests the console**, not the operator. This is a deliverable, not a nice-to-have.

---

## 1. What I verified, and what I could not

### Verified by reading the artifact in the browser

The shell is a **company → projects** model, not an area menu.

**Top bar:** `BOSS` `MISSION CONTROL` · company switcher (`GeneVault Inc ▾`) · **New project**
· right-aligned: `BURN $7,074 / $12,000` · `2 GATES` · `SSO OK` · avatar.

**Project strip** (second row, `PROJECTS`): one chip per project with a status dot, name,
phase and progress — `GeneVault BUILD 64%`, `GeneVault launch campa… DISCOVERY 22%`,
`SOC 2 readiness REVIEW 41% GATED`, `Competitive teardown SHIPPED 100%`.

**Left sidebar — nine items, in this order:**

| # | Item | Badge seen |
|---|------|-----------|
| 1 | Projects | — |
| 2 | Hive | — |
| 3 | Workforce | `2` |
| 4 | Org Chart | — |
| 5 | Decisions | `1` |
| 6 | Workflows | — |
| 7 | Receipts | — |
| 8 | Infra & Models | — |
| 9 | Settings | — |

**Sidebar footer:** `AUTONOMY` · ● `Supervised` · "Spend & deploys pause for you."
This is a mode selector, and it implies at least one other mode (autonomous).

**Projects screen:** title + "every project across GeneVault Inc", right-aligned filters
`ALL / RUNNING / GATED / DONE`, and a row per project: status chip (`RUNNING` mint,
`GATED` amber, `DONE` mint), title, one-line description, then
`lead Atlas (CEO) · 12 agents · 1847 steps · $2,318 spent`, with a phase bar on the right
(`BUILD 64%`).

**Design language** (from pixels, to be confirmed against source):
near-black navy canvas; mint/teal accent for active nav, healthy status and progress;
amber for counts, `GATED` and `REVIEW`; geometric sans for titles; **monospace for
metadata** (`MISSION CONTROL`, `BUILD 64%`, `lead Atlas (CEO)`, `RUNNING`); active nav item
carries a left accent bar and a lighter panel.

The style is close to today's console — Sean's read is right. **The break is in the
information architecture, not the paint.**

### Could not verify — I need the source

Clicks do not reach the artifact's sandboxed iframe, so I could not open `Hive`,
`Workforce`, `Org Chart`, `Decisions`, `Workflows`, `Receipts`, `Infra & Models`,
`Settings`, or any project detail.

**Ask: the artifact's source (the React/HTML file).** With it I can extract exact tokens
(hex, radii, spacing, fonts) and every sub-screen, instead of inferring them from
screenshots. Everything marked *(needs source)* below is blocked on this and only this.

---

## 2. What exists today

| | count |
|---|---|
| Console areas | **33** (18 Run, 9 Platform, 6 Govern) |
| `page.tsx` | 7,571 lines |
| Client components | 74 |
| Feed modules (`lib/`) | 93 |
| Server actions | 205 |
| API endpoints (`/v1`) | 501 |
| Web tests / engine tests | 72 / 125 |

The 33 areas: Command Center, Discovery, Meetings, Code Tasks, Approvals, CI/CD,
Observability, BOSS Agent, Metatron, Mission Dashboard, Task Board, Activity log, Work
Allocation, Agent processes, Task Shepherd, Reliability, AI SRE, Evidence, Budget, Coding
Pipeline, Communications, Operations, Assurance, Agent Roster, GitHub, System Configuration,
Capabilities, Agent Studio, Sandboxes, Foundations, Components, plus the org gate and the
agent dock.

**This is the crux of the migration: 33 flat areas collapse into 9 nav items plus a
per-project workspace.** Most of today's areas are not deleted — they stop being
*destinations* and become *views inside a project*.

---

## 3. Proposed mapping — every current area accounted for

Nothing is dropped silently. Each row is a commitment to be reviewed.

### Becomes a phase inside a project (the biggest change)

Today these are global lists; in v2 a project moves DISCOVERY → BUILD → REVIEW → SHIPPED and
each is that project's stage view.

| Today | v2 home |
|---|---|
| Discovery | Project ▸ Discovery phase |
| Meetings | Project ▸ Spec phase (the panel that produces the PRD) |
| Code Tasks | Project ▸ Build phase |
| CI/CD | Project ▸ Ship phase |
| Observability | Project ▸ Ship phase (health) + company Infra & Models |
| Coding Pipeline | Project ▸ Build (pipeline view) |
| Task Shepherd | Project ▸ Build (stuck-work explainer) |
| Agent processes | Project ▸ Workflows |

### Maps to a named v2 destination

| Today | v2 home |
|---|---|
| Approvals | **Decisions** |
| Evidence | **Receipts** |
| Agent Roster | **Workforce** + **Org Chart** |
| Agent Studio | **Workforce** (agent definition) |
| Budget | top bar `BURN` + **Receipts** |
| System Configuration, GitHub, Capabilities, Sandboxes, Foundations | **Infra & Models** |
| Operations, Assurance, Communications | **Settings** |
| BOSS Agent, Metatron | **Hive** *(needs source — Hive is the least understood item)* |
| Mission Dashboard, Task Board, Work Allocation, Activity log | **Projects** + project detail |
| Reliability, AI SRE | **Infra & Models** *(or a project Ship view — needs source)* |
| Components | design-system page; **candidate for retirement** |

### Open questions for you and Sean

1. **What is Hive?** My guess is the agent conversation surface (today's BOSS Agent +
   Metatron). If it is something else, several rows above move.
2. **`AUTONOMY: Supervised`** implies an autonomous mode. What changes between them —
   just spend/deploy gating, or more?
3. **Is `Components` worth keeping?** It is a design-system page, not a product feature.
4. **Company switcher vs today's org gate** — same concept, or does v2 support multiple
   companies per session?

---

## 4. Architecture — how both consoles coexist

The rule: **v2 is a new presentation over the same data layer.** No forking of feeds,
actions or the API.

```
apps/web/
  app/
    (v1)/…            ← today's console, untouched
    v2/
      layout.tsx      ← shell: top bar, project strip, sidebar, autonomy footer
      projects/…      ← Projects, project detail (phases)
      hive/…  workforce/…  org-chart/…  decisions/…
      workflows/…  receipts/…  infra/…  settings/…
    api/…             ← route handlers, shared
  lib/                ← feeds, SHARED by both consoles
  components/v2/      ← v2 design system
```

**The toggle.** A switch in the top bar, persisted per user, plus `?console=v1|v2`. It flips
which shell renders; both read the same `lib/` feeds. This is what lets us keep fixing v1
while v2 is incomplete, and lets you compare the two on the same data.

**Why not fork the data layer:** today's slowest bug — one endpoint costing 13s of a 20s
page — was found because every feed passes through one place. Two data layers means two sets
of those bugs.

**Performance stance for v2 (learned the hard way this week):**
- the render is *not* the cost — rendering ~50 areas measured **1ms**; fetching is everything;
- `Promise.all` waits for its slowest member, so **one slow feed holds the page**;
- v2's per-project model naturally fetches less, but the per-feed timing added to
  `lib/feed-cache.ts` stays, so the next offender names itself;
- **budget: TTFB ≤ 2s for any v2 screen**, enforced by a test, not by hoping.

---

## 5. Phased plan

Each phase ends shippable and behind the toggle. No phase is "big bang".

### Phase 0 — Foundations (needs source)
- Extract exact design tokens from the artifact into `components/v2/tokens.css`.
- Build the shell: top bar, project strip, sidebar, autonomy footer.
- Toggle + persistence + `?console=` override.
- **Exit:** v2 renders an empty shell with real company/project data; v1 untouched.

### Phase 1 — Projects
- Projects list with `ALL / RUNNING / GATED / DONE`, real status, phase, progress, spend.
- Project detail scaffold with the four phases.
- **Exit:** every real project appears with correct phase and spend; numbers reconcile with v1.

### Phase 2 — The core five
Discovery, Spec/Meetings, Build/Code Tasks, Ship/CI-CD, Observability — as phase views
inside a project. This is the bulk of the work and the highest risk of feature loss.
- **Exit:** a parity checklist (§6) passes for all five.

### Phase 3 — Decisions, Receipts, Workforce, Org Chart
Mostly re-presentation of existing feeds.

### Phase 4 — Hive, Workflows, Infra & Models, Settings
Hive is specified first (open question 1), then built.

### Phase 5 — New capability
Anything the artifact implies that BOSS cannot do yet — the autonomy modes are the obvious
candidate — with the API and platform changes that requires. Specified separately once the
source is read; **not** smuggled into earlier phases.

### Phase 6 — Cut over
v2 becomes default, v1 stays reachable for one release, then is removed.

---

## 6. Feature parity — the checklist that gates each phase

Parity is not "the screen exists". For every migrated area we record, in a machine-checkable
file (`docs/product/console_v2_parity.yaml`):

- every **action** the operator can take (button, form, menu item);
- every **fact** displayed (each number, status, link);
- every **navigation** out of the screen;
- every **empty and error state**.

A phase cannot close while any row is unaccounted for. Rows may be marked
`retired: <reason>` — deliberately dropped is fine, silently missing is not.

This exists because of a real failure this week: the deployment card had no **Unbind**
control at all — `DELETE /v1/deployment-targets/{id}` had existed the whole time and nothing
called it. A parity file would have caught that.

---

## 7. Testing — and the console test agent

### 7.1 Conventional tests
- **Component tests** (vitest) per v2 component: states, empty, error.
- **Parity tests** that read `console_v2_parity.yaml` and assert each declared action exists
  and is wired to something.
- **Performance tests**: TTFB budget per screen; a test that fails if a screen adds a feed
  slower than 1s.
- **Guard tests** carried over from this week's lessons: no Server Action on a hot path (they
  re-render the route to reply — that was the 115s "Saving…"), no invented actor, no metric
  computed from a limited page.

### 7.2 The Console Test Agent

A BOSS agent whose job is to test the console, in two modes.

**Mode A — Learn (before migration).** Drives **v1** through Chrome, and for every area
records: the DOM, every interactive element, what each does, the network calls it makes, and
the resulting state. Output: a **behavioural baseline** — "on Code Tasks, pressing Set target
issues POST …/setup-deployment and the row then shows a host."

**Mode B — Verify (after migration).** Replays each recorded behaviour against **v2**,
locating the equivalent control via the parity file, and asserts the same *outcome* — same
API call, same resulting state — not the same pixels.

It reports three categories, and the distinction matters:
- **MISSING** — a v1 behaviour with no v2 equivalent;
- **BROKEN** — exists in v2 but produces a different outcome;
- **MOVED** — same outcome, different location (informational).

**Build notes**
- Runs as a code task in BOSS itself, using the existing Chrome tooling.
- Deterministic assertions on network + state; the model is used for *locating* controls and
  *summarising* differences, never for deciding pass/fail. A model that decides pass/fail will
  eventually tell us what we want to hear.
- Runs in CI per PR against a seeded project, and on demand against goldeneye.
- **Also runs against v1 today** — before any migration — because a baseline that cannot find
  today's bugs is not a baseline. If Mode A cannot reproduce the Unbind gap, the agent is not
  ready.

**Honest limit:** this agent will catch missing and broken behaviour. It will not tell you the
UI "vibes". Visual judgement stays with you and Sean; the agent exists so your time goes there
instead of into checking whether buttons still work.

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| 33 → 9 hides features operators rely on | parity file + test agent; `retired` must be explicit |
| Hive is misunderstood and built twice | specified before Phase 4 starts |
| v2 inherits the 20s load | TTFB budget enforced by test from Phase 0 |
| Two consoles double the bug surface | shared `lib/`; only presentation forks |
| Migration stalls half-done | every phase ships behind the toggle; v1 always works |
| Test agent gives false confidence | deterministic assertions only; must first find known v1 gaps |

---

## 9. What I need from you

1. **The artifact source.** The one hard blocker. Everything marked *(needs source)* is
   guesswork until I have it — particularly exact tokens and the eight sub-screens.
2. **Answers to the four open questions** in §3, especially *what Hive is*.
3. **A ruling on retirement.** Of the 33 areas, my instinct is that `Components` and possibly
   `Metatron` (now overlapping BOSS Agent) should retire rather than migrate. Your call.

Once I have the source I will replace §1's inferred design language with exact tokens, fill
in the eight sub-screens, and turn §5 into per-phase task lists with estimates.
