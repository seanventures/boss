# CI/CD Pipelines product surface

**Status:** implemented (dry-run + provenance ledger)  
**Branch surface:** `ai/grok/platform-ops-epic`  
**API prefix:** `/v1/cicd`

## Purpose

BOSS ships a multi-vendor CI/CD **generate + catalog + validate + provenance** surface so operators and agents can:

1. Discover which CI vendors BOSS knows about (market tools).
2. Generate a deterministic, checked-in pipeline config for a language/framework.
3. Dry-run validate YAML structure before proposing a PR.
4. Record pipeline-run **provenance** on the BOSS side (ledger), without requiring live cloud tokens.

Live cloud poll/verdict remains on `pipeline_adapter` (GitHub Actions, Jenkins, GitLab) and is credential-gated. Provider **deployment** remains ChangeSet-gated in the Resource Plane — generated deploy jobs only hand off build evidence.

## Vendors

| Vendor | Config path | Template generate | Live adapter |
|--------|-------------|-------------------|--------------|
| GitHub Actions | `.github/workflows/ci.yml` | yes | yes (`github_actions`) |
| GitLab CI | `.gitlab-ci.yml` | yes | yes (`gitlab`) |
| Jenkins | `Jenkinsfile` | yes | yes (`jenkins`) |
| CircleCI | `.circleci/config.yml` | yes | no (template only) |
| Buildkite | `.buildkite/pipeline.yml` | yes | no (template only) |

Languages supported by generators: **python**, **javascript/typescript/node**, **go** (plus common frontend frameworks via the `framework` field).

## API

| Method | Path | Behavior |
|--------|------|----------|
| `GET` | `/v1/cicd/catalog` | Vendor catalog (no secrets) |
| `POST` | `/v1/cicd/generate` | `{ vendor, language, framework? }` → config path + content + checksum |
| `POST` | `/v1/cicd/validate` | `{ yaml, vendor? }` → structure validation (YAML or Jenkinsfile) |
| `POST` | `/v1/cicd/runs` | Record provenance (idempotent on `dedupe_key`) |
| `GET` | `/v1/cicd/runs?project_id=` | List recorded runs |
| `GET` | `/v1/cicd/status?project_id=` | Catalog + recent runs + links |

Legacy thin generate remains at `POST /v1/ci-config/generate` (github_actions / jenkins targets). Prefer `/v1/cicd/generate` for the full vendor set.

Related (not replaced):

- `GET /v1/pipeline-adapters/catalog` — live adapter kinds/capabilities
- Delivery / deployments APIs — promotion and Resource Plane actuation
- `POST /v1/pipeline-config/proposals` — generate + propose draft PR (budget/receipt gated)

## Hard rules

- **No real GitHub/GitLab/… writes** from this surface. Generate and validate are pure.
- **Dry-run first.** Config is returned as text; proposing a PR is a separate, policy-gated skill.
- Every spendy or external effect elsewhere still needs BudgetReservation + Receipt + dedupe_key.
- Generated deploy stages are **manual / approval / block** steps that only package release evidence.
Generated configs include an OSS **security-scan gate** hook (Semgrep / Trivy / OSV-Scanner
as comments plus optional PATH steps). Snyk is never invoked. Live GitHub Actions poll
stays credential-gated on `pipeline_adapter`.

## Provenance ledger

`POST /v1/cicd/runs` stores a BOSS-side record:

- `vendor`, `status`, `external_id`, `repository`, `ref`, `commit_sha`, `url`
- Optional links: `delivery_run_id`, `deployment_run_id`
- Optional `dedupe_key` for idempotent ingest (e.g. webhooks)

This is **not** live cloud history. Operators or future webhook adapters can append without embedding provider credentials in this module.

## Implementation map

| Piece | Path |
|-------|------|
| Generators | `engine/boss_engine/services/ci_config_generator.py` |
| Catalog / validate / runs | `engine/boss_engine/services/cicd_pipelines.py` |
| HTTP routes | `apps/api/app/routers/cicd_pipelines.py` (mounted via `platform.py`) |
| Tests | `apps/api/tests/test_cicd_pipelines_api.py` |
| Web client | `apps/web/lib/cicd-pipelines.ts` |
| Console panel | `apps/web/app/cicd-pipelines-ui.tsx` |

## Example

```bash
# Catalog
curl -s "$BOSS_API/v1/cicd/catalog" | jq '.vendors[].id'

# Generate GitLab CI for Python
curl -s -X POST "$BOSS_API/v1/cicd/generate" \
  -H 'Content-Type: application/json' \
  -d '{"vendor":"gitlab_ci","language":"python"}' | jq '{path, checksum}'

# Validate
curl -s -X POST "$BOSS_API/v1/cicd/validate" \
  -H 'Content-Type: application/json' \
  -d '{"vendor":"github_actions","yaml":"name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps: []\n"}'

# Record provenance
curl -s -X POST "$BOSS_API/v1/cicd/runs" \
  -H 'Content-Type: application/json' \
  -d '{"project_id":"boss-dogfood","vendor":"github_actions","status":"passed","external_id":"123","dedupe_key":"gha:123"}'
```

## Tests

```bash
PYTHONPATH=.:engine .venv/bin/python -m pytest apps/api/tests/test_cicd_pipelines_api.py -q
```

Tests prove generate for **all five vendors** emits non-empty configs across python/typescript/go, validate accepts generated configs, and provenance list/dedupe/status work without cloud credentials.
