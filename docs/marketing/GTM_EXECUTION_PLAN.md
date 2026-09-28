# BOSS GTM Execution Plan

**Date:** 2026-07-28 · **Owner:** Param · **System of record:** Plane module **GTM & Marketing** (project `cc97d5cc`, issues #285–#305)
**Companion docs:** [`GTM_GAP_ANALYSIS.md`](./GTM_GAP_ANALYSIS.md) (deck-vs-product gaps) · [`../planning/oss-readiness/14-engine-repo-extraction.md`](../planning/oss-readiness/14-engine-repo-extraction.md) (repo split plan)

This is a **plan of record, not the content itself** — each section is a brief for whoever (human or BOSS agent) executes it.

Positioning line for all assets (GTM deck, slide 10):
> "BOSS is the operating system for AI labor: it turns intent into governed execution — with roles, decisions, approvals, receipts, and evidence."

---

## 1. Blog / article series (Plane #285, #286, #287)

| # | Working title | Audience | Core message | Grounding sources |
|---|---|---|---|---|
| 1 | **BOSS — the Operating System for AI Labor** (pillar) | Founders, eng leaders, HN | Generation is cheap; coordination, validation, governance and trust are the bottleneck. Not vibe coding, not agents — a control plane. | Deck slides 1–3; `docs/product/PRD-v2.0.md` thesis; the 5 operating invariants in `README.md` (receipts, budget reservations, leases, no silent drops, gated self-modification) |
| 2 | **Inside the BOSS engine** (technical deep-dive) | Developers, OSS community | The engine is a real, self-hostable system: `boss_engine` package, EngineContext, 15-tool layer, engine-native agent, MCP server (any MCP client becomes the BOSS UI), CLI, standalone Docker image, Apache-2.0. | `engine/README.md`, `engine/boss_engine/tools/builtin.py`, `engine/Dockerfile`, spec `01-oss-engine-split-and-packaging.md` |
| 3 | **From directive to governed release** (demo walkthrough) | Eng leaders, QA/platform | One-line directive → multi-agent PRD meeting → design gate → governed code task (QA freezes tests, adversarial review, fail-closed gate) → PR gate → preview/canary → receipts. | `BOSS_Demo_Runbook.md`, `docs/product/GOVERNED_CODING_PIPELINE.md`, VAG specs; screenshots from console |

Notes for the writer:
- Publish order 1 → 2 → 3; post 2 lands with the public repo flip, post 3 with the first video.
- Do **not** claim community Mission Control UI, Ollama/vLLM support, or skill SDK until gaps #296/#297/#298 close (see gap analysis).
- Reusable evidence assets: `docs/product/diagrams/01..03`, VAG experiment HTML reports, SRE-wedge comparison table in `docs/planning/12-signal-to-fix-sre-agent.md`.

## 2. YouTube training series (Plane #288, #289, #290)

All episodes use the public OSS engine only; target 12–15 min each; every command must be re-verified on a clean machine first (that is Plane #294's output).

**Ep 1 — Install & run BOSS locally (Docker).** Flow: prereqs (Docker, an LLM API key) → clone → build `engine/Dockerfile` → run postgres:16 → `DATABASE_URL` → `python -m boss_engine tools | status | ask | chat`. Ends with "you have a working BOSS engine." Depends on: #294 verification, zero-Postgres check #301 (if it works, show it as the 2-minute path).

**Ep 2 — Execute a sample project end-to-end.** Flow: seed (`make seed` / `seed-demo`) → sample repo (`templates/boss-sandbox`) → `create_code_task` → phase ribbon (cloning → coding → testing → opening PR) → review packet + merge gate → preview tiers → receipts. Depends on: canonical public sample repo decision (#289 body).

**Ep 3 — BOSS via MCP: Claude/Cursor as your BOSS UI.** Flow: `python -m boss_engine serve-mcp` (stdio) + `--http --port 8791` → `claude mcp add` / Cursor config → live tour of project-scoped tools (`project_status`, `list_running_work`, `create_code_task`, `get_task_preview`, `get_costs`, …) → `read_only=True` for safe demos. Positions MCP as the primary developer interface.

## 3. Engine → separate repo, private then public (Plane #291, #292, #293)

Full plan: `docs/planning/oss-readiness/14-engine-repo-extraction.md`. Summary: `git filter-repo` extraction of `engine/` with history + pre-push secret scan → private `boss-engine` repo with CI (`verify_engine.py` gate, tests, Docker build) → monorepo consumes it as a pinned dependency → design partners onboard on the private repo (exit: 2+ externals self-serve to a completed sample code task) → public flip only after the licensing decision (#300) and readiness checklist (#293). Interface = **BOSS MCP server** + CLI; console stays commercial.

## 4. Local Docker deploy & verification (Plane #294)

Go/no-go gate before anything goes public: build the standalone engine image, run against a fresh Postgres, bootstrap schema, verify `status`/`tools`, MCP `initialize`/`tools/list`/`tools/call` over HTTP, and one live `ask` (chat E2E). Verification run started 2026-07-28; results + the exact new-user command sequence get attached to #294 and become the Ep 1 script skeleton. Known risks to confirm: engine-native schema bootstrap (vs needing `apps.api` bootstrap — would be an OSS gap), zero-Postgres path (#301).

## 5. GTM-deck alignment (Plane #295–#305)

See [`GTM_GAP_ANALYSIS.md`](./GTM_GAP_ANALYSIS.md). Launch-critical: licensing decision **#300 (urgent)**, community Mission Control decision #296, local/open-model support #297, zero-Postgres quickstart #301, landing page/one-pager #305. Deck-side fixes: add the AI SRE oncall wedge + pick the lead surface (#304 — recommendation: lead with OSS engine + commercial plane; AI SRE as enterprise wedge).

## Sequencing

1. **Decisions first:** #300 licensing, #296 community-UI scope, #304 lead surface (apply [`GTM_DECK_REVISION_BRIEF.md`](./GTM_DECK_REVISION_BRIEF.md) to the editable deck).
2. **Foundation:** #294 local verification → #291 private repo extraction → #297/#301 quickstart gaps.
3. **Content:** blogs #285–287 briefs above → landing page #305.
4. **Community:** #292 design partners → videos #288–290 → #293 public flip + launch.
