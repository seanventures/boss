# BOSS GA living plan

**Status date:** 2026-08-12  
**Overall GA:** **NOT READY** (public). **CONDITIONAL** private Community preview only.  
**System of record:** Plane `goldeneye` / BOSS · umbrella **BOSS-337**  
**Scorecard:** [`ga-readiness-2026-08-12/GA_READINESS_SCORECARD.md`](ga-readiness-2026-08-12/GA_READINESS_SCORECARD.md)  
**E2E plan (detail):** [`E2E_COMPLETION_PLAN.md`](E2E_COMPLETION_PLAN.md)  
**Plane map:** [`PLANE_TRACKING_LIVE_QA.md`](PLANE_TRACKING_LIVE_QA.md)

> **Agent rule:** After every ship, deploy, or live proof, update **this file first** (progress table + changelog), then Plane issue comments, then E2E/Plane docs if states change. Never mark Done without live dogfood evidence.

---

## North star (Community DoD — 8 criteria)

| # | Criterion | Status | Evidence / notes | Next action |
|---|---|---|---|---|
| 1 | BYOK model + infra keys | **Partial** | Community ProviderOps kernel live 200; env SecretRefs; paste-UX incomplete | One-shot key UX; docs runbook |
| 2 | CEO command → task + budget path | **Mostly** | `qa-live` command+budget PASS | Seal plan→job path if claimed |
| 3 | Code → evidence → PR reliable | **Open** | ~41 open DLQ; substrate flaky | Phase A P0-2/3 |
| 4 | Previews reachable | **Open** | Host ok; View not proven; rootfs gap | P0-3/4 |
| 5 | Honest unpaid/unconfigured UI | **Partial** | SIM/empty CI improved; Enterprise still shown | Hide Enterprise on community |
| 6 | GitHub MC connected | **Done** | 2 conns + 2 binds live | Factory dual-plane honesty (P0-6) |
| 7 | Deploy path or honest empty CI/CD | **Honest empty** | 0 targets; empty CTA | Optional sandbox target later |
| 8 | Automated live gate | **Done** | `make qa-live` 10/10 PASS | Wire as deploy mandatory (W5.2) |

**DoD score:** **~3.5 / 8** closed · Target for private preview: **6 / 8** (close 3,4,5 substrate/UI) · Target for Community GA: **8 / 8** + 14-day green gate.

---

## Phase tracker (GA path)

### Phase A — 48h (must before any GA language)

| ID | Work | Plane | State | Exit |
|---|---|---|---|---|
| A1 | Shared-tag redeploy api+worker+web from `main` | BOSS-337, 342 | **Todo** | Images match; no `--env-template` clobber |
| A2 | Re-run `make qa-live` | BOSS-351 | **Todo** (last PASS on 968bc47) | 10/10 on new tag |
| A3 | Agent Studio seed + one deterministic run | BOSS-347 | **Todo** | Catalog 13/3; seed works (404 on 968bc47) |
| A4 | Sandbox rootfs bake **or** drop microVM claims | BOSS-340 | **Partial** host up | One provision success **or** claim freeze |
| A5 | DLQ triage top classes; prove no missing-git | BOSS-342 | **Partial** git in image tree | DLQ trend + root causes filed |
| A6 | Factory vs MC GitHub dual-plane honesty | BOSS-343 follow-on | **Todo** | Binding drives factory **or** UI label |
| A7 | Claims freeze (allowed/forbidden list) | BOSS-337 | **Drafted** in scorecard | Signed in release notes |

### Phase B — 1–2 weeks (private preview)

| ID | Work | Plane | State |
|---|---|---|---|
| B1 | Weekly sealed command→code→PR→Decision | BOSS-351 | Open |
| B2 | Preview View browser proof (non-loopback) | BOSS-341 | Open |
| B3 | Hide/badge Enterprise on community | BOSS-338 residual | Open |
| B4 | Residual hydration #418 | BOSS-346 | Open |
| B5 | Assurance empty honesty + optional seed | BOSS-337/348 | Open |
| B6 | Deploy SOP requires `qa-live` | BOSS-337 | Open |

### Phase C — Community GA

| ID | Work | State |
|---|---|---|
| C1 | 14 consecutive days `qa-live` green | Open |
| C2 | Security must-fix (principals, dep CI, rate limits) | Open |
| C3 | Legal/OSS readiness if public announce | Open |
| C4 | Tag + notes with allowed claims only | Open |

---

## Workstream status (E2E map)

| Stream | Intent | Status | Closed Plane | Open / residual |
|---|---|---|---|---|
| **W0** Community keys | BYOK kernel | **Done (code+live kernel)** | BOSS-350, #157 | A1 redeploy for full main |
| **W1** UI honesty | Edition/empty | **Mostly** | 338, 339, 344 partial | Enterprise nav; 346 |
| **W2** Substrate | Worker/sandbox/preview | **Partial** | 340 host, 341 code, 342 git image | Rootfs; live shared tag; DLQ |
| **W3** Vertical slice | CEO→PR sealed | **Partial** | 343 GitHub wire, 345 next_move, 349 OpenAPI, 351 gate | Reliable code path 351 |
| **W4** Product loops | Studio/Assurance/deploy | **Open** | — | 347, 348, 344 deploy optional |
| **W5** Gates + Plane | Live proof only | **Active** | Gate script Done | Mandatory deploy gate |

---

## Live stack truth (update on every deploy)

| Field | Value | As of |
|---|---|---|
| Edition | community | 2026-08-12 |
| api image | `boss-api:968bc47` | live |
| web image | `boss-web:968bc47` | live |
| worker image | `boss-worker:91faf6d` | live (skew) |
| main HEAD | `4727de7` | git |
| `qa-live` | **10/10 PASS** | 2026-08-12 |
| Open DLQ sample | ~41 | 2026-08-12 |
| GitHub MC | 2 connections, 2 bindings | live |
| Agent Studio | 0 agents; seed 404 on live tag | live |
| Sandbox host | connected; rootfs gap | live |

---

## Plane issue board (living)

| Issue | Topic | State | Notes |
|---|---|---|---|
| BOSS-337 | Umbrella QA/GA | **In Progress** | GA assessment filed |
| BOSS-338 | Edition MC honesty | **Done** (partial residual) | Enterprise nav still visible |
| BOSS-339 | SIM community | **Done** | Ops honesty |
| BOSS-340 | Sandbox host | **Done** (host) | Rootfs still open work |
| BOSS-341 | Preview loopback | **Done** (code) | Live View proof pending |
| BOSS-342 | Worker git/image | **Done** (code) | Live shared-tag pending |
| BOSS-343 | GitHub MC wire | **Done** | Dual-plane follow-on |
| BOSS-344 | CI/CD empty honesty | **Done** | |
| BOSS-345 | next_move glue | **Done** | |
| BOSS-346 | Hydration #418 | **Open** | Residual pages |
| BOSS-347 | Agent Studio seed | **In Progress / Open** | Seed on live after A1 |
| BOSS-348 | Earned autonomy | **Open** | Not GA claim |
| BOSS-349 | OpenAPI parity | **Done** | #161; deploy lag |
| BOSS-350 | Community keys decision | **Done** | Full kernel #157 |
| BOSS-351 | Live E2E gate | **Done** (gate) | Factory E2E still open |

---

## Allowed / forbidden claims (living freeze)

### Allowed after Phase A complete

- Self-hosted governed control plane (tasks, budgets, decisions, receipts)
- Community BYOK ProviderOps kernel (env SecretRefs)
- Project GitHub connect + link + test receipts
- Metatron status Q&A
- Honest empty CI/CD / monitoring when undeployed
- Automated `qa-live` gate

### Forbidden until proven

- Autonomous unattended company / reliable factory unattended
- Agent Studio workforce out of the box
- Earned autonomy economy (G1–G3 complete)
- Sandboxes/previews always work
- Enterprise SSO/SCIM/marketplace in Community
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
| `POST /v1/cloud/easy-connect` browser + device | **Done** (fail-closed without AC URL) |
| Account Connect device authorize | **Done** (`/v1/device/*`) |
| MC Easy Connect CTA | **Done** (enterprise area card) |
| Managed elastic workers / full SaaS billing | **Planned** (catalog status honest) |
| Live deploy of this branch | **Todo** |

## Changelog (append-only)

| When | What changed | Who |
|---|---|---|
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
