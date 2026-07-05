# Bake-off report — batch `bake_6620989d17`

Total runs: 15. Costs come from `llm_calls` only ($1 = 1,000,000 micros); `est. calls` counts calls priced via the DEFAULT_PRICE fallback — add BOSS_MODEL_PRICES_JSON entries if it is non-zero.

| config | runs | pass rate | $/verified outcome | total $ | retries | mean wall clock | false pass | est. calls |
|---|---|---|---|---|---|---|---|---|
| deepseek_all | 5 | 20% | $0.07 | $0.07 | 0 | 299.8s | 0/0 | 0 |
| qwen_all | 5 | 0% | — | $0.02 | 0 | 565.0s | 0/0 | 0 |
| vag_junior_senior | 5 | 60% | $1.36 | $4.07 | 0 | 1775.8s | 0/0 | 0 |

**deepseek_all failures:** code_review_passed×2, code_written×1, nothing_broke×4, security_clean×2, written_tests_pass×4
**qwen_all failures:** run:failed×5
**vag_junior_senior failures:** code_review_passed×1, code_written×1, consistent×1, nothing_broke×1, written_tests_pass×1
