# Engine extract checklist (private repo → design partners → public flip)

| Field | Value |
|---|---|
| **Plane** | BOSS-291 (extract) · BOSS-292 (design partners) · BOSS-293 (public flip) |
| **Spec** | [`docs/planning/oss-readiness/14-engine-repo-extraction.md`](../planning/oss-readiness/14-engine-repo-extraction.md) |
| **Licensing gate** | BOSS-300 / [`DECISION-oss-licensing-strategy.md`](../planning/oss-readiness/DECISION-oss-licensing-strategy.md) — **human legal decision required before public flip** |
| **Status** | Actionable checklist (execution of extract/public flip is not claimed complete) |
| **Date** | 2026-08-12 |

## Goal

Lift `engine/` out of the BOSS monorepo into a dedicated **`boss-engine`** repository:

1. **Private first** (history preserved, secret-scanned).
2. Harden with **design partners**.
3. **Public flip** only after licensing + hygiene gates.

The monorepo remains deployable throughout. The engine must keep running with **zero paid-plane hard dependencies** (noop budget/entitlement ports; local persistence paths).

---

## Phase A — Private extract · BOSS-291

### A0. Preconditions

| # | Item | Owner | Done when | Status |
|---|---|---|---|---|
| A0.1 | `engine/` builds standalone Docker image | Eng | `docker build -f engine/Dockerfile -t boss-engine:local .` succeeds on clean machine | [ ] |
| A0.2 | `engine/scripts/verify_engine.py` green on monorepo HEAD | Eng | Script exit 0 in CI or local | [ ] |
| A0.3 | Engine unit tests green | Eng | pytest under `engine/tests` (pin expected count in CI) | [ ] |
| A0.4 | No committed secrets under `engine/` (pre-extract scan) | Sec | gitleaks/trufflehog clean on monorepo `engine/` path | [ ] |
| A0.5 | Org / account for private repo chosen (work account; not personal) | Product | Repo empty + permissions listed | [ ] |
| A0.6 | Consume strategy chosen: **(a)** pip from git tag **(b)** submodule **(c)** temporary sync | Architect | Written decision in extract PR description | [ ] |

### A1. History-preserving extraction

| # | Item | Commands / notes | Done when | Status |
|---|---|---|---|---|
| A1.1 | Mirror clone monorepo | `git clone --mirror <monorepo-url> boss-mirror.git` | Mirror exists | [ ] |
| A1.2 | filter-repo extract | `git filter-repo --path engine/ --path-rename engine/:` (preferred) or `git subtree split -P engine` | Tree root = former `engine/` | [ ] |
| A1.3 | Tree parity | `diff -rq` extracted tree vs monorepo `engine/` at same SHA | No unexpected deltas | [ ] |
| A1.4 | Full-history secret scan on extract | gitleaks + trufflehog on extracted repo | Zero unresolved hits | [ ] |
| A1.5 | Rewrite if hits | Only before first shared push | Scan clean | [ ] |

### A2. Private repo bootstrap

| # | Item | Done when | Status |
|---|---|---|---|
| A2.1 | Push private `boss-engine` | Remote exists; default branch `main` | [ ] |
| A2.2 | Branch protection on `main` | Required PR; no force-push; CODEOWNERS | [ ] |
| A2.3 | LICENSE / NOTICE match chosen interim license (engine currently Apache-2.0 files) | Files present; no contradictory headers | [ ] |
| A2.4 | README quickstart (Docker + Postgres path) | Clean-machine install steps tested | [ ] |
| A2.5 | `SECURITY.md`, issue/PR templates (private) | Files present | [ ] |

### A3. CI on private repo

| # | Item | Done when | Status |
|---|---|---|---|
| A3.1 | Lint + type/format if configured | Green on PR | [ ] |
| A3.2 | Engine test suite | Green on PR | [ ] |
| A3.3 | `scripts/verify_engine.py` | Green on PR | [ ] |
| A3.4 | Docker image build | Green on PR | [ ] |
| A3.5 | `pip install -e .` smoke on Python 3.11 and 3.12 | Green matrix | [ ] |

### A4. Release process (private registry ok)

| # | Item | Done when | Status |
|---|---|---|---|
| A4.1 | Version source of truth = `pyproject.toml` | Documented | [ ] |
| A4.2 | Tag → build → push image | Image pullable (e.g. goldeneye registry or GHCR private) | [ ] |
| A4.3 | Changelog / RELEASING.md | Process written | [ ] |

### A5. Monorepo switchover

| # | Item | Done when | Status |
|---|---|---|---|
| A5.1 | Monorepo consumes released package (or submodule pin) | PR opened | [ ] |
| A5.2 | Remove or fence in-tree `engine/` per chosen strategy | No double-source confusion | [ ] |
| A5.3 | `verify_engine.py --with-api <route-baseline> --live` green against monorepo integration | Exit 0 | [ ] |
| A5.4 | One goldeneye (or staging) deploy proves parity | Deploy notes + health | [ ] |
| A5.5 | **Exit BOSS-291** | A0–A5 complete; private repo is SoT for engine code | [ ] |

**Invariants during A:** monorepo `main` stays deployable; treat private repo as public-from-day-one (no secrets, no customer names, no internal-only hostnames in commits).

---

## Phase B — Design partners (still private) · BOSS-292

| # | Item | Done when | Status |
|---|---|---|---|
| B1 | Onboarding pack linked from private README | Docker + Postgres + MCP setup | [ ] |
| B2 | Sample project contract (non-production) documented | Acceptance criteria clear | [ ] |
| B3 | Support channel + redacted feedback template | Partners know how to file friction | [ ] |
| B4 | ≥2 external people self-serve README → completed sample code task | Written evidence (redacted) | [ ] |
| B5 | Zero open P0s for 2 consecutive weeks | Issue board snapshot | [ ] |
| B6 | Friction backlog triaged into engine issues | Each item has owner or Won’t-fix | [ ] |
| B7 | **Exit BOSS-292** | B4 + B5 met | [ ] |

Reference pack in monorepo (until extract lands): `engine/DESIGN_PARTNER_ONBOARDING.md`, `engine/COMMUNITY_READINESS.md`.

---

## Phase C — Public flip · BOSS-293

| # | Item | Blocker | Done when | Status |
|---|---|---|---|---|
| C1 | **Licensing decision executed (BOSS-300)** | Human legal/product | Decision doc status = DECIDED; NOTICE/headers consistent | [ ] |
| C2 | Second full-history secret scan | — | Clean | [ ] |
| C3 | Dependency + license audit | No GPL-incompatible surprises for chosen license | Report attached | [ ] |
| C4 | Community hygiene | CONTRIBUTING, CoC, SECURITY, templates, versioning | Files complete | [ ] |
| C5 | Feature fence documented | OSS engine vs paid control plane sharp | Doc linked from README | [ ] |
| C6 | Launch gaps consciously handled | Local models / zero-Postgres / role pack / skill SDK / community MC: **ship or reword** | GTM reconciliation signed | [ ] |
| C7 | Sync with launch content | Blogs, videos EP1–3 scripts, landing | Content checklist green | [ ] |
| C8 | Flip visibility to public | Org settings | Repo public; release tag cut | [ ] |
| C9 | **Exit BOSS-293** | C1–C8 | [ ] |

### Public flip is blocked without C1

Do **not** mark BOSS-293 Done until BOSS-300 is decided. Engine Apache-2.0 files de-facto bias options A/C; that is **not** a substitute for the human decision.

---

## Command cheat-sheet (extract day)

```bash
# 1) Mirror + extract (run on a disposable machine)
git clone --mirror git@github.com:ORG/boss.git boss-mirror.git
cd boss-mirror.git
# requires git-filter-repo
git filter-repo --path engine/ --path-rename engine/:

# 2) Secret scan before first push
gitleaks detect --source . --verbose
# trufflehog filesystem . --fail

# 3) Push private
git remote add origin git@github.com:ORG/boss-engine.git
git push -u origin main

# 4) Verify package
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
python scripts/verify_engine.py
docker build -t boss-engine:extract .
```

---

## Evidence map (for Plane comments)

| Phase | Artifact | Plane |
|---|---|---|
| Checklist authored | This file | BOSS-291/292/293 |
| Private extract complete | Repo URL + CI green + monorepo pin PR | BOSS-291 Done |
| Partners self-serve | Redacted completion notes | BOSS-292 Done |
| Public flip | License decision + public URL + scan reports | BOSS-293 Done |

## Non-claims (honesty)

- Authoring this checklist **does not** complete the extract.
- Design-partner onboarding docs in monorepo **do not** replace a live private repo.
- Public flip remains **blocked** on BOSS-300 regardless of technical readiness.
