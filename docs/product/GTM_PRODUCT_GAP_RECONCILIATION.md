# GTM product gap reconciliation

| Field | Value |
|---|---|
| **Plane** | BOSS-295 (tracking) · BOSS-304 (deck update / AI SRE wedge) |
| **Sources** | [`docs/marketing/GTM_GAP_ANALYSIS.md`](../marketing/GTM_GAP_ANALYSIS.md), [`docs/marketing/GTM_DECK_REVISION_BRIEF.md`](../marketing/GTM_DECK_REVISION_BRIEF.md), [`docs/product/ONCALL_SRE.md`](ONCALL_SRE.md) |
| **Product baseline** | Monorepo + Community/engine paths as of 2026-08-12 |
| **Purpose** | Single reconciliation of **deck promises vs shipped product**, including the **AI SRE / oncall** wedge now present as `/v1/oncall-sre/*` |
| **Date** | 2026-08-12 |

## Executive verdict

| Theme | Status | Notes |
|---|---|---|
| Phase 01 core loop (task → plan → work → evidence → decide) | **Shipped / demoable** | Governed coding path + receipts + budgets + decisions |
| Phase 02 OSS engine technical split | **Mostly shipped** | `engine/` package, Docker, MCP, Apache-2.0 files; **public flip blocked on BOSS-300** |
| Community tier copy honesty | **Partial** | Some deck claims oversold (simple MC, local models, skill SDK, role packs) — ship or reword |
| Enterprise AI SRE wedge | **Code present; deck lag** | `/v1/oncall-sre/*` + reliability bridge shipped; deck must add wedge (this doc + revision brief) |
| Hosted Cloud / managed models (Phase 03) | **Roadmap only** | Tracked as Phase-03; not active product work this cycle |
| Editable GTM deck file in repo | **Absent** | Apply [`GTM_DECK_REVISION_BRIEF.md`](../marketing/GTM_DECK_REVISION_BRIEF.md) to the Google Slides source |

---

## Lead surface decision (pick one — recommended)

**Lead with path B: OSS Engine + commercial control plane.**

```text
Developer entry: OSS Engine + CLI + MCP
        ↓
Team expansion: Commercial Control Plane (governance, ProviderOps, collab)
        ↓
Enterprise wedge: AI SRE / Autonomous Oncall
        ↓
Category vision: Business Operating System for AI labor
```

Do **not** lead with five co-equal surfaces on slide 3. Encode this in the deck; keep “Company OS” as vision, not day-one install story.

---

## Gap register (deck ↔ product)

### Critical (launch copy)

| ID | Deck claim | Product truth (2026-08-12) | Plane | Disposition |
|---|---|---|---|---|
| G1 | “Simple Mission Control” in free Community | OSS path is CLI + MCP + read-only HTTP MC; full multi-user console is commercial | #296 | **Ship lite MC or reword tier copy** before launch |
| G2 | Local / open models (Ollama, vLLM) | Cloud API keys + routing; no first-class Ollama/vLLM story as default | #297 | **Ship base-URL override + docs** or remove from Community promises |
| G3 | “Basic skill SDK” | SkillSpec registry; MCP is the working extension surface | #298 | **Market MCP as extension SDK** unless executor+docs ship |
| G4 | Community role packs in OSS artifact | Role packs live in monorepo `packages/specpack`, not necessarily in engine distro | #299 | Carve community pack into engine **or** reword |
| G5 | Sharp OSS / monetization split | Decision doc still OPEN | **#300** | **BLOCKED — human legal/product decision** |
| G6 | Zero-Postgres 5-minute quickstart | Ports exist; durable governed work still needs Postgres in honest demos | #301 | Verify clean-machine path or stop claiming zero-DB |

### Roadmap (not this-cycle product work)

| ID | Deck claim | Product truth | Plane | Disposition |
|---|---|---|---|---|
| R1 | Hosted BOSS Cloud tier (Mission Control SaaS, seats, local-node registration) | Spec + catalog honesty paths exist; multi-tenant SaaS not the active cycle | **#302** | Phase-03 roadmap — cancel active tracking this cycle |
| R2 | Turnkey managed models with plan limits + margin-aware routing | Budget Governor + MEB exist internally; no commercial plan limits product | **#303** | Phase-03 roadmap — cancel active tracking this cycle |

### Deck-side fixes (product ahead of deck)

| ID | Missing from deck | Product truth | Plane | Disposition |
|---|---|---|---|---|
| D1 | AI SRE / Autonomous Oncall wedge | Shipped control plane under `/v1/oncall-sre/*` + docs | **#304** | Add wedge + scorecard (below) |
| D2 | Launch assets (landing, one-pager) | Partial: website engine-first hero; one-pager exists | #305 | Continue marketing track |
| D3 | YouTube training | Script packages EP1–3 delivered | #288–290 | Recording outside repo |

---

## AI SRE / oncall wedge (shipped surface)

Canonical product doc: [`ONCALL_SRE.md`](ONCALL_SRE.md).

### Why it belongs on the deck

Since mid-2026 the repo’s flagship **enterprise** story is not “more chat agents” — it is **signal → investigate → governed remediation → verify**, with receipts, budgets, and fail-closed adapters. The GTM deck’s enterprise wedges predate this pivot; reconciliation requires an explicit AI SRE slide and scorecard.

### Shipped HTTP surface (`/v1/oncall-sre`)

| Method + path | Purpose |
|---|---|
| `GET/POST /v1/oncall-sre/schedules` | Rotation schedules (layers + participants) |
| `GET/POST /v1/oncall-sre/policies` | Escalation policies |
| `POST /v1/oncall-sre/pages` | Open synthetic or reliability-linked page |
| `POST /v1/oncall-sre/incidents/{id}/page` | Bridge reliability incident → page |
| `POST /v1/oncall-sre/incidents/{id}/ack` | Acknowledge |
| `POST /v1/oncall-sre/incidents/{id}/resolve` | Resolve |
| `GET /v1/oncall-sre/status?project_id=` | Who is on-call, open pages, adapters |
| `POST /v1/oncall-sre/from-reliability` | Page open reliability incidents |
| `POST /v1/oncall-sre/graph/link` | Link page/incident to knowledge-graph node |
| `GET /v1/oncall-sre/adapters` | Adapter catalog + live capability |

Distinct from conversational cards at `/v1/oncall/notify|preview` (RB-20 style).

### Governance invariants (safe to claim carefully)

- Every notify attempt uses **Receipt** + stable **dedupe_key** (`oncall_sre.notify`).
- Default without credentials: **dry-run / simulated** success (safe for CI).
- `force_live=true` without credentials: **fail closed** (HTTP 422).
- Adapters: PagerDuty, Opsgenie, Grafana OnCall, Slack, Mattermost (env-gated).

### Claim discipline for deck / sales

| Claim | Allowed? |
|---|---|
| On-call schedules, pages, ack/resolve, adapter catalog in product | **Yes** (with edition/deploy caveats) |
| Simulated notify without vendor tokens | **Yes** |
| “Fully autonomous production remediation generally available” | **No** |
| “All 20+ connectors shipped with parity” | **No** |
| Remediation-as-code / governed PR as direction + partial factory path | **Yes as wedge**, not as unattended guarantee |

### Recommended deck copy (from revision brief)

**Title:** AI SRE / Autonomous Oncall  

> Connect telemetry and a repository; BOSS investigates the alert, builds a confidence-backed root-cause case, proposes a tested governed remediation, and verifies recovery — with budgets, approvals, and receipts.

**Scorecard (buyer outcomes):**

| Buyer outcome | Measure | Evidence BOSS preserves |
|---|---|---|
| Faster triage | alert → hypothesis time | bounded telemetry / RCA evidence |
| Safer remediation | hypothesis → reviewed PR time | gates, tests, decisions |
| Lower recurrence | verified recovery / reopen rate | post-fix verification history |
| Controlled autonomy | actions stopped / escalated / approved | approvals, reservations, receipts |

---

## Child issue roll-up (BOSS-295)

| Child | Topic | Honest state this cycle |
|---|---|---|
| #296 | Community Mission Control | Open product decision (ship lite vs reword) |
| #297 | Local models | Open / partial |
| #298 | Skill SDK | Prefer MCP positioning |
| #299 | Role packs in OSS | Open carve vs reword |
| #300 | Licensing | **Todo BLOCKED human legal** |
| #301 | Zero-Postgres quickstart | Verify or drop claim |
| #302 | Hosted Cloud | **Roadmap Phase-03 — not active** |
| #303 | Margin-aware managed models | **Roadmap Phase-03 — not active** |
| #304 | Deck AI SRE + lead surface | **Reconciled in this doc + revision brief** |
| #288–290 | YouTube | Script packages delivered |
| #291–293 | Engine extract / partners / public | Checklist authored; public flip blocked on #300 |

**BOSS-295 Done criteria for this cycle:** reconciliation document exists, gaps classified, AI SRE wedge grounded in shipped `/v1/oncall-sre`, roadmap items explicitly deferred. Editable slides still require a human to apply the revision brief.

**BOSS-304 Done criteria for this cycle:** product-side reconciliation + revision brief ready to apply; actual Google Slides edit is design-source work outside the repo (brief already covers slide 03/05/06/09/10).

---

## Sequencing (aligned to deck slide 10)

1. **Now:** Keep Community/GA technical proof (Phase A shared-tag + qa-live). Do not oversell Cloud.
2. **Licensing (human):** BOSS-300 — unblocks public engine flip and honest “OSS” launch language.
3. **Pre-announce technical:** G2/G6 if still claimed; private engine extract (BOSS-291) using [`ENGINE_EXTRACT_CHECKLIST.md`](ENGINE_EXTRACT_CHECKLIST.md).
4. **Launch content:** EP1–3 scripts + blogs + landing; deck updated with AI SRE wedge.
5. **Post-launch:** design partners (292) → public flip (293) → Phase-03 Cloud epic when product prioritizes it.

---

## Evidence for Plane

| Artifact | Path |
|---|---|
| This reconciliation | `docs/product/GTM_PRODUCT_GAP_RECONCILIATION.md` |
| Prior gap analysis | `docs/marketing/GTM_GAP_ANALYSIS.md` |
| Deck revision brief | `docs/marketing/GTM_DECK_REVISION_BRIEF.md` |
| Oncall SRE product surface | `docs/product/ONCALL_SRE.md` + OpenAPI `/v1/oncall-sre/*` |
| Engine extract checklist | `docs/product/ENGINE_EXTRACT_CHECKLIST.md` |
