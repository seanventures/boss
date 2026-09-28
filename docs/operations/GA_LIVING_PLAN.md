# BOSS GA living plan

**2026-09-28 release note:** This plan's live QA rows are historical August
evidence. The product owner confirmed counsel approval for Apache License 2.0
for the reviewed full Community export (reference `01049940`), recorded in
[OSS_LICENSING_DECISION.md](OSS_LICENSING_DECISION.md). BOSS-300 implementation
and BOSS-293 public release gates remain open. The 14-day streak, current
security review, exact export, and clean x86 installer proof must be checked
afresh before any public GA claim. See the current
[PUBLIC_FLIP_CHECKLIST.md](PUBLIC_FLIP_CHECKLIST.md).

**Status date:** 2026-08-14  
**Overall GA:** **WORKING-SOFTWARE BAR MET** on dogfood — all three sample apps (BugBox, ApproveIt, HookWatch) serve real backends on `fca977c`. **Not** public/C1–C4 GA: 14-day calendar, C2, and BOSS-300 remain open.  
**System of record:** Plane `goldeneye` / BOSS · umbrella **BOSS-337**  
**Scorecard:** [`ga-readiness-2026-08-12/GA_READINESS_SCORECARD.md`](ga-readiness-2026-08-12/GA_READINESS_SCORECARD.md)  
**E2E plan (detail):** [`E2E_COMPLETION_PLAN.md`](E2E_COMPLETION_PLAN.md)  
**Plane map:** [`PLANE_TRACKING_LIVE_QA.md`](PLANE_TRACKING_LIVE_QA.md)

> **Agent rule:** After every ship, deploy, or live proof, update **this file first** (progress table + changelog), then Plane issue comments, then E2E/Plane docs if states change. Never mark Done without live dogfood evidence.

---

## The only GA bar

**Community is GA-ready when we can build a functional app through the full system and get working software as designed.**

That means, on dogfood, all of:

1. Outcome / benchmark / CEO command enters the control plane.
2. A budgeted `code.task` runs on a coding substrate (local while microVM claims stay frozen).
3. Evidence exists: plan, `npm run verify` (or equivalent) **exit 0**, receipts.
4. Operator opens a **non-fixture** preview (or merged/PR'd app) that is the designed product.

Control-plane gates (`qa-live` 10/10, catalog 55/55, simulated fixtures) are **necessary but not sufficient**.

| Live proof (2026-08-14, tag `fca977c`) | Result |
|---|---|
| Shared tag | **Live** api+worker+web `3f1d2cf` (always-on Metatron overwatch). No `--env-template`. |
| Coding substrate | Advertised `coding_substrate=local` while `coding_substrate_configured=microvm` and `microvm_claims_frozen=true` (`GET /v1/factory/wip`) |
| Prior BugBox `crun_7cc663627f2340dc85ac` | Historical: verify green, PR #83. Replay Views expired. |
| `qa-live` | **10/10 PASS + Metatron** on `f5def77`. 14-day calendar still **Day 1/14** (same-day re-runs do not add a day). |
| BugBox follow-up `crun_4c762352803e4487a3b4` | **completed**, PR [#84](https://github.com/paramg/boss-sandbox/pull/84). View `prev_0be09812b6a9484aa34b` **running**. Title **BugBox — Console Platform issue tracker**. `/health` JSON. `/api/issues` real rows. |
| ApproveIt `crun_907e7ca9e6f54ba6945f` | PR [#76](https://github.com/paramg/boss-sandbox/pull/76). View `prev_b221f5225fdb471c98c5` **running**. `/health` JSON. `/api/users` JSON. `/api/requests` JSON with `X-Acting-User-Id`. |
| HookWatch `crun_911888eefbd14166bce6` | **completed**, `npm run verify` exit 0, PR [#86](https://github.com/paramg/boss-sandbox/pull/86). View `prev_78b214cfb8fa4fbf8d08` **running**. Title **HookWatch — temporary webhook inboxes**. `/api/health` `{ok,inboxes}`. `GET /api/inboxes` list. `POST /api/inboxes` **201**. |
| Gates | `qa-live` **10/10 + Metatron**. `qa-working-software` **4/4 PASS** (picked HookWatch). harness reuse **56/56**. |

**Working-software Community dogfood GA: YES** (operator can build an app through BOSS and open it). Residuals below are not that bar.

## North star (Community DoD — 8 criteria)

| # | Criterion | Status | Evidence / notes | Next action |
|---|---|---|---|---|
| 1 | BYOK model + infra keys | **Mostly (live)** | Shared tag `647c742` live; provider-definitions **11** (openai/anthropic/compatible + clouds). Paste-and-connect route mounted. Operator still needs to paste one real key to close. | Paste a real model key on dogfood |
| 2 | CEO command → task + budget path | **Mostly** | Live `workflow_e2e_gate` **5/5 PASS** 2026-08-13 (command+tasks+budget+jobs) | Seal plan→job if claimed |
| 3 | Code → evidence → PR reliable | **Mostly** | BugBox #84, ApproveIt #76/#78, HookWatch #86 all shipped with verify green. Worker HTTP read timeout 7200s. | Keep an eye on next greenfield timeout |
| 4 | Previews reachable | **Working software live** | All three canaries serve backends. Resolver prefers owning `start`. Gateway forwards `X-Acting-User-Id`. | Keep Views from expiring during QA |
| 5 | Honest unpaid/unconfigured UI | **Mostly (live)** | Web `647c742` deployed. Paid 404 honesty in harness. Visual dogfood of badges still optional. | Spot-check MC badges on `boss.local` |
| 6 | GitHub MC connected | **Done** | 2 conns + 2 binds live; factory WIP `github_plane=mission_control_binding` on `647c742` | Keep bindings healthy |
| 7 | Deploy path or honest empty CI/CD | **Honest empty** | 0 targets; empty CTA | Optional sandbox target later |
| 8 | Automated live gate | **Done** | `make qa-live` 10/10 PASS | Wire as deploy mandatory (W5.2) |

**DoD score:** **~7 / 8** toward the working-software bar (all three sample apps open as designed on dogfood). Target for Community public GA: 8/8 + 14-day green gate + C2 + BOSS-300. Do **not** stamp public/C1–C4 GA. Community GA is **not** Done.

**Claims freeze (signed list):** [`COMMUNITY_GA_CLAIMS_FREEZE.md`](COMMUNITY_GA_CLAIMS_FREEZE.md) — Allowed vs Forbidden; SSO is Community self-hosted.

---

## Phase tracker (GA path)

### Phase A — 48h (must before any GA language)

| ID | Work | Plane | State | Exit |
|---|---|---|---|---|
| A1 | Shared-tag redeploy api+worker+web from `main` | BOSS-337, 342 | **Done live** `647c742` | Images match (rollback from crashed `64d0e99`); no `--env-template` |
| A2 | Re-run `make qa-live` | BOSS-351 | **Done live (gate)** | **10/10 + Metatron** on `647c742` (~22:44Z); 14-day still Day 1/14. New-build canary is a separate 351 residual |
| A3 | Agent Studio seed + one deterministic run | BOSS-347 | **Done live** | Seed 200; 13/13 published; catalog-smoke 13/13 passed 2026-08-13 |
| A4 | Sandbox rootfs bake **or** drop microVM claims | BOSS-340 | **Claim freeze shipped** (`BOSS_MICROVM_CLAIMS_FROZEN`) | Set freeze on dogfood; unfreeze after rootfs bake proof — see `docs/product/SANDBOX_GA_STATUS.md` |
| A5 | DLQ triage top classes; prove no missing-git | BOSS-342 / **372** | **Partial** git in image + remediations list/retry | Hist 41 still open (TPM + missing DO creds); git 0 new after image. See `qa-dlq-remediations.md` |
| A6 | Factory vs MC GitHub dual-plane honesty | BOSS-343 follow-on | **Done live** | `GET /v1/factory/wip` + FactoryScaleCard + Code Tasks advertise `github_plane` on `647c742` |
| A7 | Claims freeze (allowed/forbidden list) | BOSS-337 | **Done (doc)** | [`COMMUNITY_GA_CLAIMS_FREEZE.md`](COMMUNITY_GA_CLAIMS_FREEZE.md); SSO Community self-hosted allowed |

### Phase B — 1–2 weeks (private preview)

| ID | Work | Plane | State |
|---|---|---|---|
| B1 | Weekly sealed command→code→PR→Decision | BOSS-351 | **Open / In Progress** — new canary `crun_27b5a74441fc4c0d8ace` running; do not close 351 until complete+preview |
| B2 | Preview View browser proof (non-loopback) | BOSS-341 | **Real BugBox View PASS live** `prev_4289c3eaad02479daa47` (plus fixture honesty). New-build still open |
| B3 | Hide/badge paid surfaces on community | BOSS-338 residual | **Done (nav residual closed)** — Community primary nav is **SSO** (not Enterprise); foundations/components `hide_nav`; Account Connect / marketplace / clusters / SIM stay gated or Paid-badged |
| B4 | Residual hydration #418 | BOSS-346 | Open |
| B5 | Assurance empty honesty + optional seed | BOSS-337/348 | **Done (code)** |
| B6 | Deploy SOP requires `qa-live` | BOSS-337 | **Done (doc)** — `GOLDENEYE_DEPLOYMENT.md` redeploy must run `qa-live` + `qa-harness` catalog |

### Phase C — Community GA

| ID | Work | State |
|---|---|---|
| C1 | 14 consecutive days `qa-live` green | Open |
| C2 | Security must-fix (principals, dep CI, rate limits) | **Open** (local-code partial) | Principals map exists; BOSS-320 org-fallback test added; in-proc limiter Community-ok + existing 429/`rate_limit.exceeded` test; PyJWT 2.13.0 + cryptography 50.0.0 already latest. Residuals in [`SECURITY_C2_STATUS.md`](SECURITY_C2_STATUS.md) — **do not stamp C2 Done** |
| C3 | Legal/OSS readiness if public announce | Open |
| C4 | Tag + notes with allowed claims only | Open |

---

## Workstream status (E2E map)

| Stream | Intent | Status | Closed Plane | Open / residual |
|---|---|---|---|---|
| **W0** Community keys | BYOK kernel + paste UX | **Mostly (live)** | BOSS-350, #157; BOSS-369 paste | Operator paste one real key |
| **W1** UI honesty | Edition/empty | **Mostly (live)** | 338, 339, 344; community-ga U2 | Visual badge check; 346 residual |
| **W2** Substrate | Worker/sandbox/preview | **Partial** | 340 host, 341 code, 342 git image | Rootfs; DLQ |
| **W3** Vertical slice | CEO→PR sealed | **Partial** | 343 GitHub wire, 345 next_move, 349 OpenAPI, 351 gate | Reliable code path 351 |
| **W4** Product loops | Studio/Assurance/deploy | **Partial** | W4.5 seed (code) | 347, 348, 344 deploy optional; Metatron captain watch/drive is Community code, not a public GA stamp |
| **W5** Gates + Plane | Live proof only | **Active** | Gate script Done | Mandatory deploy gate |

---

## Live stack truth (update on every deploy)

| Field | Value | As of |
|---|---|---|
| Edition | community | 2026-08-14 |
| api image | `boss-api:f5def77` | **shared tag live** 2026-08-14 (rollback `.env.bak.cefe635`) |
| web image | `boss-web:f5def77` | **shared tag live** 2026-08-14 (same digest as cefe635) |
| worker image | `boss-worker:f5def77` | **shared tag live** 2026-08-14 |
| branch HEAD | `f5def77` | `ai/grok/community-ga` — preview resolver + 7200s worker timeout |
| Coding substrate | advertised `local` (configured `microvm` + `BOSS_MICROVM_CLAIMS_FROZEN=true`) | live `GET /v1/factory/wip` |
| `qa-live` | **10/10 PASS + Metatron** on `f5def77` | 2026-08-14T11:19Z (does not add a 14-day day) |
| `qa-working-software` | **PASS** 4/4 BugBox+ApproveIt Views | 2026-08-14 |
| `workflow_e2e_gate` | **5/5 PASS** live | 2026-08-13 |
| `factory_e2e_gate` | **3/3 required PASS** (readonly; optional proposals 405) | 2026-08-13 |
| Open DLQ | 41 historical still open (TPM + missing DO creds); not the GA bar | 2026-08-13 |
| Working software | **All 3 canaries running with JSON APIs**: BugBox `prev_0be09812b6a9484aa34b`, ApproveIt `prev_b221f5225fdb471c98c5`, HookWatch `prev_78b214cfb8fa4fbf8d08` | 2026-08-14 |
| GitHub MC | 2 connections, 2 bindings (`seanheiney/boss`, `paramg/boss-sandbox`) | live |
| Agent Studio | 13/13 active + published versions | 2026-08-13 |
| Sandbox host | connected, 8/8 free, 0 running; claims frozen; rootfs unproven for Firecracker coding | live |
| 14-day qa-live | **Day 1/14 PASS** (cron 06:15 UTC). Same-day re-runs on later tags do **not** add a day | cron 06:15 UTC |
| Previews | **3 running real apps** BugBox + ApproveIt + HookWatch. Public-base URLs; 0 loopback primary | 2026-08-14 |
| C2 security | **Open** — not Done | [`SECURITY_C2_STATUS.md`](SECURITY_C2_STATUS.md) |
| BOSS-300 legal | **Human legal/product** — not Done | leave open |

---

## Plane issue board (living)

| Issue | Topic | State | Notes |
|---|---|---|---|
| BOSS-337 | Umbrella QA/GA | **In Progress** | Do **not** close. BugBox+ApproveIt working software on `f5def77`; HookWatch in-flight; Community GA ≠ Done |
| BOSS-338 | Edition MC honesty | **Done** | Community nav: SSO kept; design-system lab hidden; paid surfaces gated/badged |
| BOSS-339 | SIM community | **Done** | Ops honesty |
| BOSS-340 | Sandbox host | **Done** (host) | Rootfs still open work; live coding is local fallback |
| BOSS-341 | Preview loopback | **Done live (API Views)** | BugBox `/health`+`/api/issues` JSON; ApproveIt `/health`+`/api/users` JSON on `f5def77` |
| BOSS-342 | Worker git/image | **Done** (code) | Shared tag `f5def77` live (api=worker=web) |
| BOSS-372 | DLQ remediations (342 residual / A5) | **In Progress** | List/create/retry fail-closed; hist 41 still open (TPM + missing DO creds) |
| BOSS-343 | GitHub MC wire | **Done** | Dual-plane follow-on |
| BOSS-344 | CI/CD empty honesty | **Done** | |
| BOSS-345 | next_move glue | **Done** | |
| BOSS-346 | Hydration #418 | **Open** | Residual pages |
| BOSS-347 | Agent Studio seed | **In Progress** | Live 13/13 active + published versions; do not treat as Community GA |
| BOSS-348 | Earned autonomy | **Open** | Not GA claim |
| BOSS-349 | OpenAPI parity | **Done** | #161; deploy lag |
| BOSS-350 | Community keys decision | **Done** | Full kernel #157 |
| BOSS-369 | BYOK paste-and-connect | **In Progress** | Catalog + easy-connect + MC paste card |
| BOSS-351 | Live E2E / working-software | **In Progress** | All 3 canaries have reachable API Views. Stays In Progress — public/C1–C4 GA not stamped |
| BOSS-314 | GA readiness closure loop | **In Progress** | Children closed this cycle listed in Plane comment; Phase A live still open |
| BOSS-288–290 | YouTube EP scripts | **Done** (scripts) | Recording is production work outside repo |
| BOSS-291 | Engine private extract | **Done** (checklist) | Execution of extract still operational work |
| BOSS-292/293 | Partners / public flip | **In Progress / blocked** | Checklist ref; 293 needs BOSS-300 |
| BOSS-295/304 | GTM reconciliation + AI SRE deck | **Done** (docs) | Apply brief to Google Slides source |
| BOSS-300 | OSS licensing decision | **Todo BLOCKED** | Human legal/product |
| BOSS-302/303 | Hosted Cloud / managed models | **Cancelled** (this cycle) | Phase-03 roadmap; reopen when Cloud tier starts |
| BOSS-97/98/101/102 | Go S1–S3 scale track | **S1 scaffold only** | In-repo `services/sandbox-service/` is fail-closed (frozen, no Firecracker). S2/S3 + live pool remain Backlog. Not Community GA. |

---

## Allowed / forbidden claims (living freeze)

**Canonical freeze document:** [`COMMUNITY_GA_CLAIMS_FREEZE.md`](COMMUNITY_GA_CLAIMS_FREEZE.md)  
(Update that file first; keep this summary in sync.)

### Allowed after Phase A complete

- Self-hosted governed control plane (tasks, budgets, decisions, receipts)
- Community BYOK ProviderOps kernel (env SecretRefs)
- Project GitHub connect + link + test receipts
- **Self-hosted SSO** (OIDC bearer, SAML SP, SCIM 2.0, IdP registry) on Community — not hosted multi-tenant IdP
- Metatron status Q&A
- Honest empty CI/CD / monitoring when undeployed
- Automated `qa-live` gate
- Mission Control hides/badges paid CTAs on Community (Account Connect, marketplace, clusters, SIM)

### Forbidden until proven

- Autonomous unattended company / reliable factory unattended
- Agent Studio workforce out of the box
- Earned autonomy economy (G1–G3 complete)
- Sandboxes/previews always work
- **Hosted** multi-tenant IdP / Account Connect / marketplace as Community features
- Self-improvement proposal canary/rollout as Community
- Enterprise cluster packs as Community
- Zero DLQ / private main protection + Actions green

---

## Public marketing UX (boss.sean.network)

| Item | Status |
|---|---|
| Engine-first hero (not full company-OS day-one) | **Live** `boss-website:9268c69` |
| Simple loop: Outcome→Plan→Work→Evidence→Decide | **Live** |
| CLI + MCP + BYOK in nav/copy | **Live** |
| Community tier honest (no oversold “basic MC / local models done”) | **Live** |
| Console simplification (`boss.local`) | **IA recommended** — not yet implemented |

Marketing audit: `docs/operations/ga-readiness-2026-08-12/PUBLIC_SITE_UX_AUDIT.md`  
**Console IA recommendations (no feature loss):** [`ga-readiness-2026-08-12/UI_UX_IA_RECOMMENDATIONS.md`](ga-readiness-2026-08-12/UI_UX_IA_RECOMMENDATIONS.md)  
Panels 16–21: founder IA, settings hub, workflow loop, cognitive load, edition IA, naming.

### Console IA implementation track (optional Phase B+)

| Phase | Work | State |
|---|---|---|
| P0 | Labels, default overview, hide design-system nav, Editions badge | **Recommended next** |
| P1 | 8-primary nav + Settings hub (embed existing panels) | Todo |
| P2 | Current Work rail + loop stepper | Todo |
| P3 | Role presets / focus mode | Todo |

### BOSS Cloud / Enterprise (product distinction)

| Item | State |
|---|---|
| Spec `docs/product/BOSS_CLOUD_ENTERPRISE_SPEC.md` | **Done** |
| Community independence (no AC required) | **Enforced** |
| `GET /v1/cloud/catalog` + `/status` | **Done** (community-mounted) |
| `GET /v1/cloud/comparison` + `/capabilities` + `/workers` | **Done** (honest planned workers) |
| `POST /v1/cloud/easy-connect` browser + device | **Done** (fail-closed without AC URL) |
| `POST .../complete` + `POST /v1/cloud/unlink` | **Done** |
| Account Connect device authorize | **Done** (`/v1/device/*`) |
| MC Easy Connect + catalog + device code + disconnect | **Done** (Community-safe feed) |
| Managed elastic workers / full SaaS billing | **Planned** (catalog status honest) |
| Live deploy of this branch | **Todo** |

## Changelog (append-only)

| When | What changed | Who |
|---|---|---|
| 2026-08-14 | **Live always-on overwatch** shared-tag `3f1d2cf`. Worker first tick applied validated preview restart (`app_preview.started` 17:32:16Z, verify ok). DLQ/decisions not applied. Working-software goal score 100. Not public GA. | grok |
| 2026-08-14 | Always-on Metatron overwatch: worker every 60s observe→measure→validate→apply-safe→verify. TUI `/overwatch`. Every action validated. FEATURES ◐. Not GA. | grok |
| 2026-08-14 | W4 residual leftovers: expanded `interpret_turn` tests for build/prioritize (parent parse landed). FEATURES.md captain stays ◐ **Not GA**. Added `make qa-metatron-captain` (unit/island only — not live dogfood). | grok |
| 2026-08-14 | W4 residual: Metatron captain watch/drive is Community code (FEATURES.md ◐), not a public GA stamp. No live dogfood of captain drive. | grok |
| 2026-08-14 | **HookWatch working software:** `crun_911888eefbd14166bce6` completed, verify exit 0, PR #86. View `prev_78b214cfb8fa4fbf8d08` title “HookWatch — temporary webhook inboxes”; `/api/health` JSON; `GET/POST /api/inboxes` (201). All 3 canaries now serve backends. `qa-working-software` 4/4. Not public/C1–C4 GA. | grok |
| 2026-08-14 | **Sample-app Views serve backends:** `f5def77` shared-tag deploy. Resolver prefers owning `scripts.start` over `web/index.html`. BugBox `prev_d06b4eabe8f3471cb2c0` `/health`+`/api/issues` JSON. ApproveIt `prev_7ad80927147e4ab3b8f3` `/health`+`/api/users` JSON. Worker read timeout 7200s. HookWatch `crun_911888eefbd14166bce6` implement. `qa-live` 10/10, working-software 4/4, harness 56/56. Not public/C1–C4 GA. | grok |
| 2026-08-13 | **Working-software bar MET:** `crun_9acc1e5f44e0464099be` verify green; View `prev_bd861455f95f4bb2b183` title “BugBox — Console Platform issue tracker”; `/health` ok; `/api/issues` live. Review-timeout residual (run failed after verify). Not public/C1–C4 GA. | grok |
| 2026-08-13 | **Live stack `647c742`:** api+worker+web shared tag (boot-fixed; rolled back from crashed `64d0e99`). Advertised coding substrate `local` while configured `microvm` + frozen. `qa-live` 10/10 ~22:44Z. `qa-working-software` **PASS** on replay BugBox `prev_4289c3eaad02479daa47` title “BugBox — capture, track, resolve”. New canary `crun_27b5a74441fc4c0d8ace` running `write_tests`. DoD #3/#4 updated. **Community GA not stamped.** 14-day still Day 1/14. C2 open. BOSS-300 human legal. 351 stays In Progress. | grok |
| 2026-08-13 | **GA bar restated:** working software through the factory. Live replay: BugBox `crun_7cc663627f2340dc85ac` verify green → preview `prev_af60b33719b04082b027` title “BugBox — capture, track, resolve”. `make qa-working-software`. Frozen microVM now falls back to local coding (`make_sandbox_provider`). Do not stamp GA until a new build on current tag. | grok |
| 2026-08-13 | A5 DLQ remediations: `GET/POST /v1/deadletters/remediations` + `POST /v1/deadletters/{id}/retry`; credit-vs-credential classifier tests; remaining live classes TPM + missing DO creds. `qa-dlq-remediations.md`. Plane BOSS-372. | grok |
| 2026-08-13 | C2 local-code: SCIM deprovision org-fallback test (BOSS-320); document in-proc rate limiter Community-ok (no Redis); PyJWT/cryptography already latest — residuals in `SECURITY_C2_STATUS.md`. C2 stays Open. | grok |
| 2026-08-13 | Factory live: WIP `github_plane=mission_control_binding` + simulated code-task no job. Preview fixture: `POST /v1/previews/fixture` serves honest `/web/` HTML (no microVM). | grok |
| 2026-08-13 | A1/A2 live: shared-tag `cd13067` api+worker+web (no `--env-template`; rollback `.env.bak.3e6c906`). `qa-live` **10/10 + Metatron**. Harness catalog **55/55**, optional schema 500s gone. Provider catalog 11. | grok |
| 2026-08-13 | Typed `standup` meeting: persist type, no-spend fixture → minutes + actions + Decision; duplicate convene refused. FEATURES meetings ✅. | grok |
| 2026-08-13 | PRD P1 glue: pack YAML import/export in MC; comms **Dry-run deliver** Receipt (no Slack/email); improvement-program create form (promote stays enterprise-badged). Catalog `built` only when reportable. Code Tasks shows GitHub plane. Deploy SOP (B6) requires `qa-live` after shared-tag bump. | grok |
| 2026-08-13 | Unified E2E QA harness: `make qa-harness` (preflight + 44-probe settings/features + control + BugBox/HookWatch/ApproveIt). Live catalog **55/55**. Launch gated by `BOSS_QA_ALLOW_CANARIES`. Docs `QA_HARNESS.md`. | grok |
| 2026-08-13 | PRD completeness audit vs current code (not Aug 12 stale): `docs/operations/qa-2026-08-13-prd-audit.md`. Top Community holes: BYOK paste, local base URL, factory plane on factory surfaces, pack import UI, typed meetings, assurance seed, comms dry-run, improvement-program writes. | grok |
| 2026-08-13 | Community PRD-partials: meetings simulated E2E + evidence; assurance honest empty + `POST /v1/assurance/seed-demo`; local OpenAI-compatible entitlement `base_url`. W4.5 Done (code). FEATURES.md: local URLs + assurance ✅; meetings stay ◐ | grok |
| 2026-08-13 | Community BYOK paste-and-connect (DoD #1): catalog + `POST /v1/providers/easy-connect` + Mission Control paste card; SecretRef/local_vault only; Plane BOSS-369 In Progress | grok |
| 2026-08-13 | QA-3 incomplete surfaces: factory WIP dual-plane + no swallowed fetch (dashboard/previews/WIP); list `project_id` defaults; verified-test View honesty. Doc `qa-2026-08-13-incomplete.md`. A6 Done (code). Schema 503 is `519ba85`. | grok |
| 2026-08-12 | U2 Community UI honesty + U5 claims freeze on `ai/grok/community-ga`: hide/badge Account Connect, marketplace, enterprise clusters, SIM; keep self-hosted SSO/IdP visible; `COMMUNITY_GA_CLAIMS_FREEZE.md`; DoD #5 Mostly (code); A7 Done (doc); B3 Done (code) | grok |
| 2026-08-12 | Plane open-issue honesty pass: YouTube EP1–3 scripts; engine extract checklist; GTM gap reconciliation (+ AI SRE `/v1/oncall-sre`); Cloud 302/303 cancelled as Phase-03 roadmap; Go S1–S3 left Backlog; licensing 300 remains human-blocked; GA umbrellas 314/337 stay In Progress pending Phase A | grok |
| 2026-08-12 | Multi-agent E2E Cloud bake: AC tests, Easy Connect link+sync, post-link team/marketplace/workers, retention+MC UX; 29 pytest green | agents |
| 2026-08-12 | Cloud product expansion: comparison/capabilities/workers + complete/unlink; MC Community-safe cloud feed; device pending codes; honest managed workers | codex |
| 2026-08-12 | BOSS Cloud product distinction: spec + `/v1/cloud/*` catalog/status/easy-connect (device+browser); Community independent; AC device authorize; MC Cloud CTA; tests 4/4 | codex |
| 2026-08-12 | Console UI/UX IA panel (6 experts): 8-primary + Settings hub; zero feature loss; recs in UI_UX_IA_RECOMMENDATIONS.md | codex |
| 2026-08-12 | Public site UX aligned to GTM simplification; redeployed boss-website `9268c69` | codex |
| 2026-08-12 | Created living plan from 15-panel GA assessment; DoD 3.5/8; Phase A defined | GA campaign |
| 2026-08-12 | Prior: BYOK full kernel #157; GitHub easy-connect #158 live; OpenAPI 0.11.0 #161; gate 10/10 | codex/grok |

---

## How to update this plan (checklist for agents)

1. Run or cite live proof (HTTP codes, gate log path, image tags).  
2. Edit **DoD table** status + **Phase tracker** state.  
3. Update **Live stack truth** after any redeploy.  
4. Move Plane issues; comment evidence (no secrets).  
5. Append **Changelog** row.  
6. If DoD score changes, note it in BOSS-337 comment.  
7. Do **not** claim GA until Phase C exits are green.
