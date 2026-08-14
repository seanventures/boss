# PRD completeness audit — Community (2026-08-13)

**Branch audited:** `ai/grok/community-ga`  
**Governing intent:** `docs/product/PRD-v2.0.md` (Constitution + Community §732)  
**Community contract:** `docs/product/BOSS_COMMUNITY_EDITION_SPEC.md`, `docs/community/FEATURES.md`  
**Current DoD:** `docs/operations/GA_LIVING_PLAN.md` (2026-08-13)  
**Official Partial ledger:** `docs/features/partial.md` (3 rows; all Cloud/Enterprise)  
**Method:** Code + tests + Mission Control sources. Aug 11–12 live panels are **historical**; each stale claim was re-checked before reuse.  
**Not a GA declaration.**

---

## 1. One-line verdict

**Community kernel, edition mounts, GitHub GHX, Studio seed, and self-hosted SSO are built; the remaining Community-completable holes are operator glue — paste-and-connect BYOK, first-class local model base URLs, pack import/export UI, factory plane honesty on the factory surfaces, typed meetings, Assurance first-run, comms dry-run close, and improvement-program writes.**

---

## 2. Capability matrix

Ratings are **code completeness** for Community-in-scope PRD / Community-spec behavior (runtime + tests + honest UI). Live dogfood reliability is noted only when it is the gap, not as a missing API.

| # | PRD / Community area | Rating | Evidence path | Gap |
|---|---|---|---|---|
| 1 | Local auth / API keys / no hosted AC required | **Complete** | `apps/api/app/main.py`, `apps/web/lib/auth-session.ts`, `apps/api/tests/test_independence.py`, `test_edition_routers.py` | None for Community. Hosted login is Cloud. |
| 2 | Canonical objects, tasks, plans, jobs | **Complete** | `db/schema.sql`, `engine/boss_engine/services/{canonical,intake,jobs}.py`, TRACE CAN/RUN | None. |
| 3 | Leases, heartbeat, fencing, DLQ | **Complete** | `jobs.py`, `apps/worker/boss_worker/`, `test_api.py`, `test_worker.py` | Substrate reliability is ops, not missing kernel. |
| 4 | Decisions inbox | **Complete** | `decisions.py`, `apps/web/app/page.tsx`, TRACE DEC-001–005 | UX noise is polish, not missing capability. |
| 5 | Receipts + dedupe | **Complete** | `receipts.py`, effect services, TRACE RUN-004 | None. |
| 6 | Events / audit / change journal | **Complete** | `core.py`, `audit.py`, TRACE AUD/EVT | None. |
| 7 | Budget Governor (API quote/reserve/consume/release) | **Complete** (HTTP) / **Partial** (engine composition) | `engine/boss_engine/services/budget.py`, `/v1/budget/*`, TRACE BUD-001–006 | Community engine composition injects `NoopBudgetPort` (`apps/api/app/bootstrap_ports.py`) so factory spend is unmetered while the dashboard still looks like a real governor. Community spec calls this a soft port; UI does not label it. |
| 8 | Metatron + Mission Control shell | **Complete** | `metatron.py`, `apps/web/app/{page.tsx,ConsoleShell.tsx}`, TRACE MET | Console IA (8-primary nav) is optional Phase B, not a missing PRD primitive. |
| 9 | HTTP API + OpenAPI + api-client | **Complete** | `api/openapi.yaml`, `packages/api-client` | Residual contract lag is not a Community feature hole. |
| 10 | CLI (`boss-engine`) + MCP server | **Complete** | `engine/pyproject.toml` `boss-engine`, `engine/boss_engine/mcp/server.py`, `engine/tests/test_mcp.py` | None. |
| 11 | Role / skill / pack formats + install | **Complete** | `packages/specpack/`, `/v1/packs`, `/v1/packs/install`, org pack list UI | Import/export UI missing (row 12). |
| 12 | Pack / skill **import/export** (PRD Community include + `community_edition.yaml`) | **Partial** | HTTP: `POST /v1/packs/import`, `GET /v1/packs/{id}/export` (`apps/api/app/main.py` ~10642), `POST /v1/skills/import`; client methods exist; tests `test_pack_import_and_export_http_endpoints` | Mission Control only lists + Install (`apps/web/app/page.tsx` Company Pack; `installPackAction` only). No YAML upload/download. GAP-PRD-AUDIT-001 P3 still valid as **UI glue**, not missing API. |
| 13 | Software Factory (spec → evidence → PR path) | **Partial** | `software_factory.py`, `governed_code_task_service.py`, TRACE SWF, `test_api.py` | Runtime exists. Reliability/substrate and dual-plane **primary-surface** honesty remain. Do not rebuild the factory. |
| 14 | Factory GitHub dual-plane (`github_plane`) | **Partial** | Engine: `software_factory.bind_project_github`, `engine/tests/test_github_dual_plane.py`; API: `GET /v1/factory/wip` attaches `github_plane`; UI: `FactoryScaleCard` only (`apps/web/app/phase3-ui.tsx`, `lib/factory.ts`) | Code Tasks, GitHub MC, and Software Factory evidence **do not** show which plane will open the PR. Living plan A6 still open. |
| 15 | GitHub project sync (GHX) | **Complete** (code) | TRACE GHX-001–009, `github-operations-ui.tsx`, `test_github_*` | Live wiring is Done per living plan. Not a missing product surface. |
| 16 | ProviderOps kernel (Community-mounted) | **Complete** (mount + runtime) | `apps/api/app/routers/enterprise.py` (providers/changesets **not** enterprise); `test_community_byok_self_infra_is_one_edition`; `test_edition_routers.py` | **Historical Aug 12 “404 / paid-gated” is stale.** Write actuation still fail-closed without creds (correct). |
| 17 | BYOK secrets + provider accounts | **Partial** | `POST /v1/secrets`, `POST /v1/secrets/keypair`, `POST /v1/providers` exist (`openapi.yaml`, `putSecretAction`, `createProviderAccountAction`) | Provider connection form is **SecretRef pointer-only** (`provider-configuration-ui.tsx` placeholder `"SecretRef pointer"`). Paste-and-create exists only on **delivery** secrets (`delivery-manage.tsx`). DoD #1. |
| 18 | Local / OpenAI-compatible base URLs | **Partial** (◐ FEATURES) | Adapter honors `OPENAI_BASE_URL` (`test_openai_adapter_base_url.py`); lane env defaults for groq/openrouter/deepseek | No first-class field on `ProviderAccount` / entitlement source; no MC control to point a connection at Ollama/vLLM. Env-only. |
| 19 | Model entitlement broker (Community) | **Complete** (local allow-all by design) | `entitlements.py`, `model_gateway.py`, TRACE MEB; Community `AllowAllEntitlementPort` | Managed multi-tenant gateway is Cloud. Community routing is allow-all + BYOK. |
| 20 | Sandbox execution plane | **Partial** / **Blocked (external)** for live rootfs | `sandbox*.py`, `docs/product/SANDBOX_GA_STATUS.md`, claims freeze `BOSS_MICROVM_CLAIMS_FROZEN` | Code path exists. Live rootfs bake is **not** a this-week product hole. |
| 21 | Operator previews | **Partial** | Preview APIs + `open_url` contract; `test_preview_urls_are_operator_reachable.py` | View-HTML of a **running** preview is live/substrate, not missing UI. |
| 22 | Deploy / CI/CD empty honesty | **Complete** (honesty) | `delivery-ui.tsx`, `monitoring-ui.tsx`, `apps/web/tests/delivery-empty-states.test.tsx` | Empty CTA is the Community DoD. Production fleet writes are external. |
| 23 | Agent Studio (catalog, wizard, seed) | **Complete** (code) | `POST /v1/agent-studio/catalog/seed`, `agent-studio-ui.tsx` first-run “Install BOSS starter catalog”, `agent-studio-actions.ts`, `test_agent_studio_a2a.py` | **Historical “Studio empty / 404 seed” is stale in code.** Live reseed is ops. Do not rebuild Studio. |
| 24 | Workflow registry + runtime | **Complete** | TRACE WFL-001, `workflows.py`, `workflow-registry-ui.tsx`, `test_workflow_runtime.py` | Sealed `ceo_command_to_pr` as a one-click product run is still a demo-path gap, not a missing runtime. |
| 25 | Agent Groups / A2A | **Complete** (runtime) | TRACE AGP-001, `governed_collaboration.py`, Studio A2A health | Not a Community ship-blocker. |
| 26 | Skills + integration registry | **Partial** | TRACE SKL/INT, `/v1/skills/import`, integration objects + workflows | No Mission Control installer for skill YAML or integration connections (wizard accepts text refs only). |
| 27 | Work orchestration / portfolio | **Complete** | `work_orchestration.py`, `work-orchestration-ui.tsx`, `test_work_orchestration.py` | None for Community. |
| 28 | Meetings / multi-agent panels | **Partial** (◐ FEATURES) | `meeting_service.py` is a **PRD-panel** (research→debate→draft); UI `meeting-room-ui.tsx`; minutes artifact + Decision | PRD §Meetings requires typed operating meetings (standup, OBR, go/no-go, budget review, …) and `propose→critique→revise→vote→dissent→publish`. `MeetingConsensusCard` is a disconnected vote calculator, not bound to a meeting run. |
| 29 | Assurance / ground-truth | **Partial** (◐ FEATURES) | TRACE AST-001, `assurance.py`, `assurance-ui.tsx`, `test_governance_runtime.py` | Runtime is deep. UI empty copy is thin (“No cases recorded yet”) under a “measured, fenced, independently verified” hero. No starter seed like Studio. Tests cover populated feed only (`assurance.test.tsx`). |
| 30 | Communications outbox/inbox ACK | **Partial** | `comms.py`, `/v1/comms/outbox/{id}/deliver`, policy modes `disabled|draft_only|real_send_allowed`, MC draft + “Queue send” | “Queue send” still requires `real_send_allowed` to actually deliver. No operator **dry-run deliver → Receipt** CTA. Slack card is render/digest only. Looks finished without a closed Community demo. |
| 31 | Self-hosted SSO / SAML / SCIM / IdP catalog | **Complete** | Community prefixes in `enterprise.py`; `test_community_sso_workflow_is_one_edition`; `identity-providers-ui.tsx`; edition helpers keep SSO visible | **Historical “SSO is paid / GAP-003 Community exclusion” is stale.** Live IdP credentials remain operator config (blocked external), not missing code. |
| 32 | Edition honesty (hide paid CTAs) | **Complete** (code) | `apps/web/lib/edition.ts`, `PaidSurfaceGate`, `edition-visibility.test.ts`, SIM honesty in `self-improvement.ts` | Live web image lag is ops. Residual IA (design-system nav) is optional. |
| 33 | Improvement programs (Community SIM alternative) | **Partial** | API: GET/POST `/v1/improvement-programs`, trials, promote (`main.py` ~6620, `test_improvement_programs.py`) | MC is **read-only list** (`page.tsx` improvement-programs-panel). No create/trial/promote controls. |
| 34 | Self-improvement canary/rollout | **Cloud/Enterprise only** | `/v1/self-improvement/*` enterprise-mounted; UI hidden on Community | Not a Community gap. |
| 35 | Easy Connect `/v1/cloud/*` | **Complete** | Community-mounted catalog/status/easy-connect; fail-closed without AC URL | Hosted AC itself is Cloud. |
| 36 | Account Connect / marketplace / clusters / HA | **Cloud/Enterprise only** | `community_edition.yaml` excludes; `PaidSurfaceGate` | Official Partial: COM-PKG-001, ACC-001, ENT-CLUST-001. |
| 37 | Earned autonomy G1–G3 | **Partial** (not Community GA claim) | `test_earned_autonomy_g1_g3.py`, claims freeze Forbidden | Do not treat as this-week Community hole. |

### Historical Aug 12 claims — verified, do **not** repeat as current gaps

| Stale claim (Aug 11–12) | Current code truth |
|---|---|
| ProviderOps `/v1/providers` enterprise-gated → Community 404 | **False.** Community-mounted; edition tests assert presence. |
| GitHub MC 0 connections / 0 bindings | **Live-historical.** Code + living plan: 2 conns / 2 binds. Not a missing feature. |
| Agent Studio empty / seed 404 | **False in code.** Seed route + first-run Install button exist; living plan A3 seed Done. |
| SSO/SAML/SCIM is Enterprise-only | **False.** Community includes self-hosted SSO; paid is hosted IdP / Account Connect. |
| SIM UI fully dead on Community | **Mostly fixed.** Proposals hidden; improvement-programs listed (write UI still missing). |
| CI/CD pretends a fleet exists | **Fixed.** Empty-state tests (BOSS-344). |

---

## 3. Top 8 Community-completable holes

Ranked by product impact for a self-hosted operator **this week**. All are implementable in this repo without Cloud, legal, rootfs, or 14-day gates.

### 1. BYOK paste-and-connect (one-shot key UX) — **P0**

**Why:** Community DoD #1. PRD: “you own the keys.” Today an operator must mint a SecretRef out-of-band (env name) and paste a **pointer** into ProviderOps. Delivery already has write-only paste (`putSecretAction` → `POST /v1/secrets`). ProviderOps does not compose the two.

**Likely files:**  
`apps/web/app/provider-configuration-ui.tsx`, `apps/web/app/actions.ts` (`createProviderAccountAction`, `putSecretAction`), `apps/web/lib/providerops.ts`, `engine/boss_engine/services/{secrets_service,resource_plane,secret_refs}.py`, `apps/web/tests/providerops.test.ts`

**E2E DoD:** From System Configuration, paste an API key (never echoed back) → `POST /v1/secrets` → `POST /v1/providers` with the new SecretRef → Test connection writes a Receipt → UI shows configured marker, never the material or pointer.

**Tests:** API: secret create + provider create + test receipt + redaction. Web: form does not submit raw key to `/v1/providers`; second visit cannot read the value.

### 2. Local / OpenAI-compatible base URL as a first-class connection field — **P0**

**Why:** FEATURES.md and Community spec mark this ◐. Adapter already accepts `openai_base_url`; ProviderAccount object has no endpoint field; MC cannot aim a BYOK lane at `http://ollama:11434/v1`.

**Likely files:**  
`packages/specpack/objects/provider_account.yaml`, `packages/specpack/models/providers.yaml`, `engine/boss_engine/services/{resource_plane,openai_adapter,entitlements}.py`, `apps/web/app/provider-configuration-ui.tsx`, `apps/api/tests/test_openai_adapter_base_url.py`

**E2E DoD:** Create an `openai`-compatible provider with SecretRef + **base URL** → entitlement/model probe uses that URL → route decision / receipt records the endpoint (not the key). Empty URL still means official API.

**Tests:** Adapter construction with stored base URL; fail-closed on unreachable URL; UI field present only for compatible providers.

### 3. Factory dual-plane honesty on the **factory** surfaces — **P0**

**Why:** Engine + WIP card already advertise `github_plane`. Operators open Code Tasks / GitHub MC, not “Factory scale.” Living plan A6.

**Likely files:**  
`apps/web/app/code-tasks-ui.tsx`, `apps/web/app/github-operations-ui.tsx`, `apps/web/app/page.tsx` (Software Factory evidence), `engine/boss_engine/services/software_factory.py`, `engine/tests/test_github_dual_plane.py`, `apps/web/lib/factory.ts`

**E2E DoD:** With a primary binding, Code Task create + Factory evidence show `mission_control_binding` + `owner/repo`. With only `BOSS_GITHUB_TOKEN`, they show `legacy_env` and a one-line “connect in GitHub” link. Propose-PR payload includes `github_plane`.

**Tests:** Extend dual-plane tests to the code-task response; web test that the badge renders both values.

### 4. Pack (and skill) import/export in Mission Control — **P1**

**Why:** PRD Community include list + `community_edition.yaml: import_export`. HTTP and client are done (`test_pack_import_and_export_http_endpoints`). UI is catalog-install only.

**Likely files:**  
`apps/web/app/page.tsx` (Company Pack card), `apps/web/app/actions.ts`, `apps/web/lib/packs.ts`, `packages/api-client/src/index.ts`, `engine/boss_engine/services/specpack.py`, `apps/api/app/main.py` (`/v1/skills/import`)

**E2E DoD:** Upload a local pack YAML → receipt `pack.import` + `pack.imported` event + pack appears installed. Export downloads YAML. Duplicate `dedupe_key` is a no-op. Invalid YAML 422 with visible error. Skill bundle import optional same card.

**Tests:** Existing API tests stay; add web action tests for import/export; keep marketplace **hidden** on Community.

### 5. Meetings as first-class operating objects (not only PRD panels) — **P1**

**Why:** FEATURES ◐. PRD requires typed meetings + consensus protocol. Runtime is a single PRD-debate pipeline.

**Likely files:**  
`engine/boss_engine/services/meeting_service.py`, `meeting_consensus.py`, `packages/specpack/objects/meeting.yaml`, `packages/specpack/workflows/meeting_consensus.yaml`, `apps/web/app/meeting-room-ui.tsx`, `apps/web/app/phase3-ui.tsx`, `apps/api/app/main.py` `/v1/meetings*`

**E2E DoD (smallest ship):** Convene one non-PRD type (e.g. `standup` or `release_go_no_go`) that produces minutes artifact, action items, optional dissent, and a Decision. Wire `MeetingConsensusCard` to a real `meeting_id` (or remove the orphan calculator).

**Tests:** Type enum persist; minutes + decision linkage; tally attached to meeting; refuse duplicate running convene (already present).

### 6. Assurance empty-state honesty + optional starter seed — **P1**

**Why:** Living plan B5. Runtime is implemented; product looks “on” at zero cases.

**Likely files:**  
`apps/web/app/assurance-ui.tsx`, `apps/web/lib/assurance.ts`, `apps/web/tests/assurance.test.tsx`, `engine/boss_engine/services/assurance.py`, optional `POST /v1/assurance/catalog/seed` mirroring Studio

**E2E DoD:** Empty project shows “No measured contracts yet — install a starter Assurance case or create one” (not a finished proof plane). Optional idempotent seed creates one metric + one case + one surface, no spend. Seeded evaluate path is visible.

**Tests:** Empty-state copy assertion; seed idempotent; no fake healthy conclusions.

### 7. Comms dry-run close-the-loop — **P1**

**Why:** Constitution §5. Draft + policy exist; “Queue send” implies delivery. Without `real_send_allowed` the operator gets a block, not a demonstrated dry-run Receipt.

**Likely files:**  
`engine/boss_engine/services/comms.py`, `apps/web/app/page.tsx` (comms), `apps/web/app/phase3-ui.tsx` (`SlackToolsCard`), `apps/web/app/actions.ts` (`queueOutboxMessageAction`), worker deliver path, `apps/web/tests/comms.test.ts`

**E2E DoD:** Default policy `draft_only`. Button **“Dry-run deliver”** writes a Receipt (`effect_kind` comms, `delivery_mode=draft_only` or `dry_run`), never calls Slack/email. UI labels the result “dry-run — no provider send”. Real send remains fail-closed until policy + creds.

**Tests:** Dry-run receipt + no adapter call; `real_send_allowed` without creds still blocks with visible reason; UI never says “sent” on dry-run.

### 8. Improvement-program write path in Mission Control — **P2**

**Why:** Community’s honest substitute for enterprise SIM. API can create programs, trials, promote, adopt-baseline. UI only lists.

**Likely files:**  
`apps/web/app/page.tsx` (improvement-programs-panel), `apps/web/lib/self-improvement.ts`, `apps/web/app/actions.ts`, `engine/boss_engine/services/improvement_programs.py`, `apps/api/tests/test_improvement_programs.py`

**E2E DoD:** Create program → add trial → see status. **Promote** stays disabled/badged on Community (enterprise SIM unmounted) with honest copy. No `/v1/self-improvement/*` buttons.

**Tests:** Create/list round-trip; Community edition does not call SIM promote; paid edition unchanged.

---

## 4. Explicit exclusions (do not file as Community gaps)

| Exclusion | Why |
|---|---|
| Hosted Account Connect, seats, billing portal | Cloud (ACC-001 Partial is correct) |
| Hosted marketplace catalog sync | Cloud |
| HA clustering / multi-node / k8s productization | Enterprise (ENT-CLUST-001) |
| Legal / OSS license decision | Human-blocked (existing BOSS-300) — do not invent a license |
| 14-day `qa-live` streak | Process / Phase C, not a feature |
| Live sandbox rootfs bake / unfreeze microVM claims | External/ops; claims freeze already honest |
| Real Slack/email/Mattermost send | External creds + policy; dry-run is the Community ship |
| Live IdP tenant (Okta/Entra production) | Operator config; **code** for SAML/SCIM is Community |
| Production ProviderOps fleet writes / DO billing keys | External + approval |
| Earned autonomy economy “done” | Forbidden claim; not Community DoD |
| Managed elastic workers / managed model gateway | Cloud planned |
| Public GA / Actions-green private main | Process |

---

## 5. What NOT to rebuild (already solid)

- Control-plane kernel: tasks, plans, jobs, leases, DLQ, receipts, events, decisions, artifacts  
- Budget **HTTP** quote/reserve/consume/release (do not replace; optionally label Community `NoopBudgetPort` on factory)  
- Edition router: ProviderOps + self-hosted SSO **on** Community; AC / marketplace / SIM / clusters **off**  
- GitHub GHX plane + Mission Control GitHub operations UI  
- Software Factory / governed code-task **engine** (add honesty + BYOK, do not rewrite)  
- Agent Studio catalog seed + commissioning wizard  
- Workflow registry/runtime, Agent Groups, work orchestration  
- CI/CD and Monitoring **empty-state honesty**  
- Easy Connect fail-closed catalog  
- MCP server + `boss-engine` CLI  
- Specpack roles/skills/workflows/community_edition pack  
- Kernel tests for dedupe, denial visibility, dual-plane bind, pack import HTTP  

---

## 6. Relationship to official Partial ledger

`docs/features/partial.md` lists only COM-PKG-001, ACC-001, ENT-CLUST-001. Those are **Cloud/Enterprise**. They must not be copied into Community gap lists.

Community ◐ items that **should** eventually appear in FEATURES/TRACE after this week’s ships: local base URLs, meetings, assurance first-run, pack import UI, BYOK paste UX. Do not mark them Implemented until the E2E DoDs above pass.

---

*Audit complete. Coordinate implementation on `ai/grok/community-ga` or focused `ai/grok/<hole>` branches. Do not claim Community GA from this document.*
