# Verifier meta-eval — measuring the verifier (VAG #105)

> Spec for Plane #105 (Engine & Model Quality / VAG module). Implemented on
> `feat/vag-verifier-meta-eval`. Default **dry-run** (offline, $0); real reviewer is opt-in.

## 1. Why

VAG's premise is that **verification is the binding constraint**: cheap models can generate,
but a verifier that waves bad code through — or cries wolf on good code — collapses the
pipeline. The refute panel (#104) gave the verifier a **confidence score**, but a number is not
evidence the verifier *works*. #105 supplies the evidence: run the verifier over a **labeled
corpus** of known-bugged and known-clean implementations and measure whether it actually
discriminates, then **gate on that**.

## 2. What it measures (and how it differs from the bake-off)

The bake-off (`bakeoff.py`) runs the **full generate pipeline** per probe and reports one number:
`false_pass` (a known-bad task whose gate passed). That couples verifier quality to generator
variance and only sees the false-*pass* axis.

Meta-eval is the **decoupled** sibling: it feeds the reviewer **fixed** implementations (no
generation, no repair loop — one review call per item) and computes the **full confusion matrix**,
adding the false-*fail* axis the bake-off never sees.

Positive class = **bugged** (the verifier should FLAG it):

| | verifier flags | verifier passes |
|---|---|---|
| **bugged** | TP (bug caught) | FN — **false pass** (fooled) |
| **clean** | FP — **false fail** (over-strict) | TN |

Reported: **recall** (bug-detection rate), **false-pass rate** (the #1 guardrail),
**fp_rate** (false alarms on clean code), precision, accuracy.

## 3. The gate

Fail-closed on the verifier itself (`meta_eval_gate`): **PASS iff `recall ≥ min_recall` and
`fp_rate ≤ max_fp_rate`**, and the corpus contains both classes (else the numbers are meaningless).
Thresholds: `BOSS_VERIFIER_META_MIN_RECALL` (0.8), `BOSS_VERIFIER_META_MAX_FP_RATE` (0.2).

## 4. Implementation

- **`apps/api/app/services/verifier_meta_eval.py`** — pure, model-free scoring: `normalize_verdict`,
  `confusion`, `metrics`, `meta_eval_gate`, `run_meta_eval(corpus, verify_fn, …)` (injectable
  verifier), `render_markdown`, `load_corpus`. Plus two verifiers:
  - `review_verify_fn(settings, …)` — the **real** reviewer: materializes each item's files into a
    throwaway dir and runs the production `_review_prompt` + `_parse_verdict` via a read-only
    `run_role` call (one paid call per item).
  - `stub_verify_fn` — replays each item's `stub_verdict` (a perfect reviewer) for an offline dry-run.
- **`evals/verifier_meta/corpus.json`** — the labeled corpus (starts at 4 bugged + 4 clean;
  bug classes: off-by-one, wrong-tiebreak, missing-edge-case, input-mutation). Keep it **balanced**.
- **`POST /v1/verifier-meta-eval`** — `{corpus?, dry_run?, min_recall?, max_fp_rate?, engine?, model?}`.
  **`dry_run` defaults to true** (never spends budget by accident); `dry_run=false` runs the real reviewer.
- **`scripts/run_verifier_meta_eval.py`** — `--real`, `--engine`, `--model`, `--save`; exit code = gate.

## 5. Testing

`apps/api/tests/test_verifier_meta_eval.py` (16): verdict normalization (fail-closed), confusion
matrix, metric rates, the gate (passes a strong verifier; fails on missed bugs, on over-flagging,
on a single-class corpus), the run harness with perfect/fooled/over-strict/erroring verifiers, the
shipped corpus dry-run, the real reviewer adapter (fake agent — file materialization + verdict parse),
and the endpoint dry-run.

## 6. Next

- Grow the corpus (more bug classes: concurrency, resource leaks, auth/authz, injection; harder/subtler
  defects) — the corpus IS the measurement, so its breadth caps what we can claim.
- Once #104 (refute panel) merges, run `--real` with `BOSS_CODE_REVIEW_PANEL_SIZE>1` to quantify the
  panel's recall/FP lift over a single reviewer — the head-to-head the panel sweep couldn't finish.

## 7. Cross-run ratchet (#107)

The corpus shouldn't stay hand-authored — it should **grow from the verifier's own mistakes**. The
ratchet (`apps/api/app/services/verifier_ratchet.py`) captures every human↔gate disagreement as a
labeled corpus item so the verifier is scored against it from then on:

- **gate FAILED + human OVERRODE** → a **clean** item (a false-fail). Auto-captured in
  `override_code_task` — the judged files are already in `finalize_ctx.pending_diff`.
- **gate PASSED + human REJECTED the PR** → a **bugged** item (a false-pass — the highest-value
  signal). Captured via `POST /v1/verifier-ratchet/capture {run_id, label:"bugged", files?}` (a
  gate-passed run doesn't retain its diff, so supply the files or wire artifact recovery — the
  documented follow-on).

Items are tagged onto the run (`finalize_ctx.ratchet`, the bake-off idiom — no migration) with a
self-contained copy of the files, and merged into the meta-eval corpus by `merged_corpus` (folded into
`POST /v1/verifier-meta-eval` by default, scopeable by `project_id`). Each carries a ground-truth
`stub_verdict` so the offline dry-run stays deterministic. Tests: `apps/api/tests/test_verifier_ratchet.py` (7).

**Follow-on:** auto-capture on `decision.rejected` for a `review_pull_request` decision whose run's gate
passed (needs the passed-run diff — persist it on pass, or read the PR's `diff_artifact_id`); feed the
ratchet into the mutation-gate (#106) and independence (#110) work.
