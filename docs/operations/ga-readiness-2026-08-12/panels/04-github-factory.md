# Panel 04 — GitHub Mission Control + Software Factory (GA)

**Date:** 2026-08-12  
**Grade:** YELLOW (code GREEN; dual-plane + substrate YELLOW/RED)

## Live correction (2026-08-12 dogfood)

Contrary to earlier live audits (0 connections):
- **2 connections** (dogfood-github-pat + easy-connect-proof)
- **2 bindings** (seanheiney/boss primary, paramg/boss-sandbox source)
- Repo list endpoint returns accessible repos
- easy-connect API live (POST)

## Completeness

| Surface | Code | Live dogfood |
|---|---|---|
| Project GitHub connections + easy-connect | High | Wired |
| GHX MC (leases/sync/merge/recon) | High | Needs usage proof |
| Software Factory PR evidence chain | High | Substrate-dependent |
| Code tasks → PR | High design | Flaky / DLQ history |
| Factory uses project connection (not only settings token) | Incomplete | Dual plane RED |

## Risks (ranked)

1. Dual GitHub planes: factory/code-tasks still primarily settings/`owner-repo` adapter, not primary binding
2. Worker/sandbox substrate flakiness → code.task DLQ
3. Actions disabled / branch protection process for GA demos
4. Orchestration CEO→PR not fully sealed auto-graph

## GA checklist

- [x] Easy-connect or PAT connection active on dogfood
- [x] Primary binding present
- [ ] Factory path consumes primary binding (or documented dual path)
- [ ] One sealed code-task → PR → Decision with artifacts in last 7 days
- [ ] No double-PR on dedupe replay

## Verdict

**Connect/link/easy-auth: GO for self-host.**  
**Factory loop as marketing claim: CONDITIONAL** until substrate + dual-plane fix + recent green E2E.
