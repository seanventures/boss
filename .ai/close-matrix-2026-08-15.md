# Close / hold matrix — 2026-08-15

Inspect-only. **No Plane state changes.** **No public GA stamp.** Secrets not printed.

| Field | Value |
|---|---|
| Date | 2026-08-15 |
| Repo HEAD (`main`) | `3af9c6481e1b9eb456b951f993690c50f0dce48b` — `feat(ops): five remaining operator slices` |
| Live shared tag | `b6d8ccc` (api + worker + web) — **9 commits behind HEAD** |
| Plane | workspace `goldeneye` · project `cc97d5cc-e8c6-4893-ac14-7f646c37f3d7` |
| Rule | Close only when ticket DoD + live-proof (if required) are actually true. Umbrellas / legal / sealed / vault / official-reports stay open per hard rules. |

## How this was built

1. Read each listed ticket’s description (Plane dumps + issue HTML) and the latest **recorded** comments / evidence notes.
2. Inspect `main` for shipped code and tests (read-only).
3. Compare to live dogfood probes already on disk (`.ai/sealed-live-blocker.md`, `.ai/byok-live-blocker.md`, `.ai/official-qa-live.md`).

Plane comment bodies used (latest on file, not re-fetched this session):

- BOSS-314 description is a long comment-log of the GA loop (through v0.10.0 / `32d6d9d`).
- BOSS-347 description includes the 2026-08-12 seed implementation comment (`95cd800`).
- BOSS-372 comment `22892af1` (2026-08-13): remediations path In Progress; remaining live classes TPM + missing DO creds.
- BOSS-377 issue `91b0edce` (updated 2026-08-14 19:22Z): still In Progress.
- BOSS-351 issue `c1f3b9c6` (updated 2026-08-13 22:50Z): In Progress, `completed_at=null`.
- BOSS-381 comment `fa14b433` (2026-08-15): inspect-only; stays In Progress.
- `.ai/status.md` + `.ai/activity-log.md` (2026-08-15) for 341/347/370/375/378/379/380.

Tickets that are **not** In Progress in the last Plane list are still in the table so the hold is explicit.

## Matrix

| Ticket | Plane state (last known) | Title (short) | Shipped-in-main commit(s) | Live-proof required | CLOSE now? | Reason | Remaining blocker (name-only) |
|---|---|---|---|---|---|---|---|
| **292** | Backlog (list dump) / treated In Progress in GA plan | Design-partner & developer onboarding on private engine repo | `f85c873` (PR #129 pack) · week-1 path in v0.12.0 / PR #173 (`e5c93a7` release cut) | Two external partners self-serve the pack to a sample (or triaged block); zero open P0s for 2 weeks | **no** | Docs exist and match the *pack* (`docs/operations/DESIGN_PARTNER_ONBOARDING.md`, `engine/DESIGN_PARTNER_ONBOARDING.md`, `make onboard`, `OnboardingHelpCard`). Ticket DoD / B7 also requires partner self-serve evidence + sample-repo ownership. That is not on file. | two-partner-self-serve |
| **293** | In Progress | Public-flip readiness checklist for engine repo | `923c27a` (PR #130 checklist) · `docs/operations/PUBLIC_FLIP_CHECKLIST.md` | C1–C8: license decided, secret scan, dep/license audit, hygiene, fence, GTM, flip | **no** | Public-GA umbrella. Checklist verdict NOT READY. Hard rule: stay open. | public-flip-gates |
| **300** | Todo | GAP: resolve OSS licensing strategy | none (must not change LICENSE) | Human legal/product `Status: DECIDED` in `docs/operations/OSS_LICENSING_DECISION.md` | **no** | Human legal. Never close. File is UNDECIDED. Apache-2.0 *files* are not a decision. | human-legal-decision |
| **314** | In Progress | GA readiness closure loop — release baseline and evidence | Many children merged (`8e8dd4e`, `e5c93a7` v0.12.0, tip `3af9c64`). Umbrella is not a child. | Phase A–C live + legal + public-flip; not a green local suite | **no** | Public-GA umbrella. Coordinates children; does not close because children shipped. | public-ga-umbrella |
| **337** | In Progress | Deep agentic production QA (goldeneye live) — 2026-08-11 | Living plan + gates on main (`docs/operations/GA_LIVING_PLAN.md`, `qa-live`, captain, canaries) | Phase C / 14-day calendar + C2 + BOSS-300; working-software bar is not public GA | **no** | Public-GA umbrella. Hard rule: stay open. Live still `b6d8ccc`. | public-ga-umbrella |
| **341** | Done live in GA plan (2026-08-14); 2026-08-15 residual on main | Preview View URLs expose 127.0.0.1 | Earlier publicize on live tag · residual gates `3af9c64` (`feature_sweep` + `factory_e2e_gate` refuse loopback View) | Live `GET /v1/previews` operator `url`/`open_url` never loopback; View opens or says unavailable | **no** | If Plane is already Done, do not re-close. Residual loopback *gates* are on HEAD, not proven on live `b6d8ccc`. | live-view-loopback |
| **347** | In Progress | Agent Studio empty on dogfood — seed / first-run | Seed path `95cd800` era · workflows + honest A2A `38d8feb` | Live catalog 13 agents / 3 panels / workflows; A2A `not_configured` (not false-red); no blank Studio | **no** | Workflow seed + A2A honesty landed **after** live tag. FEATURES stays ◐. | live-tag-lag |
| **351** | In Progress | One sealed dogfood E2E — command → evidence → PR decision → receipts | Contract `sealed_e2e_contract.py` · live probe `73a39f4` · gate `scripts/qa/sealed_e2e_live.py` | Live `simulated=false` + `open_pr=true` **receipt + hold decision** pair | **no** | Hard rule. Probe 2026-08-15: no sealed receipt; latest receipts are ticks/overwatch; factory create flag off. | factory-allow-create |
| **369** | In Progress | Community BYOK paste-and-connect (model + infra keys) | Easy-connect + MC paste (v0.12 / Community GA) · bind probe `73a39f4` · tests `test_provider_easy_connect.py` | Real `local_vault` SecretRef + bind (`secret_ref_id` + `last4`, no raw leak) | **no** | Hard rule. Live easy-connect `local_vault` is 503. Existing openai bind is `env:` only. | vault-master-missing |
| **370** | In Progress | QA harness feature sweep | Catalog + workforce GETs · loopback/KMS probes `3af9c64` · `scripts/tests/test_feature_sweep.py` | Live `scripts/qa/feature_sweep.py` on current tag (2xx / honest skip; no invented pass) | **no** | Sweep expanded after live tag (KMS, catalog, loopback fail). Live `b6d8ccc` is not that sweep. | live-tag-lag |
| **372** | In Progress | DLQ remediations (342 residual) | Operator path 2026-08-13 · MC feed `7e5469e` · `test_deadletter_remediations.py` | Live remediations list + one durable retry/escalate/hold (not silent requeue) | **no** | Remediations UI commit is **after** live tag. Historical 41 DLQs (TPM / missing DO creds) remain. | live-tag-lag |
| **375** | In Progress | Expand factory E2E gate: quality loop + scorecard v1.1 (no spend) | Quality loop `91d041a` · gate honesty / preview-url structure `3af9c64` | Live `factory_e2e_gate` read-only / fixture only; scorecard `not_scored` stays honest | **no** | Code on HEAD. Live tag predates the 2026-08-15 gate checks. No-spend ≠ Done until live gate run. | live-tag-lag |
| **376** | In Progress | Process runtime harden (receipts, evidence, panel) | `57d632d` (`AgentProcessService` receipts + fail-closed panel) · `engine/tests/test_agent_process.py` | Live process run with receipts; panel fail-closed; no silent drop | **no** | Implementation on main. FEATURES ◐. Not proven on live tag as hardened runtime. | live-process-dogfood |
| **377** | In Progress | NL + manual + Metatron authoring of agent processes | `dd5d6ba` (compiler, MC builder, Metatron create/list/start) | Live compile + start (simulated, no spend) from NL / builder / captain | **no** | Issue still In Progress (updated 2026-08-14). FEATURES ◐. Authoring not live-dogfooded as Done. | live-process-dogfood |
| **378** | In Progress | Project Manager operate / test / validate / improve | `dd5d6ba` + reject/no-spend copy `3af9c64` · `test_process_manager.py` | Live `GET /v1/process-manager` + one no-spend steward tick | **no** | Mounted on HEAD; QA harness still documents 404-skip on older tags. Live is older tag. | live-tag-lag |
| **379** | In Progress | Global searchable activity log | `f1cd30b` + `e5f0851` + `b6d8ccc` (on live) · actor/stuck banner `3af9c64` | Live `GET /v1/activity` search/filter + MC area; no invented success | **no** | Core log is on live tag; actor filter + stuck-count banner are **after** live. FEATURES ◐. | live-tag-lag |
| **380** | In Progress | SecretRef consumption (factory / OpenAI / GitHub / Plane) | `254c85d` + `9afc7e0` · `engine/tests/test_secretref_consumption.py` | Live consume via bound SecretRef (fail-closed if bound-but-broken) | **no** | Code after live tag. Live still env-pointer openai. Consumption not proven on goldeneye. | live-tag-lag |
| **381** | In Progress | Official QA /reports v1.1 (honesty) | `254c85d` + `73a39f4` + `42ed960` · `scripts/qa/official_reports.py` | Official `/reports` for BugBox/HookWatch/ApproveIt with honesty blob; no invented scores | **no** | Hard rule. Inspect-only is not Done. `BOSS_QA_ALLOW_OFFICIAL` unset. Catalog `/report`s missing; existing reports `ui`/`ux` `not_scored`. | official-allow-flag |

## CLOSE now? summary

**Zero yes.** Do not move any of these to Done from this matrix.

| Bucket | Tickets |
|---|---|
| Never close (rule) | 300, 293, 314, 337 |
| Stay open until live sealed / vault / official | 351, 369, 381 |
| Docs shipped; partner exit not met | 292 |
| Code on `main`, live tag lag or FEATURES ◐ | 341 residual, 347, 370, 372, 375, 376, 377, 378, 379, 380 |

## Live vs main (why “shipped” ≠ close)

Live `b6d8ccc` includes activity-log constructor fix and earlier Community GA. It does **not** include:

`254c85d` SecretRef + official honesty · `73a39f4` named blockers · `9afc7e0` GitHub SecretRef connection · `42ed960` official inspect `project_id` · `7e5469e` DLQ remediations feed · `38d8feb` Studio workflows + A2A truth · `3af9c64` five operator slices (341/370/375/378/379 polish).

Redeploy shared tag from `3af9c64` (no `--env-template`) is a prerequisite for closing any of the live-tag-lag rows — and is still not enough for 351/369/381/300/umbrellas.

## Ticket notes (description + latest comment)

### 292 — onboarding
**DoD (issue):** private-repo pack: README, sample project, MCP setup, support channel, feedback loop; flip blocked until partners self-serve deploy + sample. Later comment: stay In Progress until PR merges **and** owner supplies sample repo; two-partner exit.

**Shipped:** pack + `make onboard` + overview card + tests. **Missing:** two redacted partner completions; B5 two-week P0-clear.

### 293 — public flip
**DoD:** C1 license (300) through C8 repo public. `PUBLIC_FLIP_CHECKLIST.md` = NOT READY. Umbrella stays open.

### 300 — legal
**DoD:** human `DECIDED`. Agents must not pick a license or close.

### 314 / 337 — GA umbrellas
**DoD:** release-control / live production QA until Phase C + legal + flip. Children can Done; these cannot.

### 341 — preview loopback
**DoD:** operator-reachable View; never hand `127.0.0.1` as openable URL; live proof. GA plan marked API Views Done 2026-08-14. 2026-08-15 added mechanical gates on HEAD only.

### 347 — Studio seed
**DoD:** seed demo agents/panels **or** wizard; A2A honest; not a blank flagship. First-run now also seeds specpack workflows (`38d8feb`). Live image predates that.

### 351 — sealed E2E
**DoD:** scripted live E2E with artifacts/receipts. 2026-08-15 probe: allow-create off; no `github.pull_request.create` in latest 50 receipts; no hold pair matching `validate_sealed_receipt` + `validate_sealed_decision`.

### 369 — BYOK bind
**DoD:** paste-and-connect to SecretRef. Paste UX exists. Live bind: `POST /v1/providers/easy-connect` `backend=local_vault` → 503 because vault master flag is absent. Env pointer is not a `local_vault` bind.

### 370 — feature sweep
**DoD:** curated operator GET sweep; 404 on old tags = skip; loopback View = fail; no spendy POST.

### 372 — DLQ remediations
**Latest comment:** remediations list/create/retry fail-closed; classify credit≠credential; remaining live classes TPM + missing DO creds. MC feed commit not on live tag.

### 375 — factory E2E / scorecard v1.1
**DoD:** quality loop + UI/UX dimensions; unknown stays `not_scored`; no spend.

### 376 / 377 / 378 — process + PM
**376 DoD:** receipts, evidence, fail-closed panel. **377 DoD:** NL + manual + Metatron authoring. **378 DoD:** PM operate/test/validate/improve; metrics evidence-only; no silent template rewrite.

### 379 — activity log
**DoD:** searchable/sortable SoR over events + process + jobs + DLQ + receipts; never invent success. Core on live; actor/stuck banner not.

### 380 — SecretRef consumption
**DoD:** SecretRef first; env last-resort only if no ref bound; bound-but-broken fail-closed. Wired in OpenAI, factory guest, GitHub, Plane. Not on live tag.

### 381 — official /reports
**Latest comment:** inspect only. Official POST blocked. Catalog built `/report` missing (HookWatch pointer 404). Existing reports: `ui`/`ux` `not_scored`, no invented null scores. Inspect ≠ Done.

## Blocker names only

| Name | Means (no values) |
|---|---|
| `human-legal-decision` | BOSS-300 still UNDECIDED |
| `public-flip-gates` | C1–C8 / PF-* open |
| `public-ga-umbrella` | 314 / 337 / 293 stay In Progress |
| `two-partner-self-serve` | 292 B4/B5 |
| `live-view-loopback` | operator View still needs live non-loopback proof on current tag |
| `live-tag-lag` | HEAD not deployed (`b6d8ccc` vs `3af9c64`) |
| `factory-allow-create` | live create/canary flags off; no sealed spend |
| `vault-master-missing` | `local_vault` write-through cannot run |
| `official-allow-flag` | official POST not enabled |
| `live-process-dogfood` | process/PM authoring not live-accepted |

## Not done in this session

- Did not PATCH Plane states.
- Did not POST official QA, sealed `code.task`, or easy-connect with a real key.
- Did not redeploy goldeneye.
- Did not stamp FEATURES ✅ or public GA.
