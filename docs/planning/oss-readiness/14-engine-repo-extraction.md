# 14 — Engine Repo Extraction (private → public)

**Status:** PLANNED · **Plane:** GTM & Marketing module, issues #291 (extract), #292 (design partners), #293 (public flip) · **Date:** 2026-07-28
**Prereqs shipped:** engine split merged (v0.8.0), `boss-engine` 0.9.1 standalone package + Docker image, Apache-2.0 LICENSE at repo/engine/client levels, ports+defaults seams, MCP server.
**Open blocker:** licensing strategy decision (`DECISION-oss-licensing-strategy.md`, Plane #300) must land before the *public* flip — extraction to a **private** repo can proceed now.

## Goal

Phase 4 of the OSS split (spec 01): lift `engine/` out of the monorepo into its own repo — **private first**, hardened with design partners, then flipped public. The developer-facing interface is **the BOSS MCP server** (plus the `python -m boss_engine` CLI); the console stays commercial.

## Target shape

- New repo: `boss-engine` (org TBD with licensing decision; work account `paramg-kaapppu` per identity policy — do NOT use the personal account for org repos).
- Contents: `engine/` subtree promoted to repo root (`boss_engine/`, `pyproject.toml`, `Dockerfile`, `LICENSE`, `README.md`, `scripts/verify_engine.py`, tests).
- History: **preserved** via `git filter-repo --path engine/ --path-rename engine/:` on a fresh clone (fallback: `git subtree split -P engine`). Run the full-history **secret scan (gitleaks + trufflehog) on the extracted history before the first push** — cheaper to fix pre-push than to rewrite a shared repo later.
- Monorepo keeps consuming the engine. Decision (recommended → a): 
  a. **pip dependency** on the private repo (`boss-engine @ git+ssh://…@vX.Y.Z`) — cleanest fence, forces the package boundary to stay honest; 
  b. git submodule at `engine/` — minimal churn but submodule ergonomics; 
  c. temporary two-way sync (copybara-style) — only if hotfix velocity demands it during transition.

## Work plan

### Phase A — Extract (private) · Plane #291
1. `git filter-repo` extraction on a mirror clone; verify file tree == current `engine/` at HEAD.
2. Secret scan full extracted history; scrub if hits (extraction is the one cheap chance to rewrite).
3. Push private repo; branch protection on `main`; CODEOWNERS.
4. CI (GitHub Actions): lint, engine test suite (48 tests as of agent-merge branch; 17 on main baseline — pin whichever `main` carries), `scripts/verify_engine.py`, Docker image build, `pip install -e .` smoke on 3.11/3.12.
5. Release process: tag → build → push image to registry (goldeneye :5000 for now; GHCR when public), publish version per `pyproject.toml`.
6. Monorepo switchover PR: remove `engine/` (or pin submodule), consume released package; `verify_engine.py --with-api <route-baseline> --live` green; deploy to goldeneye once to prove prod parity.

### Phase B — Harden with design partners (still private) · Plane #292
7. Onboarding pack: quickstart README (Docker + Postgres path verified in Plane #294), MCP setup for Claude Code/Cursor/Copilot, sample project (`templates/boss-sandbox` derivative), support channel.
8. Invite design partners/developers with read access; every friction point becomes a repo issue.
9. Exit criteria: ≥2 external people self-serve from README to a completed sample code task without help; zero P0s open for 2 weeks.

### Phase C — Public flip · Plane #293
10. Licensing decision executed (#300); NOTICE/headers consistent.
11. Second secret scan + dependency/license audit (no GPL-incompatible pins).
12. Community hygiene: CONTRIBUTING, CoC, issue/PR templates, SECURITY.md, versioning policy.
13. Gaps that shape the first impression, from the GTM deck (see `docs/marketing/GTM_GAP_ANALYSIS.md`): local-model support (#297), zero-Postgres quickstart proven (#301), community role pack (#299), skill-SDK story (#298), community Mission Control decision (#296).
14. Flip to public in sync with launch content (blogs #285–287, videos #288–290, landing page #305).

## Invariants

- `main` of the monorepo stays deployable throughout (same rule as the original split).
- No commit lands in the private repo that would need rewriting for the public flip (treat it as public from day one: no secrets, no customer names, no internal URLs — goldeneye/tailnet hostnames included).
- Engine must keep running with **zero paid-plane dependencies** (Noop budget/entitlement ports, SQLite/in-memory persistence) — the OSS story dies if a control-plane import sneaks back in; `verify_engine.py` in CI is the guard.
