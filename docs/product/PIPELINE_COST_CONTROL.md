# Pipeline Cost Control — Tunable Efficiency for the Governed Coding Pipeline

Status: Phased build on `feat/governed-coding-pipeline`
Date: 2026-06-19
Owner: param

## Why

The governed pipeline buys correctness by running more agents — which costs more tokens and adds
latency. AI budget is finite, so that overhead must be a **customer-tunable dial**, not a fixed
cost: a prototype should run low-cost, a production change should run strict. This makes BOSS's
built-in efficiency a *setting* customers control, and avoids the "BOSS looks expensive" trap on
first trial. The dial is a **profile** (Lean / Balanced / Strict, or custom) that scales how much
of the pipeline runs and which models run it.

## Profiles (the dial)

| Profile | For | Tier range | Models | Rough cost |
|---|---|---|---|---|
| **Lean** | prototypes, throwaway | 0–1 | low-cost everywhere | ≈ 1× |
| **Balanced** (default) | normal work | 0–3 (inferred) | low-cost models for low-cost steps, strong for implement/review/security | ≈ 1.5–3× |
| **Strict** | production / enterprise | 2–3 | strong everywhere, two-person at top | ≈ 4–8× |
| **Custom** | enterprise | any | any per-stage | — |

A profile clamps the inferred risk tier, routes each step to a low-cost or strong model, and toggles
the efficiency levers (caching, parallel checks). Risk auto-escalates to high when the change
touches login/payments/secrets/migrations/deploys, regardless of profile floor.

## Phases

### Phase 1 — Foundation: profiles + tier inference + cost accounting (runtime)
- Define the Lean/Balanced/Strict profiles and a tier-inference function (replaces the hardcoded
  tier).
- Runtime reads the profile (per project / per task), infers the tier, and runs accordingly.
- Record **per-step cost + tier + profile** on every run so tiers can be compared.
- **Outcome:** the dial works in the runtime; every run is cost-attributed and comparable.

### Phase 2 — Efficiency levers (runtime)
- **Prompt caching** of the stable repo context across steps (kills the per-step re-read cost).
- **Parallelize** the independent checks (review code / security check / check tests on the same
  diff) to cut latency.
- **Per-step model routing** through the model gateway (low-cost model for low-cost steps).
- **Outcome:** Lean ≈ 1×; the overhead of higher tiers is materially reduced.

### Phase 3 — Profiles API + custom profiles + edition gating
- CRUD for profiles (presets + custom) per project, on the policy/settings API.
- **Edition-gate** custom profiles + per-stage routing (enterprise); default new projects to
  Balanced, trials to Lean.
- **Outcome:** customers choose/create profiles; enterprise gating; safe low-cost defaults.

### Phase 4 — UI: settings page + cost comparison
- A "Coding Pipeline" settings page: pick a profile, see which steps run per tier, edit (custom),
  override per task.
- A **cost/latency comparison view**: estimated and actual cost+time per profile/tier, side by
  side, so the tradeoff is visible before and after a run.
- **Outcome:** a visible cost dial — no "expensive surprise," and a clear story for customers on
  how built-in efficiency scales to the work.

## Status log
- 2026-06-19 — Plan drafted. Phase 1 starting.
- 2026-06-19 — Phase 1 done: profiles + tier inference + per-step cost accounting; sensitive
  changes safety-floored to standard rigor even under a low-cost profile.
- 2026-06-19 — Phase 2 done: **steps are tier-driven** (tier 0 = 1 agent call, tier 1 = 2,
  tier 2 = 3) and each step routes to a low-cost/strong model per profile; per-step cost + model
  recorded. Live demo (same `is_even` task, gpt-4o): **Lean $0.0261 / 2 steps → PR** vs
  **Strict $0.0378 / 3 steps → review refuted, blocked** — ~31% lower cost from step-count alone;
  model routing widens it when low-cost/strong models are configured. Remaining levers (prompt
  caching, parallel independent checks) pay off once the additional review steps are wired live.
- 2026-06-19 — Renamed the model class `cheap` → `low_cost` (low cost, not low quality).
- 2026-06-19 — Phase 3 done: profiles are a managed resource. `pipeline_profiles` table +
  service + API (`GET/POST /v1/pipeline-profiles`, `POST /v1/pipeline-profiles/default`).
  Presets (lean/balanced/strict) always available; **custom profiles are enterprise-gated**
  (community → 403, uses presets); **community trials default to `lean`** so a first trial never
  looks expensive. The runtime resolves the profile per-task > project default > edition default.
  Verified: presets list, set-default persists, community custom-create → 403, enterprise
  custom-create + per-task resolve work.
