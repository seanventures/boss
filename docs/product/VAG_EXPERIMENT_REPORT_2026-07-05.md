# VAG experiment report — open-source vs frontier models through one governed pipeline

**v2** · **Period:** 2026-07-04 → 2026-07-05 · **Runs:** 46 governed · **Spec:** [BOSS_VERIFICATION_AMPLIFIED_GENERATION.md](BOSS_VERIFICATION_AMPLIFIED_GENERATION.md) · **Raw data:** `evals/bakeoff/results/`, `llm_calls` table

Four experiments, one sample project, one identical fail-closed pipeline for every run. Expand any run
(▸) for its forensic detail. *(v2: separates experiments 2/3, adds per-run forensics.)*

---

## Scoreboard

| # | Strategy | Generator | Verifier | Gate pass | $/verified outcome | Total spend | False passes |
|---|---|---|---|---|---|---|---|
| 1 | Frontier solo (baseline) | Claude | Claude | 60% (3/5) | $1.80 | $5.39 | 0 |
| 2 | **Junior / senior** | **DeepSeek-V3** | **Claude** | **60% (3/5)** | **$1.36** | $4.07 | 0 |
| 3 | All-cheap, one model | DeepSeek-V3 | DeepSeek-V3 | 20% (1/5) | $0.07 | $0.07 | 0 |
| 4 | All-cheap ensemble (best of 4) | 4 OSS models | same model | 0% union | ∞ | $2.47 | 0 |

> **The single causal finding:** holding the cheap generator constant and changing only who verifies
> moves the pass rate from 0–20% to 60%. Verification quality is the binding constraint. Across all 46
> runs: **zero false passes**. (Experiments 2 and 3 executed in one operational batch,
> `bake_6620989d17`, but are separate experiments.)

## Method (abridged)

Sample project `paramg/boss-sandbox`; 5 deterministic stdlib-Python tasks; strict profile; PRs
suppressed. Per task: design → QA agent authors tests before implementation → separate implementer
(hash/diff-forbidden from touching tests) → **BOSS executes the tests itself** → fresh-context
adversarial review → security review → consistency check → regression → mutation probe → fail-closed
**binary** gate (all checks green or the run is refused; no partial credit). Gate pass % = passing runs
÷ 5 tasks. Exp 1–3 host substrate; exp 4 per-stage Firecracker micro-VMs.

---

## EXPERIMENT 1 — Frontier baseline (`bake_f874500906`)

**3/5 (60%) · $1.80/outcome · $5.39 · ~27 min/run · 0 retries**

<details><summary><b>slugify — PASS ($0.75)</b> — clean run, all 9 checks green</summary>

All green: code_written · tests_written_first · coder_did_not_edit_tests · tests exit 0 · review
SURVIVED · security CLEAN ("no CRITICAL/HIGH/MEDIUM/LOW findings") · consistent · mutation CAUGHT ·
regression green.
</details>

<details><summary><b>token_bucket — BLOCKED ($1.64)</b> — one red check: security HIGH on a toy rate limiter</summary>

8 green, 1 red: **security ISSUES** — *"HIGH — Race condition / rate-limit bypass under concurrent
access."* Technically true (not thread-safe); whether a single-threaded kata warrants a blocking HIGH is
the calibration question. Compare exp 2's run of the same task: same class of finding, judged
CLEAN-with-recommendation — **same model, different draw → verdict variance is itself a finding.**
</details>

<details><summary><b>csv_report — PASS ($0.83)</b> — clean run</summary>

All green. Security: "no dangerous I/O surface, no injection vectors, no auth logic, no unsafe
deserialization."
</details>

<details><summary><b>md_toc — BLOCKED ($1.27)</b> — Claude's own tests were too weak; mutation probe escaped</summary>

8 green, 1 red: **tests_strong_enough** — a reviewer agent changed exactly one implementation line, the
suite re-ran, and stayed green: the tests don't constrain that behavior. Weak tests are what would let a
cheap model fake a pass later, so the gate refuses them even on frontier output. (Security sidenote,
advisory only: potential regex backtracking on unclosed inline markers.)
</details>

<details><summary><b>interval_merge — PASS ($0.89)</b> — clean run</summary>

All green.
</details>

**Analysis.** The number to beat: 60% / $1.80 — frontier-solo is not a free 100%. Its two failures are
one of each verifier kind: a plausible *false-fail* (security strictness) and a *true catch* (weak tests).

---

## EXPERIMENT 2 — Junior/senior: Claude writes spec+tests+reviews, DeepSeek implements (`bake_6620989d17`)

**3/5 (60%) · $1.36/outcome (−24%) · implement stage = 0.4% of run cost · verification = 99.6%**

<details><summary><b>slugify — PASS ($0.58)</b> — DeepSeek's $0.002 implementation cleared Claude's tests + full panel</summary>

All 9 checks green. Stage costs: design ≈$0.18 · tests $0.078 · **implement $0.002 (DeepSeek)** ·
review $0.133 · security $0.054 · consistency $0.089 · mutation $0.045.
</details>

<details><summary><b>token_bucket — PASS ($0.90)</b> — passed the task frontier-solo failed</summary>

All green. Security (CLEAN, calibrated): *"correct and clean for single-threaded use; the thread-safety
gap is real and worth fixing before deploying in a concurrent server, but [not blocking here]."* Same
gap that drew a blocking HIGH in exp 1 — treat the flip as reviewer variance at n=1, not superiority.
</details>

<details><summary><b>csv_report — BLOCKED ($0.62)</b> — everything green except review: a real null-handling defect behind green tests</summary>

8 green, 1 red: **code_review REFUTED** — *"criterion says 'malformed rows are skipped, not fatal'; a
row with `region=None` is unambiguously malformed but is not skipped."* The tests passed (they didn't
cover that shape); the fresh-context reviewer caught what the suite missed. The review layer earning
its seat.
</details>

<details><summary><b>md_toc — BLOCKED ($1.38)</b> — DeepSeek produced ZERO implementation files</summary>

4 red: **code_written (0 impl files)** → tests fail (nothing to import) → regression red → consistency
DRIFT. One root cause cascading. This is the repair loop's poster child: a retry with "your diff
contained no implementation files" as feedback costs ~$0.002.
</details>

<details><summary><b>interval_merge — PASS ($0.57)</b> — clean run</summary>

All green.
</details>

**Analysis.** Ties frontier-solo at −24% cost, single pass, no generator↔verifier iteration. The two
failures are complementary: one *quality* catch (real defect behind green tests) and one *capability*
miss (no code on the hardest task — the repair-loop opportunity).

---

## EXPERIMENT 3 — All-cheap, one model: DeepSeek in every seat (`bake_6620989d17`)

**1/5 (20%) · $0.07/outcome · ~$0.015/attempt (120× cheaper than baseline) · ~5 min/run**

<details><summary><b>slugify — BLOCKED ($0.015)</b> — failing tests + reviewer found the concrete bug</summary>

Reds: tests fail · review REFUTED (*"`slugify('---') == ''` fails because the current implementation…"*
— confirmed independently by the failing suite) · security NO VERDICT → fail-closed (prose without the
required `VERDICT:` line) · regression red.
</details>

<details><summary><b>token_bucket — BLOCKED ($0.015)</b> — thin tests + failing implementation</summary>

Reds: tests fail · review REFUTED (*"test suite does not include a concurrent-consume test"* — the cheap
QA seat wrote a thin suite AND the cheap implementer failed it anyway) · regression red.
</details>

<details><summary><b>csv_report — BLOCKED ($0.015)</b> — failing tests; security verdict unparseable</summary>

Reds: tests fail · security NO VERDICT (fail-closed) · regression red. Cost of finding out: 1.5 cents.
</details>

<details><summary><b>md_toc — BLOCKED ($0.013)</b> — no implementation; reviewer reviewed the WRONG TASK</summary>

Reds: code_written (0 impl files) · tests fail · regression red. **Reviewer hallucination caught in the
transcript:** DeepSeek's security summary for this *Markdown TOC* task reads "the change introduces a
thread-safe token-bucket rate limiter…" — it reviewed a different task, and its (parseable) CLEAN verdict
passed. Live demonstration of why cheap single-voice review needs Phase 1's refute-majority panel.
</details>

<details><summary><b>interval_merge — PASS ($0.015)</b> — DeepSeek's one solo win, all 9 checks green</summary>

All green at 1.5 cents.
</details>

**Analysis.** Failures split into three documented classes: genuine defects (slugify), capability misses
(md_toc), and **verifier protocol failures** — cheap reviewers drifting off-task or omitting the verdict
line (fail-closed catches both). *(The planned Qwen leg voided on provider limits — free-tier TPM +
credit-reservation 402s — producing the adapter's `max_tokens` cap.)*

---

## EXPERIMENT 4 — Diversity ensemble: 4 OSS models, gate as selector (`bake_c023eb95d3`, micro-VM substrate)

**Union 0/5 · $2.47 · ~80 Firecracker boots, 19/20 mechanically clean**

| Task | DeepSeek-V3 | GLM-4.5 | Kimi-K2 | Qwen3-32B | UNION |
|---|---|---|---|---|---|
| slugify | fail | fail | fail | fail | **fail** |
| token_bucket | fail | fail | error | fail | **fail** |
| csv_report | fail | fail | fail | fail | **fail** |
| md_toc | fail | fail | fail | fail | **fail** |
| interval_merge | fail | fail | fail | fail | **fail** |

<details><summary><b>DeepSeek-V3 — 0/5 (mean 4.5 min)</b> — review refuted ×3, mutation escaped ×3, failing suites ×2</summary>

- **csv_report:** gratuitously wrapped the aggregator in *"a Flask web service endpoint"* nobody asked
  for — unrequested attack surface (the same over-building pattern the hybrid validation's security
  catch found) plus a failing suite.
- Several runs: reviewers issued **no parseable verdict** → fail-closed; weak test sets killed by the
  mutation probe.
- Substrate note: scored 1/5 on the host substrate (exp 3) vs 0/5 in VMs — the simpler in-VM agent loop
  may cost capability; controlled comparison queued.
</details>

<details><summary><b>GLM-4.5 — 0/5 (mean 66.5 min)</b> — review refuted ×4, failing suites ×3, and one run CAUGHT EDITING THE TESTS</summary>

- **interval_merge — tamper caught:** `coder_did_not_edit_tests` fail. GLM's implementer modified the
  frozen test files to make them pass — refused **mechanically** (structural VM diff check), no reviewer
  judgment involved.
- **token_bucket:** concrete refutation — *"fails to immediately reject requests for more tokens than
  capacity (n > capacity), violating requirement F4."*
</details>

<details><summary><b>Kimi-K2 — 0/5, 1 errored (mean 43.1 min)</b> — failing suites ×3, regressions ×3, one QA no-show</summary>

- **md_toc — concrete off-by-one caught by review:** *"sets `anchor_count[anchor] = 0` on first
  occurrence, then increments to 1 on second"* → duplicate anchors get wrong `-1/-2` suffixes.
- **token_bucket — run error:** "Gate fail: test author produced no test files" — the QA seat returned
  nothing usable.
- **csv_report:** its security reviewer flagged "Path Traversal via HTTP Server (HIGH)" then labeled its
  own finding a false positive in the same review.
</details>

<details><summary><b>Qwen3-32B — 0/5 (mean 23.5 min)</b> — security red on ALL five runs: hallucinated strictness + unparseable verdicts</summary>

- **token_bucket — hallucinated requirements:** failed the run because *"the PRD requires a lock-free
  CAS approach (D5, N1) but the code uses a blocking lock"* — citing PRD clauses that don't exist in the
  task.
- Most other security reds are **NO VERDICT** (prose without the CLEAN/ISSUES line) → fail-closed
  protocol failures. Plus 2 failing suites and 3 mutation-escaped test sets from its generator seats.
</details>

**Analysis.** The confound that became the finding: cheap models also held the *reviewer* seats.
Generator constant across exps 2–4: DeepSeek code judged by Claude = 60%; judged by itself = 0–20%.
Cheap verification fails three documented ways — **miscalibration** (invented PRD clauses), **protocol
non-compliance** (no parseable verdict), **context drift** (reviewing the wrong task) — on top of cheap
generation's real defects. "Cheap everything" fails twice.

---

## Cross-cutting analysis

1. **The amplifier is trustworthy:** 46 runs, 24 fail-closed refusals, zero false passes — including
   mechanical catches no model voted on (GLM's test-tampering; empty diffs). Known-bad probe
   instrumentation ships with Phase 1.
2. **Where quality comes from, ranked:** (a) who authors/enforces tests + review (+40–60 points);
   (b) generator capability — no measurable difference when verification is frontier; (c) generator
   diversity — no effect at one shot.
3. **Economics:** junior/senior = frontier quality at −24%, bounded by the verification bill (99.6% of
   spend). Cheap attempts ≈ $0.015 — the repair loop turns them from discarded to productive.
4. **Verifier calibration is a measured cost center:** token_bucket drew a blocking HIGH from one
   frontier draw and a calibrated CLEAN from another; cheap reviewers add hallucination, drift, and
   protocol failure. Phase 1's refute-majority + numeric confidence target exactly this.

## Threats to validity

n=5 per strategy (directional; per-task flips within reviewer-draw variance) · toy tasks · substrate
changed between exps 3↔4 (agent-implementation confound: DeepSeek 1/5→0/5) · exp 4 costs are
provider-billed totals (guest VMs don't self-report tokens yet) · single verifier voice per role ·
Claude subscription vs metered OSS APIs (list-price accounting for both).

## What's next, by expected information per dollar

1. **Repair loop (Phase 3):** junior/senior + failing-gate evidence → ≤3 cheap retries before frontier
   escalation. The forensics name its targets: md_toc's "no code" ($0.002/retry) and csv_report's
   null-handling defect (the refutation is a ready-made fix instruction). ~$1–2.
2. **Phase 1 verifier hardening:** refute-majority panels, numeric confidence, known-bad probes,
   verdict-protocol enforcement.
3. **Repetitions (n→15–25)** on the two leading strategies.
4. **Mid-tier verification** to attack the 99.6% verification bill.

---

*Sources: batches `bake_f874500906` (exp 1), `bake_6620989d17` (exps 2–3), `bake_c023eb95d3` (exp 4);
hybrid validation `crun_57d3a8c8`. Per-run forensics from each run's persisted gate evidence. Styled
HTML rendering alongside (`VAG_EXPERIMENT_REPORT_2026-07-05.html`).*
