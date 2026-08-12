# Plane tracking — live QA + PRD critical gaps

**Workspace:** `goldeneye`  
**Project:** BOSS (`cc97d5cc-e8c6-4893-ac14-7f646c37f3d7`)  
**UI:** https://plane.local.sean.network  
**Contract:** `AGENTS.md` — Plane is the system of record; dedupe before create; never commit API keys.

## Umbrella

| Issue | Title | State |
|---|---|---|
| **BOSS-337** | BOSS deep agentic production QA (goldeneye live) — 2026-08-11 | In Progress |

Evidence docs:

- `docs/operations/LIVE_QA_2026-08-11.md`
- `docs/operations/LIVE_UI_FUNCTIONAL_AUDIT_2026-08-11.md`
- `docs/operations/PRD_CRITICAL_GAPS_2026-08-12.md`
- `.ai/live-qa-memory.md`

## Child issues (filed 2026-08-12)

### P0 / Todo

| Issue | Title |
|---|---|
| **BOSS-338** | Edition-aware Mission Control — stop enterprise/ProviderOps/SIM 404 fan-out on community |
| **BOSS-339** | Self-Improvement UI dead on community — rewire or hide |
| **BOSS-340** | Sandbox agent host unreachable (connection refused) |
| **BOSS-341** | Preview View URLs expose 127.0.0.1 |
| **BOSS-345** | Orchestration glue — `next_move` not worker-capable; seal CEO vertical slice |
| **BOSS-350** | Product decision: Community ProviderOps — **DECIDED**: community gets 3rd-party + infra API keys (catalog/accounts/inventory); ChangeSet write-automation stays paid |
| **BOSS-351** | Sealed dogfood E2E command → evidence → PR decision → receipts |

### P1 / Todo or Backlog

| Issue | Title |
|---|---|
| **BOSS-342** | Align boss-worker image; install git; cut code.task deadletters |
| **BOSS-343** | Wire or hide GitHub Mission Control on dogfood |
| **BOSS-344** | CI/CD + Monitoring empty-state honesty |
| **BOSS-346** | React hydration #418 on core Run areas |
| **BOSS-348** | Earned autonomy + value-based scheduling (G1–G3) |

### P2 / Backlog

| Issue | Title |
|---|---|
| **BOSS-347** | Agent Studio empty on dogfood — seed/setup |
| **BOSS-349** | OpenAPI 0.11.0: lifecycle schemas + factory/previews/orchestration; openapi-live-qa-parity 79 routes OK |

## Related existing (not re-created)

| Issue | Note |
|---|---|
| BOSS-237 | System configuration suite UX (Done) — live edition still 404s ProviderOps APIs |
| BOSS-246–255 | GHX feature stack (mostly Done) — dogfood MC unwired |
| BOSS-296 | OSS “Simple Mission Control” gap (Done row historically) — packaging still relevant |
| BOSS-324 | boss-sandbox main protection |
| GAP-002/003/004 | External Account Connect / IdP / live provider writes — keep fail-closed; track when unblocking |

## Agent rules for this workstream

1. Before starting work: search Plane for existing issue; use BOSS-33x ids above.  
2. Move issue **Todo → In Progress** when starting; **Done** only when live dogfood evidence exists.  
3. Link commits/PRs and re-run relevant live checks; comment evidence on the issue.  
4. Do not open duplicate Plane issues for the same finding; comment on the existing one.  
5. Never print or commit `PLANE_API_KEY` / ops tokens.
