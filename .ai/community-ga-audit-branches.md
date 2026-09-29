# Community GA branch audit against v1.0-rc1

Audit date: 2026-09-29. Compared every local and `origin` tip to tag `v1.0-rc1` = commit `b194c65c3028b80cd621fc187280f4d79e84df02` (annotated tag object `04bb2e79f84acb1c3d7074f1617f4c6b37aa452d` peels to that commit). Fetch was `git fetch --all --prune`. No worktree was pruned. Nothing was merged, deleted, reset, or pushed.

A tip is outside the tag when `git merge-base --is-ancestor <tip> b194c65c` is false. Ahead is `git rev-list --count b194c65c..<tip>` (different SHAs, not unique behavior). Patch equivalence is `git cherry v1.0-rc1 <tip>`: zero `+` lines means every non-merge commit's patch-id is already in the tag. Those tips are `patch-equivalent` even though the SHAs were replayed and are not ancestors.

`superseded` means the patch-id is absent, but the behavior is not current unique work: either the tip is from before 2026-09-01, or a post-2026-09-01 diff was rewritten into the tag (export pins, MCP matrix counts, docs snapshots, or the same fix under a later patch). `needs port` would be unique behavior from 2026-09-01 onward that is not already in the tag. There is no such tip.

## Counts

| Set | Count |
|---|---:|
| Refs whose tip is outside the tag (local and origin counted separately) | 313 |
| Unique tips outside the tag | 289 |
| Unique tips `patch-equivalent` | 219 |
| Unique tips `superseded` | 70 |
| Unique tips `needs port` | 0 |
| Unique tips `separate product line` | 0 |
| Unique tips `our unmerged audit branch` | 0 |

## Already in the tag

These were reviewed and are ancestors of `b194c65c` (or are the tag). They are not outside, so they are not in the tables below.

- `origin/main` `6407e2f9e8e24a26a3f63a9902a4827661540a18` (2026-09-28, "Every lane's model list follows its own engine, and a stale model is cleared") is an ancestor. The tag is 443 commits ahead of it. Ahead of the tag: 0.
- Local `ai/codex/boss-293-community-integrate` **is** the tag (`b194c65c3028b80cd621fc187280f4d79e84df02`).
- `origin/ai/codex/boss-293-community-integrate` `371a1c72a1fd49f3336f5790b55407d51886412a` (2026-09-29, "docs: hand off Community GA completion to Grok") is an ancestor. The tag is 18 commits ahead and has not been pushed. Those 18 commits are the GA proof merges (`ga-release-gates`, `ga-ui-agents`, `ga-reconcile-ledger`, `ga-installer`, `ga-api-tui-mcp`, `ga-web-suite`, `ga-export-46`, `ga-release-cut`, `ga-db-proof`) plus `release: name the community candidate v1.0-rc1`.
- This session's `ai/grok/ga-audit-branches`, `ai/grok/ga-audit-main`, `ai/grok/ga-audit-paramg`, `ai/grok/ga-audit-prs`, and `ai/grok/ga-audit-unknown` all pointed at the tag when this audit started. `ai/grok/ga-qualify-46` `46c10ca957e8d4f01403c0ff6c6c290be44ae156` is an ancestor (the release commit is a descendant). They do not hold unmerged work. There is no outside tip to mark `our unmerged audit branch`.
- The other 2026-09-29 `ai/grok/ga-*` proof branches listed above are also ancestors.

## `main` (local)

| Ref | Tip | Date | Ahead | Disposition |
|---|---|---|---:|---|
| `heads/main` | `d8069168cc85a88f4551edc8ff4c7361ebc19519` | 2026-08-15 | 346 | `patch-equivalent` |

Local `main` is the 2026-08-15 cut "release: cut v0.14.0", 1715 commits behind the tag. `git cherry` reports 0 patch-unique commits (256 equivalent non-merge commits). It is a replayed SHA of history already in the tag, not a second product line.

## `/Volumes/DataSSD1/test/boss`

Read-only. Branch `ai/grok/boss-101-go-gateway`, HEAD `b49e5b0a4b624bde35190b45c3842e5ab60959e5` (2026-08-15, "fix(s2): keep make test-go on the tenant gateway only"). That SHA is **not** an ancestor of the tag (`rev-list` ahead 274, behind 1715). `git cherry` reports **0** patch-unique commits, including the two Go gateway commits:

- `077c5808c37347db3b99ab70793517f0a0d8a14b` feat(s2): optional Go tenant gateway library (not mounted) — patch-equivalent
- `b49e5b0a4b624bde35190b45c3842e5ab60959e5` fix(s2): keep make test-go on the tenant gateway only — patch-equivalent

So the **commits are in the tag as patches**, not as ancestors. The same branch name exists locally in this repo at that same SHA and is `patch-equivalent` in the table below. `origin` does not have `ai/grok/boss-101-go-gateway`; the sibling `origin/ai/grok/boss-102-go-dispatcher` is an ancestor of the tag.

Uncommitted state (4 modified, 10 untracked paths) is the **separate Go gateway / Go scale effort** (BOSS-97 / 101 / 102), not this Community release:

- Tracked edits are a 2026-08-15 activity-log/status note for the S3 webhook dispatcher, a `test-go` recipe that also runs `services/webhook-dispatcher`, and `docs/operations/GO_SCALE_TRACK.md` calling S2/S3 in-repo scaffolds and not Community GA.
- `services/` (sandbox-service and webhook-dispatcher) matches the tag byte for byte (14 files).
- These untracked files match the tag byte for byte: `engine/boss_engine/contracts/__init__.py`, `engine/boss_engine/contracts/go_job_v1.schema.json`, `engine/boss_engine/services/go_job_contract.py`, `engine/boss_engine/services/go_webhook_dispatcher.py`, `scripts/go_scale_dry_run.py`.
- Deltas versus the tag are an older tree plus one honesty bit the release did not keep: worktree `go_scale.py` adds `s3_in_repo_scaffold: True`, which is absent from the tag. The tag already has the S3 sources, `make test-go-dispatcher`, and a later `GO_SCALE_TRACK.md`. The worktree `Makefile` is the August file and is missing later Community targets (`test-v1-lock`, `openapi-validate`, and others). This checkout is not Community-release WIP.

## Needs port

None. No outside tip has unique behavior from 2026-09-01 onward that is missing from the tag.

The 36 unique tips whose `git cherry` output still has a `+` commit dated on or after 2026-09-01 were checked against the tag tree. The added behavior is already present under a later patch, or it is an intermediate pin/count/snapshot the tag replaced:

- Community integration tips (`community-web-integration-check`, `community-openapi-export-remap`, `community-fixture-proof`, `community-focused-test-export`, `community-integration-smoke`, `community-metatron-proof`, `community-metatron-target`, `boss-416-*`, `boss-429-mcp-full-product`, `boss-448-*`, `boss-community-reference-hygiene`, `community-inspect-host-read-guard`, `community-model-setup-probe-gate`) — sampled added lines are in the tag. The two export-manifest commits only differ by a sha256 the tag repinned at `46c10ca9`. The MCP matrix commit's counts (community 981) were replaced by the tag's 986.
- `origin/boss/code/crun_b3535fbc1fbe43c7b00b` (Paramaguru, 2026-09-01, ahead 22) — per-file `apps/dep_freshness` proposal checkpoints. That app is in the tag; `ai/codex/paramg-pr-reconcile` is patch-equivalent. Not a second copy to port.
- `heads/ai/codex/community-checkup-docker-permissions` — the one patch-unique commit is a 2026-09-28 qa-live streak note. The tag's `docs/operations/QA_LIVE_14DAY.md` already carries that calendar (through 2026-09-25 in the table).
- Cursor `critical-bug-investigation-*` tips with a missing patch-id were spot-checked against tag code (Build submits `assign(input)`, Claude token is loaded from the secret store, Codex chat calls `ensure_authorised`, investigation SQL strips strings before WHERE/CTE checks, nested preview/meeting ids are read, `_operator_confirmed` is stripped from model args). The behavior is in the tag. They are not an open bug to port. Older cursor refs are skipped on the same rule: patch-id absent is not enough without a confirmed still-open bug, and this pass did not find one.

## Cursor investigation refs

Skipped as not relevant to this release: 53 cursor refs, 53 unique tips, all outside the tag as SHAs. 13 are `patch-equivalent` (the fix patch is already in the tag). 40 are `superseded` (patch-id absent; not confirmed as a still-open bug). They stay in the tables so the outside set is complete. No cursor tip is `needs port`.

## Contributor branches other than `ai/grok` and `ai/codex`

Non-bot tips outside the tag, other than `main`, are June–August history or the one September factory checkpoint above. July `origin/feat/*` branches (Paramaguru: VAG, APP-1–6, Plane webhook, Go async contract) are `patch-equivalent`. `origin/boss/code/crun_9ad921eab34f4d90bdfe` (2026-08-15) and `origin/boss/code/crun_b3535fbc1fbe43c7b00b` (2026-09-01) are `superseded` factory checkpoints. Two June 2026 roots have no meaningful shared history to port: `heads/codex/providerops-runtime-slice` and `heads/docs/boss-prd-resource-plane-v1.1`.

## Outside tips

One row per unique tip. Refs share that SHA. Dates are committer dates.

### `superseded` (70 unique tips)

| Date | Ahead | Tip | Refs | Subject |
|---|---:|---|---|---|
| 2026-06-05 13:55:52 -0400 | 109 | `019dd04ae1ca0df6479b8eb4aab5fdd09993360b` | `heads/ai/codex/qa-e2e-comms` | feature(monitoring): full DoD states/tests for health/risk/event/deadletter (P2 polish + AUD/MET DoD) |
| 2026-06-05 15:02:22 -0400 | 113 | `1bbace1980c8dc655a663278fdeb0f0c3cd9733c` | `heads/ai/codex/arch-pag-expand-full` | qa(e2e-selfimp): complete journeys tests + states (P2 e2e + SIM DoD) |
| 2026-06-05 14:58:43 -0400 | 108 | `26a3a8c1fb39e0bf00e63e7637f9ea5c6960dab5` | `heads/ai/codex/ui-ux-toasts-client-feedback` | chore(3x): stage all pending from toasts side (trace, tests, api-client, etc from parallel agents) |
| 2026-06-05 15:05:55 -0400 | 119 | `309908a0c686fbdf28077f055b2d8c1b0a8f9b16` | `heads/ai/codex/devops-worker-tests` | ux(toasts-complete): finish client feedback toasts + tests (P2 ux-review + DoD, GAP context) |
| 2026-06-05 14:38:23 -0400 | 113 | `64f8b230f8b19cd9872c790d2a0dcd875d002e27` | `heads/ai/codex/feature-providerops-polish` | docs(coord): add self-improvement provider QA handoff |
| 2026-06-05 13:53:00 -0400 | 107 | `8538e9e5fc91ecaf506dc7dca55de2ee6954a915` | `heads/ai/codex/feature-comms-ui` | ux(commercial): local surfaces states/polish + tests (COMPKG P2 + DoD, P3 pack context) |
| 2026-06-05 13:51:45 -0400 | 106 | `de7be3792876812f09be0d552c0fd4553dca42f1` | `heads/ai/codex/packs-local-import-export` | feature(comms): UI interactions polish + tests (COM P2 + DoD) |
| 2026-06-24 08:43:10 -0400 | 130 | `f46bd7755d7e86619a6d49b27672b92a9916967f` | `heads/ai/codex/baremetal-deploy-sync` | deploy: remove localhost API fallback from web bundle |
| 2026-07-16 05:08:14 +0000 | 1 | `c29246854fd892e09078004fdc07746f9e6029cc` | `origin/cursor/critical-bug-investigation-4a4f` | fix: bind plan dispatch to authorized project |
| 2026-07-28 05:13:30 +0000 | 1 | `4a906eafeecde2b47acf1a8093b6ccaa1f1e00ed` | `origin/cursor/critical-bug-investigation-d7d3` | fix: stop chat discuss tools from cross-tenant reads; auth SSH deploy |
| 2026-07-29 05:08:02 +0000 | 3 | `37a9a89731f89677ccf1d5497ea98b42b0c12ff7` | `origin/cursor/critical-bug-investigation-4363` | fix(skills): include constraints in model.generate_code dedupe key |
| 2026-07-31 05:06:22 +0000 | 2 | `3d89ed5cf893d8f4056fd186caf689537c729049` | `origin/cursor/critical-bug-investigation-ca78` | fix: auth gate override/preview; block dissent-ship on nonfinal slices |
| 2026-08-01 05:04:39 +0000 | 3 | `055602656e6cf28dd5003be3183c81e900e9d667` | `origin/cursor/critical-bug-investigation-a499` | fix: require approved Decision before resuming deferred agent tools |
| 2026-08-02 05:05:00 +0000 | 4 | `1b8683c62d70b6d73cba792a2aa7e46673e53cea` | `origin/cursor/critical-bug-investigation-f4f1` | fix: fence MCP governance tools to the session project |
| 2026-08-03 05:04:39 +0000 | 7 | `db195c276333b95816562cba93a00260a307fec0` | `origin/cursor/critical-bug-investigation-b31f` | test: update registry tool counts for steer/watch |
| 2026-08-04 05:11:08 +0000 | 8 | `7dc7828503d308d39f936f26139f9f8fff4e763f` | `origin/cursor/critical-bug-investigation-71af` | fix: require actor + task_create for code-task retry |
| 2026-08-05 05:12:31 +0000 | 4 | `7dcc8f6fead428f9443dbd56518fe6deafb6208c` | `origin/cursor/critical-bug-investigation-c228` | fix: stale-base merge completion and project-to-org privilege escalation |
| 2026-08-06 05:10:37 +0000 | 5 | `604181e5d6b19b0b6d0007ecaeafd2cbeda848d3` | `origin/cursor/critical-bug-investigation-031d` | test(delivery): patch db.begin for execute verdict regressions |
| 2026-08-14 15:26:24 -0400 | 1 | `12fff4fad4925c38c46d3dce66c6718226ebbc62` | `origin/ai/grok/boss-370-qa-feature-sweep` | feat(qa): honest workforce catalog probes for BOSS-370 |
| 2026-08-14 15:29:33 -0400 | 1 | `a9eccfc6ed8a346c8a8dcf9bab0185ac406041d8` | `origin/ai/grok/boss-375-factory-e2e-quality` | test(qa): prove factory quality surface honesty (BOSS-375) |
| 2026-08-15 09:47:32 -0700 | 6 | `24bd9341a1e0f5ca98a6ca1510bf0e6e90b16779` | `origin/boss/code/crun_9ad921eab34f4d90bdfe` | BOSS proposal update: scripts/tests/test_sealed_e2e_canary_note.py |
| 2026-08-17 05:06:48 +0000 | 1 | `37ea5d1b9d58fc9bd289d4264d4f424ea9de6450` | `origin/cursor/critical-bug-investigation-e634` | fix(agent): stop the console shell from inheriting DATABASE_URL |
| 2026-08-20 05:07:05 +0000 | 1 | `87e8e13fd12a27993030822f1f9fccfb24b671d4` | `origin/cursor/critical-bug-investigation-2e44` | fix(agent): stop the console shell from inheriting DATABASE_URL |
| 2026-08-21 05:05:14 +0000 | 1 | `7392318418bb33b6a155c133f0405b1a86c5b6f1` | `origin/cursor/critical-bug-investigation-7cf3` | fix(agent): stop the console shell from inheriting DATABASE_URL |
| 2026-08-22 05:09:03 +0000 | 1 | `0a7f3f7916dfffe06dfaddbce274dcb1e312728e` | `origin/cursor/critical-bug-investigation-44e7` | fix: close fleet attach/deploy auth holes and agent shell leaks |
| 2026-08-23 05:10:02 +0000 | 1 | `30c964f56f0736fde461ae018eae82566943b317` | `origin/cursor/critical-bug-investigation-3839` | fix: stop cancel-path crash, tenant writes, and agent/fleet leaks |
| 2026-08-24 05:10:41 +0000 | 1 | `2a3f22017d94ee962adfafd6a3d5c10211d67848` | `origin/cursor/critical-bug-investigation-a469` | fix: stop first-deploy from shadowing targets and close remaining leaks |
| 2026-08-25 05:07:12 +0000 | 1 | `bd931349461832fbddcdab58cb68b61ffbb40838` | `origin/cursor/critical-bug-investigation-cff2` | fix: stop first-deploy from shadowing targets and close remaining leaks |
| 2026-08-26 05:07:42 +0000 | 1 | `9ca4c841f9b150dd1562dc46159e31a91fdd902b` | `origin/cursor/critical-bug-investigation-d806` | fix: stop first-deploy from shadowing targets and close remaining leaks |
| 2026-08-27 05:07:20 +0000 | 1 | `e72c7f305be7f3802fc318282306ea30f227c1d9` | `origin/cursor/critical-bug-investigation-ecf7` | deploy: the only target in the repo is not this app |
| 2026-08-28 05:14:24 +0000 | 1 | `bfa21c52ae0970fe8bedd0f95c665ea72c3a6802` | `origin/cursor/critical-bug-investigation-8cbe` | fix: first-deploy health checks must actually probe the public URL |
| 2026-08-29 05:24:24 +0000 | 1 | `4c8557c4f92858802db8dce857c40ca1a85dcff9` | `origin/cursor/critical-bug-investigation-6340` | fix: refuse to bind a deployment host from another project |
| 2026-08-30 05:15:07 +0000 | 1 | `c73982a62aa0d62822be9a4dc825acf3fe5a87e5` | `origin/cursor/critical-bug-investigation-3193` | fix: stop delivery workflows from deploying without a person |
| 2026-08-31 05:09:17 +0000 | 1 | `be5084d886a604cb02ee3807f02117de44f8b06b` | `origin/cursor/critical-bug-investigation-4c78` | fix: timeout-approve must still run the work, not skip it |
| 2026-09-01 22:36:51 -0700 | 22 | `5d07c89440bf02268148e65febf34a4224b811c5` | `origin/boss/code/crun_b3535fbc1fbe43c7b00b` | governed: checkpoint after write_tests — Follow-up: deferred review findings — [verify] app |
| 2026-09-02 05:14:41 +0000 | 1 | `eb8e059ff9da2538d29a22b83074713b388a99a0` | `origin/cursor/critical-bug-investigation-8623` | fix: preview and meeting wait on the id the live tools return |
| 2026-09-03 05:07:35 +0000 | 1 | `3bb6cacd0d3cfe8b3180622669393a1892cfefec` | `origin/cursor/critical-bug-investigation-41d2` | fix: a model cannot confirm its own host command |
| 2026-09-05 05:06:47 +0000 | 1 | `538d7df73592b2465c2d9cc4c60604010a96198c` | `origin/cursor/critical-bug-investigation-edb9` | fix: a 529 after a write must not run the write again |
| 2026-09-06 05:08:33 +0000 | 1 | `72b3323ca008411d2a710809dc8e16a2412d6a9b` | `origin/cursor/critical-bug-investigation-f725` | Chat rebuilt every write as safe, so a merge gate approved itself |
| 2026-09-07 05:05:46 +0000 | 1 | `0042b429123742d1d93f4271c2bdb8dd35bf71ef` | `origin/cursor/critical-bug-investigation-79d7` | fix: an investigation UPDATE whose text says "where" must not hit every row |
| 2026-09-08 05:08:31 +0000 | 1 | `bf3762b76bc74c864bbef83b91d21315e1d59a1b` | `origin/cursor/critical-bug-investigation-47b2` | fix: a WITH CTE must not let an investigation write without a WHERE |
| 2026-09-09 05:09:08 +0000 | 1 | `6bb359bdac2c60643f2050324f1f5ab845dee676` | `origin/cursor/critical-bug-investigation-4201` | fix: a stalled chat turn must not treat the previous reply as this one |
| 2026-09-10 05:07:36 +0000 | 1 | `383375f02cc62d7129644da7133d3d227057a778` | `origin/cursor/critical-bug-investigation-ee4a` | fix: clicking Build must start a build, not an ask |
| 2026-09-12 05:05:30 +0000 | 1 | `baa4478c4c44d43f0abf8e283e24ffbb8331ca89` | `origin/cursor/critical-bug-investigation-915b` | fix: setup must not fetch a caller URL before it knows who you are |
| 2026-09-13 05:07:09 +0000 | 1 | `382e52938af59167d6771ce29be4831b28ad7e69` | `origin/cursor/critical-bug-investigation-f726` | fix: draft_workflow must not overwrite a catalogue workflow |
| 2026-09-15 05:08:23 +0000 | 1 | `d58bc77b199b89ac1e2a60bfdb87e44cebd751b4` | `origin/cursor/critical-bug-investigation-46bf` | Native bash must not inherit the control plane's DATABASE_URL |
| 2026-09-16 05:07:23 +0000 | 1 | `1d26940743b2f3443d84a1a4743b8c073429fe78` | `origin/cursor/critical-bug-investigation-a78c` | A Codex question must find the stored login, and must not see the production database |
| 2026-09-17 05:16:24 +0000 | 1 | `9de6d4ebcec9ae1a73c975b7a7e050cf36974e30` | `origin/cursor/critical-bug-investigation-92ac` | fix: store a subscription as a real project member, not as setup |
| 2026-09-18 05:06:28 +0000 | 1 | `832b49b112fe6c3f428dbee64d774ed5fe59b084` | `origin/cursor/critical-bug-investigation-7449` | fix: a wizard-stored Claude token must reach the driver that builds |
| 2026-09-19 05:06:12 +0000 | 1 | `207214cad089d3ebbdd701f4358e2ad2c12abcd5` | `origin/cursor/critical-bug-investigation-2e80` | fix: Connect again must keep the repository it already bound |
| 2026-09-24 15:11:05 -0400 | 1 | `109ae38124cd645ff72bb69b740fbd4b2e32bc48` | `heads/ai/codex/community-metatron-target`<br>`origin/ai/codex/community-metatron-target` | BOSS-293 keep Metatron target in Community export |
| 2026-09-24 15:11:56 -0400 | 1 | `2686766eaf11e7cd88d28731f2a6065ef485ca4b` | `heads/ai/codex/community-metatron-proof` | Validate Community Metatron export in integration proof |
| 2026-09-24 14:10:13 -0400 | 25 | `3d31a50530b901ae8666f8365f857eb66a1a038a` | `heads/ai/codex/community-web-integration-check` | Merge branch 'ai/codex/community-installer-lifecycle' into ai/codex/community-web-integration-check |
| 2026-09-24 14:43:21 -0400 | 3 | `611a0f24189cb8c92c1589de2abddd64b69ed570` | `heads/ai/codex/community-fixture-proof` | BOSS-293: force Community edition before test app import |
| 2026-09-24 14:45:14 -0400 | 2 | `91591dc52b8ab8b6583cdd6c3214871f3214f6fa` | `heads/ai/codex/community-openapi-export-remap`<br>`origin/ai/codex/community-openapi-export-remap` | build: pin Community API contract with agent template routes |
| 2026-09-24 15:14:20 -0400 | 2 | `ad10983f3b2270df8b0f4533d6c80fd5041aa56b` | `heads/ai/codex/community-integration-smoke` | Merge Community Metatron Make target for integration preview |
| 2026-09-24 14:57:52 -0400 | 2 | `d91f788e89435c1ce992ad93592b2e17e86a5caa` | `heads/ai/codex/community-focused-test-export`<br>`origin/ai/codex/community-focused-test-export` | BOSS-293 split Community ProviderOps feed coverage |
| 2026-09-26 05:18:11 +0000 | 1 | `5938ede0036051a191b77244bd338e4b94cc16a6` | `origin/cursor/critical-bug-investigation-5e51` | An agent that stops for approval was recorded as finished |
| 2026-09-27 05:08:15 +0000 | 1 | `266ecb454a58f6c3dd0df83002225f7cbcd6f249` | `origin/cursor/critical-bug-investigation-1d1c` | Start one can see the inputs a workflow declared |
| 2026-09-28 15:31:19 -0400 | 1 | `1fc59268bc4537e655d3c7284968a0d0ff12e014` | `heads/ai/codex/boss-448-meetings-docs-authority` | BOSS-448 authorize meeting and documentation work routes |
| 2026-09-28 18:36:24 -0400 | 2 | `2e5dec0a335a34d7bcdb7a99f53b08744c0c7622` | `heads/ai/codex/boss-429-mcp-full-product` | BOSS-429: split public and private MCP capability matrices |
| 2026-09-28 18:51:49 -0400 | 1 | `4d139fa76a2a549ecd18706856709c99f229d5bb` | `heads/ai/codex/boss-416-typed-bodies` | BOSS-416: type common API request bodies for terminal clients |
| 2026-09-28 19:12:52 -0400 | 2 | `50dd24a79782ca52c635614ebd5e5320796d52d1` | `heads/ai/codex/boss-416-runtime-inputs-final` | Document preview feedback message requirement |
| 2026-09-28 17:10:41 -0400 | 1 | `6627bc1fc97edd9d7942bfe4b415c31c45fe27e9` | `heads/ai/codex/community-inspect-host-read-guard` | fix: parse read-only host inspection commands |
| 2026-09-28 15:59:15 -0400 | 7 | `73d927cf9d030bc00839d18f71ec9a0a097879c5` | `heads/ai/codex/boss-448-factory-authority` | Verify Plane TLS by default with explicit local override |
| 2026-09-28 17:29:48 -0400 | 2 | `781f28cbf4ff764871b802a993f119b04c21714b` | `heads/ai/codex/community-model-setup-probe-gate` | Keep Community setup status from probing model providers |
| 2026-09-28 15:24:59 -0400 | 1 | `a9846c7560081cf258b6130a80c76d56e75855ca` | `heads/ai/codex/boss-448-build-family-authority` | BOSS-448 authorize build entrypoints and code task steps |
| 2026-09-28 14:18:03 -0400 | 1 | `b108bfad886876a870dbaedfe39bc2a6ce50993e` | `heads/ai/codex/boss-community-reference-hygiene` | Scrub private infrastructure references from Community export |
| 2026-09-28 14:19:04 -0400 | 4 | `b64bd6a9a543e77d3dc68a7efea458ad8c3e3853` | `heads/ai/codex/community-checkup-docker-permissions`<br>`origin/ai/codex/community-checkup-docker-permissions` | docs(community): use verified engine module in installed API container |
| 2026-09-28 17:18:23 -0400 | 3 | `d0f5c4316050be0fc7de5d3ef3f82cb1bdf37846` | `heads/ai/codex/boss-448-tool-run-read-scope` | Gate Community coding agent proof and host port discovery |

### `patch-equivalent` (219 unique tips)

| Date | Ahead | Tip | Refs | Subject |
|---|---:|---|---|---|
| 2026-06-03 22:04:26 -0400 | 3 | `067ea48e6a85ed3ac03c5bdf8c62a88cf3faae24` | `heads/docs/boss-prd-v2-ultimate-spec` | feat: add commercial and enterprise governance slices |
| 2026-06-03 15:42:05 -0400 | 32 | `43d5368444e1ac341ebd6b8d610c614fe4fcc7fa` | `heads/codex/providerops-runtime-slice` | Ship ProviderOps runtime slice and release v0.3.0. |
| 2026-06-03 15:46:26 -0400 | 1 | `9e56b0a39a204ed2d258d9a9acffecd803c63368` | `heads/codex/model-oauth-entitlements` | Ship ProviderOps runtime slice and release v0.3.0. |
| 2026-06-03 10:32:24 -0400 | 31 | `cc7c115008fdc675fa7cd4665412b1eb863536aa` | `heads/docs/boss-prd-resource-plane-v1.1` | docs: add Elastic Resource Plane to BOSS PRD |
| 2026-06-05 09:46:44 -0400 | 48 | `25b54920857902adef2ece93c8379711de32d7a6` | `heads/codex/mission-control-commercial-enterprise` | chore: remove accidental image artifact |
| 2026-06-05 13:37:00 -0400 | 104 | `26caced1549223130ac8f1f4a06df290e6be98c4` | `heads/ai/codex/arch-pagination-expand`<br>`heads/ai/codex/qa-e2e-selfimp-provider` | docs(scale-3x): doubled agents to ~20 for 3x speed; launched 10+ specialists on 12+ full DoD polish slices (pack P3, ux-toasts, e2e-comms/selfimp/provider, providerops, commercial, pag-expand, monitoring, comms-ui +); updated gap (12+ slices), status/handoff (active ~20 ids), activity 15min, all 9 base agents/*.md + new; PR#3 |
| 2026-06-05 15:40:57 -0400 | 126 | `2c0521c8a77379cceb50bdb0025659d3c8813b54` | `heads/ai/codex/e2e-regression` | qa(e2e-regression): cross-slice tests for 3x features (pack+selfimp+toasts+commercial + budget) (P2 e2e + DoD) |
| 2026-06-05 14:42:57 -0400 | 105 | `3789013097a58dc63367b21115b38f2303b2cafe` | `heads/ai/codex/feature-monitoring-polish` | docs(3x): mark GAP-PRD-AUDIT-001 P3 pack Closed (agent 019e98d6-e1e8 62fd3ab); update plan/counts; append handoff/status/activity with new 7+ agents launched for remaining slices (verify-pack, toasts, monitoring, commercial, e2e-selfimp, pag-full, worker, release-docs); 3x grind continue |
| 2026-06-05 15:45:02 -0400 | 130 | `76007c14dfba6b1d80c7179fdcf7846746d9af51` | `heads/ai/codex/a11y-full` | ux(a11y): full expand for 3x features (pack/selfimp/toasts/commercial/monitoring/pag + toasts role/alert) (P2 + DoD) |
| 2026-06-05 13:13:35 -0400 | 101 | `7cebd471b3fad4c566229c9a913adcf4836199af` | `heads/codex/auto-004-env-release` | devops: address AUTO-004 (.env.example comments for all BOSS_* incl bootstrap/infra/account/oidc; release-blockers script enhance; add make full-validate + deps-audit; run all validations; .ai/ updates) refs AUTO-004 GAP-AUTO-004 |
| 2026-06-05 13:53:42 -0400 | 108 | `a040b6b086d2903753950375ceeedc407d53f88e` | `heads/ai/grok/ux-commercial-surfaces` | ux(commercial): local surfaces states/polish + tests (COMPKG P2 + DoD, P3 pack context) |
| 2026-06-06 19:40:18 -0400 | 144 | `4f5b290744c51837c04da631b532adf53fb94dc7` | `heads/ai/codex/api-production-hardening` | fix(api): harden production API contracts and Account Connect integration |
| 2026-06-06 18:34:18 -0400 | 142 | `d612007e898e09e7ccd5f9ce6073333d861a42ac` | `heads/codex/spec-closure-audit` | feat(api): ship v0.5.0 API baseline for UI team |
| 2026-06-07 12:08:43 -0400 | 167 | `0f757b71cadf522f991f55ffc25070ff8180ce33` | `heads/fix/consolidation-release` | fix: restore API contracts and commercial services after bad merge |
| 2026-06-07 12:08:55 -0400 | 168 | `7c0cb0b27d5ff514cca9db3e4ac657cca9e6fa57` | `heads/codex/local-main-pre-v0.8.0` | chore(release): cut v0.6.0 consolidation release |
| 2026-07-11 18:32:35 -0700 | 160 | `44b985b40ac8fd1f6bc2dcbcf99fee28961d5d37` | `heads/ai/codex/plane-system-of-record` | docs(oss): spec 10 — Go concurrency services for high-scale task execution |
| 2026-07-15 10:53:29 -0400 | 167 | `04ec5e5e3e4b8e92e69a092a78bb93fbec84d4b9` | `heads/ai/codex/reactive-docs-operability` | docs(ai): record operability PR handoff |
| 2026-07-15 09:44:57 -0400 | 163 | `0c87110f440e8c6433dcabc32a2a0fae8326ba1b` | `heads/ai/codex/oss-reactive-docs-foundation` | docs(agents): make Plane the BOSS system of record |
| 2026-07-15 13:07:50 -0400 | 168 | `519f30a89baf9fb5c4b6bcbdfe4569ad00c48f34` | `heads/ai/codex/github-community-agents` | feat: add GitHub community agents (BOSS-84 BOSS-86 BOSS-87 BOSS-88) |
| 2026-07-15 11:39:05 -0400 | 166 | `64a4a5e1bdb85f479000e15f426a5005434b26cc` | `heads/ai/codex/github-community-adapter` | feat: add GitHub community adapter (BOSS-85 BOSS-89) |
| 2026-07-15 10:17:53 -0400 | 165 | `8c902e3a6efde50cea5a2a92708c9866e248ec5a` | `heads/ai/codex/webhook-foundation` | docs(ai): record webhook PR handoff |
| 2026-07-15 08:58:59 -0400 | 161 | `a055c44b9d547aec0a3e51b15793b8aa73dc9384` | `heads/ai/codex/boss-112-design-system` | feat(web): implement BOSS mission-control design system (BOSS-112) |
| 2026-07-15 13:33:57 -0400 | 187 | `cfb39d3d7faea21d9632b9f6b77aab39d860cd3e` | `heads/ai/codex/integrate-main` | chore: finalize BOSS-118 integration validation |
| 2026-07-15 11:29:27 -0400 | 168 | `e460046a48ab959612a2c8862ca0d881f9d2e146` | `heads/ai/codex/docs-agent` | feat: add governed reactive docs agent (BOSS-73 BOSS-74 BOSS-75) |
| 2026-07-15 12:32:21 -0400 | 161 | `e5fd918e547767da32456667ccbebb1aeb992f36` | `heads/ai/codex/skill-executor-registry` | feat: add skill executor registry (BOSS-45) |
| 2026-07-15 12:48:18 -0400 | 167 | `ee7ddcc72af78cddeee64c0c99d363b7806b567a` | `heads/ai/codex/conversational-orchestrator` | feat: add conversational orchestration agent (BOSS-78 BOSS-79 BOSS-80 BOSS-81 BOSS-82) |
| 2026-07-16 15:48:56 -0400 | 191 | `16efd014dae73c59c9aabfa0d3764045c9c257bc` | `heads/ai/codex/boss-35-43-63-preview-feedback` | docs(preview): record BOSS slice delivery evidence |
| 2026-07-16 08:31:04 -0400 | 189 | `228e654bb6b6e6baf84a6ee1d3efae59edd1accc` | `heads/ai/codex/boss-46-51-52-skill-foundation` | docs(ai): record BOSS skill CI gate |
| 2026-07-16 11:57:15 -0400 | 193 | `4343b4c0b87c17ef26f3c92ac091c1effbf230ce` | `heads/ai/codex/boss-48-50-55-56-skill-marketplace` | docs(ai): record marketplace PR handoff |
| 2026-07-16 17:25:44 -0400 | 200 | `8d048582ea8d01530322a892b1339fd2b5e2ef86` | `heads/ai/codex/release-v0.7.0` | release: prepare BOSS v0.7.0 (BOSS-120) |
| 2026-07-16 09:34:00 -0400 | 191 | `f14dae7ea546e48b8f594be49799988887a36727` | `heads/ai/codex/boss-47-53-54-skill-resolution` | docs(ai): record skill resolution PR handoff |
| 2026-07-16 14:57:43 -0400 | 189 | `f6265113337fad264e67e436b2484e09e34375c5` | `heads/ai/codex/boss-58-62-cicd-promotion` | docs(cicd): link BOSS-58-BOSS-62 delivery evidence |
| 2026-07-17 20:58:03 -0700 | 1 | `39acbb3786259c265add752570a5e84f407eb54f` | `origin/feat/vag-mutation-gate` | feat(vag): #106 mutation-gate the frozen tests (deterministic, model-free) |
| 2026-07-17 19:31:37 -0700 | 1 | `895ba9ed0c6ddc51306eed200d4b165ae3f66ea0` | `origin/feat/model-call-pacing` | feat(#111): rate-limit-aware model-call pacing (default paced, opt-in burst) |
| 2026-07-17 17:40:35 -0700 | 1 | `afd766ceb8cefb9df1c73df2aac41353d1bd6d98` | `origin/feat/plane-webhook-intake` | feat(plane): Phase 1 inbound webhook -> auto code-task (#114) |
| 2026-07-17 20:46:08 -0700 | 1 | `c62d6661a3717e595f8e33954059b3e08bb4a75d` | `origin/feat/console-vag-explainer` | feat(console): explain VAG in the Coding Pipeline area |
| 2026-07-17 19:50:50 -0700 | 1 | `cce01d9ce2719b5f6bb5aab2b14a58c120cddf95` | `origin/feat/vag-verifier-meta-eval` | feat(vag): #105 meta-eval the verifier — labeled corpus + TP/FP gate |
| 2026-07-17 20:34:51 -0700 | 1 | `d19f02840d1ccec01984a921a84795fbe3ca2956` | `origin/feat/vag-ratchet` | feat(vag): #107 cross-run eval ratchet — capture human↔gate disagreements |
| 2026-07-17 19:29:28 -0700 | 4 | `d1d2458f3844136da9071f352bc63975ed7f7a4b` | `origin/feat/vag-phase1-refute-panel` | docs(vag): Phase 1 refute-panel sweep report (11 valid runs) |
| 2026-07-18 16:37:35 -0400 | 221 | `2c2f0ebb6d1a5f75b2e426d7f4a26c354f1887bd` | `heads/ai/codex/boss-133-product-capability-taxonomy` | docs(product): separate composable BOSS capabilities (BOSS-133) |
| 2026-07-18 16:59:43 -0400 | 228 | `2f78f2590ebe3aa4de0064cc530102c395371c6d` | `heads/ai/codex/boss-132-merge-handoff` | docs(workflows): record merged delivery (BOSS-132) |
| 2026-07-18 16:54:59 -0400 | 226 | `56571c8bf3f635668bf0477a54b1d3cdc4e13cec` | `heads/ai/codex/boss-134-143-workflow-runtime` | docs(workflows): record external CI gate (BOSS-132) |
| 2026-07-19 12:44:26 +0530 | 1 | `0d5b6b0de2ffbf8b5e0519b325d0710b73372c06` | `origin/feat/app5-retrieval` | feat(app): APP-5 whole-repo coherence via retrieval (#95) |
| 2026-07-19 11:07:41 +0530 | 1 | `233b5e47ebac7b8ddefaed945ef1b0a41bc93c17` | `origin/feat/vag-repair-policy` | feat(vag): #109 budget-variable repair depth + implementer-tier escalation |
| 2026-07-19 12:30:07 +0530 | 1 | `785a587a331d98a14798f92d31ec557627acaff5` | `origin/feat/app1-scaffold` | feat(app): APP-1 framework-aware scaffolding service (#91) |
| 2026-07-19 12:39:11 +0530 | 1 | `7a1d7ee8c98bd72485290dcfe9dd792cc4fce147` | `origin/feat/app2-design-role` | feat(app): APP-2 design/product role + design-aware prompts (#92) |
| 2026-07-19 11:03:34 +0530 | 1 | `b3cb797f71949cd074257072dd5a9cddaad8dc4b` | `origin/feat/vag-debate` | feat(vag): #108 bounded generator<->verifier debate on contested cases |
| 2026-07-19 10:59:05 +0530 | 1 | `b44d9b503a2118664fe5ee0a8919af06cc84aa06` | `origin/feat/vag-independence` | feat(vag): #110 generator/verifier independence contract |
| 2026-07-19 12:49:52 +0530 | 1 | `c1f4dfa2ada2aeee9b3ae5d50a507949c257e8f3` | `origin/feat/app3-web-verify` | feat(app): APP-3 web-appropriate verification target (#93) |
| 2026-07-19 13:20:40 +0530 | 1 | `c74f00b523c3a96ca428a89c2c41f06472c925b1` | `origin/feat/app6-design-routing` | feat(app): APP-6 hybrid frontier+OSS routing for the design axis (#96) |
| 2026-07-19 13:15:27 +0530 | 1 | `d600a4e3de3010feb2b51d8fb32af1c3d51c46c7` | `origin/feat/app4-visual-gate` | feat(app): APP-4 real deterministic visual/UX gate (#94) |
| 2026-07-20 10:58:44 +0530 | 1 | `8033637b3f2b8b266259e6d806e9701269b011df` | `origin/feat/go-async-contract` | feat(runtime): #99 async Python<->Go sandbox contract + #103 load harness |
| 2026-07-20 09:46:40 +0530 | 1 | `ce8b67aa69ed3e70d0bdc09a940b122e651f4d66` | `origin/fix/plane-label-resolve` | fix(plane): resolve webhook label UUIDs -> names (real delivery shape) |
| 2026-07-20 10:15:33 +0530 | 1 | `e34e9343c0fcb9fb14424e93678ed5005f2f8706` | `origin/feat/plane-phases-2-4` | feat(plane): Phases 2-4 — status sync-back, richer mapping, closed loop |
| 2026-07-25 05:04:38 +0000 | 2 | `f8f0dea600546ae93a132acee12f3248c303ce5b` | `origin/cursor/critical-bug-investigation-5bbd` | test(deploy): assert shlex-quoted destination for malicious host |
| 2026-07-26 18:03:52 -0400 | 6 | `045aaf09da57b329c60521028de90a9e4e82a0a7` | `heads/ai/codex/boss-253-merge-authorizations` | feat(github): govern protected merge queue |
| 2026-07-26 17:04:44 -0400 | 2 | `073f3b6145a026d9db451c24eefcc5d205e6cda6` | `heads/ai/codex/boss-247-github-project-connection` | fix(github): bind receipt replays to test scope (BOSS-247) |
| 2026-07-26 16:26:58 -0400 | 26 | `291e20fe99f6d25ab0a1df875d6427fabe20556f` | `heads/ai/codex/boss-237-system-config-ux` | feat(providerops): add system configuration workspace |
| 2026-07-26 12:10:31 -0400 | 4 | `2deda32c8a9ceb2856d603bf8423cbffa55e0f55` | `heads/ai/codex/boss-assurance-workflows` | Add assurance workflow catalog (BOSS-195–214) |
| 2026-07-26 10:54:43 -0400 | 2 | `40a470d8d26654142b42705dd315e9028ec1cb12` | `heads/ai/codex/boss-assurance-contracts` | feat(specpack): define assurance contracts (BOSS-175–194) |
| 2026-07-26 12:13:00 -0400 | 5 | `43b506e501232e1532efa89b32f2a18a3ac29181` | `heads/ai/codex/boss-assurance-workflows-handoff` | Record BOSS-195–214 shipment evidence |
| 2026-07-26 18:46:22 -0400 | 9 | `567572d8f73bdcc41659f0b2de22422cf1804990` | `heads/ai/codex/boss-255-github-mission-control` | docs(ai): record BOSS-255 handoff |
| 2026-07-26 22:24:38 -0400 | 12 | `6197e2ce03d1757c7c7f99f379e5c174bb223488` | `heads/ai/codex/boss-270-work-orchestration` | feat: project deploy presence to status channels |
| 2026-07-26 17:43:05 -0400 | 5 | `741479f6244c562e0d369120c346c75ce73c3dd6` | `heads/ai/codex/boss-251-252-safe-sync-conflicts` | feat(github): add safe sync and conflict resolution (BOSS-251 BOSS-252) |
| 2026-07-26 14:36:25 -0400 | 1 | `8fa1e6189420ff640884d916fa4bcf271ed41fa2` | `origin/ai/codex/boss-236-provider-credential-lifecycle` | feat(providerops): add credential lifecycle |
| 2026-07-26 10:56:14 -0400 | 3 | `960cdfa2a6ab38100d9bc7424eb8196eb2bdcaec` | `heads/ai/codex/boss-assurance-handoff` | docs(ai): record BOSS-175–194 shipment |
| 2026-07-26 18:28:41 -0400 | 7 | `9703a32a78f0a4f35ce24dca89c5837fc36cb2a5` | `heads/ai/codex/boss-254-github-reconciler` | feat(github): add durable repository reconciler |
| 2026-07-26 10:54:43 -0400 | 1 | `97c464baa0620e1c576e2d985f716ca9b95be640` | `origin/ai/codex/boss-assurance-contracts` | feat(specpack): define assurance contracts (BOSS-175–194) |
| 2026-07-26 17:04:44 -0400 | 1 | `aadf21ed40bc068004ec1cb66d86b0c836890197` | `origin/ai/codex/boss-247-github-project-connection` | fix(github): bind receipt replays to test scope (BOSS-247) |
| 2026-07-26 12:10:31 -0400 | 1 | `ae36080fbf63c70ea25db2c759c6a47f3a8ab55e` | `origin/ai/codex/boss-assurance-workflows` | Add assurance workflow catalog (BOSS-195–214) |
| 2026-07-26 12:13:00 -0400 | 1 | `afa1e5ff683ebcd442b2d62dfe01610e00ce3f09` | `origin/ai/codex/boss-assurance-workflows-handoff` | Record BOSS-195–214 shipment evidence |
| 2026-07-26 10:56:14 -0400 | 1 | `b25b190868fb8132c2261228672d07a44fc38e95` | `origin/ai/codex/boss-assurance-handoff` | docs(ai): record BOSS-175–194 shipment |
| 2026-07-26 21:17:26 -0400 | 9 | `b84937271e76f4431b20be2bdd61da65f1701496` | `heads/ai/codex/boss-256-agent-studio-a2a` | Merge remote-tracking branch 'origin/main' into ai/codex/boss-256-agent-studio-a2a |
| 2026-07-26 13:58:49 -0400 | 23 | `bbb27bb02aebbd872fb821e7ddd062c9d2586d77` | `heads/ai/codex/oss-ports-baseline` | feat(engine): complete OSS runtime seams |
| 2026-07-26 14:36:25 -0400 | 25 | `ceb1f6e82bad9ddf52d1cffb4fb6292e547bafda` | `heads/ai/codex/boss-236-provider-credential-lifecycle` | feat(providerops): add credential lifecycle |
| 2026-07-26 17:20:40 -0400 | 4 | `d9659e3a5600fbc585deeb82032371ffb6a9dc39` | `heads/ai/codex/boss-250-branch-leases` | feat(github): add fenced branch leases (BOSS-250) |
| 2026-07-26 17:10:41 -0400 | 3 | `f41ab635e858e2803b99a27a6c4dbe9d2dee8667` | `heads/ai/codex/boss-248-249-branch-policy-snapshots` | feat(github): add branch policy and topology snapshots (BOSS-248 BOSS-249) |
| 2026-07-27 07:24:07 -0400 | 40 | `68b5574b58d29759df23f46b82a25d7faaeae1c2` | `heads/ai/codex/release-v0.9.1` | release: ship BOSS v0.9.1 budget settlement patch (BOSS-282) |
| 2026-07-27 06:42:49 -0400 | 36 | `946a6a7f021d06d9f0ed19cfdc7159a0354bcf99` | `heads/ai/codex/release-v0.9.0` | Merge remote-tracking branch 'origin/main' into ai/codex/release-v0.9.0 |
| 2026-07-28 06:52:38 +0530 | 1 | `c81879720ebbe36841398c939878fb1825f1ca25` | `origin/fix/health-endpoint-resilience` | fix(api): make /health starvation-proof + give healthchecks a start_period |
| 2026-08-08 10:13:23 -0400 | 14 | `109f36e37895d4639d607629c82608186866af32` | `heads/ai/codex/ga-budget-pressure-contract` | test(pipeline): align budget pressure with profiles |
| 2026-08-08 09:07:37 -0400 | 6 | `153fbfcfad7236c7e9854a2f14af2233099e1e20` | `heads/ai/codex/ga-deployment-target-secret-contract` | fix(api): preserve deployment secret-ref contracts |
| 2026-08-08 20:47:41 -0400 | 77 | `200d2e754b3af284bd993e46316b2bae80e737a4` | `heads/ai/codex/unified-model-gateway` | Merge remote-tracking branch 'origin/main' into ai/codex/unified-model-gateway |
| 2026-08-08 17:16:30 -0400 | 70 | `32d6d9d9e17ed9c646b0fb4052c6fc61d6fc012b` | `heads/ai/codex/knowledge-work-plane` | release: prepare v0.10.0 |
| 2026-08-08 11:03:21 -0400 | 22 | `38e46deb5ed1ca96aa5e1e94eb2e194b1822b8be` | `heads/ai/codex/boss-289-e2e-video` | docs(marketing): add governed sample project recording kit |
| 2026-08-08 12:11:59 -0400 | 34 | `3d4acd86cbc9716d1847736d4557a4203af37c8d` | `heads/ai/codex/ga-readiness-evidence-refresh` | docs(ga): refresh current readiness handoff |
| 2026-08-08 17:50:49 -0400 | 71 | `8a68a2ca4f88831b2b72e1f61e3e60f4dc2dc928` | `heads/ai/codex/model-agnostic-agent-toolchain` | feat(toolchain): add sanitized model-agnostic intake |
| 2026-08-08 17:06:01 -0400 | 42 | `96aaf3a6399599d09ab4fc764d964216651287f3` | `heads/ai/codex/qa-benchmark-apps` | feat(qa): run durable benchmark tuning sweep |
| 2026-08-08 13:19:20 -0400 | 38 | `a43f6f1eba1c9e591b0f63d6738acbc30b530551` | `heads/ai/codex/ga-engine-dependency-security` | fix(security): remediate engine auth dependency advisories |
| 2026-08-08 11:06:52 -0400 | 25 | `aa0ebfccbc3cef8d250e64bea9a5f67fb3a5a27a` | `heads/ai/codex/boss-293-public-flip-hygiene` | docs(engine): add public release readiness kit |
| 2026-08-08 11:23:11 -0400 | 32 | `b1019903dd62c8a07e6f696823cc1db3c3728d42` | `heads/ai/codex/ga-security-integration` | test(secrets): remove unused fixture variable |
| 2026-08-08 08:44:45 -0400 | 2 | `dba69840c5d116e84cf1af5d2556415de50f52b1` | `heads/ai/codex/boss-312-313-web-baseline-rebased` | fix(web): restore AgentOrb SSR and current design contracts (BOSS-312 BOSS-313) |
| 2026-08-08 09:29:42 -0400 | 12 | `df23a1cfabf9b2bd3eafb50684fe3a3c491f79a7` | `heads/ai/codex/ga-deploy-ssh-safety` | fix(deploy): keep SSH transfer shell-safe |
| 2026-08-08 12:16:05 -0400 | 36 | `e60022fd27145d85b14d57f4be8269719ef7def7` | `heads/ai/codex/ga-doc-link-portability` | docs(ga): make implementation links portable |
| 2026-08-08 20:44:49 -0400 | 74 | `ed0f9f8191ad94811a28434f6d6078a0b4ea0d20` | `heads/ai/codex/autonomous-improvement-loop` | Merge remote-tracking branch 'origin/main' into ai/codex/autonomous-improvement-loop |
| 2026-08-11 21:57:10 -0400 | 157 | `ce92c790f53fa72567de2fd5e23f31695c66f7d1` | `heads/ai/grok/community-byok-self-infra` | feat(edition): community BYOK self-managed infra (full ProviderOps kernel) |
| 2026-08-11 21:14:55 -0400 | 137 | `fde0dd48653f21e94ea3d7ecacc065171eff8ce7` | `heads/ai/grok/community-provider-keys` | docs(ops): E2E completion plan for dogfood vertical slice and bug backlog |
| 2026-08-12 17:41:04 -0400 | 189 | `0aeb88fc50e83961c2ebd07cd36592eb8ab17f3c` | `heads/ai/grok/api-e2e-completeness` | feat(api): llm-calls list/summary operator ledger over llm_calls |
| 2026-08-12 10:00:10 -0400 | 165 | `130cff76eccd8d6d2eee012cf8113fa305437752` | `heads/ai/codex/boss-352-trust-evidence` | feat(autonomy): add auditable trust evidence ledger |
| 2026-08-12 14:01:45 -0400 | 186 | `228b1c48b9204018ece32c856fb03d034c11baf7` | `heads/ai/codex/boss-cloud-product-distinction` | merge(main): integrate worker registry with multi-cloud and Goldeneye |
| 2026-08-12 09:57:03 -0400 | 164 | `4727de7e8f73a11eef599e93a5c0e6ef7dabc2be` | `heads/ai/codex/boss-353-work-scoring` | Merge pull request #161 from seanheiney/ai/codex/boss-349-openapi-parity |
| 2026-08-12 18:48:47 -0400 | 201 | `52432cca95a9fd9425b3c03afe1bb8510b482f77` | `heads/ai/grok/agent-kms` | feat(kms): BOSS-wide agent key management with OSS vault backends |
| 2026-08-12 14:23:42 -0400 | 187 | `5f2c5d826f7cd2cbba8fd12c39a4df5fef2bcf3d` | `heads/ai/grok/depth-previous-work` | feat: deepen multi-cloud, FinOps, topology, SLOs, knowledge, and Goldeneye |
| 2026-08-12 10:54:53 -0400 | 165 | `73fa0523a2bab9fbcd16dcf46bbdcf957ff1f463` | `heads/ai/codex/boss-354-admission` | feat(autonomy): add budget-aware work admission gate |
| 2026-08-12 09:56:41 -0400 | 163 | `7e98bd9cc796ae4ddc009f5acb7a811a1a63b430` | `heads/ai/codex/boss-349-openapi-parity` | fix(openapi): BOSS-349 schema parity and lifecycle route coverage |
| 2026-08-12 18:42:01 -0400 | 200 | `8507925161fd76614d5effb35c75473c11237a9b` | `heads/ai/grok/plane-open-issues` | feat(providers): PRV-10 conformance report API + area-data loaders + Azure TF |
| 2026-08-12 20:13:56 -0400 | 202 | `90e9206bd65dfea1c7aaaabe0a23644c3c413260` | `heads/ai/grok/community-sso` | feat(edition): self-hosted SSO (OIDC/SAML/SCIM) available on Community |
| 2026-08-12 18:04:23 -0400 | 194 | `b15262484075f06d32863d91b01da5c77a0d8b12` | `heads/ai/grok/platform-ops-epic` | feat(platform-ops): identity, on-call/SRE, CI/CD, security, release, scale, tokens, obs |
| 2026-08-12 10:56:28 -0400 | 165 | `cc58f766a907de2295090e6166e1a5cd97d61920` | `heads/ai/codex/boss-360-worker-registry` | feat(worker): add durable capability registry |
| 2026-08-12 10:17:03 -0400 | 165 | `d4029f7010249565452c5c76af73f910c2ee8bf0` | `heads/ai/codex/boss-361-completion-contracts` | feat(worker): require proof for contracted completions |
| 2026-08-14 13:48:59 -0400 | 254 | `e5c93a7ca8c8c4981c9b582d94c0d0506f29422b` | `heads/ai/grok/community-ga` | release: prepare BOSS v0.12.0 |
| 2026-08-14 13:47:11 -0400 | 252 | `eeb14bf3d7ae409187b0ca2062fe907cc9388f95` | `heads/ai/grok/boss-292-design-partner-onboarding` | merge(main): resolve BOSS-292 onboarding onto Community GA main |
| 2026-08-15 12:08:40 -0400 | 273 | `1024be0d87b843868e13f36aeac3a1964fb24447` | `heads/ai/grok/boss-98-sandbox-go-harden` | feat(sandbox): fail-closed Go S1 scaffold without unfreezing Firecracker |
| 2026-08-15 12:10:14 -0400 | 273 | `5823e970c36b6256bac7ece5b599ba8544b653d5` | `heads/ai/grok/boss-102-go-dispatcher` | feat(s3): add optional Go webhook fan-out dispatcher (BOSS-102) |
| 2026-08-15 13:26:23 -0400 | 287 | `6370178dcd29c7809c0cba2fbe35d65ba6a1f212` | `heads/ai/grok/official-app-scoped-verify` | fix(factory): scope official verify to the app, not the sandbox monorepo |
| 2026-08-15 12:59:23 -0400 | 285 | `71c9e070fb879cd57e4b3db7518d4d25b319a0b2` | `heads/ai/grok/fix-official-pr-retries` | fix(factory): re-reserve official PR retries and expose honest ui/ux |
| 2026-08-15 13:35:08 -0400 | 289 | `a5c8951315f0339c7115c1a3463d9d32c3d91358` | `heads/ai/grok/v1-feature-verify` | docs(v1): add evidence-only feature verification ledger |
| 2026-08-15 12:09:05 -0400 | 273 | `a6da073e2bd679dddca7f066c1bd1ea7cd35dfd6` | `heads/ai/grok/boss-97-go-scale-seam` | feat(runtime): lock the Python⇄Go scale seam without a Go runtime |
| 2026-08-15 12:06:59 -0400 | 274 | `b49e5b0a4b624bde35190b45c3842e5ab60959e5` | `heads/ai/grok/boss-101-go-gateway` | fix(s2): keep make test-go on the tenant gateway only |
| 2026-08-15 11:38:49 -0400 | 272 | `bd4cc36aa6e35cceb11607291d7dfedfc78c1ef1` | `heads/ai/grok/release-v0.13.0` | release: prepare BOSS v0.13.0 |
| 2026-08-15 11:02:18 -0400 | 270 | `c29e7e72e90239ae2726d03df65ba16737c71616` | `heads/ai/grok/next-five-operator-slices` | feat(ops): five operator SoR slices for decisions, receipts, QA, jobs |
| 2026-08-15 13:12:23 -0400 | 286 | `c9fb08732da86a3c948abdfa4a3d83af2d775633` | `heads/ai/grok/fix-official-report-evidence` | fix(qa): read factory verify evidence and stop monorepo score inflation |
| 2026-08-15 16:00:30 -0400 | 346 | `d8069168cc85a88f4551edc8ff4c7361ebc19519` | `heads/ai/grok/closeout-engine-failover`<br>`heads/ai/grok/closeout-half-baked`<br>`heads/ai/grok/closeout-v1-lock-image`<br>`heads/main` | release: cut v0.14.0 (AI SRE + v1 lock + honest empty-200) |
| 2026-08-15 12:08:36 -0400 | 273 | `dc0b7472d5b93474f78c5878a0c0bb138473a401` | `heads/ai/grok/boss-381-official-reports` | fix(factory): keep official PRs alive when the review packet overflows |
| 2026-08-15 12:13:33 -0400 | 283 | `e22b2415108f0c7d207f7885e28c6d12be553ddc` | `heads/ai/grok/swarm-scale-qa` | docs(ai): record scale/QA swarm integration |
| 2026-08-15 13:32:35 -0400 | 288 | `e35e7114830def35948823866ea3c249ed17760c` | `heads/ai/grok/ai-sre-kernel` | feat(sre): add honest AI SRE capability ledger from awesome-ai-sre |
| 2026-09-01 05:08:10 +0000 | 1 | `f69a023ac8ace802e14b6139267b0cceffa2aa3e` | `origin/cursor/critical-bug-investigation-2d8b` | fix: a completed build with no PR is not a reviewer refusal |
| 2026-09-04 05:07:39 +0000 | 1 | `95bdb3b4e5e2e45741680a0eae2a2801032d94a7` | `origin/cursor/critical-bug-investigation-c6d6` | fix: the follow-up guard asked for a column that does not exist |
| 2026-09-11 05:06:40 +0000 | 1 | `7878bf3c9fe0dbf1fc8fe7f664bf9aba3391b804` | `origin/cursor/critical-bug-investigation-b153` | fix: build on research must start Standard Product Delivery by key |
| 2026-09-14 05:07:40 +0000 | 1 | `15c1c295424b498609242afd1d6b5c5e74077e8d` | `origin/cursor/critical-bug-investigation-cc1e` | fix: draft_workflow must not become the next Build |
| 2026-09-20 05:09:16 +0000 | 1 | `1dd4e1b14173315f4ae9b3fa5f907ef5f7b764d1` | `origin/cursor/critical-bug-investigation-ead9` | fix: governed web verify must run the whole target, not just install |
| 2026-09-21 05:18:17 +0000 | 1 | `688c526e7d2979fa39389f87dfb98d72cb47c26d` | `origin/cursor/critical-bug-investigation-0aa9` | fix: a security check that never ran must not ship as a verdict |
| 2026-09-22 05:26:58 +0000 | 1 | `74b6f2906f72e2e201d561014f735c9846937ab4` | `origin/cursor/critical-bug-investigation-5650` | A readiness repair must finish before the run previews and ships it |
| 2026-09-24 05:16:04 +0000 | 1 | `2488b5cc3d08f010ece9e460ff6e9dabcead7a8f` | `origin/cursor/critical-bug-investigation-e22e` | A direct media link must not fetch the machine's own network |
| 2026-09-24 14:38:19 -0400 | 2 | `54c9c6461ea57ae49e3e4abc4c6a357cc7f3887e` | `heads/ai/codex/community-openapi-cloud-routes`<br>`origin/ai/codex/community-openapi-cloud-routes` | BOSS-293: align Community OpenAPI with Cloud route boundary |
| 2026-09-24 14:42:48 -0400 | 4 | `57218045ed80e33648c5820082cad7651372a629` | `heads/ai/codex/community-openapi-mobile-template`<br>`origin/ai/codex/community-openapi-mobile-template` | BOSS-293: document mobile template routes in both API contracts |
| 2026-09-24 14:12:42 -0400 | 2 | `a4be57a9ad7d459adfb880fd30ef09bf45f09e0a` | `heads/ai/codex/community-db-export`<br>`origin/ai/codex/community-db-export` | BOSS-293: pin Community schema export remap |
| 2026-09-24 14:55:33 -0400 | 2 | `d51ab453ac4427a3de39c770af79698792e319d9` | `heads/ai/codex/community-final-test-boundary` | BOSS-293 split Community ProviderOps feed coverage |
| 2026-09-25 05:17:27 +0000 | 1 | `899a22f5ec33270fdfe30d293c2032e3c104e594` | `origin/cursor/critical-bug-investigation-f318` | Start and fork the workflow version that is live |
| 2026-09-26 10:40:17 -0400 | 5 | `2d89d9e66e73d61eb140f97cf9f89d1f52967d24` | `heads/ai/codex/community-vm-installer` | docs(installer): specify trusted HTTPS console proxy contract |
| 2026-09-26 10:40:17 -0400 | 1 | `413b56819ccfff3597aeff7c676c648993df2750` | `heads/cursor/critical-bug-investigation-84c0` | Re-running the installer must not mint a new secret-store key |
| 2026-09-26 10:40:41 -0400 | 17 | `47db6bc57087ce77051faac295402059eb07abb4` | `heads/ai/codex/community-installer-lifecycle` | BOSS-428 Point installer output to public Community docs |
| 2026-09-26 10:38:23 -0400 | 3 | `79a41435a1155e125dbf7d4f89847957804b5fb8` | `heads/ai/codex/community-feedback-public` | BOSS-431 Make public feedback docs self-contained |
| 2026-09-26 10:38:51 -0400 | 1 | `a78b57005615c9cfee2a4613155b0ef6c7a5c14f` | `heads/ai/codex/public-secret-hygiene` | Remove tracked environment backup before public release |
| 2026-09-26 10:38:51 -0400 | 1 | `a924854463750f8abbcfc6018906ae38accaf09f` | `heads/ai/codex/web-proxy-session-boundary` | Gate Community console proxy behind owner session |
| 2026-09-26 10:38:49 -0400 | 2 | `bc0cdaf6518bb9cb98d7cda0b2983cc76c45d95f` | `heads/ai/codex/tui-api-parity` | BOSS-416 Point terminal users to runtime API schema |
| 2026-09-26 10:38:31 -0400 | 3 | `fed70ca28b23326a96a5cb3b19541e2d53e08298` | `heads/ai/codex/community-mcp` | fix(mcp): authenticate non-loopback read transport |
| 2026-09-28 15:32:18 -0400 | 1 | `01579d5918b95a809d88c6e4fb8ebbf548145a59` | `heads/ai/codex/boss-448-worker-compose-map` | fix(community): honor preserved API principal map in Compose |
| 2026-09-28 13:54:59 -0400 | 2 | `03798eda1181ba6b7eeeaf27c040891643859c06` | `heads/ai/codex/boss-293-integrated-export-pins` | BOSS-293 refresh reviewed Community database schema |
| 2026-09-28 13:56:52 -0400 | 2 | `07744dce832b17a014cfd1c10a8c0a2aca045ac3` | `heads/ai/codex/boss-434-security-integration` | fix(web): harden BOSS-434 redirects and origin checks |
| 2026-09-28 14:52:24 -0400 | 1 | `0af61fca6560369c48c7c631e841d21061fb0256` | `heads/ai/codex/boss-448-mobile-build-priority` | BOSS-448 lower priority of local mobile device tools |
| 2026-09-28 18:46:01 -0400 | 1 | `155dbad9be25064647958e141cb799d2870bff38` | `heads/ai/codex/boss-429-deployment-effect-guard` | BOSS-429 fail closed on ungoverned deployment connector execution |
| 2026-09-28 14:52:11 -0400 | 1 | `16be0221227ed2b805dca1d05764a94108f54d11` | `heads/ai/codex/boss-448-deploy-await-guard` | Test await guards through settled behavior after runner refactor |
| 2026-09-28 19:36:32 -0400 | 1 | `16ee82221e6bf0b62958319fcb71af066f988d47` | `heads/ai/codex/boss-448-legacy-shell-env` | BOSS-448 Keep model credentials out of legacy console shell |
| 2026-09-28 21:43:48 -0400 | 1 | `18927d858624cc931dd4be49725e8398ccae9a91` | `heads/ai/codex/boss-454-api-log-retention`<br>`origin/ai/codex/boss-454-api-log-retention` | ops(goldeneye): retain API logs across container recreation (BOSS-454) |
| 2026-09-28 21:56:39 -0400 | 4 | `1ae20cf4810811efe1a9e2eb8d3838bc17825da4` | `heads/ai/codex/boss-api-5xx-deploy-fanout`<br>`origin/ai/codex/boss-api-5xx-deploy-fanout` | test(web): exempt server deployment deadline from hydration scan |
| 2026-09-28 19:43:29 -0400 | 1 | `1e62029ed3f1946f6a88df16fe64d06f746b1603` | `heads/ai/codex/boss-448-cursor-test-fixture` | test(BOSS-448): align direct deployment tests with governance hold |
| 2026-09-28 22:40:26 -0400 | 1 | `21d26cfc2db84cd14b87fc4f49fd3f79bab07383` | `heads/ai/codex/boss-300-notice-attribution`<br>`origin/ai/codex/boss-300-notice-attribution` | docs(community): retain pinned third-party source notices |
| 2026-09-28 19:24:53 -0400 | 1 | `22195ca6eb827090a6f3398e97171922b23a3004` | `heads/ai/codex/boss-436-model-onboarding` | BOSS-436 Record Community coding subscriptions without live proof |
| 2026-09-28 22:56:30 -0400 | 1 | `25be6d312bee5b5c39dc1b4dfc310fc9d35dbf60` | `heads/ai/codex/boss-sample-agent-acceptance`<br>`origin/ai/codex/boss-sample-agent-acceptance` | fix(examples): require complete newsletter schedule inputs (BOSS-456) |
| 2026-09-28 15:12:05 -0400 | 2 | `2a3660a363ae6306f8596fa325c7a0efb6d39cdb` | `heads/ai/codex/boss-300-image-license` | BOSS-300 distinguish missing notice files from metadata gaps |
| 2026-09-28 21:42:52 -0400 | 1 | `2c4759c50c107c995d7fc9ac9a07afbcc1979991` | `heads/ai/codex/boss-453-workflow-plan-inputs`<br>`origin/ai/codex/boss-453-workflow-plan-inputs` | fix(workflows): render custom input objects in plan (BOSS-453) |
| 2026-09-28 16:32:51 -0400 | 1 | `2ecc1b4517b848d65d3c6e0b67154c46d6b8e885` | `heads/ai/codex/boss-sandbox-write-hardening` | fix(api): close ungoverned sandbox host writes |
| 2026-09-28 17:35:15 -0400 | 3 | `2ee2f9fa9a7741a54825554178d7df2c45abb169` | `heads/ai/codex/community-install-startup-order` | fix(install): activate secrets profile for bootstrap exec |
| 2026-09-28 15:24:14 -0400 | 4 | `3015347f44cb11f3b24cc112b13e3b8a3f9ec2c7` | `heads/ai/codex/boss-448-ga-auth-final` | BOSS-448 Require bound API and configured worker principals |
| 2026-09-28 14:59:54 -0400 | 1 | `33dfd64f264ef251472465c6cf086bb8b82226a5` | `heads/ai/codex/boss-448-env-assertion-triage` | Test sandbox caches and Claude token precedence by behavior |
| 2026-09-28 19:04:55 -0400 | 1 | `3479284ba2c5ffb53f33e5d88075fd4f4be00d3c` | `heads/ai/codex/boss-429-mcp-policy-parity` | Harden MCP project authority and audit operator-only actions |
| 2026-09-28 14:16:49 -0400 | 1 | `35666d056f9c0398a832a997564c258398ac52de` | `heads/ai/codex/boss-293-api-ga-gates` | BOSS-447 govern Community API grants schedules and SMTP tests |
| 2026-09-28 19:28:20 -0400 | 2 | `36e05bb62f4ac9888388cb8f908aec5e7f010b9e` | `heads/ai/codex/paramg-pr-reconcile` | feat(samples): reconcile Paramg dependency freshness app and preview gate test |
| 2026-09-28 13:49:19 -0400 | 1 | `38866a2c6cfdd137353fe807400a090fbf73c3d2` | `heads/ai/codex/boss-314-mkdocs-links` | BOSS-314 repair strict MkDocs source links |
| 2026-09-28 22:22:27 -0400 | 1 | `3a7f60260e0364bd3499c285be3ceb7cd027c0af` | `heads/ai/codex/boss-429-mcp-human-attribution`<br>`origin/ai/codex/boss-429-mcp-human-attribution` | fix(api): enforce v1 auth behind proxy root path |
| 2026-09-28 18:48:44 -0400 | 1 | `413a3df179e9cff7c25cddc7a2f343b9a59cc572` | `heads/ai/codex/boss-429-deploy-ui` | fix(web): show deployment ChangeSet review before execution |
| 2026-09-28 22:21:29 -0400 | 2 | `43dbf2307d48ed793653da1178213fd0646147d9` | `heads/ai/codex/boss-452-api-admission`<br>`origin/ai/codex/boss-452-api-admission` | fix(api): admit deployment reads before threadpool (BOSS-452) |
| 2026-09-28 21:15:43 -0400 | 1 | `4a0b85032e12d2bd5a2ab4d3120e34d47bcaada4` | `heads/ai/codex/boss-430-tui-usability`<br>`origin/ai/codex/boss-430-tui-usability` | feat(tui): add direct project task commands (BOSS-430) |
| 2026-09-28 22:52:07 -0400 | 1 | `4bc0a66d0c0aec1e5e8b8efee2a86a8484ed8df5` | `heads/ai/codex/boss-430-tui-decision-inbox`<br>`origin/ai/codex/boss-430-tui-decision-inbox` | feat(tui): add direct decision review commands (BOSS-430) |
| 2026-09-28 15:22:14 -0400 | 1 | `4effc92fe64bdb0459195f519317f12e91469228` | `heads/ai/codex/boss-448-kms-write-authority` | BOSS-448 require project authority for Community KMS routes |
| 2026-09-28 14:59:32 -0400 | 1 | `511d7b5b14e58c8059829e8004e1b1592bf95b6a` | `heads/ai/codex/boss-448-custom-step-grants` | BOSS-448 exercise custom-step grant with real settings |
| 2026-09-28 18:52:49 -0400 | 2 | `5491e1a423c3dd5feef0a2710a7b06563ad2d9d2` | `heads/ai/codex/boss-416-runtime-inputs-2` | BOSS-429 keep first and merge deployments as governed plans |
| 2026-09-28 14:06:14 -0400 | 3 | `54c97268305dab80ae3899c82c87fd3ac3d82062` | `heads/ai/codex/boss-443-445-workflow-integration` | Start one can see the inputs a workflow declared |
| 2026-09-28 19:18:30 -0400 | 1 | `5852cef0115139bec969ff67d73d7efa4bdc19b9` | `heads/ai/codex/boss-typed-decision-governance` | BOSS-451 Hold ungoverned typed-decision model spend |
| 2026-09-28 19:50:19 -0400 | 2 | `5b3c6894f87834077991b6d159fde17ba7fe96eb` | `heads/ai/codex/boss-448-raw-sql-tenant-hold` | fix: confine raw SQL to explicit local admin CLI calls |
| 2026-09-28 22:50:00 -0400 | 1 | `5f465e494afb78f045f4312ea45def811c5d39be` | `heads/ai/codex/boss-455-claude-cli-login`<br>`origin/ai/codex/boss-455-claude-cli-login` | feat(community): add CLI-owned Claude Code login path |
| 2026-09-28 16:49:20 -0400 | 1 | `64b6ae59edbc43ad3224c245a42801ed172b664c` | `heads/ai/codex/boss-community-delivery-workflow-authority` | fix(api): authorize delivery workflow controls |
| 2026-09-28 14:56:12 -0400 | 1 | `712c8fc2230c2beb6232f3bb29f90fe42f774a0b` | `heads/ai/codex/boss-448-oidc-receipt-triage` | Keep OIDC timeout and receipt ratchet tests current |
| 2026-09-28 15:15:07 -0400 | 2 | `779295a775efd9470b2a8883129f14a2f4ba4863` | `heads/ai/codex/boss-293-ga-copy-review` | BOSS-293 explain Docker group login after install |
| 2026-09-28 15:38:48 -0400 | 1 | `80642a662bc166eb95487076e834146e8e3c1878` | `heads/ai/codex/boss-448-investigation-run-authority` | fix(api): authorize investigation execution and results list |
| 2026-09-28 19:16:43 -0400 | 1 | `81514ee23a414ad059a5510b2baf528d97751684` | `heads/ai/codex/boss-300-dependency-notice-review` | Record interim Community dependency notice evidence |
| 2026-09-28 21:57:53 -0400 | 3 | `81ace76ede86a5457766ee6a5385db3a38c94426` | `heads/ai/codex/boss-429-mcp-authority-hardening`<br>`origin/ai/codex/boss-429-mcp-authority-hardening` | fix(BOSS-429): preserve revoked default members during bootstrap |
| 2026-09-28 15:23:51 -0400 | 1 | `85225a69c06deefbb0deb2360cfd0af4815a6ab7` | `heads/ai/codex/boss-448-preview-sandbox-auth` | fix(community): authorize preview sandbox routes and redact env |
| 2026-09-28 17:06:07 -0400 | 1 | `922b34597e9f5030f10f88d12ab6217475aa7ef5` | `heads/ai/codex/boss-449-host-fleet-gate` | fix: gate Community deployment-host fleet writes |
| 2026-09-28 14:47:51 -0400 | 1 | `932572385ff83f70040739251809818e752e9288` | `heads/ai/codex/boss-448-suite-followup` | BOSS-448 align retry and console assertions with current behavior |
| 2026-09-28 18:58:38 -0400 | 1 | `96ccb86365bd46a56dcbb85751ab24c592089e25` | `heads/ai/codex/boss-429-deploy-controls` | fix(web): show gated deployment plans across app controls |
| 2026-09-28 19:30:06 -0400 | 2 | `9c26660a536b71b83894857015b07f66465a606e` | `heads/ai/codex/boss-436-installer-lifecycle` | BOSS-436 Preserve installer choices and report model setup honestly |
| 2026-09-28 16:41:13 -0400 | 1 | `a32eda1e162c20889d1bf1ee2a92c772431288ae` | `heads/ai/codex/boss-community-object-read-authority` | fix(api): authorize object detail reads by stored project |
| 2026-09-28 16:35:23 -0400 | 1 | `a67e9f521386371412725e20b106ab18f1ff3d12` | `heads/ai/codex/boss-community-project-count-label` | fix(web): distinguish software project count from control project |
| 2026-09-28 14:48:22 -0400 | 1 | `a81764504a37ef58952608cd7fc9e8fc9f12a554` | `heads/ai/codex/boss-integration-fixture-triage` | Restore builder palette role choices after integration |
| 2026-09-28 22:13:55 -0400 | 5 | `a9fef8362c3da6e088eb025ac766851a65f4d96a` | `heads/ai/codex/boss-436-installer-safe-rerun`<br>`origin/ai/codex/boss-436-installer-safe-rerun` | docs(installer): fail closed on legacy recovery volume evidence |
| 2026-09-28 17:16:37 -0400 | 1 | `adf2d68381d25249cbeda22b05ff942f09552a6f` | `heads/ai/codex/community-agent-loop-shell-gate` | fix: pause legacy agent shell in Community |
| 2026-09-28 15:44:15 -0400 | 1 | `b56153828e127aac1257a2e56a2bb0f23eaffab6` | `heads/ai/codex/boss-448-agent-read-authority` | fix(api): authorize agent briefing and thread reads |
| 2026-09-28 18:17:39 -0400 | 2 | `b63cea51b3a1e52c9ebf9392ad75d0727e0dac10` | `heads/ai/codex/boss-438-first-run-workforce` | fix(first-run): separate agent work from role definitions |
| 2026-09-28 20:03:31 -0400 | 1 | `b807e6f64fefcc7dea8c0ee4a6c408179133a5cd` | `heads/ai/codex/boss-448-catalog-test-reconcile` | test(agent-studio): match checks to shipped catalog |
| 2026-09-28 20:31:12 -0400 | 4 | `c584ae4044682e5ffb7736887fca8e8ce0696f39` | `heads/ai/codex/boss-430-mcp-confirmation` | fix(mcp): hold decision approvals pending human attribution |
| 2026-09-28 22:43:58 -0400 | 1 | `c5d3fdb438093afeb60068b21daff329deb98af4` | `heads/ai/codex/boss-430-mcp-held-parity`<br>`origin/ai/codex/boss-430-mcp-held-parity` | feat(mcp): confirm project-local cleanup routes (BOSS-429) |
| 2026-09-28 14:37:02 -0400 | 1 | `c6b28a7c4a586c127a647a79c0256df3bc33c614` | `heads/ai/codex/boss-448-auth-security-integration` | BOSS-448 reconcile critical auth and credential guards |
| 2026-09-28 20:07:35 -0400 | 1 | `c89a2fa4133157006c6965a1eafd5d3325881db3` | `heads/ai/codex/boss-436-sudo-preflight` | Fix noninteractive sudo preflight for VM installer lifecycle |
| 2026-09-28 14:57:28 -0400 | 1 | `c97f6d8de51bd272c1b5d32fb414083307318e2f` | `heads/ai/codex/boss-448-skill-catalog-promotion` | BOSS-448 reconcile skill catalog and fork promotion tests |
| 2026-09-28 16:54:04 -0400 | 1 | `cd5bab0e8569441e86d78a62addc25984d0700a2` | `heads/ai/codex/boss-community-goldeneye-install-gate` | fix(community): block Goldeneye SSH package installs |
| 2026-09-28 18:30:23 -0400 | 1 | `d86b9cf62b0bd58097cf45e965141647b9767517` | `heads/ai/codex/boss-416-openapi-input-contract` | fix(api): sync private OpenAPI request inputs to mounted routes (BOSS-416) |
| 2026-09-28 05:09:50 +0000 | 1 | `d9f3dcff7ea330ba479001731cee5774810ddfb5` | `origin/cursor/critical-bug-investigation-c405` | A configured secret store was skipped, so channel credentials went into Postgres |
| 2026-09-28 14:03:03 -0400 | 1 | `da2506fa3924279d443cae13a604003155954850` | `heads/ai/codex/boss-434-deployment-profiles` | fix(deploy): wire console session gate across deployment profiles |
| 2026-09-28 14:33:54 -0400 | 1 | `dce222eed66a4b3c43e6f1982c9e3c7c0b501996` | `heads/ai/codex/boss-448-fleet-security` | BOSS-448 reconcile fleet and deployment security fixes |
| 2026-09-28 19:08:05 -0400 | 2 | `de69c19d118860b64c1273b74f06bdd3364d2da6` | `heads/ai/codex/boss-430-tui-full-surface` | BOSS-430 clarify terminal project and paging guidance |
| 2026-09-28 18:06:19 -0400 | 1 | `e513d859f6461316f1feb3e36c9755cdeff76788` | `heads/ai/codex/boss-416-api-contract-map` | fix(api): give every mounted operation a stable discoverable identity |
| 2026-09-28 18:29:42 -0400 | 1 | `e98ae7bbbc36a8a3fb0fe0ff6b742bc096685774` | `heads/ai/codex/boss-416-coordination-api` | feat(api): expose project coordination snapshot (BOSS-416) |
| 2026-09-28 18:43:24 -0400 | 1 | `eb3fdd20e60b2b3b1d4edd914327d5facb5f6c21` | `heads/ai/codex/boss-429-mcp-schema-overlay` | BOSS-429: describe curated Community MCP request fields |
| 2026-09-28 19:01:16 -0400 | 1 | `efc5fa4f40a5ec88275878be21cb35915143c3b9` | `heads/ai/codex/boss-416-runtime-inputs-wave2` | BOSS-416 type deployment and provider request bodies for terminal clients |
| 2026-09-28 15:12:45 -0400 | 2 | `f28b1b39c398323c2cbfe0e4cb03a3b50f7ba522` | `heads/ai/codex/community-makefile-docker-access` | fix(community): align Playwright browser and pin coding CLIs |
| 2026-09-28 18:20:59 -0400 | 4 | `fdfc4ee4913906dcec6bfbea4979aeccc5e5eb4b` | `heads/ai/codex/boss-430-tui-full-product` | fix(tui): redact KMS key value echoes (BOSS-430) |
| 2026-09-29 05:08:03 +0000 | 1 | `a3148c12d317c18c0caf8738667e0d020dbff3b9` | `origin/cursor/critical-bug-investigation-3dc0` | Deleting a finished subworkflow left its parent waiting forever |

