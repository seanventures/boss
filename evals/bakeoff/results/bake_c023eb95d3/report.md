# Bake-off report — batch `bake_c023eb95d3`

Total runs: 20. Costs come from `llm_calls` only ($1 = 1,000,000 micros); `est. calls` counts calls priced via the DEFAULT_PRICE fallback — add BOSS_MODEL_PRICES_JSON entries if it is non-zero.

| config | runs | pass rate | $/verified outcome | total $ | retries | mean wall clock | false pass | est. calls |
|---|---|---|---|---|---|---|---|---|
| deepseek_all | 5 | 0% | — | $0.05 | 0 | 271.8s | 0/0 | 0 |
| glm_all | 5 | 0% | — | $0.87 | 0 | 3992.5s | 0/0 | 35 |
| kimi_all | 5 | 0% | — | $0.58 | 0 | 2588.4s | 0/0 | 32 |
| qwen_all | 5 | 0% | — | $0.08 | 0 | 1409.2s | 0/0 | 0 |

**deepseek_all failures:** code_review_passed×3, nothing_broke×2, security_clean×2, tests_strong_enough×3, written_tests_pass×2
**glm_all failures:** code_review_passed×4, coder_did_not_edit_tests×1, consistent×1, nothing_broke×3, security_clean×2, tests_strong_enough×1, written_tests_pass×3
**kimi_all failures:** code_review_passed×1, nothing_broke×3, run:failed×1, tests_strong_enough×1, written_tests_pass×3
**qwen_all failures:** code_review_passed×2, consistent×1, nothing_broke×2, security_clean×5, tests_strong_enough×3, written_tests_pass×2
