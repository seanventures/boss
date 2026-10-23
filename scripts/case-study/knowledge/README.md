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
python3 run.py setup --set retail --github-owner paramg          # once: repos, projects, clones (below)
python3 run.py run --stage smoke --arm A --engine E2 --tasks T1..T4 --approved-by "<operator>" --resume
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

### Setup (once per operator, rerun any time)
One command makes everyone's setup the same, ours included. It is idempotent: what is already in
place is reported and left alone, so rerun it after any operator step.
```sh
export BOSS_CASE_STUDY_API_KEY=...                 # a BOSS API key; never committed, never printed
python3 run.py setup --set banking --github-owner paramg --dry-run     # the plan: reads only
python3 run.py setup --set banking --github-owner paramg               # do it
```
Per arm (A, AP, B; per engine as `harness.yaml` `fineract.forks` names them) it:
1. **creates the private repository** `<owner>/northpeak-fineract-{a,ap,b}` if it is missing: one
   snapshot commit of Apache Fineract at the pinned SHA, "NorthPeak core banking, based on Apache
   Fineract 1.15.0", every `LICENSE_*`/`NOTICE_*` intact and a demo note in `README.md`. The three
   for `paramg`/E2 exist (`63dd10e41`). `--github-owner` must be the owner `harness.yaml` names;
   another operator edits the `forks` entries first (they are the one place the names live);
2. **creates the arm's BOSS project** (`northpeak-bank-a`, `-ap`, `-b`) and sets its
   `knowledge_graph` switch to `off`, `keyword` or `on`, so what one arm's graph captured never
   reaches another (the graph indexes even when the switch is off);
3. **checks the BOSS GitHub App reaches each repository** (it re-reads the App's list), allows it
   for BOSS and registers it in the arm's project with base branch `main`. If the App cannot reach
   one, it prints the step: GitHub › Settings › Applications › Installed GitHub Apps › the BOSS app ›
   Configure › Repository access › Only select repositories › add the repository › Save;
4. **clones each repository** to `scripts/case-study/repos/<name>` (gitignored), where `run` finds it.

It exits 3 while any step needs the operator, and lists them last. `--set retail` does the same for
the Saleor set (`northpeak-saleor-*` and an empty `northpeak-fulfilment-*` per arm, projects
`northpeak-retail-*`). Known blockers on today's BOSS, which the dry run shows:
- **BOSS cannot create a project through its API** (no `POST /v1/projects`; the `project:create`
  permission has no route), so step 2 reports `blocked` until BOSS offers one. Setup uses the route
  as soon as BOSS's OpenAPI lists it.
- **Arm AP needs `knowledge_graph=keyword`**, which arrives with `rel/rc10.10-kg` (`f1f7fb6af`); a
  BOSS that offers only `on`/`off` blocks AP's switch and `run` refuses AP before any spend.

Also needed for a run: `FINERACT_JAVA_HOME` at a JDK 21, and `gh` logged in as an account that can
merge in the demo repositories (the harness merges with `gh` when BOSS does not, below).

### How a task is graded, and how knowledge accumulates
The tasks of a run are sequential and each starts from the accepted result of the ones before it:
1. the arm's clone is reset to origin's `main` (fetch, checkout, `reset --hard`, `clean -fd`;
   ignored build output stays). B3/B4 (and T4) then push their `fault.patch` onto `main` as the
   release; a resumed run whose `main` already holds the fault pushes nothing;
2. BOSS works the task as a code task (`owner` and bare `repo` name, base `main`, open a PR), and
   `answers.yaml` answers its decisions, except the pull request's merge decision (`kind:
   pull_request`), which waits for grading;
3. the harness finds the PR BOSS opened (the run's `pull_request_number`, or its head branch
   `boss/code/<root run>`), fetches its head (`pull/<n>/head`, or the branch), checks it out clean
   and runs the hidden suite there. No PR: unsolved, nothing graded. A PR that carries a hidden
   suite file: unsolved;
4. **passed:** the harness approves BOSS's merge decision (the `after: grading` rule in
   `answers.yaml`) and waits up to `merge.wait_minutes` for BOSS to merge. If BOSS does not (a BOSS
   that leaves merging to a person, or a run with no merge decision), the harness merges exactly
   the graded commit with `gh pr merge --merge --match-head-commit` and records that it did.
   **Failed:** recorded, nothing merged, the decision left unanswered;
5. either way the clone is reset to origin's `main` before the next task.

Each result records `main_before`, `release_commit`, `pr` (number, head branch and SHA),
`graded_commit`, `evaluation` and `merge` (`merged`, `by`: `boss` or `harness (gh)`, `why`,
`merge_commit`), and `main_after`. `report.py` lists them under "What was graded".

### bank_smoke (only after the operator approves the spend)
```sh
python3 run.py estimate --stage bank_smoke         # open model (E2), A/AP/B x B1..B4: 12 builds, $7.20–22.80, ceiling $36
python3 run.py estimate --stage bank_learning_e1   # Sonnet 5 (E1), A/B x B1..B4: 8 builds, $48–80, ceiling $120
```
One arm at a time (A, then AP, then B); each finds its project and clone from `harness.yaml`:
```sh
export BOSS_CASE_STUDY_API_KEY=...                 # never committed
export FINERACT_JAVA_HOME=/path/to/jdk-21
for arm in A AP B; do
  python3 run.py run --stage bank_smoke --arm "$arm" --engine E2 --tasks B1..B4 \
      --approved-by "<operator>" --resume
done
python3 report.py artifacts/case-study/knowledge/<date>
```
`--project-id` (an id, slug or name) and `--fineract-dir` override the defaults.
