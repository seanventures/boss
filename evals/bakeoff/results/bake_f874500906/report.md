# Bake-off report — batch `bake_f874500906`

Total runs: 5. Costs come from `llm_calls` only ($1 = 1,000,000 micros); `est. calls` counts calls priced via the DEFAULT_PRICE fallback — add BOSS_MODEL_PRICES_JSON entries if it is non-zero.

| config | runs | pass rate | $/verified outcome | total $ | retries | mean wall clock | false pass | est. calls |
|---|---|---|---|---|---|---|---|---|
| claude_baseline | 5 | 60% | $1.80 | $5.39 | 0 | 1608.5s | 0/0 | 0 |

**claude_baseline failures:** security_clean×1, tests_strong_enough×1
