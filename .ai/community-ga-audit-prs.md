# Community GA audit: open PRs against v1.0-rc1

Report only. No PR was merged, closed, commented on, or pushed.
GitHub `mergeable` was not treated as a test result and was not used for disposition.

## Base

| Item | Value |
| --- | --- |
| Tag | `v1.0-rc1` (annotated; local) |
| Tag object | `04bb2e79f84acb1c3d7074f1617f4c6b37aa452d` |
| Commit (`v1.0-rc1^{}`) | `b194c65c3028b80cd621fc187280f4d79e84df02` |
| Tree | `18cc3354852e5fb1e5204bbde4ba0a5f482ab762` |
| Commit time | 2026-09-29 11:43:41 -0400 (2026-09-29 15:43:41Z) |
| Subject | release: name the community candidate v1.0-rc1 |
| Open PRs | 103 (`gh pr list --repo seanheiney/boss --state open --limit 200`, confirmed with paginated `GET /repos/seanheiney/boss/pulls?state=open`) |
| Draft / ready | 100 draft, 3 ready (#174, #191, #224), all state OPEN |
| Authors | seanheiney 60, app/cursor 41, paramg 2 |

Every listed head was fetched as `refs/pull/N/head` and matched GitHub `headRefOid`.
No open PR was updated after the tag (latest update is #280 at 2026-09-29T14:09:04Z).

## Method

For each open PR head:

1. `git merge-base --is-ancestor <head> v1.0-rc1^{}`. An ancestor is `already contained`.
2. Otherwise `git cherry v1.0-rc1^{} <head>`. Every commit marked `-` and none marked `+` is `patch-equivalent` (same patch-id already in the candidate, even when a later merge-tree conflicts).
3. Otherwise `git merge-tree --write-tree v1.0-rc1^{} <head>`. A clean merge whose tree equals the candidate tree is `superseded` (unique patch-ids, zero net tree change).
4. A conflict is not `needs port` by itself. Those heads were compared to the candidate: distinctive added lines, then the current behavior (shell environment, SQL guards, deploy auth, registry confirmation, setup URL checks, Community export pins). Where rc1 already has the fix, usually rewritten, the disposition is `superseded`.
5. `#207` is `held` even though its patch is absent. See the callout. It is not a port candidate.

Dispositions used: `already contained`, `patch-equivalent`, `superseded`, `held`, `needs port`, `unknown`.

## Counts

| Disposition | Count |
| --- | ---: |
| already contained | 41 |
| patch-equivalent | 26 |
| superseded | 35 |
| held | 1 |
| needs port | 0 |
| unknown | 0 |
| **open PRs** | **103** |

needs port: none

## Callouts

### #174 — superseded

Ready PR by paramg, base `main`, head `24bd9341a1e0f5ca98a6ca1510bf0e6e90b16779`, not an ancestor (ahead 6, behind 1447). `git cherry` is `+6/-0`. `git merge-tree --write-tree` is clean and the result tree equals v1.0-rc1. The sealed-e2e canary commits add nothing the candidate does not already have.

### #191 — superseded

Ready PR by paramg, base `main`, head `5d07c89440bf02268148e65febf34a4224b811c5`, not an ancestor (ahead 22, behind 954). `git cherry` is `+22/-0`. Merge-tree conflicts (add/add) on `apps/dep_freshness/*`, `apps/api/tests/test_preview_approval_gate.py`, and `package.json`. 58 of 60 sampled distinctive added lines are already in v1.0-rc1. This is not a clean port, and the verify/dep-freshness work is already in the candidate in later form.

### #207 — held

Draft by app/cursor, base `main`, head `832b49b112fe6c3f428dbee64d774ed5fe59b084`, not an ancestor (ahead 1, behind 677). `git cherry` is `+1/-0`. Merge-tree conflicts in `engine/boss_engine/services/claude_cli_adapter.py` and `engine/boss_engine/services/code_agent.py`.

The commit `832b49b1` (`fix: a wizard-stored Claude token must reach the driver that builds`) injects `settings.claude_code_oauth_token` / the secret-store value into `ClaudeCodeAgent` and `ClaudeCliAdapter` as `CLAUDE_CODE_OAUTH_TOKEN`. That behavior is not in v1.0-rc1.

Provider/counsel clearance is not in the repo. v1.0-rc1 says the opposite:

- `SETUP.md` states Claude setup-token import is disabled and that no provider or counsel clearance for copying a Claude session token into BOSS is recorded. Counsel reference `01049940` covers first-party Apache licensing only.
- `docs/community/MODEL_CONNECTIONS.md` marks legacy token import disabled for the same reason and points at CLI-owned `claude auth login` plus `--connect-claude-local`.
- `install.sh` rejects `--connect-claude`.

Do not port #207.

### #280 — already contained

Draft by seanheiney, base `main`, head `371a1c72a1fd49f3336f5790b55407d51886412a` (`docs: hand off Community GA completion to Grok`). The head is an ancestor of v1.0-rc1 (ahead 0, behind 18). Updated 2026-09-29T14:09:04Z, before the tag. Nothing on this PR is missing from the candidate.

### #284 — patch-equivalent

Draft by seanheiney, base `main`, head `1ae20cf4810811efe1a9e2eb8d3838bc17825da4`, not an ancestor (ahead 4, behind 50). `git cherry` is `+0/-4`. Merge-tree is clean and the tree equals v1.0-rc1. BOSS-452 deployment lookup fan-out is already in the candidate as the same patches.

### #286 — patch-equivalent

Draft by seanheiney, base `main`, head `2c4759c50c107c995d7fc9ac9a07afbcc1979991`, not an ancestor (ahead 1, behind 50). `git cherry` is `+0/-1`. Merge-tree is clean and the tree equals v1.0-rc1. The BOSS-453 workflow plan fix is already in the candidate.

### #287 — patch-equivalent

Draft by seanheiney, base `main`, head `18927d858624cc931dd4be49725e8398ccae9a91`, not an ancestor (ahead 1, behind 50). `git cherry` is `+0/-1`. Merge-tree is clean and the tree equals v1.0-rc1. The BOSS-454 Goldeneye API log retention change is already in the candidate.

### #288 — patch-equivalent

Draft by seanheiney, base `main`, head `43dbf2307d48ed793653da1178213fd0646147d9`, not an ancestor (ahead 2, behind 39). `git cherry` is `+0/-2`. Merge-tree is clean and the tree equals v1.0-rc1. The BOSS-452 direct deployment-read bounds are already in the candidate.

### #294 — patch-equivalent

Draft by app/cursor, base `main`, head `a3148c12d317c18c0caf8738667e0d020dbff3b9`, not an ancestor (ahead 1, behind 443). `git cherry` is `+0/-1` for `Deleting a finished subworkflow left its parent waiting forever`. Merge-tree is clean and the tree equals v1.0-rc1. The same change is already in the candidate (it is also in the history of #280, which is an ancestor).

## Opened or updated on or after 2026-09-28

Cutoff: `createdAt >= 2026-09-28` or `updatedAt >= 2026-09-28`. None of these need a port.

| PR | Created | Updated | Draft | Ancestor | Disposition |
| --- | --- | --- | --- | --- | --- |
| #277 | 2026-09-28T05:10:04Z | 2026-09-28T05:10:04Z | draft | no | patch-equivalent |
| #279 | 2026-09-28T18:13:54Z | 2026-09-28T18:19:13Z | draft | no | superseded |
| #280 | 2026-09-28T18:47:00Z | 2026-09-29T14:09:04Z | draft | yes | already contained |
| #281 | 2026-09-29T01:15:58Z | 2026-09-29T01:15:58Z | draft | no | patch-equivalent |
| #283 | 2026-09-29T01:29:46Z | 2026-09-29T02:14:14Z | draft | no | patch-equivalent |
| #284 | 2026-09-29T01:34:05Z | 2026-09-29T01:56:52Z | draft | no | patch-equivalent |
| #285 | 2026-09-29T01:34:41Z | 2026-09-29T01:58:08Z | draft | no | patch-equivalent |
| #286 | 2026-09-29T01:38:06Z | 2026-09-29T01:42:54Z | draft | no | patch-equivalent |
| #287 | 2026-09-29T01:44:01Z | 2026-09-29T01:44:01Z | draft | no | patch-equivalent |
| #288 | 2026-09-29T02:15:55Z | 2026-09-29T02:21:44Z | draft | no | patch-equivalent |
| #289 | 2026-09-29T02:41:47Z | 2026-09-29T02:41:47Z | draft | no | patch-equivalent |
| #290 | 2026-09-29T02:44:24Z | 2026-09-29T02:45:59Z | draft | no | patch-equivalent |
| #291 | 2026-09-29T02:50:24Z | 2026-09-29T02:50:24Z | draft | no | patch-equivalent |
| #292 | 2026-09-29T02:52:25Z | 2026-09-29T02:53:05Z | draft | no | patch-equivalent |
| #293 | 2026-09-29T02:57:03Z | 2026-09-29T02:59:48Z | draft | no | patch-equivalent |
| #294 | 2026-09-29T05:08:20Z | 2026-09-29T05:08:20Z | draft | no | patch-equivalent |

Titles:

- #277: Keep channel credentials in a configured secret store
- #279: BOSS-428/436: make Community checks work with sudo Docker access
- #280: BOSS-293/448 integrate Community export and release hardening
- #281: BOSS-430: direct task workflow in terminal
- #283: BOSS-436: back up pasted installer reruns before upgrade
- #284: fix(web): bound deployment lookup fan-out (BOSS-452)
- #285: BOSS-429: bind project mutations to authenticated actors
- #286: BOSS-453 fix fast delivery workflow plan 500
- #287: BOSS-454 preserve Goldeneye API tracebacks across recreation
- #288: fix(api): bound direct deployment reads (BOSS-452)
- #289: BOSS-300: retain versioned Community third-party notices
- #290: BOSS-429 confirm project-local MCP cleanup routes
- #291: BOSS-455 Add CLI-owned Claude Code sign-in for Community
- #292: BOSS-430: direct decision workflow in terminal
- #293: BOSS-456: make sample newsletter schedules runnable
- #294: Refuse deleting a finished run a waiting parent still needs

## Every open PR

| PR | Title | Author | Draft | State | Head | Base | Ancestor of v1.0-rc1 | Disposition | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| #174 | sealed-e2e-canary | paramg | ready | OPEN | `24bd9341a1e0f5ca98a6ca1510bf0e6e90b16779` | `main` | no | superseded | cherry +6/-0; merge-tree clean; result tree equals rc1 18cc3354852e |
| #176 | fix(agent): stop run_shell from inheriting DATABASE_URL | app/cursor | draft | OPEN | `37ea5d1b9d58fc9bd289d4264d4f424ea9de6450` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #177 | fix(agent): stop run_shell from inheriting DATABASE_URL | app/cursor | draft | OPEN | `87e8e13fd12a27993030822f1f9fccfb24b671d4` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #178 | fix(agent): stop run_shell from inheriting DATABASE_URL | app/cursor | draft | OPEN | `7392318418bb33b6a155c133f0405b1a86c5b6f1` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #179 | fix: fleet attach/deploy auth holes and agent shell leaks | app/cursor | draft | OPEN | `0a7f3f7916dfffe06dfaddbce274dcb1e312728e` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #180 | fix: cancel-path crash, tenant writes, and agent/fleet leaks | app/cursor | draft | OPEN | `30c964f56f0736fde461ae018eae82566943b317` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #181 | fix: first-deploy route collision, cancel crash, and remaining fleet/agent leaks | app/cursor | draft | OPEN | `2a3f22017d94ee962adfafd6a3d5c10211d67848` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #182 | fix: first-deploy route collision, cancel crash, and remaining fleet/agent leaks | app/cursor | draft | OPEN | `bd931349461832fbddcdab58cb68b61ffbb40838` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #183 | fix: first-deploy collision, cross-tenant unbind, and remaining fleet/agent leaks | app/cursor | draft | OPEN | `9ca4c841f9b150dd1562dc46159e31a91fdd902b` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #184 | fix: first deploy of a second app must not overwrite the first | app/cursor | draft | OPEN | `e72c7f305be7f3802fc318282306ea30f227c1d9` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #185 | fix: first-deploy health checks must actually probe the public URL | app/cursor | draft | OPEN | `bfa21c52ae0970fe8bedd0f95c665ea72c3a6802` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #186 | fix: refuse to bind a deployment host from another project | app/cursor | draft | OPEN | `4c8557c4f92858802db8dce857c40ca1a85dcff9` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #187 | fix: stop delivery workflows from deploying without a person | app/cursor | draft | OPEN | `c73982a62aa0d62822be9a4dc825acf3fe5a87e5` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #188 | fix: timeout-approve must still run the work, not skip it | app/cursor | draft | OPEN | `be5084d886a604cb02ee3807f02117de44f8b06b` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #189 | fix: a completed build with no PR is not a reviewer refusal | app/cursor | draft | OPEN | `f69a023ac8ace802e14b6139267b0cceffa2aa3e` | `main` | no | patch-equivalent | cherry +0/-1; merge-tree clean (tree=rc1) |
| #190 | fix: preview and meeting wait on the id the live tools return | app/cursor | draft | OPEN | `eb8e059ff9da2538d29a22b83074713b388a99a0` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #191 | [verify] approval releases the build | paramg | ready | OPEN | `5d07c89440bf02268148e65febf34a4224b811c5` | `main` | no | superseded | cherry +22/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #192 | fix: a model cannot confirm its own host command | app/cursor | draft | OPEN | `3bb6cacd0d3cfe8b3180622669393a1892cfefec` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #193 | fix: the follow-up guard asked for a column that does not exist | app/cursor | draft | OPEN | `95bdb3b4e5e2e45741680a0eae2a2801032d94a7` | `main` | no | patch-equivalent | cherry +0/-1; merge-tree clean (tree=rc1) |
| #194 | fix: a 529 after a write must not run the write again | app/cursor | draft | OPEN | `538d7df73592b2465c2d9cc4c60604010a96198c` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #195 | fix: chat must not approve a merge gate without a click | app/cursor | draft | OPEN | `72b3323ca008411d2a710809dc8e16a2412d6a9b` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #196 | fix: an investigation UPDATE whose text says "where" must not hit every row | app/cursor | draft | OPEN | `0042b429123742d1d93f4271c2bdb8dd35bf71ef` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #197 | fix: a WITH CTE must not let an investigation write without a WHERE | app/cursor | draft | OPEN | `bf3762b76bc74c864bbef83b91d21315e1d59a1b` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #198 | fix: a stalled chat turn must not treat the previous reply as this one | app/cursor | draft | OPEN | `6bb359bdac2c60643f2050324f1f5ab845dee676` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #199 | fix: clicking Build must start a build, not an ask | app/cursor | draft | OPEN | `383375f02cc62d7129644da7133d3d227057a778` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #200 | fix: build on research must start Standard Product Delivery by key | app/cursor | draft | OPEN | `7878bf3c9fe0dbf1fc8fe7f664bf9aba3391b804` | `main` | no | patch-equivalent | cherry +0/-1; merge-tree clean (tree=rc1) |
| #201 | fix: setup must not fetch a caller URL before it knows who you are | app/cursor | draft | OPEN | `baa4478c4c44d43f0abf8e283e24ffbb8331ca89` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #202 | fix: draft_workflow must not overwrite a catalogue workflow | app/cursor | draft | OPEN | `382e52938af59167d6771ce29be4831b28ad7e69` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #203 | fix: draft_workflow must not become the next Build | app/cursor | draft | OPEN | `15c1c295424b498609242afd1d6b5c5e74077e8d` | `main` | no | patch-equivalent | cherry +0/-1; merge-tree clean (tree=rc1) |
| #204 | fix: native bash must not inherit the control plane DATABASE_URL | app/cursor | draft | OPEN | `d58bc77b199b89ac1e2a60bfdb87e44cebd751b4` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #205 | fix: Codex chat must restore the stored login and must not inherit DATABASE_URL | app/cursor | draft | OPEN | `1d26940743b2f3443d84a1a4743b8c073429fe78` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #206 | fix: store a subscription as a real project member, not as setup | app/cursor | draft | OPEN | `9de6d4ebcec9ae1a73c975b7a7e050cf36974e30` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #207 | fix: a wizard-stored Claude token must reach the driver that builds | app/cursor | draft | OPEN | `832b49b112fe6c3f428dbee64d774ed5fe59b084` | `main` | no | held | cherry +1/-0; merge-tree conflicts in claude_cli_adapter.py and code_agent.py; token injection not in rc1; no clearance |
| #208 | fix: Connect again must keep the repository it already bound | app/cursor | draft | OPEN | `207214cad089d3ebbdd701f4358e2ad2c12abcd5` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #209 | fix: governed web verify must run the whole target, not just install | app/cursor | draft | OPEN | `1dd4e1b14173315f4ae9b3fa5f907ef5f7b764d1` | `main` | no | patch-equivalent | cherry +0/-1; merge-tree clean (tree=rc1) |
| #210 | fix: a security check that never ran must not ship as a verdict | app/cursor | draft | OPEN | `688c526e7d2979fa39389f87dfb98d72cb47c26d` | `main` | no | patch-equivalent | cherry +0/-1; merge-tree clean (tree=rc1) |
| #213 | A readiness repair must finish before the run previews and ships it | app/cursor | draft | OPEN | `74b6f2906f72e2e201d561014f735c9846937ab4` | `main` | no | patch-equivalent | cherry +0/-1; merge-tree clean (tree=rc1) |
| #217 | A direct media link must not fetch the machine's own network | app/cursor | draft | OPEN | `2488b5cc3d08f010ece9e460ff6e9dabcead7a8f` | `main` | no | patch-equivalent | cherry +0/-1; merge-tree clean (tree=rc1) |
| #222 | BOSS-433: Add governed Grok CLI subscription coding harness | seanheiney | draft | OPEN | `47c95b80dd3a0e3946d74e9c9dbf62cf0eb8ebd4` | `main` | yes | already contained | ancestor; ahead 0, behind 564 |
| #224 | Define full Community repository export boundary | seanheiney | ready | OPEN | `7f0691dce27d71ec6b062a10d30f4842fb2086a9` | `main` | yes | already contained | ancestor; ahead 0, behind 563 |
| #225 | BOSS-434: gate Community console proxy with owner session | seanheiney | draft | OPEN | `add10d3cff433adaca0f464cf1c1df071800f6d4` | `main` | yes | already contained | ancestor; ahead 0, behind 566 |
| #226 | BOSS-432 Simplify Mission Control and expose task coordination | seanheiney | draft | OPEN | `39a434a0c7316343f4822bbde4b8e8173023230b` | `main` | yes | already contained | ancestor; ahead 0, behind 565 |
| #227 | Add fail-closed Community export candidate tool | seanheiney | draft | OPEN | `f7c10d557d1d00bfd2760b307b2663685f6864d3` | `main` | yes | already contained | ancestor; ahead 0, behind 565 |
| #228 | BOSS-293 Isolate Community API route mount from paid router | seanheiney | draft | OPEN | `9082100353e277ce38d4a0ebb3309041f8fcb7d6` | `main` | yes | already contained | ancestor; ahead 0, behind 566 |
| #230 | BOSS-439 Add governed Android and iOS Simulator skill executor | seanheiney | draft | OPEN | `ee99ed9cf9a7375fa75b79b54edb86dafedc4cee` | `main` | yes | already contained | ancestor; ahead 0, behind 564 |
| #231 | Inline contextual help across BOSS console | seanheiney | draft | OPEN | `39906ed2f4935a4d3b62a9e3b8e4c5d96e04def0` | `ai/codex/boss-ui-cleanup` | yes | already contained | ancestor; ahead 0, behind 564 |
| #234 | BOSS-293 Move paid SOC 2 API behind enterprise composition | seanheiney | draft | OPEN | `7f0bc3d308906c6b0708acd5a9141a4136a146e0` | `ai/codex/community-api-boundary` | yes | already contained | ancestor; ahead 0, behind 565 |
| #235 | BOSS-293 Move hosted Account Connect routes behind paid composition | seanheiney | draft | OPEN | `3d671f78e1a1c3a30c4fefdfd73002e76adecf42` | `ai/codex/community-api-composition` | yes | already contained | ancestor; ahead 0, behind 564 |
| #236 | Build a fail-closed Community container candidate | seanheiney | draft | OPEN | `37545aa82061ccddd995036df5bd9e472d49d858` | `ai/codex/community-export-tool` | yes | already contained | ancestor; ahead 0, behind 560 |
| #237 | BOSS-293 Move Enterprise cluster and support routes behind paid composition | seanheiney | draft | OPEN | `907cf2be28a3c9a5fc5fb298fe55c09c034b3505` | `ai/codex/community-marketplace-composition` | yes | already contained | ancestor; ahead 0, behind 563 |
| #238 | BOSS-293 Start Community repository routing unbound | seanheiney | draft | OPEN | `bf62919ac5f8556f34ec57f69e77d950b1ef01cc` | `main` | yes | already contained | ancestor; ahead 0, behind 562 |
| #239 | BOSS-293 Permit only audited shared settings in Community export | seanheiney | draft | OPEN | `61c978c4fe6f3ddd9d4083f8680d89bf6531a988` | `ai/codex/community-container-split` | yes | already contained | ancestor; ahead 0, behind 559 |
| #240 | BOSS-293: split Community API client from paid extension | seanheiney | draft | OPEN | `875f8986668d7b9391cd2456cc0ccd47818e4ffe` | `ai/codex/community-enterprise-ops-composition` | yes | already contained | ancestor; ahead 0, behind 560 |
| #241 | BOSS-293 Prepare concise public Community README | seanheiney | draft | OPEN | `cd074cccbe7ae04fa7214de93bcfc145abf5b7e9` | `main` | yes | already contained | ancestor; ahead 0, behind 559 |
| #242 | BOSS-293: split Community web modules and enforce public API client entrypoint | seanheiney | draft | OPEN | `c17f0a09824f2509caddbf7cff3dd5b88ccae3bf` | `ai/codex/community-container-split` | yes | already contained | ancestor; ahead 0, behind 553 |
| #243 | BOSS-293: move paid API models out of shared schemas | seanheiney | draft | OPEN | `56c46dbeb744593b7629664c305f132e6dcfc7c4` | `ai/codex/community-api-client-split` | yes | already contained | ancestor; ahead 0, behind 559 |
| #244 | BOSS-293: decouple Community entitlement broker from paid service | seanheiney | draft | OPEN | `6f8a290983407c23ffc4a1e03d418b2ed6a2753f` | `ai/codex/community-schema-split` | yes | already contained | ancestor; ahead 0, behind 557 |
| #245 | BOSS-293: ship Community console route and reviewed public docs remaps | seanheiney | draft | OPEN | `0f9f4d6815eef07375336979f4631ec69196cff8` | `ai/codex/community-web-split` | yes | already contained | ancestor; ahead 0, behind 547 |
| #246 | BOSS-293: split Community SSO from paid enterprise service | seanheiney | draft | OPEN | `ad96b1f9de49685437ecc9496972b79737e72028` | `ai/codex/community-entitlements-split` | yes | already contained | ancestor; ahead 0, behind 556 |
| #247 | BOSS-293: audit shared Community fields and pin public export artifacts | seanheiney | draft | OPEN | `7d952a650eefb08475f1fa8d4fa0542124809901` | `ai/codex/community-page-split` | yes | already contained | ancestor; ahead 0, behind 542 |
| #248 | BOSS-293: isolate paid model gateway from Community entitlements | seanheiney | draft | OPEN | `8044db87f37f473385f72155d9cf92ed26c15682` | `ai/codex/community-api-main-paid-imports` | yes | already contained | ancestor; ahead 0, behind 555 |
| #249 | BOSS-293: split Community database bootstrap from hosted tables | seanheiney | draft | OPEN | `cca8440ea9f20e58e05a55363078c12baa7bce27` | `main` | yes | already contained | ancestor; ahead 0, behind 563 |
| #250 | BOSS-416: generate edition-mounted OpenAPI contracts | seanheiney | draft | OPEN | `a05afc7f5b222148a3a7cc2ada4dd9f36d22bd27` | `main` | yes | already contained | ancestor; ahead 0, behind 563 |
| #251 | BOSS-293: pin Community database schema into public export | seanheiney | draft | OPEN | `a4be57a9ad7d459adfb880fd30ef09bf45f09e0a` | `ai/codex/community-shared-export` | no | patch-equivalent | cherry +0/-2; merge-tree conflicts (tree differs or no clean tree) |
| #252 | BOSS-293: isolate Cloud client routes from API main | seanheiney | draft | OPEN | `96bec071286a1152f7531a96d2a8b150681b4797` | `ai/codex/community-entitlement-gateway-split` | yes | already contained | ancestor; ahead 0, behind 554 |
| #253 | BOSS-293: export checksum-pinned Community OpenAPI schema | seanheiney | draft | OPEN | `91591dc52b8ab8b6583cdd6c3214871f3214f6fa` | `ai/codex/community-shared-export` | no | superseded | cherry +2/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #254 | BOSS-293: extract Community edition and Easy Connect clients | seanheiney | draft | OPEN | `b3a74fa62d3297d8815e2c608901d7ac50a926d7` | `ai/codex/community-entitlement-gateway-split` | yes | already contained | ancestor; ahead 0, behind 554 |
| #255 | BOSS-293: pin Community traceability ledger into export | seanheiney | draft | OPEN | `b7f6665cf00748211a597d69519e9651f9384708` | `ai/codex/community-shared-export` | yes | already contained | ancestor; ahead 0, behind 541 |
| #256 | BOSS-293: split Community specpack and audit service boundaries | seanheiney | draft | OPEN | `6f3664ae4df8056a20737e4b43f3666e1a317e04` | `ai/codex/community-cloud-client-split` | yes | already contained | ancestor; ahead 0, behind 552 |
| #257 | BOSS-293: pin shared Community Easy Connect export | seanheiney | draft | OPEN | `e4424cf6e10593f2e08889ad3903a107b200cf92` | `ai/codex/community-shared-export` | yes | already contained | ancestor; ahead 0, behind 541 |
| #258 | BOSS-293: pin Community specpack manifest and safe policy defaults | seanheiney | draft | OPEN | `4d9437c3ea7512936c16d9ee82d2902863a1775b` | `ai/codex/community-shared-export` | yes | already contained | ancestor; ahead 0, behind 541 |
| #259 | BOSS-293: keep private API client aggregator out of Community export | seanheiney | draft | OPEN | `6993306437cf897bf4797b1e7622a59f647909a6` | `ai/codex/community-shared-export` | yes | already contained | ancestor; ahead 0, behind 541 |
| #260 | BOSS-293: audit shared specpack entitlement marker contract | seanheiney | draft | OPEN | `2ef67f3934579269911be07291ad2bc20f912ae4` | `ai/codex/community-specpack-manifest` | yes | already contained | ancestor; ahead 0, behind 540 |
| #261 | BOSS-293: keep internal Community brief out of public export | seanheiney | draft | OPEN | `0247c7e39be557d617b21693d40fceb890464512` | `ai/codex/community-shared-export` | yes | already contained | ancestor; ahead 0, behind 541 |
| #262 | BOSS-293: split Community and paid Cloud API routes | seanheiney | draft | OPEN | `b3eda658c401a001b7c7a7576bdd8dc1da7b9bb7` | `ai/codex/community-specpack-audit-boundary` | yes | already contained | ancestor; ahead 0, behind 552 |
| #263 | BOSS-438: calm the console loading shell | seanheiney | draft | OPEN | `8c0031e40446dd0fa1dea9af9711e081bb43d60f` | `ai/codex/boss-438-agent-org-ux` | yes | already contained | ancestor; ahead 0, behind 560 |
| #264 | BOSS-293: export reviewed Community API entrypoint | seanheiney | draft | OPEN | `3c02ae68903ed5453be60777503acf16d82ca611` | `ai/codex/community-shared-export` | yes | already contained | ancestor; ahead 0, behind 541 |
| #265 | BOSS-293: align Community OpenAPI with Cloud route split | seanheiney | draft | OPEN | `54c9c6461ea57ae49e3e4abc4c6a357cc7f3887e` | `ai/codex/community-cloud-route-boundary` | no | patch-equivalent | cherry +0/-2; merge-tree conflicts (tree differs or no clean tree) |
| #266 | BOSS-293: sanitize shared Community test fixtures | seanheiney | draft | OPEN | `54e65b31618e8d7c998701eb7f620f69a34a25c9` | `ai/codex/community-specpack-runtime-export` | yes | already contained | ancestor; ahead 0, behind 537 |
| #267 | BOSS-293: remove paid service import from pipeline profiles | seanheiney | draft | OPEN | `3ab703840c70921a72b5edf77b34c5cb1f97ffb2` | `ai/codex/community-cloud-client-split` | yes | already contained | ancestor; ahead 0, behind 553 |
| #268 | BOSS-293: add agent template routes to edition OpenAPI contracts | seanheiney | draft | OPEN | `57218045ed80e33648c5820082cad7651372a629` | `ai/codex/community-openapi-cloud-routes` | no | patch-equivalent | cherry +0/-4; merge-tree conflicts (tree differs or no clean tree) |
| #270 | BOSS-293: map public API client root to Community entrypoint | seanheiney | draft | OPEN | `5f8bdbeff3eb4754f2dec661f7c035042c0a277e` | `ai/codex/community-client-package-boundary` | yes | already contained | ancestor; ahead 0, behind 540 |
| #272 | BOSS-293: retain focused Community tests across paid suite boundary | seanheiney | draft | OPEN | `d91f788e89435c1ce992ad93592b2e17e86a5caa` | `ai/codex/community-test-fixtures` | no | superseded | cherry +1/-1; merge-tree conflicts; behavior already in rc1 under later commits |
| #273 | BOSS-293 Keep Metatron available in Community Makefile | seanheiney | draft | OPEN | `109ae38124cd645ff72bb69b740fbd4b2e32bc48` | `ai/codex/community-main-export-rewrite` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #274 | Start and fork the live workflow version after a revert | app/cursor | draft | OPEN | `899a22f5ec33270fdfe30d293c2032e3c104e594` | `main` | no | patch-equivalent | cherry +0/-1; merge-tree conflicts (tree differs or no clean tree) |
| #275 | An agent that stops for approval was recorded as finished | app/cursor | draft | OPEN | `5938ede0036051a191b77244bd338e4b94cc16a6` | `main` | no | superseded | cherry +1/-0; merge-tree conflicts; behavior already in rc1 under later commits |
| #276 | Start one can see the inputs a workflow declared | app/cursor | draft | OPEN | `266ecb454a58f6c3dd0df83002225f7cbcd6f249` | `main` | no | superseded | cherry +1/-0; merge-tree clean; result tree equals rc1 18cc3354852e |
| #277 | Keep channel credentials in a configured secret store | app/cursor | draft | OPEN | `d9f3dcff7ea330ba479001731cee5774810ddfb5` | `main` | no | patch-equivalent | cherry +0/-1; merge-tree clean (tree=rc1) |
| #279 | BOSS-428/436: make Community checks work with sudo Docker access | seanheiney | draft | OPEN | `b64bd6a9a543e77d3dc68a7efea458ad8c3e3853` | `main` | no | superseded | cherry +1/-3; merge-tree conflicts; behavior already in rc1 under later commits |
| #280 | BOSS-293/448 integrate Community export and release hardening | seanheiney | draft | OPEN | `371a1c72a1fd49f3336f5790b55407d51886412a` | `main` | yes | already contained | ancestor; ahead 0, behind 18 |
| #281 | BOSS-430: direct task workflow in terminal | seanheiney | draft | OPEN | `4a0b85032e12d2bd5a2ab4d3120e34d47bcaada4` | `ai/codex/boss-293-community-integrate` | no | patch-equivalent | cherry +0/-1; merge-tree conflicts (tree differs or no clean tree) |
| #283 | BOSS-436: back up pasted installer reruns before upgrade | seanheiney | draft | OPEN | `a9fef8362c3da6e088eb025ac766851a65f4d96a` | `ai/codex/boss-293-community-integrate` | no | patch-equivalent | cherry +0/-5; merge-tree conflicts (tree differs or no clean tree) |
| #284 | fix(web): bound deployment lookup fan-out (BOSS-452) | seanheiney | draft | OPEN | `1ae20cf4810811efe1a9e2eb8d3838bc17825da4` | `ai/codex/boss-293-community-integrate` | no | patch-equivalent | cherry +0/-4; merge-tree clean (tree=rc1) |
| #285 | BOSS-429: bind project mutations to authenticated actors | seanheiney | draft | OPEN | `81ace76ede86a5457766ee6a5385db3a38c94426` | `ai/codex/boss-293-community-integrate` | no | patch-equivalent | cherry +0/-3; merge-tree conflicts (tree differs or no clean tree) |
| #286 | BOSS-453 fix fast delivery workflow plan 500 | seanheiney | draft | OPEN | `2c4759c50c107c995d7fc9ac9a07afbcc1979991` | `ai/codex/boss-293-community-integrate` | no | patch-equivalent | cherry +0/-1; merge-tree clean (tree=rc1) |
| #287 | BOSS-454 preserve Goldeneye API tracebacks across recreation | seanheiney | draft | OPEN | `18927d858624cc931dd4be49725e8398ccae9a91` | `ai/codex/boss-293-community-integrate` | no | patch-equivalent | cherry +0/-1; merge-tree clean (tree=rc1) |
| #288 | fix(api): bound direct deployment reads (BOSS-452) | seanheiney | draft | OPEN | `43dbf2307d48ed793653da1178213fd0646147d9` | `ai/codex/boss-293-community-integrate` | no | patch-equivalent | cherry +0/-2; merge-tree clean (tree=rc1) |
| #289 | BOSS-300: retain versioned Community third-party notices | seanheiney | draft | OPEN | `21d26cfc2db84cd14b87fc4f49fd3f79bab07383` | `ai/codex/boss-293-community-integrate` | no | patch-equivalent | cherry +0/-1; merge-tree conflicts (tree differs or no clean tree) |
| #290 | BOSS-429 confirm project-local MCP cleanup routes | seanheiney | draft | OPEN | `c5d3fdb438093afeb60068b21daff329deb98af4` | `ai/codex/boss-293-community-integrate` | no | patch-equivalent | cherry +0/-1; merge-tree conflicts (tree differs or no clean tree) |
| #291 | BOSS-455 Add CLI-owned Claude Code sign-in for Community | seanheiney | draft | OPEN | `5f465e494afb78f045f4312ea45def811c5d39be` | `ai/codex/boss-293-community-integrate` | no | patch-equivalent | cherry +0/-1; merge-tree conflicts (tree differs or no clean tree) |
| #292 | BOSS-430: direct decision workflow in terminal | seanheiney | draft | OPEN | `4bc0a66d0c0aec1e5e8b8efee2a86a8484ed8df5` | `ai/codex/boss-293-community-integrate` | no | patch-equivalent | cherry +0/-1; merge-tree clean (tree=rc1) |
| #293 | BOSS-456: make sample newsletter schedules runnable | seanheiney | draft | OPEN | `25be6d312bee5b5c39dc1b4dfc310fc9d35dbf60` | `ai/codex/boss-293-community-integrate` | no | patch-equivalent | cherry +0/-1; merge-tree conflicts (tree differs or no clean tree) |
| #294 | Refuse deleting a finished run a waiting parent still needs | app/cursor | draft | OPEN | `a3148c12d317c18c0caf8738667e0d020dbff3b9` | `main` | no | patch-equivalent | cherry +0/-1; merge-tree clean (tree=rc1) |

## Needs port

needs port: none

