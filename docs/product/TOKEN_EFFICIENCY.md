# Token Efficiency & Consumption Control

Status: Implemented on `ai/grok/platform-ops-epic`  
Owner: platform-ops  
Ledger of record: `llm_calls` (read-only analytics)

## Why

LLM spend is mostly **tokens**. BOSS already records every call on `llm_calls` and surfaces
deterministic cost insights (`GET /v1/budget/insights`). This pack adds the **token-side control
plane**:

1. **Policies** — per-project / per-stage token and daily cost caps  
2. **Scorecard** — cache ratio, failed-call burn, avg tokens/call, cost per ok call  
3. **Recommendations** — cost_insights rules plus token-specific deterministic rules  
4. **Estimate** — tiktoken-free packing estimate (`chars/4`) + compress suggestions  
5. **Routing** — stage → preferred model class from pipeline profiles  

No external model calls. No fake ML. Every number comes from SQL aggregates or pure heuristics.

## API (`/v1/token-efficiency`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/scorecard?project_id=&window_hours=` | Efficiency scorecard from `llm_calls` |
| GET | `/policies?project_id=` | Effective policies (defaults ∪ stored) |
| PUT | `/policies` | Upsert policies `{project_id, policies:[…]}` |
| POST | `/estimate` | `{messages:[{role,content}], max_context_tokens?}` |
| POST | `/recommendations` | `{project_id, profile_name?}` |
| GET | `/consumption?project_id=&window_hours=` | Rolling usage + policy pressure |
| GET | `/routing?profile_name=` | Stage → `low_cost` \| `strong` from profiles |

Mounted via `apps/api/app/routers/token_efficiency.py` → `mount_platform_routers`.

## Scorecard metrics

| Metric | Definition | Grade targets |
|--------|------------|---------------|
| **cache_ratio** | `cache_read / (prompt + cache_read)` | good ≥ 0.40, fair ≥ 0.15 |
| **failed_burn** | sum(`cost`) where `ok=false` / total spend | good ≤ 1%, warn ≥ 5% |
| **avg_tokens_per_call** | `sum(total_tokens) / calls` | good ≤ 12k, warn ≥ 40k |
| **cost_per_ok_call** | ok spend / ok calls | informational |

Grades are traffic lights (`good` / `fair` / `poor` / `n/a`) — not a trained model.

## Token budget policies

Table: `token_budget_policies`

| Column | Notes |
|--------|--------|
| `project_id` | Tenant scope |
| `stage` | `*` = project-wide default; else matches `llm_calls.stage` |
| `max_prompt_tokens` / `max_completion_tokens` / `max_total_tokens_per_call` | Per-call caps |
| `max_tokens_per_day` / `max_cost_usd_micros_per_day` | Daily envelopes |
| `alert_at_pct` | (0, 1] — consumption alerts fire at this utilization |
| `preferred_model_class` | `low_cost` \| `strong` \| null |
| `enabled` | Soft switch |

Defaults ship in code for known pipeline stages (planning → low_cost, review → strong).
`GET /policies` merges stored rows over defaults; `PUT` upserts and bumps `version`.

## Estimate heuristic

```
tokens ≈ max(1, ceil(len(content) / 4))   # empty → 0
```

Same chars/4 convention as `ToolchainRegistryService.pack_context`. Compress suggestions:

- **merge_system** — multiple system messages → one stable prefix  
- **truncate_long_message** — blobs ≥ ~6k chars  
- **stable_prefix_first** — put system/developer first for cache  
- **drop_empty** — remove empty turns  
- **pack_under_budget** — large totals → use knowledge pack_context  

## Recommendations (deterministic)

Extends `boss_engine.services.cost_insights.build_insights` and adds:

| Kind | Trigger |
|------|---------|
| `raise_cache_ratio` | cache ratio &lt; 0.40 |
| `high_avg_tokens` | avg tokens/call ≥ 40k (≥3 calls) |
| `stage_over_token_cap` | stage avg &gt; policy `max_total_tokens_per_call` |
| `route_planning_low_cost` | ≥10% spend on planning-class stages |
| (+ cost_insights) | `cache_leverage`, `failed_call_burn`, `strong_on_cheap`, … |

Each recommendation has `finding`, `action`, optional `$` / token savings, and `source`
(`cost_insights` \| `token_efficiency`).

## Model routing suggestion

Pure function over `pipeline_profiles.PROFILES` via `model_for_step`:

- **lean** — low_cost everywhere  
- **balanced** — low_cost build; strong review/security/consistency  
- **strict** — strong default  
- **vag** — strong default; implement + write_tests + standards low_cost  

`GET /routing` returns the map operators can compare to actual spend by stage.

## Console

- `apps/web/lib/token-efficiency.ts` — fetch helpers  
- `apps/web/app/token-efficiency-ui.tsx` — scorecard, consumption, policies, recommendations  
- Wired into the Budget area of Mission Control (`page.tsx`)

## Hard rules

- Analytics are **read-only** over `llm_calls`.  
- The only write path is `token_budget_policies` via `PUT /policies`.  
- **No** external model, billing, or provider API calls.  
- Failures surface as empty scorecards / HTTP 422 — never silent drops.  

## Related

- `docs/product/PIPELINE_COST_CONTROL.md` — profile dial  
- `engine/boss_engine/services/cost_insights.py` — $ insights  
- `engine/boss_engine/services/llm_telemetry.py` — ledger write/read  
- `engine/boss_engine/services/knowledge_context.py` — pack under token budget  
