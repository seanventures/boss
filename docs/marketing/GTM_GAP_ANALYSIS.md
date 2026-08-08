# GTM Deck vs Product — Gap Analysis

**Date:** 2026-07-28 · **Deck:** [BOSS GTM Review (10 slides)](https://docs.google.com/presentation/d/11Sm8v0J8Yw_-zDXD1fgs7pQFQLswn16bAOvlfkqxk3Q) · **Product baseline:** repo `main` @ v0.9.1 (engine `boss-engine` 0.9.1, Apache-2.0)
**Plane:** module *GTM & Marketing*, tracking issue #295, gap issues #296–#305.

## Verdict

The deck's Phase 01 ("prove the core loop") is **done and demoable** (demo runbook, governed coding pipeline, receipts). Phase 02 ("open-source the engine") is **~80% built technically** — the engine is split, packaged, licensed and MCP-served — but the *community-tier promise* in the deck oversells what the OSS artifact contains today, and two launch blockers are decisions, not code.

## What the deck promises that is already built

| Deck item | Where it lives |
|---|---|
| Control plane for AI labor: roles, tasks/decisions, approvals, receipts, routing, visibility (slides 1–3) | PRD-v2.0 planes; 5 operating invariants in README; receipts/budget/lease kernel |
| Software Factory: intent → spec → tasks → code → tests → review bundles → releases (slide 3) | `GOVERNED_CODING_PIPELINE.md`; VAG gate family; demo runbook end-to-end |
| Agent Control Plane: govern workers, models, skills, budgets, approvals, receipts | RBAC kernel, Budget Governor, Model Entitlement Broker, policy packs |
| Mission Control UI (slide 3, surface 04) | `boss-web` console — **enterprise side only** (see gap G1) |
| OSS engine + commercial plane path (slides 4B, 6) | `engine/boss_engine` split shipped v0.8.0; ports+defaults seams; standalone Dockerfile; MCP server (15 tools); CLI |
| BYO keys / multi-provider (slide 7 mode 02) | OpenAI/Anthropic/xAI/Google keys, routing + receipts |
| Phase 01 demo script (slide 10) | `BOSS_Demo_Runbook.md` — matches the deck demo line item-for-item |
| Deploy with evidence (wedge 02, QA/Release plane) | Tier-1/2/3 previews + smoke canary, fail-closed gates, auto-rollback rollouts |

## Critical gaps (deck says it, repo doesn't have it)

**G1 — "Simple Mission Control" in the free Community tier** (slides 6, 8 · Plane #296 · HIGH)
The deck's community tier explicitly lists "Simple Mission Control"; the OSS engine today is CLI + MCP only — the console sits on the paid side of the split. This is the single largest divergence. Options: minimal read-only mission-control page shipped with the engine, a console-lite build, or edit the tier copy. Decide before launch copy is written.

**G2 — Local/open model support: Ollama / vLLM / private endpoints** (slide 7 modes 01+03, slide 6 "Local model support", slide 2 signal 4 · #297 · HIGH)
Deck calls single-provider lock-in a non-starter and makes local models the community wedge. The model layer today is cloud-API keys; no OpenAI-compatible base-URL override, no Ollama/vLLM adapter documented. Table stakes for the OSS launch audience.

**G3 — "Basic skill SDK"** (slide 6 · #298 · MEDIUM)
SkillSpec registry exists but has no wired executor (oss-readiness triage finding; spec 03). Either wire a minimal executor + authoring docs, or market **MCP as the extension SDK** (which genuinely works today) and drop "skill SDK" from tier copy.

**G4 — "Basic role specs / role packs" in the OSS artifact** (slides 6, 8 · #299 · MEDIUM)
Role packs (`core_v1`, `metatron_operator_v1`, `codex_software_engineer_v1`) live in the monorepo specpack, not in the engine distribution. Carve a community role pack into the engine so the free-tier promise holds and "premium role & skill packs" (Cloud Plus) has a real fence.

**G5 — Licensing decision unresolved = public-flip blocker** (slide 4B risk: "the OSS/monetization split must be sharp" · #300 · URGENT)
`DECISION-oss-licensing-strategy.md` is still open (A: engine-only Apache · B: whole community control plane AGPL · C: engine Apache + thin AGPL community plane) while PRD-v2.0 still documents open-coring the whole control plane. Engine already carries Apache-2.0 files, which de-facto picks A/C. This is a product+legal decision, not code — and everything public-facing waits on it.

**G6 — Zero-Postgres quickstart unverified** (slide 6 "local/self-host mode" · #301 · MEDIUM)
The OSS-split acceptance test ("standalone script + LLM key + sample repo → diff, zero Postgres") is spec'd, ports/defaults are marked done, but nobody has proven it on a clean machine. It defines the 5-minute onboarding and YouTube Ep1. (Local Docker verification run started 2026-07-28 — see Plane #294.)

## Roadmap gaps (deck phases 03/04 — expected, not launch blockers)

- **BOSS Cloud (Tier 02, Phase 03)** — hosted control plane, hosted Mission Control, team/workspace mgmt, seat billing, local-node registration: none of this multi-tenant SaaS exists (goldeneye is single-tenant internal). #302.
- **Turnkey managed models (Mode 04 / Cloud Plus)** — deck itself flags "needs plan limits + margin-aware routing"; neither exists. #303.

## Deck-side fixes (product is ahead of the deck)

- **AI SRE / Autonomous Oncall wedge is missing from the deck.** Since 2026-07-21 it is the repo's flagship enterprise wedge (spec 12, RB-1..RB-20, Cleric-benchmarked, remediation-as-code differentiator) — yet slide 5's wedges and slide 9's scorecard don't include it. The exact revision copy is in [`GTM_DECK_REVISION_BRIEF.md`](./GTM_DECK_REVISION_BRIEF.md). #304.
- **"Pick one surface" is still unpicked** (slide 3's own warning). The launch recommendation is **path B (OSS engine + commercial plane)**, **AI SRE** as the enterprise wedge, "Company OS" as the vision umbrella; the application copy is in the [deck revision brief](./GTM_DECK_REVISION_BRIEF.md). #304.
- **No launch assets exist** — no landing page, no one-pager anywhere in the repo. Reusable evidence: value-flow diagrams (`docs/product/diagrams/`), VAG experiment HTML reports, demo-runbook pitch copy. #305.

## Sequencing recommendation (maps to deck slide 10)

1. **Now:** G5 licensing decision → unblocks everything public.
2. **Pre-announce:** G2 local models + G6 zero-Postgres quickstart + engine repo extraction (Plane #291) + local Docker verification (#294).
3. **Launch window:** blog series (#285–287) + videos (#288–290) + landing page (#305), with tier copy adjusted for G1/G3/G4 (ship or reword).
4. **Post-launch:** design partners on private repo (#292) → public flip via checklist (#293) → BOSS Cloud epic (#302).
