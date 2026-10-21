# NorthPeak Retail: the Knowledge Graph case study harness

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
