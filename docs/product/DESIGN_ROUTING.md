# Hybrid frontier+OSS routing for the design axis (APP-6 / #96)

> Plane #96, part of the #90 first-time-output-quality epic. Spec `09 §3.6`.

## Why

Coherence (taste, hierarchy, on-domain copy) is decided in a few stages; correctness fill-in is not.
Spend frontier tokens where design is *decided* and cheap OSS tokens on the verified fill-in — the
**Govern** side of the design axis, so coherence is an *affordable, metered* cost rather than "run the
whole loop on a frontier model."

## What it does (`design_routing.py`)

- **Design-axis steps** (`spec`, `plan`, `review_plan`, `product_design`, `scaffold_theme`,
  `visual_judge`) → **frontier** tier. **Fill-in steps** (`implement`, `repair`, `write_tests`) →
  **cheap OSS**. `resolve_tier` / `resolve_engine` (a per-step override wins; empty tier engines fall
  back to the default, so it's a safe no-op when unconfigured — composes with `route_for_step`).
- The design pass is a **distinct budgeted line item**: `design_pass_plan` reserves a fraction of the
  task budget for frontier design and caps how many passes it buys; `should_run_design_pass` is the
  governor — allow another frontier pass only under the cap AND within the envelope, else **fall back
  to OSS** for the rest of the task. A runaway can't burn the whole budget on taste.

## Config

`BOSS_DESIGN_FRONTIER_ENGINE` / `_OSS_ENGINE` (empty → strong / low-cost), `_PASS_BUDGET_FRACTION`
(0.4), `_MAX_PASSES` (2), `_EST_PASS_COST_USD_MICROS` (300k). `POST /v1/design-routing` (step → tier+engine, else the plan). **7 tests**.

## Next

Wire into the pipeline once the design stages exist as stages: route `product_design` (APP-2) and the
`visual_judge` (APP-4 vision follow-on) through `resolve_engine`, and reserve/settle the design
envelope as a budget line item via the existing governor — completing the frontier-design / OSS-fill-in
split end-to-end.
