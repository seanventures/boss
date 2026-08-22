# Bake-off report — batch `bake_a78b9ffa55`

Total runs: 25. Costs come from `llm_calls` only ($1 = 1,000,000 micros); `est. calls` counts calls priced via the DEFAULT_PRICE fallback — add BOSS_MODEL_PRICES_JSON entries if it is non-zero.

The three decision axes: **quality** (pass rate), **budget** ($/verified), **time** (s/verified = wall-clock per passing run; p90 = tail latency a user waits on).

| config | runs | pass rate | $/verified | s/verified | mean/p90 wall | repairs used | total $ | false pass |
|---|---|---|---|---|---|---|---|---|
| full_azure_repair3 | 5 | 0% | — | — | 0s/0s | 0 | $0.00 | 0/0 |
| full_claude_repair3 | 5 | 0% | — | — | 0s/0s | 0 | $0.00 | 0/0 |
| hybrid_azure_repair3 | 5 | 0% | — | — | 0s/0s | 0 | $0.00 | 0/0 |
| hybrid_claude_norepair | 5 | 0% | — | — | 0s/0s | 0 | $0.00 | 0/0 |
| hybrid_claude_repair3 | 5 | 0% | — | — | 0s/0s | 0 | $0.00 | 0/0 |

**full_azure_repair3 failures:** run:blocked×5
**full_claude_repair3 failures:** run:blocked×5
**hybrid_azure_repair3 failures:** run:blocked×5
**hybrid_claude_norepair failures:** run:blocked×5
**hybrid_claude_repair3 failures:** run:blocked×5
