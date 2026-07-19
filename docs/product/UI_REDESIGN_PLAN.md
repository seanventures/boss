# BOSS Console — UI Redesign to the Mission-Control Design System

Source of truth: `/Users/param/Documents/design_handoff_boss_design_system` (README + `BOSS Design
System.dc.html`). Goal: recreate the design's flat, hairline, high-contrast "mission-control" look
and its component language in the live console — **reskin + rebuild controls, keep functionality,
wire every control to the real API.** Branch: `feat/ui-redesign-design-system`.

Principle (do not regress): flat hairline surfaces (8px radius, 0 for data containers), **status is
colored mono text not pills**, motion only on live things, human-in-control, calm power.

---

## Phase 0 — Foundation ✅ DONE
Design tokens remapped (void `#090D17`, teal→blue brand gradient, signal colors), Sora/Manrope/
JetBrains Mono type, flat 8px radii, solid hairline borders, new hexagonal BOSS mark, brighter
text hierarchy + larger type scale. Reusable primitives in CSS (`.kpi-strip`, `.status-text`,
`.hairline-row`, `.orb`). *Token/asset-driven — no logic touched.*

## Phase 1 — Mission Dashboard ✅ DONE (reference implementation)
Rebuilt into a hairline **KPI strip** + **Health & Signals** panel with status-as-text, on live
`GET /v1/dashboard`. Establishes the component patterns the rest of the app adopts.

---

## Phase 2 — Command Center (`overview`)
- **Outcome Composer** restyle to spec (bordered panel, `NEW OUTCOME` eyebrow, large Sora text +
  blinking teal caret, "Auto-route → N agents" affordance, gradient **Assign** CTA); also a
  single-line command bar with `⌘K`. API: `proposeCommand` (exists).
- **KPI strip** (reuse Phase-1 primitive) for the command-center headline metrics.
- **Coordination graph** — dark panel, central BOSS core node + agent orbs linked by flowing
  dashed lines; live rings. API: derive from active jobs/agents (may need a small "active work per
  agent" projection).
- **"What changed" activity feed** — live-pulse header; hairline rows (orb + "Who verb target" +
  colored mono tag + timestamp). API: `events` (exists).

## Phase 3 — Decision Queue / Approvals (`decisions`)
- Rebuild as **hairline-divided proposal rows**: orb, "Who proposes" + mono impact label
  (SCOPE/MERGE/ACCESS), Sora title, rationale, evidence refs, then **Approve / Request changes /
  Reject**; once decided the row dims + shows a colored mono verdict.
- API: `GET /v1/decisions/inbox`, `POST /v1/decisions/{id}/approve|reject` (exist). Modal confirm
  for irreversible actions.

## Phase 4 — Agent Roster (`org`)
- Render the roster as a **divided list** (Agent / Now / State): 40–52px **agent orbs** with state
  rings (running=rotating conic ring, verified=green, blocked=breathing coral, idle=dashed), Sora
  initials, role, and a **current-task line**.
- API: agents/roles/modules (exist). **Likely new:** a "what is each agent working on right now"
  signal — check if derivable from tasks/jobs owner_role; if not, add `GET /v1/agents/activity`.

## Phase 5 — Task Board (`work`)
- **5 columns** (Backlog / In Progress / Review / Blocked / Done) with colored square column
  markers; **flat 8px cards**: mono priority + `T-###`, Sora title, footer orb + receipts count +
  right-aligned mono state. API: tasks (exists).

## Phase 6 — Evidence (`reports`)
- **Evidence receipt** view: header (orb, action, `rcpt_…` mono meta, VERIFIED) → divided meta
  strip (Changes `+/−` / Duration / Cost / Tests) → verification checklist (green checks) →
  monospace diff block. API: receipts / quality_gates / cost_receipts (exist).

## Phase 7 — Global controls & polish
- **Buttons** to spec (Assign/CTA gradient, Primary, Secondary, Approve, Halt/Reject, Ghost).
- **Toggles/checkboxes** to spec; **progress** ring + shimmer bar.
- **Status-as-text everywhere** — replace remaining pills/badges across all areas.
- **Modal** (440px, radius 12, blurred overlay) for irreversible actions; **Toast** for completed
  agent work. Respect `prefers-reduced-motion`.
- Sidebar: active = `rgba(47,111,224,0.12)` + 2px teal inset bar; operator identity footer with
  green "in control" dot.

---

## Delivery loop (per phase)
Build controls → wire live API → local preview (`localhost:3007`) → user validates → deploy to
goldeneye (`build → push → POST /deploy`) → next phase. Each phase is one PR-sized batch.

## Status
Phase 0–1 done (branch `feat/gap-phase0-mvp`… now `feat/ui-redesign-design-system`), previewable
locally, **not yet deployed to goldeneye** (awaiting look-and-feel sign-off).
