# Wire-Phase 1 — the live review panel (security check · check tests · run all tests)

Goal: make the higher tiers actually run *more agents* than the core, so `strict`/custom mean
something and the spec-driven research (spec-kit/Kiro/BMAD steps) lands in the runtime.

## Where the runtime lives
`apps/api/app/services/governed_code_task_service.py` → `run_governed_code_task(engine, settings,
jobs, run_id)`. Today it runs (after one local sandbox clone + profile/tier resolve):
- tier≥1: **write_tests** (`_qa_prompt`, `agent.run_role(..., model=_model_for("write_tests"))`)
- **implement** (`_impl_prompt` or `_solo_prompt`)
- BOSS re-runs `test_cmd` authoritatively (`sandbox.run(test_cmd)`) → `tests_passed`
- tier≥2: **review_code** (`_review_prompt`, `run_role(read_only=True, model=_model_for("review_code"))`)
- builds `gates{}` + `evidence{}` → `_record_quality_gate` → PR if pass.

Agent seam: `agent = make_code_agent(settings)`; `agent.run_role(prompt, cwd, read_only=, model=)`
→ `{summary, usage, cost_usd_micros, turns, model}`. `_model_for(step)` → strong/low_cost model
name (or None=engine default). `_parse_verdict(summary)` → survived|refuted|not_run.

## What Wire-Phase 1 adds (all gated at tier≥2, after review_code, before final gate)

1. **security_check** — agent, read-only, fresh context. New `_security_prompt(title)`: find a
   REAL security defect (injection, authz/authn, secret handling, data exposure, SSRF); end with
   `VERDICT: CLEAN` / `VERDICT: ISSUES`. `run_role(read_only=True, model=_model_for("security_check"))`.
   Gate `security_clean = verdict==clean`. On ISSUES → (bounded) loop back to implement, else record.
   Reuse `_parse_verdict` with a CLEAN/ISSUES variant (add `_parse_security`).

2. **check_tests** (mutation) — prove the tests catch bugs. Approach (workdir-safe, no extra clone):
   - snapshot impl file contents: `orig = {p: read(root/p) for p in impl_files}`.
   - `run_role(_mutate_prompt(impl_files), model=_model_for("check_tests"))` → introduces ONE subtle
     bug into an impl file (NOT tests).
   - `sandbox.run(test_cmd)` → expect **fail** (tests caught it). `tests_strong = not mutated_run.ok`.
   - **restore**: write `orig` contents back (so the real implementation is intact for the PR).
   - Gate `tests_strong_enough = tests_strong`.

3. **run_all_tests** (integration) — deterministic, no model: `sandbox.run("python3 -m unittest
   discover -v")` (or project full-suite). Gate `nothing_broke = result.ok`. Cheap.

## Tier gating
`do_review = tier≥2` already. Add `do_panel = tier≥3` OR fold security/check_tests/run_all into
`tier≥2`. Recommended: security_check + run_all_tests at **tier≥2**; check_tests (mutation) at
**tier≥2** too (it's the differentiator). Keep core (write/implement/review) for tier 1.
Update `stepsForTier` in `apps/web/lib/pipeline-profiles.ts` to include the new steps so the UI
agent-count reflects reality (tier 2 → 5-6 agents).

## Parallelism + broker (the capacity-aware part)
Constraint: **check_tests mutates the shared workdir**, so it can't run concurrently with the
read-only steps. Safe plan: run **security_check ∥ run_all_tests** concurrently (both read-only),
then **check_tests** serial (mutate→test→restore). Use a thread pool for the two read-only calls.
Capacity/queueing: route model calls through the entitlement broker so concurrent calls wait for
an available lane (Wire-Phase 1b; first cut can run the 2 concurrently with a small worker pool).

## Evidence + gates to add
- `gates`: `security_clean` (tier≥2), `tests_strong_enough` (tier≥2), `nothing_broke` (tier≥2).
- `evidence.steps_run`: append security_check / check_tests / run_all_tests when run.
- `evidence.stage_costs` + `stage_models`: add the new agent steps.
- `evidence`: `security_verdict`, `mutation_caught` (bool), `regressions`.
- All gated fail-closed via existing `_record_quality_gate` (status pass iff no failing gate).

## Acceptance
A tier-2 run on `paramg/boss-sandbox` records ≥5 steps_run, a security verdict, a mutation result,
a full-suite result, and per-step costs; gate fails closed if any panel check fails. Validate
live on native gpt-4o, same as before.

## Follow-ons (Wire-Phase 2/3)
Front of pipeline (refine, plan, review_plan, consistency_check, standards-from-repo) + design
sign-off gate; then tasks/work-graph parallel sub-tasks. See GOVERNED_CODING_PIPELINE.md §11.
