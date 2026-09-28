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

The inclusion and exclusion rules are in `scripts/community_export_manifest.json` in the source checkout. They select API, worker, web, engine, Goldeneye Community kit and control plane, Community specpack, CLI/MCP, installer, and supporting tests/docs. Known hosted Account Connect, Cloud/Enterprise service, and commercial pack paths are excluded. Internal UI audit screenshots under `apps/web/audits/` are excluded; new media and metadata need review before inclusion. Files that mix Community and paid code are withheld and listed as split blockers. Any file containing known private source/host or paid symbols is also withheld. These byte markers are a conservative guard, **not** a complete classification or a replacement for manual source review. A new, unfamiliar proprietary name could evade them. The report therefore always says `ready_for_publication: false`.

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

## Stacked Community root page

The reviewed `apps/web/app/page-community.tsx` supplies the full Community console route while the private `page.tsx` keeps its existing paid composition. The exporter remaps the Community file to `page.tsx` only when its SHA-256 matches the pinned manifest value, the private route is withheld, and the Community source passes the normal marker scan. A change to the page requires review and a deliberate hash update. The Community page retains project/work, agent and Studio, team structure, local modules, and self-hosted identity provider/SCIM management. A Community-only identity feed reads the self-hosted endpoints without fetching paid cluster/support data. The shared channel setup now uses the API's configured OAuth callback URI instead of a private host default.

A disposable combined candidate with PR #240 client and current installer branches contained 1,675 files and 205 blockers. Redacted gitleaks found zero matches. Frozen offline `pnpm install`, `pnpm --filter boss-web lint`, and `pnpm --filter boss-web build` passed. The Next build included the `/` route. This proves compilation of the emitted web route, not live API/worker behavior or full Community release qualification. The Community page duplicates substantial markup from the private route; future page changes must be reviewed for parity until the two are factored into shared components.

The tool also supports an exact, checksum-pinned replacement of root `README.md` from `README.community.md` once the latter is present in the combined source from PR #241. The standalone page branch leaves this rule inactive because the source is absent. The combined export must include the public README source, activate the rule, verify its relative links, and retain the independent legal/security release gates.

## Audited shared status fields and Community pack

The exporter now masks only five exact shared schema lines used for optional Cloud link status and two exact entitlement broker lines used for Community Easy Connect readiness. The Python lines must occur once in their expected class or method; a changed, duplicated, or relocated line blocks export. New paid marker occurrences still withhold the file. The Community pack is no longer classified as a required split once its exact `hosted_account_connect_required: false` declaration, Apache-2.0 candidate, `legal_review_required: true`, and absence of the commercial packaging policy are verified. This permits the full Community capability pack after the Apache pack update is integrated.

The entitlement broker still contains `enterprise_model_gateway` behavior tied to the private enterprise gateway adapter. That semantic paid source was not covered by the byte marker list. The exporter explicitly withholds the broker until this paid behavior is moved behind an extension; the two Easy Connect line exceptions cannot make the entire file exportable. None of these mechanical checks grant public-release approval or replace legal review.

The checked-in Community README, contributor guide, and Makefile are mapped to their public root filenames only when their SHA-256 values match the manifest. The Makefile mapping requires the private root file to remain withheld. The exporter also checks local Markdown links against the candidate tree and reports every missing or escaping target. In the combined 2026-09-24 preview, the README, contributor guide, feedback process, and security relative links resolved; the remaining document links exposed withheld spec/OpenAPI files and the pending root LICENSE. Public issue forms are selected by four exact paths and are required release files.

## 2026-09-28 Apache license export

The product owner supplied counsel reference `01049940` approving Apache License 2.0 for the full Community repository. The exporter now includes the existing root `LICENSE` only when its SHA-256 matches the reviewed manifest pin. A changed file is withheld with a `reviewed-license-changed` blocker; an unpinned root license is rejected by manifest validation. The source license text was not rewritten.

Against the prior combined source commit `9891ad69f1367766595ec57d81f74f18a9a3c3de`, the candidate grew from 1,743 to 1,744 files and the three `LICENSE`-related blockers fell to zero. The redacted gitleaks candidate scan found zero matches. This is mechanical export evidence only: the report retains `ready_for_publication: false` while the BOSS-300 decision record, NOTICE/package metadata review, independent scans, clean Linux installer acceptance, and remaining BOSS-293 release gates are completed.
