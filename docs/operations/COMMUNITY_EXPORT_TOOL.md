# Community export candidate tool — BOSS-293

This is the mechanical companion to [the full Community export boundary](COMMUNITY_REPO_EXPORT.md) in PR #224. It selects tracked blobs from one fixed private commit into a **new, history-free local tree**. It does not push, publish, rewrite source history, grant a license, or approve the BOSS-293 public flip. `seanventures/boss` remains private and empty.

## Run

Use a fresh output directory **outside** the source checkout. Never point the command at the public staging repository.

```sh
python3 scripts/community_export.py \
  --source-ref origin/main \
  --output /tmp/boss-community-candidate-1 \
  --report /tmp/boss-community-candidate-1.json \
  --preview
```

The default mode refuses to write a candidate when it finds blockers. `--preview` writes a quarantined candidate for local inspection and gitleaks scanning, but still exits nonzero. The report contains source commit, counts, missing runtime/release files, static Python/TypeScript import edges, Dockerfile/Compose edges, excluded private references, and redacted gitleaks metadata. It contains no secret values. The candidate has no `.git` directory and includes `EXPORT_CANDIDATE_BLOCKED.txt`. Delete it after review.

The inclusion and exclusion rules are in [`scripts/community_export_manifest.json`](../../scripts/community_export_manifest.json). They select API, worker, web, engine, Goldeneye Community kit and control plane, Community specpack, CLI/MCP, installer, and supporting tests/docs. Known hosted Account Connect, Cloud/Enterprise service, and commercial pack paths are excluded. Internal UI audit screenshots under `apps/web/audits/` are excluded; new media and metadata need review before inclusion. Files that mix Community and paid code are withheld and listed as split blockers. Any file containing known private source/host or paid symbols is also withheld. These byte markers are a conservative guard, **not** a complete classification or a replacement for manual source review. A new, unfamiliar proprietary name could evade them. The report therefore always says `ready_for_publication: false`.

## 2026-09-24 baseline result

The fixed source commit `9a72bd9a02f14ca5ff71b44b5fe5af72ff19ccce` generated 1,541 candidate files and failed with 479 blockers. The candidate had zero files containing the tool's known private/paid markers. All 842 selected Python files parsed. The included Community Compose YAML passed `docker compose config --no-interpolate --quiet`; a build remains blocked because its referenced Dockerfile is withheld. The main failures are structural:

- API `apps/api/app/main.py` and web `apps/web/app/page.tsx` / `actions.ts` interleave Community and paid paths. Removing paid services leaves hundreds of static import edges into withheld files.
- The shared `Dockerfile` copies all of `apps/` and `packages/`, including hosted Account Connect and private packages. The Community installer Compose file builds that Dockerfile. These need Community-only packaging.
- The Community pack still says `license_candidate: AGPLv3` and installs `commercial_packaging_policy`; it needs the recorded BOSS-300 license decision implemented. Root `LICENSE` is withheld until counsel reference and metadata review.
- The current installer points to the private source repository. Public `README.md`/contributor/security documents require a Community-specific review; root `SECURITY.md` and `CODE_OF_CONDUCT.md` are absent from the baseline main commit (feedback PR #221 supplies them).
- Gitleaks found one `generic-api-key` match in `engine/tests/test_a_failed_check_says_why.py` line 81 in the quarantined tree. Treat it as unresolved until Security triages the fixture. No value is copied into this document.

At the baseline above, selected Python files parsed, but API import and image build could not pass while required entrypoints and the Dockerfile were withheld. Do not replace those files with stubs or ship a reduced product to make the check green. Split the mixed modules, preserve self-hosted SSO and ProviderOps, then rerun API/worker/web tests and clean-Linux installer acceptance. PRs #218–#223 and the UI/installer work may change the source; rerun from the merged fixed commit, then perform the independent scanner, dependency/license audit, counsel review, and remaining BOSS-293 gates before any private target push or visibility change.

## Stacked Community container seam

Commit `1d32d4f67827c56f22d67b7c82b1b2d87183251d` adds `Dockerfile.community`. The export manifest now selects it instead of the monorepo `Dockerfile`; the candidate copy of installer Compose changes its three `dockerfile:` references to the Community file. The source Compose and source Dockerfile remain unchanged for private installs. The rewrite requires exactly three matches and blocks if that count drifts. The Community Dockerfile requires generated `EXPORT_CANDIDATE_STATUS.json` with `mechanically_complete: true`, so a source checkout or blocked preview cannot produce a release image. That marker does **not** imply legal or public-release approval.

On that fixed source commit, the quarantined candidate contained 1,544 files and **459 blockers**, down from 479 in the immediate parent commit. Redacted gitleaks stayed clean. Compose syntax validation passed with all three services pointing at `Dockerfile.community`, and Dockerfile syntax validation reported no warnings. A real API build stopped because three required source inputs are still withheld: `api/openapi.yaml` (paid API content), `db/schema.sql` (mixed paid schema), and `docs/product/TRACEABILITY.md` (mixed paid feature ledger). API/web entrypoints and the Community pack remain withheld as well. The 40 TypeScript import edges still need a Community web split. Do not remove these copy requirements merely to make an incomplete image build.

`docs/product/GOLDENEYE_PAAS.md` explicitly assigns both `packages/goldeneye/` and `engine/boss_engine/services/goldeneye_paas.py` to Community. Commit `e31995bb9664e23cdd1c64b043547e393c2a432f` corrects their mistaken paid classification, copies the kit into the Community API/worker image, and preserves only three exact, internal kit symlinks. The Goldeneye install example no longer points at the private source repository. From that commit, the quarantined candidate contains **1,612 files and 459 blockers**: 68 more Community files at the same blocker count. Redacted gitleaks reports zero findings, Compose config passes, and the generated status remains `mechanically_complete: false` / `ready_for_publication: false`. The added Goldeneye service and tests expose three more Python import edges into withheld API/config files; they need the Community API split rather than another exclusion. The full image build remains blocked by the three missing COPY inputs above.

## Stacked Community web seam

The web split separates paid server actions, edition chrome, hosted agent-module mapping, three paid panels, and paid-only assertions from the shared Community modules. On branch `ai/codex/community-web-split` at commit `2f630b82`, the same-base preview contains 1,615 files and 416 blockers, down from 459 before this slice. TypeScript imports into withheld files fell from 40 to zero. The candidate gitleaks scan has zero findings. The private web typecheck and 52 affected tests pass.

The export tool also supports an exact public API client package entrypoint rewrite. It requires an included `packages/api-client/src/community.ts` and the expected private `exports["."]` value before changing the candidate's package entrypoint to `./src/community.ts`. This rewrite is activated only in a combined source that includes the API client split from PR #240; the standalone web branch does not include that source yet. Missing source or a changed package shape blocks the candidate.

In a disposable integration of the web slice with the current API client and installer branches, the strict rewrite applied and produced 1,671 candidate files with 208 blockers and no gitleaks findings. A frozen offline `pnpm install`, exported web typecheck, and exported Next production build passed. **That build has no root `/` route:** `apps/web/app/page.tsx` remains withheld because it interleaves Community and paid UI. It cannot be used as evidence that the Community web product runs. The root page needs a Community composition that retains project and agent workflows, Org Chart, and self-hosted SSO; the exporter already flags its absence as a required runtime blocker.
