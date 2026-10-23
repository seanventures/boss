# The Knowledge Graph case study harness

Two task sets: **NorthPeak Retail** on Saleor (T1..T12, below) and **NorthPeak Bank** on Apache
Fineract (B1..B4, [banking set](#the-banking-set-fineract-b1b4)).

## NorthPeak Retail (Saleor)

The proof plan is [KNOWLEDGE_GRAPH_CASE_STUDY.md](../../../docs/product/KNOWLEDGE_GRAPH_CASE_STUDY.md).
This folder runs it. Phase 0 delivers tasks T1–T4; T5–T12 follow the same layout.

| Path | What it is |
|---|---|
| `harness.yaml` | Saleor pin, engines, arms, stages, per-build estimate and ceiling |
| `answers.yaml` | Scripted human approvals: the same answers for both arms. An unscripted decision stops the run |
| `tasks/Txx/brief.md` | The plain-language brief BOSS receives, with the contract the hidden suite tests |
| `tasks/Txx/hidden_tests/` | The hidden acceptance suite, never shown to agents. Named `hidden_*.py` so BOSS's own test runs never collect it; the harness installs it into the fork as `test_hidden_*.py` and refuses a fork that already contains one |
| `tasks/T4/fault.patch` | The controlled fault, pushed as the "release" before T4 (an off-by-one in Saleor stock allocation) |
| `selftest/t1_reference.py` | A reference T1 service that proves the T1 suite: a correct service passes, a broken one fails |
| `run.py` | `estimate`, `validate-suites` (no spend), `run` (spends; needs an approved stage) |
| `report.py` | Results → `report.md` and SVG charts |

## Before any spend
```sh
python3 run.py estimate --stage smoke            # open model (E2): 8 builds, ~$5–15, ceiling $24
python3 run.py estimate --stage smoke_e1         # Sonnet 5 (E1): 8 builds, $48–80, ceiling $120
python3 run.py validate-suites --saleor-dir ../northpeak-saleor --pytest-cmd ".venv/bin/pytest -q --create-db"
```
The validation proves each hidden suite without spending anything:
- the T2 and T3 suites fail on the pinned SHA, because the work is not done yet;
- the T4 suite passes on the pristine SHA and fails with the fault applied;
- the T1 suite is proven by `scripts/tests/test_case_study_harness.py` against `selftest/`.

## A stage (only after the operator approves its spend)

Operator decision, 2026-10-21: no stage is approved yet. The smoke runs after Phases 1–3, on
the open-model engine (`smoke`) first; the Sonnet 5 smoke (`smoke_e1`) comes after.
```sh
export BOSS_CASE_STUDY_API_KEY=...                # a BOSS API key scoped to the case-study project
python3 run.py run --stage smoke --arm A --engine E2 --tasks T1..T4 --approved-by "<operator>" \
    --project-id <project> --saleor-dir ../northpeak-saleor --fulfilment-dir ../northpeak-fulfilment --resume
python3 report.py artifacts/case-study/knowledge/<date>
```
`run` refuses unless the stage covers the arm, engine and tasks, and BOSS reserves the stage's
budget ceiling. It works only through BOSS's public API: `/v1/code-tasks`, `/v1/decisions`,
`/v1/settings`, `/v1/budget/reserve` and `/v1/llm-calls`. Arm B needs the per-project
`knowledge_graph` setting, which arrives with the knowledge features in Phase 1 and later.
Until then, arm B refuses to start and says so.

Results are written to `artifacts/case-study/knowledge/<date>/<engine>/<arm>/Txx-rN.json`, which
is gitignored. The final `report.md` is committed to `docs/case-studies/`.

## The banking set (Fineract, B1–B4)

Cases 1 and 3 of [KNOWLEDGE_GRAPH_BANKING_CASE_STUDIES.md](../../../docs/product/KNOWLEDGE_GRAPH_BANKING_CASE_STUDIES.md)
(Plane BOSSAPP-758), on Apache Fineract **1.15.0** (`d5636847ac55`), the "legacy core". Two learning pairs:

| Task | Case | What BOSS receives | Hidden suite (module) | How the suite is proven |
|---|---|---|---|---|
| B1 | 1 | Extract the progressive-loan interest rules into `InterestCalculationService`, not one cent changed | 200 generated loans (day-count conventions, leap-year strategies, rounding modes and currency digits, principal and interest grace, flat and declining) compared with schedules recorded on 1.15.0, plus a reflection-only contract (`fineract-progressive-loan`) | Behaviour passes on 1.15.0; contract fails there on assertions; `selftest/b1_reference.patch` (a behaviour-keeping extraction) passes both; 3 one-line mutants are caught |
| B2 | 1 | Extract the fee and charge rules into `ChargeCalculationService` | 240 generated charges (every calculation and charge time, caps, rounding, installment split, schedule portions) against 1.15.0, plus a contract (`fineract-loan`) | As B1, with `selftest/b2_reference.patch` and 4 mutants |
| B3 | 3 | Incident report: repayments post more than the customer paid | Posting conservation and allocation order for `mifos-standard-strategy`, 120 generated loans and repayment sequences plus 3 named scenarios (`fineract-loan`) | Passes on 1.15.0, fails on assertions with `fault.patch`, passes with B4's fault |
| B4 | 3 | Incident report: early repayments post more than the customer paid | The same rules for the due/in-advance strategy (`fineract-loan`) | As B3, with its own `fault.patch`; passes with B3's fault |

- **Learning pairs.** B1→B2 and B3→B4 run in that order. B2's and B4's `task.yaml` hold the
  `expected_reuse` list: the invariants the first task should have discovered (listed under
  `invariants` in B1 and B3) that the second should reuse rather than rediscover. A run records the
  list; a grader marks `reuse: {id: true|false}` on the result and `report.py` counts it.
- **The faults are one class through two code paths.** B3 drops a deduction in the standard
  strategy's handler; B4 drops one in a different strategy's in-advance handler. Both post the same
  money twice. Each suite passes with the other's fault, which proves the paths are distinct.
- **Arms.** A (knowledge off), AP = A′ (keyword search only) and B (knowledge on), set through the
  `knowledge_graph` project setting (`off | keyword | on`). `keyword` gives agents plain full-text
  search over the graph's records and nothing else: no graph traversal, no vectors, no learned facts,
  no precedent, on the injected packs and the `kg_*` MCP tools alike. It arrives in BOSS with
  `rel/rc10.10-kg` `f1f7fb6af`; an older BOSS refuses the value and AP stops before any spend.
- **Why 1.15.0.** 1.16.0 needs JDK 25; BOSS's build image ships Temurin JDK 21
  (`Dockerfile.community`), and 1.15.0's Gradle toolchain is Java 21. The B1/B2 golden files were
  recorded on 1.15.0 (it differs from 1.16.0 in some rounding, so they are not interchangeable).

### Build and validate (no spend)
Fineract 1.15.0 needs **JDK 21** (Gradle toolchain) and runs on the Gradle 8.14.5 wrapper.
```sh
git clone --depth 1 --branch 1.15.0 https://github.com/apache/fineract ../fineract-1.15.0
export FINERACT_JAVA_HOME=/path/to/jdk-21            # or JAVA_HOME; e.g. Temurin 21
python3 run.py validate-suites --fineract-dir ../fineract-1.15.0 --tasks B1..B4
```
Each suite runs through the narrowest task that covers it, one module's unit tests filtered to the
suite (`harness.yaml` `fineract.test_command`), never the whole build: no database and no
integration tests. Measured on an M-series laptop: the first run is about 3 minutes (dependency
download and compiling the core modules), and each later suite run is 15–35 s. The validator needs
a clean checkout and puts every file and patch back afterwards.

### Setup for a banking stage (once)
1. **The demo repositories** (`harness.yaml` `fineract.forks`), one per arm, private, on the
   operator's personal account `paramg`. Each is one snapshot commit of the 1.15.0 tree,
   "NorthPeak core banking, based on Apache Fineract 1.15.0" (`63dd10e41`); `LICENSE_*` and
   `NOTICE_*` are intact and `README.md` carries a demo-fork note.

   | Arm | Repository |
   |---|---|
   | A | https://github.com/paramg/northpeak-fineract-a |
   | AP (A′) | https://github.com/paramg/northpeak-fineract-ap |
   | B | https://github.com/paramg/northpeak-fineract-b |

   The Sonnet pairs (`bank_learning_e1`, engine E1) need their own set; until `fineract.forks.E1`
   names one, the harness refuses E1 before it touches BOSS.
2. **Give BOSS's GitHub App the three repositories**: github.com › Settings › Applications ›
   Installed GitHub Apps › the BOSS app › Configure › Repository access › Only select
   repositories › add the three › Save. Then register each one in BOSS for its arm's project.
3. **One BOSS project per arm** (for example `northpeak-bank-a`, `-ap`, `-b`), so the
   `knowledge_graph` switch and what the graph captured in one arm never reach another.
4. **A local clone per arm**, for the faults the harness pushes before B3 and B4 and for the hidden
   suites it runs: `git clone https://github.com/paramg/northpeak-fineract-ap ../northpeak-fineract-ap`.
5. **BOSS with the keyword switch** (`rel/rc10.10-kg` at `f1f7fb6af` or later), a BOSS API key for
   the case-study project in `BOSS_CASE_STUDY_API_KEY`, and `FINERACT_JAVA_HOME` at a JDK 21.

### Stages (only after the operator approves the spend)
```sh
python3 run.py estimate --stage bank_smoke         # open model (E2), A/AP/B x B1..B4: 12 builds, $7.20–22.80, ceiling $36
python3 run.py estimate --stage bank_learning_e1   # Sonnet 5 (E1), A/B x B1..B4: 8 builds, $48–80, ceiling $120
```
`bank_smoke`, one arm at a time (A, then AP, then B), each in its own project and clone:
```sh
export BOSS_CASE_STUDY_API_KEY=...                 # never committed
export FINERACT_JAVA_HOME=/path/to/jdk-21
for arm in A AP B; do
  slug=$(echo "$arm" | tr A-Z a-z)
  python3 run.py run --stage bank_smoke --arm "$arm" --engine E2 --tasks B1..B4 --approved-by "<operator>" \
      --project-id "northpeak-bank-$slug" --fineract-dir "../northpeak-fineract-$slug" --resume
done
python3 report.py artifacts/case-study/knowledge/<date>
```
Before B3 and B4 the harness pushes the task's `fault.patch` onto the arm's `main` as the release.
