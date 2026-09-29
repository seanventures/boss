# Community GA audit of unknown and held tips

Recorded against tag `v1.0-rc1` commit `b194c65c3028b80cd621fc187280f4d79e84df02` (annotated tag object `04bb2e79f84acb1c3d7074f1617f4c6b37aa452d`).
The previous ledger is `.ai/community-ga-review-ledger.md` on that commit. It left 19 non-ancestor tips unknown and held PR #207 (same tip as `cursor/critical-bug-investigation-7449`).
This file only re-disposes those rows. It does not merge, push, close, or comment.

Every listed tip still exists, at the same SHA the ledger recorded. Comparison is `git cherry v1.0-rc1 <tip>` plus a diff stat. `+` is a patch-id not on the tag. Triple-dot `--shortstat` is the whole branch since the merge-base, so it still counts patch-equivalent commits. The unique-commit stat is only the `+` commits.

`patch-equivalent` means cherry has no `+` lines (here, the tip is an ancestor).
`superseded by <sha>` means the unique patch-id is not identical, but the production behavior is already on the tag in that commit. A cursor ref whose merge-tree conflicts are an old test file, or a rewritten file that already contains the fix, is superseded, not a port.
`held` is only the wizard Claude session-token path. `97379d130464d0857c98ab61ab4fdb0e9408217d` disables that import and records that no provider or counsel clearance is in the repo.
`needs port` names the current files a port would change. It is not permission to merge the old tip. Those branches also conflict on ancient paths.

## Result

Tips that still need a port: **3**.

- `ai/codex/arch-pag-expand-full`
- `ai/codex/devops-worker-tests`
- `ai/codex/ui-ux-toasts-client-feedback`

PR **#207** remains **held**. Do not port the session-token path.

## Held

| Ref | Tip | Cherry | Diff stat | Disposition |
| --- | --- | --- | --- | --- |
| PR #207 | `832b49b112fe6c3f428dbee64d774ed5fe59b084` | +1/-0, ahead 1, not an ancestor | tip commit: 4 files, +108/-2. `git diff --shortstat v1.0-rc1...tip` is that same stat | held |
| `cursor/critical-bug-investigation-7449` | same SHA as #207 | +1/-0 | same | held |

Merge-tree conflicts on `engine/boss_engine/services/claude_cli_adapter.py` and `engine/boss_engine/services/code_agent.py`. The tag does not inject a wizard-stored Claude session token into the coding driver. `97379d130464d0857c98ab61ab4fdb0e9408217d` turns the import off in the installer, console, and backup path. `docs/community/MODEL_CONNECTIONS.md` marks it Disabled. Counsel reference `01049940` is Apache licensing only. No clearance was added after the ledger.

## Unknown tips

| Ref | Tip | Cherry | Diff stat | Disposition |
| --- | --- | --- | --- | --- |
| `ai/grok/ga-release-gates` | `7fdc91e1affbb05a894a7a71aee666f3be4fe5ee` | +0/-0, ancestor, ahead 0 | empty | patch-equivalent |
| `ai/codex/arch-pag-expand-full` | `1bbace1980c8dc655a663278fdeb0f0c3cd9733c` | +8/-105, ahead 113 | triple-dot 237 files, +63740/-5192. Eight unique commits, largest `1bbace1980` 9 files +179/-48 | needs port |
| `ai/codex/baremetal-deploy-sync` | `f46bd7755d7e86619a6d49b27672b92a9916967f` | +49/-73, ahead 130 | triple-dot 157 files, +23252/-1401. Tip commit itself is cherry `-` | superseded by `5d4eacb8dbdd548260cc325bb3e3045812f08f4e` |
| `ai/codex/devops-worker-tests` | `309908a0c686fbdf28077f055b2d8c1b0a8f9b16` | +13/-106, ahead 119 | triple-dot 241 files, +64637/-5051. Tip unique commit 6 files, +968/-702 | needs port |
| `ai/codex/feature-comms-ui` | `8538e9e5fc91ecaf506dc7dca55de2ee6954a915` | +2/-105, ahead 107 | triple-dot 236 files, +65266/-5319. Unique: `de7be379` 8 files +1138/-167; tip 6 files +143/-4 | superseded by `64879a651d509b360e3c972e093e19b2a3628efc` |
| `ai/codex/feature-providerops-polish` | `64f8b230f8b19cd9872c790d2a0dcd875d002e27` | +5/-108, ahead 113 | triple-dot 240 files, +64012/-5136. Tip unique commit is 28 lines in `.ai/activity-log.md` | superseded by `64879a651d509b360e3c972e093e19b2a3628efc` |
| `ai/codex/packs-local-import-export` | `de7be3792876812f09be0d552c0fd4553dca42f1` | +1/-105, ahead 106 | triple-dot 235 files, +65127/-5319. The one unique commit is the comms polish above | superseded by `64879a651d509b360e3c972e093e19b2a3628efc` |
| `ai/codex/qa-e2e-comms` | `019dd04ae1ca0df6479b8eb4aab5fdd09993360b` | +1/-108, ahead 109 | triple-dot 236 files, +63280/-5022. Unique commit 8 files, +96/-10 | superseded by `abe7eb6bcb77c44a766ff201d697ccb20a52f63e` |
| `ai/codex/ui-ux-toasts-client-feedback` | `26a3a8c1fb39e0bf00e63e7637f9ea5c6960dab5` | +1/-107, ahead 108 | triple-dot 241 files, +64640/-5243. Unique commit 6 files, +24/-26 | needs port |
| `cursor/critical-bug-investigation-031d` | `604181e5d6b19b0b6d0007ecaeafd2cbeda848d3` | +3/-2, ahead 5 | triple-dot 31 files, +1708/-160. Tip commit is only `tests/test_delivery_execute_verdict.py` | superseded by `1a5bf262e5b3dcb527ee1634f3a08afbd07ec8df` |
| `cursor/critical-bug-investigation-4363` | `37a9a89731f89677ccf1d5497ea98b42b0c12ff7` | +3/-0, ahead 3 | triple-dot 14 files, +418/-29 | superseded by `1a5bf262e5b3dcb527ee1634f3a08afbd07ec8df` |
| `cursor/critical-bug-investigation-4a4f` | `c29246854fd892e09078004fdc07746f9e6029cc` | +1/-0, ahead 1 | 5 files, +84/-7 | superseded by `eaf115b82c30525b7dd34bc2a0e2450e22b82fd6` |
| `cursor/critical-bug-investigation-71af` | `7dc7828503d308d39f936f26139f9f8fff4e763f` | +8/-0, ahead 8 | triple-dot 21 files, +1235/-88 | superseded by `1a5bf262e5b3dcb527ee1634f3a08afbd07ec8df` |
| `cursor/critical-bug-investigation-a499` | `055602656e6cf28dd5003be3183c81e900e9d667` | +3/-0, ahead 3 | triple-dot 20 files, +708/-40 | superseded by `1a5bf262e5b3dcb527ee1634f3a08afbd07ec8df` |
| `cursor/critical-bug-investigation-b31f` | `db195c276333b95816562cba93a00260a307fec0` | +7/-0, ahead 7 | triple-dot 21 files, +1169/-84. Tip commit is only `engine/tests/test_registry.py` | superseded by `1a5bf262e5b3dcb527ee1634f3a08afbd07ec8df` |
| `cursor/critical-bug-investigation-c228` | `7dcc8f6fead428f9443dbd56518fe6deafb6208c` | +4/-0, ahead 4 | triple-dot 26 files, +1524/-140 | superseded by `86cc2aa0224b59b85149f0fea137bb19c6844a94` |
| `cursor/critical-bug-investigation-ca78` | `3d89ed5cf893d8f4056fd186caf689537c729049` | +2/-0, ahead 2 | triple-dot 18 files, +603/-40 | superseded by `1a5bf262e5b3dcb527ee1634f3a08afbd07ec8df` |
| `cursor/critical-bug-investigation-d7d3` | `4a906eafeecde2b47acf1a8093b6ccaa1f1e00ed` | +1/-0, ahead 1 | 6 files, +262/-16 | superseded by `1a5bf262e5b3dcb527ee1634f3a08afbd07ec8df` |
| `cursor/critical-bug-investigation-f4f1` | `1b8683c62d70b6d73cba792a2aa7e46673e53cea` | +4/-0, ahead 4 | triple-dot 21 files, +1038/-71 | superseded by `1a5bf262e5b3dcb527ee1634f3a08afbd07ec8df` |

## Why each row

`ai/grok/ga-release-gates` is commit `7fdc91e1`, now inside the tag (17 commits behind, 0 ahead). Cherry prints nothing. The docs note is retained, not a product port.

`ai/codex/baremetal-deploy-sync`: origin tip `a0cd5ac34f75c1c0fa851e18463436e5f0042f72` is an ancestor of the tag and has the same subject as the local tip. The local tip commit is cherry-equivalent. The 49 `+` commits all have the same subject already on the tag, from `c1983e92f4aee1ef3489f294e42c4b88eb794957` through `5d4eacb8dbdd548260cc325bb3e3045812f08f4e`. Merge-tree conflicts are early files the tag deleted or moved (`apps/api/app/services/intake.py` and the other service modules). Not a port.

`ai/codex/feature-comms-ui` and `ai/codex/packs-local-import-export` share unique commit `de7be379` (old Mission Control toast redirects and comms panel polish). The pack-import commit on the packs branch is already cherry-equivalent, and `import_pack` is on the tag. Client toasts landed as `showToast` / `Toaster` in `64879a651d509b360e3c972e093e19b2a3628efc`. The commercial follow-up `8538e9e5` only adds tests and `.ai` notes.

`ai/codex/feature-providerops-polish`: the monitoring heading is already in `apps/web/app/page.tsx` via `abe7eb6bcb77c44a766ff201d697ccb20a52f63e`. The page-level toast banner was replaced by `64879a651`. List offset that this era added for KPIs and the other landed routes is in `17fa9b0dd5b421f04987666cbe09aa9f6c31b481` and later. The tip itself is a stale `.ai/activity-log.md` note. Do not port that hunk.

`ai/codex/qa-e2e-comms`: the only unique commit is that same monitoring heading plus an accessibility assertion and `.ai` notes. Superseded by `abe7eb6bc`.

Cursor refs are one bug-hunt family reapplied on different bases. Merge-tree conflicts on `apps/api/tests/test_critical_auth_and_routing.py`, `engine/tests/test_registry.py`, `engine/tests/test_mcp.py`, `engine/tests/test_mcp_http.py`, `engine/tests/test_governance_tool_scope.py`, and rewritten production files. The production lines are already on the tag:

- `1a5bf262e5b3dcb527ee1634f3a08afbd07ec8df` is the tag commit titled `fix: re-apply critical auth holes still open from PR #112`. It holds `_model_generate_code_dedupe_key` (constraints in the dedupe), `_serve_mcp_read_only` (HTTP MCP defaults to read-only), the suspended-run approval decision check, `_can_ship_pr` on the dissent path, `task_create` on code-task retry, steer, override, and preview, and the SSH deploy actor plus `infra_provision` check (`apps/api/app/main.py` around the deploy endpoint).
- `86cc2aa0224b59b85149f0fea137bb19c6844a94` has the same subject as the c228 tip: stale-base merge completion and the project-to-org mirror guard (`DEFAULT_MEMBER_IDS`, `ON CONFLICT DO NOTHING`).
- `eaf115b82c30525b7dd34bc2a0e2450e22b82fd6` binds `dispatch_plan` to `project_id` (`WHERE id = :id AND project_id = :project_id` in `engine/boss_engine/services/intake.py`, called from `apps/api/app/main.py`). The old `apps/api/app/services/` paths are gone, which is why 4a4f conflicts.
- `89b9c1d64b304c418bfd8e70c5e7f97069a68814` keeps `_DISCUSS_EXCLUDED_READ_TOOLS` as `query_db`. `build_discuss_read_registry` is what chat uses.
- `603344e242b3ba2410860882ebe66c16a2db41fc` still treats `rolled_back` as not a successful deploy. The 031d tip only patches the old verdict test.

`cursor/critical-bug-investigation-b31f` and the 031d tip change test files only. The production fix is already on the tag, so those are superseded, not ports. c228's `.ai` bug-hunt notes are stale and are not a port.

## Needs port

These are the offset query parameters the June pagination scratch added that the tag never wired. Do not merge the branches. Later lists (tasks, KPIs, receipts, deployment targets, events, self-improvement proposals) already take `offset`.

`ai/codex/arch-pag-expand-full`: `list_outbox_messages` and `list_inbox_messages` in `apps/api/app/main.py` take `limit` only. `engine/boss_engine/services/comms.py` already applies `OFFSET`, and `packages/api-client/src/community.ts` already sends `offset`, which the API ignores. A port changes:

- `apps/api/app/main.py`
- `engine/boss_engine/schemas.py` (`OutboxMessageListResponse`, `InboxMessageListResponse`)
- `api/openapi.yaml`
- `api/openapi.community.yaml`

`ai/codex/devops-worker-tests` includes that outbox/inbox gap and four more handlers that are still limit-only or unbounded:

- `list_artifacts` and `list_deadletters` in `apps/api/app/main.py` (the client already sends `offset`)
- `list_change_journal` in `apps/api/app/main.py` and `engine/boss_engine/services/audit.py` (the client does not send `offset` either)
- `decision_inbox` in `apps/api/app/main.py` (no `limit` or `offset`; the SQL returns every row)

Files: `apps/api/app/main.py`, `engine/boss_engine/services/audit.py`, `engine/boss_engine/schemas.py` (`ArtifactListResponse`, `DeadletterListResponse`, `ChangeJournalResponse`, `DecisionInboxResponse`, plus the outbox/inbox models), `api/openapi.yaml`, `api/openapi.community.yaml`, and `packages/api-client/src/community.ts` (`listChangeJournal` only). Pack import in the same tip commit is already on the tag. The old `apps/api/tests/test_api.py` hunk is not a separate port.

`ai/codex/ui-ux-toasts-client-feedback`: `GET /v1/self-improvement/proposals` already accepts `offset` and returns it (`17fa9b0dd` era, schema `SelfImprovementProposalListResponse`). The client method `listSelfImprovementProposals` still sends only `limit`, and the TypeScript response type omits `limit` and `offset`. File: `packages/api-client/src/community.ts`. The same commit also forwards `offset` from `apps/web/lib/deadletters.ts`, but `GET /v1/deadletters` does not accept `offset`, so that one-line forward is not a port by itself. Do not port the June `docs/user-stories.md` rewrite.

## Not done

No merge, no push, no branch delete, no PR comment, no deploy, and no product test run.

## Ported after this audit

The release branch now accepts `offset` on artifacts, outbox, inbox, deadletters, and the change journal, and optional `limit` plus `offset` on the decision inbox. The decision inbox stays unbounded when `limit` is omitted. The three branch tips were not merged.
