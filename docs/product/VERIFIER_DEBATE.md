# Bounded generator↔verifier debate (VAG #108)

> Spec for Plane #108 (Engine & Model Quality / VAG module). Implemented on
> `feat/vag-debate`. Orchestration is pure; the debater roles are opt-in (paid).

## 1. Why

The refute panel (#104) returns a verdict *and a confidence*. Decisive verdicts we act on. But a
**contested** case — the panel split near its quorum — is exactly where a single fail-closed default
is most likely to be *wrong*: reject it → false-fail on good code; pass it → a real bug slips through.
Rather than guess, resolve it the way people do: a short, **bounded** argument (Ng's multi-agent
debate pattern, quorum-cheap).

## 2. How

`is_contested(confidence, low, high)` gates the debate to the mushy middle (default band 0.34–0.66) —
outside it, trust the panel and skip the cost. `run_debate` then alternates generator-defense and
verifier-challenge for up to `max_rounds`, ending the instant one side yields:

| outcome | verdict | confidence |
|---|---|---|
| generator **concedes** | `refuted` (bug is real) | high (0.95) |
| verifier **withdraws** | `survived` (objection didn't hold) | high (0.9) |
| unresolved + **judge** | judge's call | medium (0.6) |
| unresolved, no judge | `refuted` (**fail-closed** — a live dispute is not a pass) | low (0.5) |

"Bounded" is the point: `BOSS_DEBATE_MAX_ROUNDS` (default 2) caps the spend; early concession/withdrawal
usually ends it in round 1.

## 3. Implementation

- **`apps/api/app/services/verifier_debate.py`** — `is_contested`, `run_debate(claim, defend_fn,
  challenge_fn, judge_fn?, max_rounds)`, and `resolve_contested(panel_verdict, panel_confidence,
  debate_thunk)` (the panel→maybe-debate glue: the paid debate is a thunk, only built when needed).
  `agent_debaters(settings, …)` wires the three roles to real models — the challenger runs on a
  **different engine** than the builder (independence, #110); each turn is one read-only call ending in
  a machine-readable `CONCEDE/WITHDRAW/VERDICT` line.
- Config: `BOSS_DEBATE_MAX_ROUNDS` (2), `BOSS_DEBATE_CONTESTED_LOW` (0.34), `BOSS_DEBATE_CONTESTED_HIGH` (0.66).
- **8 tests** (`apps/api/tests/test_verifier_debate.py`): the contested band, early concession/withdrawal,
  unresolved fail-closed vs judge, the round bound, and the panel glue skipping/running the debate.

## 4. Next

Wire into the governed review panel: when `_aggregate_panel` returns a confidence in the contested
band, call `resolve_contested` with an `agent_debaters` thunk before finalizing the gate; record the
debate transcript in the run evidence. Tune the band from the #105 meta-eval (where do false-fails and
false-passes actually cluster on the confidence axis?).
