# V1 live dogfood probe

**When:** 2026-08-15T19:03:30Z  
**Target:** `https://boss-api.local.sean.network`  
**Auth:** `X-Boss-Api-Key` from goldeneye `/srv/apps/boss-app/.env` (not printed)  
**Method:** production-qa — working, not deployed. Exact paths only.  
**Deploy:** none. No image rebuild. No compose recreate. No merge.  
**GA:** not stamped.

Observed HTTP status codes only. 500 stays 500. 404 stays 404. 422 stays 422. No invented 200.

| Method | Path | Status |
|--------|------|--------|
| GET | `/health` | 200 |
| GET | `/v1/token-efficiency/policies` | 422 |
| GET | `/v1/worker-autoscaling/status` | 422 |
| GET | `/v1/goldeneye/status` | 422 |
| GET | `/v1/elastic/status` | 422 |
| GET | `/v1/ai-sre/capabilities` | 404 |
| GET | `/v1/identity-providers` | 200 |
| GET | `/v1/security-scan/latest` | 404 |
| GET | `/v1/oncall-sre/status` | 422 |
| GET | `/v1/knowledge/graph/health?project_id=boss-dogfood` | 200 |

`GET /v1/ai-sre/capabilities` is 404 until that stack is deployed. This probe did not deploy it.

## Plane tracking (not closed, not GA)

Workspace `goldeneye`, project `cc97d5cc-e8c6-4893-ac14-7f646c37f3d7`. Both issues left **In Progress**.

| Issue | State after comment | Comment |
|-------|---------------------|---------|
| [BOSS-388](https://plane.local.sean.network/goldeneye/projects/cc97d5cc-e8c6-4893-ac14-7f646c37f3d7/issues/a9cc36ec-68a7-4371-8244-1a9e77f2ccda) | In Progress | `7022aad3-3b5b-4bf3-849a-30c40ac46fdd` |
| [BOSS-387](https://plane.local.sean.network/goldeneye/projects/cc97d5cc-e8c6-4893-ac14-7f646c37f3d7/issues/b01bf8fb-2262-463d-9e1c-5dc15c6704be) | In Progress | `ff51715c-64be-4861-aa56-c57a564c9b20` |

Comments state a 10-agent E2E stack is in flight (`v1-e2e-stack` + per-domain `e2e-*` branches). Branch `ai/grok/e2e-plane-probe` from `origin/main` @ `6370178`.
