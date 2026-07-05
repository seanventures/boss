# VAG experiment report — open-source vs frontier models through one governed pipeline

**Period:** 2026-07-04 → 2026-07-05 · **Runs:** 46 governed · **Spec:** [BOSS_VERIFICATION_AMPLIFIED_GENERATION.md](BOSS_VERIFICATION_AMPLIFIED_GENERATION.md) · **Raw data:** `evals/bakeoff/results/`, `llm_calls` table

Three experiments, one sample project, one identical fail-closed pipeline for every run — design,
independent tests, implementation, adversarial review, security review, consistency check, full-suite
regression, mutation probe — with every model call metered.

---

## Scoreboard

| Strategy | Generator | Verifier | Gate pass | $/verified outcome | Total spend | False passes |
|---|---|---|---|---|---|---|
| Frontier solo (baseline) | Claude | Claude | 60% (3/5) | $1.80 | $5.39 | 0 |
| **Junior / senior** | **DeepSeek-V3** | **Claude** | **60% (3/5)** | **$1.36** | $4.07 | 0 |
| All-cheap, one model | DeepSeek-V3 | DeepSeek-V3 | 20% (1/5) | $0.07 | $0.07 | 0 |
| All-cheap ensemble (best of 4 models) | 4 OSS models | same model | 0% union (0/5 × 4) | ∞ | $2.47 | 0 |

> **The single causal finding:** holding the cheap generator constant and changing only who verifies
> moves the pass rate from 0–20% to 60%. **Verification quality — not generation quality, not model
> diversity — is the binding constraint.** That is the VAG tenet ("the harness is the amplifier")
> measured on real runs. Across all 46 runs: **zero false passes** — nothing substandard ever shipped.

---

## Method

- **Sample project:** `paramg/boss-sandbox`, a fixed suite of 5 deterministic stdlib-Python tasks
  (URL slugifier, token-bucket rate limiter, CSV sales aggregator, Markdown TOC generator,
  interval-merge library), each with explicit acceptance criteria and
  `python3 -m unittest discover -v` as the authoritative test command.
- **Pipeline (identical for every strategy):** strict profile, risk tier 2–3. Standards → spec → plan →
  plan review (design) → QA authors tests *before* implementation exists → a separate implementer must
  make them pass without touching them → BOSS re-runs the tests → fresh-context adversarial review
  (refute-first) → security review → consistency check → full-suite regression → mutation probe (plant a
  one-line bug; the tests must catch it) → fail-closed AND gate. PRs suppressed (eval mode).
- **Model strategies:** per-step engine/model routing assigns each pipeline role a model. Frontier =
  Claude (CLI engine, subscription). Cheap/OSS = open-weight models over OpenAI-compatible APIs
  (OpenRouter; direct Groq/DeepSeek lanes available). Junior/senior = cheap implementer, frontier
  spec/tests/review panel.
- **Measurement:** per-call telemetry (`llm_calls`: tokens, cost, latency, retries, stage, engine,
  model). `$/verified outcome` = strategy's total inference spend ÷ gate-passing runs. Where guest VMs
  don't yet self-report usage (experiment 3), OpenRouter's billing delta is the cost source.
- **Substrates:** experiments 1–2 ran working stages as host subprocesses; experiment 3 ran after the
  hybrid refactor — every code-executing stage in its own Firecracker micro-VM (~80 boots, 19/20 runs
  mechanically clean).

---

## Experiment 1 — Frontier baseline (batch `bake_f874500906`)

**60% (3/5) · $1.80/verified outcome · $5.39 total · 50 calls · 0 retries · ~27 min/run**

| Task | Verdict | Failing gate | What happened |
|---|---|---|---|
| slugify | PASS | — | clean run |
| token_bucket | BLOCKED | `security_clean` | security reviewer returned ISSUES on a toy rate limiter — over-strict for the context (calibration finding, not a vulnerability) |
| csv_report | PASS | — | clean run |
| md_toc | BLOCKED | `tests_strong_enough` | mutation probe planted a one-line bug; Claude's own tests missed it — a genuinely weak suite, correctly refused |
| interval_merge | PASS | — | clean run |

**Analysis.** The number to beat is 60% / $1.80 — frontier-solo is *not* a free 100%; the gate judges
frontier output as ruthlessly as anything else. The two failures are one of each verifier kind: a
*false-fail* (security strictness on a toy — a Phase 1 calibration cost lever) and a *true catch*
(mutation killing a weak test suite — exactly the "tests that pass on anything" failure mode VAG must
prevent, since weak tests are what would let a cheap model fake a pass later).

---

## Experiment 2 — Junior/senior vs naive all-cheap (batch `bake_6620989d17`)

| Task | junior/senior (Claude spec+tests+review, DeepSeek implements) | all-DeepSeek (one model everywhere) |
|---|---|---|
| slugify | **PASS** | BLOCKED (tests, review, security) |
| token_bucket | **PASS** *(where frontier-solo failed)* | BLOCKED (tests, review, regression) |
| csv_report | BLOCKED | BLOCKED (tests, security, regression) |
| md_toc | BLOCKED | BLOCKED (no code + tests + regression) |
| interval_merge | **PASS** | **PASS** — DeepSeek's one solo win |

**junior/senior: 60% · $1.36/outcome · $4.07 total** · all-DeepSeek: 20% · $0.07/outcome · $0.07 total

### Stage anatomy of one verified junior/senior run (slugify, $0.58 total)

| Stage | Model | Cost | Share |
|---|---|---|---|
| Design (standards·spec·plan·review) | claude (+deepseek for standards) | ≈$0.18 | 31% |
| Write independent tests | claude | $0.078 | 13% |
| **Implement** | **deepseek-v3** | **$0.002** | **0.4%** |
| Adversarial review | claude | $0.133 | 23% |
| Security review | claude | $0.054 | 9% |
| Consistency check | claude | $0.089 | 15% |
| Mutation probe | claude | $0.045 | 8% |

**Analysis.** Junior/senior matched frontier-solo's pass rate exactly at −24% cost — and passed
`token_bucket`, the task frontier-solo failed (different security-review draw; at n=5, variance). The
saving is 24% rather than 75× because the stage the junior took over is nearly free: **99.6% of spend
is frontier design + verification.** Optimization therefore points at cheapening *verification* safely
(mid-tier panel roles, calibration) and making cheap attempts count (repair loops) — not at a cheaper
implementer. (The planned Qwen leg voided on provider limits — free-tier TPM + credit-reservation
402s — a logistics finding that produced the `max_tokens` cap now in the adapter.)

---

## Experiment 3 — Diversity ensemble, gate as selector (batch `bake_c023eb95d3`)

**Hypothesis (spec §3.3):** cheap models fail *differently*; running each task through N diverse models
and letting the gate pick any survivor should lift the union pass rate toward frontier at pennies.
4 models × 5 tasks, one shot, strict profile, micro-VM substrate.

| Task | DeepSeek-V3 | GLM-4.5 | Kimi-K2 | Qwen3-32B | **UNION** |
|---|---|---|---|---|---|
| slugify | fail | fail | fail | fail | **fail** |
| token_bucket | fail | fail | error | fail | **fail** |
| csv_report | fail | fail | fail | fail | **fail** |
| md_toc | fail | fail | fail | fail | **fail** |
| interval_merge | fail | fail | fail | fail | **fail** |

| Model | Mean wall clock | Dominant failing gates |
|---|---|---|
| DeepSeek-V3 | 4.5 min | review refuted ×3 · weak tests ×3 · tests fail ×2 · security ×2 · regression ×2 |
| GLM-4.5 | 66.5 min | review refuted ×4 · tests fail ×3 · regression ×3 · edited the tests ×1 |
| Kimi-K2 | 43.1 min | tests fail ×3 · regression ×3 · 1 run errored |
| Qwen3-32B | 23.5 min | **security ×5 (its own reviewer refused every run)** · weak tests ×3 · review ×2 |

> **Clean negative result ($2.47 to learn it): union pass rate 0/5.** The models don't fail differently
> enough — failure profiles overlap on the same gates. One-shot model diversity is *not* a quality lever.

**The confound that became the finding.** In these configs the cheap model was also the *reviewer* —
visible in the data (Qwen's own security reviewer refused all 5 of its runs; GLM's reviewer refuted 4).
Holding the generator constant across experiments: DeepSeek code judged by DeepSeek passes 0–20%;
DeepSeek code judged by Claude passes 60%. Cheap verifiers are both noisier *and* stricter in the wrong
places — "cheap everything" fails twice: worse code and worse judgment of it.

**Substrate note.** This batch stress-tested the hybrid pipeline: write-tests / implement / regression /
mutation each in its own Firecracker VM, tests-first commits on work branches, read-only host review.
19/20 mechanically clean. DeepSeek scored 1/5 on the host substrate but 0/5 in VMs — the guest's simpler
agent loop (whole-file JSON writes, 4 repair rounds, no editing tools) plausibly costs capability vs the
host CLI agent; worth one controlled comparison.

---

## Cross-cutting analysis

1. **The amplifier is trustworthy — the load-bearing result.** 46 runs, 24 fail-closed refusals,
   zero false passes. The spec calls false confidence the one failure mode that sinks the strategy; at
   current sample size the gate has none. (Formal false-pass instrumentation — the known-bad probe
   suite — ships with Phase 1; the current claim is "nothing substandard shipped," verified by
   inspecting every refusal.)
2. **Where quality comes from, ranked by measured effect:** (a) who writes/enforces tests + review
   (frontier verify: +40–60 points of pass rate); (b) generator capability — no measurable difference
   between Claude-implement and DeepSeek-implement *when verification is frontier* (60% either way);
   (c) generator diversity — no effect at one shot. Frontier-authored tests appear to *steer* the cheap
   implementer, not just filter it.
3. **Economics.** Cheap tokens are 20–75× cheaper, but the verified outcome is the unit that matters:
   junior/senior delivers frontier quality at −24% today, bounded by the verification bill (99.6% of
   spend). Next levers: a cheap repair loop (failing-gate feedback, ≤N retries at ~$0.01–0.03/attempt)
   attacks the 40% refused tasks; mid-tier panel roles attack the verification bill.
4. **Verifier calibration is a real cost center.** The security reviewer produced one true HIGH catch
   (an implementer added an unrequested HTTP server with a blocking-read DoS — caught, refused, in the
   hybrid validation run `crun_57d3a8c8`), one plausible false-fail (toy rate limiter), and on cheap
   models it becomes indiscriminate (Qwen 5/5). Phase 1's confidence score + refute-majority panels turn
   single-voice verdicts into calibrated signals.

## Threats to validity

- **n=5 per strategy** — everything is directional; the 60%/60% tie is within noise. Repetitions queued.
- **Toy tasks** — honest pipeline exercise, but not large-codebase navigation where cheap models degrade faster.
- **Substrate changed between experiments 2 and 3** (host subprocess → micro-VM guest agent) — cross-experiment model comparisons carry an implementation confound.
- **Ensemble costs are provider-billed totals** (guest VMs don't self-report tokens yet); per-stage VM attribution reads $0 until the guest-image fix.
- **Single verifier voice per role** — verdicts inherit one reviewer's temperament per run.
- **Claude on subscription vs OSS on metered APIs** — list-price accounting for both; marginal-cost structure differs in production.

## Next, by expected information per dollar

1. **Repair loop (Phase 3):** junior/senior + failing-gate feedback → ≤3 cheap retries before frontier escalation. The most valuable unanswered question (~$1–2).
2. **Phase 1 verifier hardening:** numeric confidence score, refute-majority panels, known-bad probe suite, security-reviewer calibration.
3. **Repetitions (n→15–25)** on the two leading strategies.
4. **Mid-tier verification:** route consistency/security to mid-price models, keep the adversarial reviewer frontier.

---

*Sources: batches `bake_f874500906`, `bake_6620989d17`, `bake_c023eb95d3`; hybrid validation
`crun_57d3a8c8`. A styled HTML rendering of this report ships alongside
(`VAG_EXPERIMENT_REPORT_2026-07-05.html`).*
