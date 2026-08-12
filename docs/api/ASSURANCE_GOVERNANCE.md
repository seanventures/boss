# Assurance, agent groups & integration registry

Governance HTTP surface (primarily `apps/api/app/routers/governance.py`) plus related enterprise/break-glass controls.

| Prefix | Role |
|--------|------|
| `/v1/assurance/*` | Metrics, conclusions, cases, claims, evidence, surfaces, verification |
| `/v1/agent-groups/*` | Versioned multi-agent collaboration runs |
| `/v1/integration-registry/*` | Typed connections + capability intersection |
| `/v1/break-glass/*` | Time-boxed elevated grants |
| `/v1/enterprise/*` | Enterprise clusters / identity hooks |

Product: `docs/product/ASSURANCE_CONTRACTS.md`. Permission for most mutations: `workflow_manage`.

```bash
export BOSS_API=http://127.0.0.1:8000
export BOSS_KEY=dev-key
export PROJECT=<project_id>
AUTH=(-H "Authorization: Bearer $BOSS_KEY" -H "Content-Type: application/json")
```

---

## Assurance overview

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/assurance/overview?project_id=$PROJECT" | jq .
```

Ground-truth style read model: metrics health, open cases, surface ownership, verification backlog.

---

## Metrics (measured SLAs)

| Method | Path |
|--------|------|
| `GET` | `/v1/assurance/metrics?project_id=` |
| `POST` | `/v1/assurance/metrics` |
| `POST` | `/v1/assurance/metrics/schedule` |
| `POST` | `/v1/assurance/metrics/{id}/observations` |
| `POST` | `/v1/assurance/metrics/{id}/baseline` |

### Create metric

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"metric_key\": \"deploy_success_rate\",
  \"metric_kind\": \"ratio\",
  \"unit\": \"ratio\",
  \"comparator\": \"gte\",
  \"baseline\": 0.95,
  \"threshold\": 0.90,
  \"cadence_seconds\": 3600,
  \"alert_sla_seconds\": 1800,
  \"owner_ref\": \"role:sre\",
  \"evidence_query\": {\"source\": \"deployment_runs\", \"window_hours\": 24},
  \"metadata\": {}
}" "$BOSS_API/v1/assurance/metrics" | jq .
```

### Schedule due evaluations

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"system\",
  \"force\": false
}" "$BOSS_API/v1/assurance/metrics/schedule" | jq .
```

### Record observation (worker-leased)

Observations require evaluator job identity + lease + **dedupe_key** (no silent double-count).

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"value\": 0.97,
  \"evidence\": {\"sample_size\": 40},
  \"evaluator_job_id\": \"<job_id>\",
  \"evaluator_worker_id\": \"worker-1\",
  \"lease_token\": \"<lease>\",
  \"dedupe_key\": \"metric_obs:deploy_success_rate:2026-08-12T12\"
}" "$BOSS_API/v1/assurance/metrics/<metric_id>/observations" | jq .
```

### Baseline change (governed)

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"baseline\": 0.96,
  \"threshold\": 0.92,
  \"reason\": \"Post-GA improved reliability\",
  \"approved_decision_id\": \"<decision_id>\"
}" "$BOSS_API/v1/assurance/metrics/<metric_id>/baseline" | jq .
```

Missed SLA → escalation signal / decision, not silent skip.

---

## Conclusions & publication guard

| Method | Path |
|--------|------|
| `GET` | `/v1/assurance/conclusions?project_id=` |
| `POST` | `/v1/assurance/conclusions` |
| `POST` | `/v1/assurance/conclusions/{id}/reopen` |
| `POST` | `/v1/assurance/publication-guard` |

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"conclusion_key\": \"root_cause:webhook_hmac\",
  \"conclusion\": \"Invalid signatures caused false deploys\",
  \"scope\": {\"service\": \"api\"},
  \"owner_ref\": \"role:sre\",
  \"tell_patterns\": [\"signature mismatch\"],
  \"cause_menu\": [\"missing secret rotation\"],
  \"evidence_refs\": [{\"type\": \"receipt\", \"id\": \"r1\"}],
  \"reopen_conditions\": [\"recurrence within 7d\"]
}" "$BOSS_API/v1/assurance/conclusions" | jq .

# Scan outbound content for publication tripwires
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"publication_boundary\": \"public_status\",
  \"content\": \"Draft status post...\",
  \"dedupe_key\": \"pubscan:status:1\"
}" "$BOSS_API/v1/assurance/publication-guard" | jq .
```

Reopen requires expected version, reason, new evidence, and approved decision id.

---

## Cases, claims & evidence

| Method | Path |
|--------|------|
| `GET/POST` | `/v1/assurance/cases` |
| `POST` | `/v1/assurance/cases/{id}/evaluate` |
| `POST` | `/v1/assurance/claims` |
| `POST` | `/v1/assurance/claims/{id}/evaluate` |
| `POST` | `/v1/assurance/evidence` |
| `POST` | `/v1/assurance/evidence/{id}/invalidate` |

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"subject_type\": \"release\",
  \"subject_id\": \"v0.11.0\",
  \"title\": \"GA readiness case\"
}" "$BOSS_API/v1/assurance/cases" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"case_id\": \"<case_id>\",
  \"claim_key\": \"tests_green\",
  \"statement\": \"All required suites pass on merge commit\"
}" "$BOSS_API/v1/assurance/claims" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"claim_id\": \"<claim_id>\",
  \"evidence_kind\": \"test_report\",
  \"content\": {\"passed\": 1088, \"failed\": 0},
  \"dedupe_key\": \"ev:tests:v0.11.0\"
}" "$BOSS_API/v1/assurance/evidence" | jq .

curl -sS "${AUTH[@]}" -d "{\"project_id\": \"$PROJECT\", \"actor\": \"ceo\"}" \
  "$BOSS_API/v1/assurance/claims/<claim_id>/evaluate" | jq .

curl -sS "${AUTH[@]}" -d "{\"project_id\": \"$PROJECT\", \"actor\": \"ceo\"}" \
  "$BOSS_API/v1/assurance/cases/<case_id>/evaluate" | jq .
```

Claims/cases evaluate deterministically against evidence; invalidation is explicit.

---

## Surface ownership

Provider-boundary ownership with version fencing.

| Method | Path |
|--------|------|
| `GET` | `/v1/assurance/surfaces?project_id=` |
| `POST` | `/v1/assurance/surfaces/acquire` |
| `POST` | `/v1/assurance/surfaces/{ownership_id}/transfer` |
| `POST` | `/v1/assurance/surfaces/preflight` |

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"surface_key\": \"github:seanheiney/boss-sandbox:main\",
  \"owner_ref\": \"agent:code-task\",
  \"version\": 1
}" "$BOSS_API/v1/assurance/surfaces/acquire" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"surface_key\": \"github:seanheiney/boss-sandbox:main\",
  \"expected_owner_ref\": \"agent:code-task\",
  \"expected_version\": 1
}" "$BOSS_API/v1/assurance/surfaces/preflight" | jq .
```

Stale version / wrong owner → fail closed before writes.

---

## Verification policies & runs

| Method | Path |
|--------|------|
| `POST` | `/v1/assurance/verification-policies` |
| `GET` | `/v1/assurance/verification-runs?project_id=` |
| `POST` | `/v1/assurance/verification-runs` |
| `POST` | `/v1/assurance/verification-runs/schedule-due` |
| `POST` | `/v1/assurance/verification-runs/{run_id}/complete` |

Standing verifiers are **budgeted, receipted, leased** jobs (sampled automatically when scheduled).

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\"
}" "$BOSS_API/v1/assurance/verification-runs/schedule-due" | jq .
```

---

## Agent groups

One leased member job per participant; deadlines, budgets, capability scope, attributed outputs, visible partial failure/dissent. **Quorum is not proof.**

| Method | Path |
|--------|------|
| `GET/POST` | `/v1/agent-groups` |
| `GET/POST` | `/v1/agent-groups/runs` |
| `POST` | `/v1/agent-groups/runs/{run_id}/members/{member_key}/complete` |
| `POST` | `/v1/agent-groups/runs/{run_id}/finalize` |

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/agent-groups?project_id=$PROJECT" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"name\": \"design-review-panel\",
  \"members\": [
    {\"key\": \"architect\", \"agent_ref\": \"agent:architect\"},
    {\"key\": \"security\", \"agent_ref\": \"agent:security\"}
  ],
  \"quorum\": 2,
  \"budget_usd_micros\": 2000000
}" "$BOSS_API/v1/agent-groups" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"agent_group_id\": \"<group_id>\",
  \"input\": {\"topic\": \"API auth redesign\"}
}" "$BOSS_API/v1/agent-groups/runs" | jq .
```

---

## Integration registry

Typed connections with SecretRef credentials and **intersection-only** capability resolution.

```bash
# Paths under /v1/integration-registry/* — list/create/resolve
# Exact methods in ROUTE_INDEX.md / OpenAPI
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/meta/api-catalog/governance" | jq '.routes[] | select(.path|test("integration"))'
```

Capabilities available to a workflow are the **intersection** of connection scopes and policy — never the union of wishful flags.

---

## Break-glass

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/break-glass/grants?project_id=$PROJECT" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"target_member_id\": \"oncall\",
  \"reason\": \"Incident response SEV-1\",
  \"granted_permissions\": [\"providerops_manage\"],
  \"scope\": {\"ttl_minutes\": 60}
}" "$BOSS_API/v1/break-glass/grants" | jq .

curl -sS "${AUTH[@]}" -d "{\"actor\": \"ceo\"}" \
  "$BOSS_API/v1/break-glass/grants/<grant_id>/revoke" | jq .
```

Grants are time-boxed, audited, and revocable.

---

## Invariants

- Measurements, conclusions, and verification produce **immutable evidence** trails.  
- Observations and publication scans use **dedupe_key**.  
- Standing verifiers spend budget via reservations/receipts.  
- Surface preflight enforces owner + version fence.  
- Agent group completion attributes outputs per member; dissent remains visible.  
- Failures escalate to decisions — never silent green.  
