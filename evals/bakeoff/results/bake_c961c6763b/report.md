# Bake-off report — batch `bake_c961c6763b`

Total runs: 25. Costs come from `llm_calls` only ($1 = 1,000,000 micros); `est. calls` counts calls priced via the DEFAULT_PRICE fallback — add BOSS_MODEL_PRICES_JSON entries if it is non-zero.

The three decision axes: **quality** (pass rate), **budget** ($/verified), **time** (s/verified = wall-clock per passing run; p90 = tail latency a user waits on).

| config | runs | pass rate | $/verified | s/verified | mean/p90 wall | repairs used | total $ | false pass |
|---|---|---|---|---|---|---|---|---|
| full_azure_repair3 | 5 | 0% | — | — | 6145s/6924s | 3×5 | $1.01 | 0/0 |
| full_claude_repair3 | 5 | 80% | $2.24 | 1402s | 1122s/1873s | 0 | $8.96 | 0/0 |
| hybrid_azure_repair3 | 5 | 40% | $0.26 | 18840s | 7536s/7908s | 3×1 | $0.52 | 0/0 |
| hybrid_claude_norepair | 5 | 80% | $1.72 | 3533s | 2826s/3468s | 0 | $6.87 | 0/0 |
| hybrid_claude_repair3 | 5 | 80% | $1.67 | 5524s | 4420s/5056s | 1×1, 3×1 | $6.69 | 0/0 |

### Per-model latency & cost (all calls in the batch)

| model | calls | avg latency | p90 latency | avg $/call | total $ | errors |
|---|---|---|---|---|---|---|
| claude-opus-4-8[1m] | 128 | 37.7s | 58.3s | $0.18 | $22.46 | 0 |
| gpt-4o | 101 | 27.1s | 36.6s | $0.02 | $1.52 | 0 |
| deepseek/deepseek-chat | 36 | 8.2s | 13.4s | $0.00 | $0.07 | 0 |

**full_azure_repair3 failures:** nothing_broke×5, written_tests_pass×5
**full_claude_repair3 failures:** security_clean×1
**hybrid_azure_repair3 failures:** code_review_passed×2, nothing_broke×1, run:failed×1, written_tests_pass×1
**hybrid_claude_norepair failures:** code_review_passed×1, code_written×1, consistent×1, nothing_broke×1, written_tests_pass×1
**hybrid_claude_repair3 failures:** code_review_passed×1, code_written×1, consistent×1, nothing_broke×1, written_tests_pass×1
