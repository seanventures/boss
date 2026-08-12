# Live QA memory (do not forget)

**Living plan:** `docs/operations/GA_LIVING_PLAN.md` — **update this first** after every ship.  
**GA scorecard:** `docs/operations/ga-readiness-2026-08-12/GA_READINESS_SCORECARD.md`  
**Umbrella:** Plane **BOSS-337** (In Progress)

## Settled deploy truth (2026-08-12)

- App: `boss-app` → `boss.local.sean.network` + `boss-api.local.sean.network`
- **Edition: community**
- **Live images:** api/web `968bc47`, worker `91faf6d` (**skew**; main HEAD `4727de7`)
- Health OK (DB/GitHub/OpenAI configured)
- **Never** redeploy with `--env-template` (clobbers secrets)

## Superseded (do not re-derive)

| Old “settled” (08-11) | Current truth |
|---|---|
| ProviderOps enterprise-only 404 | **Community full kernel** 200 (#157) |
| GitHub MC 0 connections | **2 connections + 2 bindings** (#158 live) |
| OpenAPI 0.2.1 | **0.11.0 on main** (#161); live lags until redeploy |
| Sandbox host refused | **host.connected=true**; rootfs still missing |
| Worker no git | **Dockerfile fixed**; live shared-tag still needed |

## Still true / open

- ~**41** open deadletters (mostly code.task) — substrate reliability
- Agent Studio **empty** on dogfood; seed **404** on image `968bc47` (redeploy main for #159)
- Previews: View browser proof not sealed
- Enterprise nav still shown on community (honesty residual)
- Residual hydration #418 (BOSS-346)
- Factory may still use **settings** GitHub token path vs project binding (dual plane)

## Green live (2026-08-12)

- `make qa-live` / `live_dogfood_gate` **10/10 PASS**
- Community: providers, secret-refs, changesets, worker-pools **200**
- Enterprise: account-connect / self-improvement **404**
- Budget green; command propose → task; Metatron PASS
- GitHub easy-connect + repo list work

## GA verdict

**NOT GA-READY** public. **CONDITIONAL** private Community preview with **narrow claims**.  
Phase A: shared-tag redeploy, Studio seed, rootfs/claims, DLQ, dual-plane honesty, claims freeze.  
See `GA_LIVING_PLAN.md`.

## Plane

- Children BOSS-338–351: see `PLANE_TRACKING_LIVE_QA.md` (Done vs residual split)
- Done only with live functional proof
- Ops `/requests` auto-file Plane — do not duplicate

## Product decisions settled

1. Community **must** support third-party + infra API keys (BYOK).  
2. Full ProviderOps **kernel** (including ChangeSets/pools) is community; paid = Account Connect, marketplace, SSO/SCIM, self-improvement automation.  
3. Easy GitHub auth = env SecretRef (not browser paste).
