# Generator/verifier independence contract (VAG #110)

> Spec for Plane #110 (Engine & Model Quality / VAG module). Implemented on
> `feat/vag-independence`. Pure and model-free.

## 1. Why

Every VAG verifier mechanism — the refute panel (#104), the meta-eval (#105), the gate's
`reviewer_independent` flag — is only meaningful if the verifier is genuinely **independent** of the
generator. A model reviewing its own output shares its blind spots: the same training produces the
same wrong intuitions, so it rubber-stamps the very bugs it would have written. Independence was a
*convention* (`BOSS_CODE_REVIEW_ENGINE` defaults to a different engine); #110 makes it a **contract** —
measured, and enforced fail-closed.

## 2. Two axes

- **Diverse family.** The reviewer must not be the same model *family* as the builder — because
  correlated errors come from shared training lineage, not just a shared vendor. Separation is ranked:

  | rank | level | meaning |
  |---|---|---|
  | 0 | `same_model` | same family + same model — the builder judging itself |
  | 1 | `same_family` | same lineage (e.g. two GPTs), different model |
  | 2 | `same_provider` | different family, same vendor (e.g. two models on OpenRouter) |
  | 3 | `independent` | different family **and** different provider |

  Families are derived from the engine + model string (`claude`, `gpt`, `gemini`, `deepseek`, `qwen`,
  `llama`, `mistral`, `grok`), falling back to the provider lane for unknown lineages.

- **No shared trace.** The reviewer must judge the *code*, not the builder's reasoning. If the
  builder's chain-of-thought leaks into the review context, the reviewer inherits its rationalizations.
  `shares_trace` detects a verbatim run of the builder trace appearing in the review context.

## 3. The gate

`independence_gate` is fail-closed: **every** verifier must be separated from the builder by at least
`required_level` (`off | model | family | provider`; default **`family`**). The panel is only as
independent as its **weakest** member, so one same-family reviewer fails the contract even alongside
independent ones. `BOSS_INDEPENDENCE_REQUIRED_LEVEL` sets the bar.

## 4. Implementation

- **`apps/api/app/services/independence.py`** — `family_of`, `separation`, `assess_independence`
  (weakest-link), `independence_gate`, `shares_trace`, and `configured_contract(settings)` which
  audits BOSS's *current* builder-vs-reviewer config. Reuses `model_rate_limiter.provider_of`.
- **`POST /v1/independence-check`** — assess an explicit `{builder, verifiers}`, or (no payload)
  audit the running configuration.
- **9 tests** (`apps/api/tests/test_independence.py`): family taxonomy, separation ranks, weakest-link
  aggregation, gate pass/fail per level + self-review + off, shared-trace detection, the config audit,
  and the endpoint.

## 5. Next

- Enforce it in the pipeline: block/annotate a governed run whose configured panel is below the
  required level, and write the measured level into `quality_gates.reviewer_independent`.
- Extend `shares_trace` beyond verbatim overlap (semantic similarity); expand the family taxonomy as
  new model lineages appear.
