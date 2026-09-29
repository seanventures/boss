# Community GA gate gap

**Audited SHA:** `371a1c72a1fd49f3336f5790b55407d51886412a`  
**Branch:** `ai/grok/ga-release-gates` (created from that SHA; no later commits except this report)  
**Date:** 2026-09-29  
**Verdict:** not ready to tag, deploy, or flip public. BOSS-293 is not done.  
This audit did not update Plane, did not close GitHub PRs, and did not choose a GA version.

Contained in source is not deployed and is not live-verified. Nothing below fixes the direct API pool-pressure 500 or the isolated overview 502.

## Incident PRs (#284, #286, #287, #288)

GitHub still shows all four PRs **OPEN** against `ai/codex/boss-293-community-integrate`, with `mergeCommit: null`. Their head SHAs are not ancestors of `371a1c72`. The same patches are already on the integration history (stable `git patch-id` match) and are present in this tree. No test suite was re-run for this audit. No Goldeneye deploy was performed.

| PR | Behavior asked about | At `371a1c72` | Integration commits (patch-identical to the open PR) | Not claimed |
|---|---|---|---|---|
| #284 | Bounded web deployment lookup fan-out | **Contained in source** | `34053b42`, `6e60675f`, `4160a112`, `88a61821`. All nine PR files are byte-identical to open PR head `1ae20cf4`. No later commit touches them. | Not deployed. Not live-verified. |
| #286 | Delivery-workflow-plan 500 (custom input objects in the plan) | **Contained in source** | `e28904a4`, patch-identical to open PR head `2c4759c5`. `engine/boss_engine/services/workflow_explain.py`, `docs/product/customer-workflows-design.md`, and `apps/api/tests/test_a_workflow_says_what_it_will_do.py` match that head. No later commit touches them. | Not deployed. Not live-verified. Does not prove the pool-pressure 500. |
| #287 | API log retention across container recreation | **Contained in source** | `edcaf56f`, patch-identical to open PR head `18927d85`. `deploy/goldeneye.compose.yaml` and `docs/operations/GOLDENEYE_DEPLOYMENT.md` match that head. | **Not deployed.** Not live-verified. The handoff already recorded that this patch was not on the live API. |
| #288 | Direct API admission before the sync threadpool | **Contained in source** | `fd1c46e6` and `db6069de`, patch-identical to open PR commits `9237e3fa` and `43dbf230`. `apps/api/tests/test_code_task_deployment_admission.py` and `docs/operations/BOSS_452_DEPLOYMENT_READ_ADMISSION.md` match PR head `43dbf230`. `apps/api/app/main.py` still defines `_admit_code_task_deployment_read` with a 4-slot non-blocking semaphore. Later `eabf884b` (proxy-root auth) edits `main.py` and does not change those admission lines. | Not deployed. Not live-verified. **Does not prove the direct API pool-pressure 500 is fixed.** |

**Isolated overview 502:** not declared fixed. No source patch in this set is evidence that it is gone, and it was not checked live.

## Version inventory (not bumped, not tagged)

No GA version was chosen. `git tag --points-at HEAD` is empty. These are the strings in this tree:

| Surface | Path | Value |
|---|---|---|
| Root package | `package.json` | `"version": "0.14.0"` |
| Engine metadata | `engine/pyproject.toml` | `version = "0.14.0"` |
| Engine runtime | `engine/boss_engine/__init__.py` | `__version__ = "0.14.0"` |
| API client | `packages/api-client/package.json` | `"version": "0.14.0"` |
| create-boss | `packages/create-boss/package.json` | `"version": "0.1.0"` |

Related, not changed, and not a release decision: `apps/web/package.json` and `api/openapi.yaml` `info.version` are also `0.14.0`. `install.sh` has `BOSS_INSTALL_VERSION="1.1.0"` (installer script revision, not the product version). `packages/goldeneye/pyproject.toml` is `0.2.0`.

## PF-1 through PF-9

Checklist verdict in `docs/operations/PUBLIC_FLIP_CHECKLIST.md` remains **not ready**. Every gate below is still open or not started. In-tree files are historical or mechanical inputs. They are not a pass for SHA `371a1c72`.

Outside-tree artifacts under the operator visualization directory exist for older source `0db60f6c` (qualification report, history-scan summary, image inventory, conditional MCP security review). They are not in this commit and do not qualify this SHA. Paths are not repeated here.

| Gate | Checklist state | Evidence in this tree | Still missing |
|---|---|---|---|
| PF-1 License / BOSS-300 | **Open** | `docs/operations/OSS_LICENSING_DECISION.md` (first-party Apache-2.0 **DECIDED**, counsel reference `01049940` only). Root `LICENSE`, `engine/LICENSE`, `packages/api-client/LICENSE`, `packages/create-boss/LICENSE`. `docs/operations/COMMUNITY_DEPENDENCY_LICENSE_REVIEW.md` records older image inventories through `0db60f6c` and says PF-1 and PF-4 stay open. | **Image license counsel** for Claude Code redistribution, GPL-enabled ffmpeg and source duties, Linux `psycopg-binary`, sharp/libvips, CC-BY (`caniuse-lite`, `spdx-exceptions`), Playwright Chromium, Node/Codex/Grok notices, and optional Redis/Infisical if shipped. Final-image inventory of **this** SHA. |
| PF-2 Community/paid boundary | **Open** for final source | `docs/operations/COMMUNITY_REPO_EXPORT.md` (last qualified export is `0db60f6c`, 1,880 files; later source not requalified). `scripts/community_export_manifest.json` says it is a candidate and not publication approval. | Exact export and **clean VM of this SHA**. Runtime acceptance of API, worker, web, TUI, MCP, and installer on that export. |
| PF-3 Reachable-history secrets | **Open** | No scan report and no rotation receipt in the tree. The checklist only points at an outside-tree scan of unpublished one-root commit `af200616` from source `0db60f6c`. | **Secret rotation receipts** for the four private-history credential fields. A fresh gitleaks-plus-second-scanner pass on the history that would actually be published from this SHA. Clean final scan after rotation. |
| PF-4 Security and dependencies | **Open** | `SECURITY.md` (file exists; see mechanical check). `docs/operations/COMMUNITY_DEPENDENCY_LICENSE_REVIEW.md`. `docs/operations/COMMUNITY_GA_CLAIMS_AUDIT.md` is a copy audit and says it does not qualify the runtime or the final export. | **Image license counsel** and exact-image notice disposition for this SHA. Live TLS/CSRF proof. A must-fix security review of this SHA, not the conditional older review. |
| PF-5 Fresh VM install | **Open** for final source and public delivery | `docs/operations/VM_QUALIFICATION_2026-09-24.md` is an older ARM64 local-bundle smoke. It says x86 was untested in that run and that it is not a public-release qualification. `COMMUNITY_REPO_EXPORT.md` describes the later `0db60f6c` x86 VM, whose report is outside the tree. | **Clean VM of this SHA.** **Public installer URL** proof. The `0db60f6c` run used local-file transport, not public HTTPS. |
| PF-6 Live Community QA | **Open** | `docs/operations/QA_LIVE_14DAY.md`: 47 consecutive daily PASS artifacts through 2026-09-28, required 10/10. The same file says PF-6 stays open. Reports do not record a QA source SHA. The latest pass is tied to image tag `main-7b3ce2f3-202609280458`, not `371a1c72`. Working-software proof cited there is 4/4 on 2026-08-14 in `docs/operations/GA_LIVING_PLAN.md`. | **Live `qa-live` on this release commit**, current working-software proof, and Ops/QA signoff. No backfill. |
| PF-7 Claims and partners | **Open** | `docs/operations/COMMUNITY_GA_CLAIMS_FREEZE.md` (allowed/forbidden list, dated 2026-08-12). `docs/operations/COMMUNITY_GA_RELEASE_NOTES_DRAFT.md` (internal draft; last qualified source named as `0db60f6c`). `docs/operations/COMMUNITY_GA_CLAIMS_AUDIT.md`. | **Claims freeze signoff.** Partner notice or an explicit zero-partner owner signoff. Release-owner review of copy against evidence for this SHA. |
| PF-8 Release controls | **Open** | `docs/operations/COMMUNITY_TARGET_CONTROLS_1A8102.md` (status PF-8 open on 2026-09-28 for reviewed source `1a8102a2`, not this SHA). Target recorded private and empty; Actions disabled; branch protection/rulesets rejected on the private plan; secret scanning disabled; no rollback SHA. `install.sh` defaults `BOSS_REPO_URL` to `https://github.com/seanventures/boss.git` and `BOSS_REPO_REF` to `main`. | **Release-owner approval.** Reviewed export SHA and checksum for this head. Rollback target. **Public installer URL** pinned to the reviewed commit and verified. Eligible target controls, or an approved revised publication sequence. This audit did not recheck GitHub. |
| PF-9 Public publication | **Not started** | No publication record in the tree. The checklist row is the only in-tree statement, and it says not started. | All of PF-1 through PF-8, then visibility change, public URL, tag, published notes, and first-hour monitoring on BOSS-293. |

### Gates with in-tree evidence vs gates with none

- **Some in-tree evidence, none of it a pass for this SHA:** PF-1, PF-2, PF-4, PF-5, PF-6, PF-7, PF-8.
- **No in-tree evidence file:** PF-3 (older scan lives outside the repo), PF-9.

### Mechanical subset only

`python3 scripts/check_public_flip.py` (the body of `make public-flip-check`) was run in this worktree. It is local and read-only.

- Result: `MECHANICAL_PASS`, exit 0.
- Checks that passed: license decision header DECIDED, `SECURITY.md` present, private reporting channel configured, claims-freeze doc present, checklist present, no committed `.env` / credential-basename paths.
- The script's own note: a pass does not authorize publication. It does not inspect history, export coverage, runtime, license notices in images, partner status, or live QA. It does not close BOSS-293 or BOSS-300.

## Claim text

`docs/operations/COMMUNITY_GA_CLAIMS_FREEZE.md` and `docs/operations/COMMUNITY_GA_RELEASE_NOTES_DRAFT.md` were read against the checklist. They were **not edited**.

- The claims freeze is an allowed/forbidden list dated 2026-08-12. It does not say PF-1 through PF-9 passed or that a release owner signed off. Its `qa-live` "10/10 PASS" row is the same historical control-plane result that `QA_LIVE_14DAY.md` refuses to treat as PF-6 closure.
- The release notes are labeled an internal draft. They name last qualified source `0db60f6c`, say the active integration still needs a new export, scans, and VM lifecycle, and say final-image terms, live release-commit QA, target controls, and owner approval remain gates. That matches the checklist. It does not claim this SHA passed a gate.

No signoff was invented.

## Not done

No deploy, no image push, no public flip, no git tag, no version bump, no push, no write to `seanventures/boss`, no secret values recorded, no Plane issue closed, no GitHub PR closed, no BOSS-293 Done stamp.
