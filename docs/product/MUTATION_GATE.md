# Mutation gate — measuring the frozen tests (VAG #106)

> Spec for Plane #106 (Engine & Model Quality / VAG module). Implemented on
> `feat/vag-mutation-gate`. Deterministic and **model-free**.

## 1. Why

BOSS's gate trusts the **frozen tests** as its *authoritative* verifier — a passing suite is what
lets a cheap-model implementation through. But a **weak** suite passes buggy code just as happily as
correct code. #105 measured the *review* verifier; #106 measures the *test* verifier, closing the
loop on both halves of the gate.

The method is classic **mutation testing**: plant a small change ("mutant") in the implementation
and check the tests **fail** ("kill" it). A mutant the tests *don't* catch is a bug-class they're
blind to. Score = `killed / run`; the gate fails closed below a floor.

## 2. What's new vs the existing check

The governed pipeline already has a `check_tests` stage — but it asks a **model** to plant **one**
bug and records a boolean. #106 replaces that weak, non-deterministic signal with:

- **Deterministic AST operators** (no model call): comparison flips (`<`↔`>=`, `==`↔`!=`, …),
  arithmetic swaps (`+`↔`-`, `*`↔`/`, …), boolean-op flips (`and`↔`or`), numeric/boolean constant
  bumps, and return-value voiding (`return x` → `return None`). One canonical mutation per site.
- A real **mutation score** over *many* mutants, not a single boolean — cheap, reproducible, reviewable.
- **Named survivors**: each uncaught mutant is reported with its operator + line, so QA sees exactly
  which behavior the suite fails to pin down.

Example: on a `last_n(xs, n)` whose suite tests `n=2` and `n=0` but not `n=1`, the `n<=0`→`n<=1`
mutant **survives** — the gate surfaces the missing boundary test instead of certifying a blind suite.

## 3. The gate

`mutation_gate_decision` fails closed when: the **baseline is red** (tests don't pass on the
unmutated code → nothing can be concluded), the **score is below the floor**, or **property tests are
required but absent**. Thresholds: `BOSS_MUTATION_MIN_SCORE` (0.8), `BOSS_MUTATION_MAX_MUTANTS` (30,
caps cost — one test run per mutant), `BOSS_MUTATION_TIMEOUT_SECONDS` (30),
`BOSS_MUTATION_REQUIRE_PROPERTY_TESTS` (false — Hypothesis `@given` presence detector).

## 4. Implementation

- **`apps/api/app/services/mutation_gate.py`** — pure, model-free: `count_sites`, `generate_mutants`
  (deterministic single-site mutants), `mutation_score`, `mutation_gate_decision`, `has_property_tests`;
  plus `run_mutation_gate(files, target_file, test_command, …)` which baselines the suite, runs the
  tests once per mutant, and scores. Executes the provided tests, so run it in a sandbox / trusted context.
- **`POST /v1/mutation-gate`** — `{files: {path: content}, target_file, test_command?, min_score?, …}`.
- **10+ tests** (`apps/api/tests/test_mutation_gate.py`): operator coverage + determinism, scoring,
  gate pass/fail/baseline-red/no-mutants/property-required, and the end-to-end runner on a strong
  suite (kills all → PASS) vs a weak suite (survivors → FAIL) vs a broken baseline.

## 5. Next

- **Wire it into the governed pipeline** as the `check_tests` gate (replacing the LLM single-mutant),
  running *before* the expensive implement/repair loop where a golden/reference solution exists — a
  weak suite should stop the run before it wastes the implementer.
- More operators (slice bounds, `+=`/`-=`, off-by-one on literals, short-circuit removal) and
  equivalent-mutant filtering.
- Feed survivors back to QA (or the #107 ratchet) as "add a test that kills this mutant."
