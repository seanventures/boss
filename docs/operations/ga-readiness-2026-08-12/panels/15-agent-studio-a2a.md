# Panel 15 — Agent Studio / A2A / Metatron

**Date:** 2026-08-12  
**Verdict:** CODE-SUBSTANTIAL / LIVE-SHELL

## Live (2026-08-12)

- Catalog API 200 with **agents=0, panels=0**
- `POST /v1/agent-studio/catalog/seed` → **404** on dogfood image `968bc47` (deploy lag vs main #159 seed routes)
- Metatron: live gate **PASS**
- A2A: historically degraded without Redis health; not proven live

## Code

- 13 SpecPack agents + 3 panels; bootstrap seed; UI install CTA; deterministic_brief runs
- live_model blocked by release flag (correct)
- A2A: Postgres outbox + Redis Streams; fail-closed degraded

## GA

| Claim | Decision |
|---|---|
| Agent Studio is GA product | **NO** until seeded + one deterministic run live |
| Engine exists | **YES** |
| Metatron status Q&A | **YES** (live) |
| A2A multi-agent bus live | **NO** until Redis + channels proven |

## Actions

1. Redeploy main (seed route) to dogfood
2. Seed catalog; verify 13/3
3. Run one agent + one panel deterministic_brief
4. Prove A2A only if Redis healthy
