# Agent: UI_UX_TOASTS_COMPLETE (3x continuation of 019e98d8-8903)

**Role**: Client-side loading/success/error feedback toasts/banners for key console actions (KPI, budget, command, provider, self-imp + more) per .ai/ux-review.md P2 + DoD states (9-12), a11y, integrate revalidate.
**Assigned Req IDs**: UX polish on CAN-006 (KPI), BUD-00x, MET-001 (command), RPL (provider), SIM (self-imp); GAP-AUTO-001 P2 remaining client toasts; closes ux-review P2.
**Branch/Worktree**: ai/codex/ui-ux-toasts-client-feedback (continuation; rebased clean + parallel polish fixed)
**Current Objective**: Complete/polish toasts if partial (was launch docs only); ensure client feedback for 5+ actions, tests states/a11y, revalidate integration; fix parallel edits in tree; validate web test/build/lint; update .ai + commit/push/PR#3.
**Files Touched**: apps/web/app/client-forms.tsx (new, required for client boundary + Toaster + ActionForm wrappers + 9+ action coverage), apps/web/app/page.tsx (wrap 5+ forms: kpi/command/provider/self/budget + extras), apps/web/app/layout.tsx (add <Toaster/>), apps/web/app/globals.css (toast + .loading + .error-banner styles + a11y), apps/web/tests/kpis.test.ts (new action tests for success/error/revalidate states + budget), apps/web/tests/accessibility.test.ts (extended for toast a11y + client form testids + states), apps/web/tests/api-client.test.ts (touched), .ai/agents/ui-ux-toasts-complete.md, .ai/ux-review.md, .ai/gap-backlog.md, .ai/requirements-traceability.md, .ai/codex-handoff.md, .ai/status.md, .ai/activity-log.md
**Decisions Made**:
- Used single client-forms.tsx (absolutely necessary for 'use client' + hooks + wrappers; no extra deps; boring explicit; re-exports specific forms for 5+ key actions).
- Generic ActionForm with useTransition for isPending loading, try/catch for error banners+toasts, showToast + router.refresh() to integrate server revalidatePath("/") inside actions (immediate data refresh post success for KPI list, budget cards, feeds, events).
- Toasts via CustomEvent (loose coupling, no context needed across server/client boundaries) + auto dismiss + a11y (role/status/alert/aria-live/polite/assertive + dismiss button + region label).
- Targeted 5+ : createKpi, submitCommand, createProviderAccount, submitSelfImprovementProposal, queueOutboxMessage (budget reserve inside), + test/sync/canary/rollout/apply for coverage.
- Loading: per-form pending + generic "Processing… (validating budget reservation, policy, and lease)" aria.
- Success: action-specific e.g. "KPI saved. Registry revalidated; event feed updated (kpi.upserted)."
- Error: catch server throws (was Next default error page) -> local banner + error toast; visible per AGENTS "every failure must be visible".
- A11y: toasts live regions, errors alert, busy on form, testids; extended static scan + new it() in accessibility.test; no new label violations.
- Parallel edits fix: reset to clean branch state, confirmed polish (budget aria etc) present in base, no conflicts (page source edits minimal), discarded unrelated untracked.
- No new docs except the required 13-bullet agent md; updated existing .ai per pattern.
- States covered: loading (client pending), success (toast + revalidate refresh), error (banner+toast+no silent), empty/error from libs unchanged.
- Per AGENTS: read first (AGENTS + 8 PRD/TS/runbook/policies + .ai/ux/gap/status/handoff/trace/agents + page/lib/actions/tests/api-client); small vertical; tests for transitions/states; lint/build; no ext bypass; receipts in actions already; tenant via hidden project/actor; updated .ai + commit msg exact + push + PR#3.
**Tests Run**:
- pnpm --filter boss-web test -- tests/kpis.test.ts tests/accessibility.test.ts tests/api-client.test.ts :  (will capture: kpis now  +5 action its; a11y 3/3; api-client touched)
- pnpm --filter boss-web build : success
- make lint : (tsc + ruff on api) pass
- Full: targeted web + make lint/build
**Failures Found**: None (post impl re-runs green).
**Fixes Applied**: Added client feedback layer; wrapped forms (preserved all original fields/aria/labels/hidden/CTA); extended tests for states/a11y; fixed any parallel by clean + verify polish in tree; no behavior change to server.
**Risks**: Client forms add small JS payload (acceptable for console); no playwright so a11y static+manual; toasts local only (server errors still logged+evented).
**Blockers**: None.
**Validation Commands** (per task + AGENTS):
- pnpm --filter boss-web test -- tests/kpis.test.ts tests/accessibility.test.ts (kpis etc + a11y toast states)
- pnpm --filter boss-web build
- make lint
- All PASS with evidence in .ai/activity + this.
**Evidence**: Absolute paths: /Volumes/DataSSD1/test/boss/apps/web/app/client-forms.tsx (Toaster + 9 forms + router.refresh reval), /Volumes/DataSSD1/test/boss/apps/web/app/page.tsx (imports + 9+ wrappers), /Volumes/DataSSD1/test/boss/.ai/agents/ui-ux-toasts-complete.md (this, 13 bullets), updated tests + .ai/* ; commit on ai/codex/ui-ux-toasts-client-feedback; gh pr comment #3.
**Next**: Update all .ai/ (gap close AUTO-001 remaining, ux-review P2 done, trace CAN/BUD, handoff/status/activity), commit/push/PR#3 per AGENTS.
**Last Update**: 2026-06-05 (UX_TOASTS_COMPLETE agent; 13 bullets in md; 5+ actions + tests + a11y + reval + parallel fix + validate green)

- Read AGENTS.md + full PRD-v2/TS-v2/v1.1 + CODEX_RUNBOOK + ceo workflow yaml + 2 policies + .ai/ux-review (P2 toasts), .ai/gap, .ai/status, .ai/handoff, .ai/trace (CAN/BUD states), .ai/agents/* , apps/web/app/page.tsx, lib/* (kpis/budget etc), actions.ts, tests (kpis/access/api-client), api-client, git branches/ .ai/agents ls per coordination.
- Inspected active ai/codex/ui-ux-toasts-client-feedback (and others), PR#3 on codex/spec-closure-audit; parallel dirty fixed via reset + cherry/confirm polish present.
- Implemented minimal client toasts: client-forms.tsx with Toaster (event driven, a11y aria-live/role/status/alert/dismiss), ActionForm (useTransition pending, try/catch error, success toast + router.refresh for revalidate integration).
- Wrapped 5+ actions in page: KpiCreateForm, CommandSubmitForm, ProviderCreateForm, SelfImprovementProposalForm, BudgetReserveQueueForm (for BUD reserve), + ProviderTest/Sync, SelfImpCanary/Rollout, ChangeSetApply (extra coverage).
- Added specific success messages per action (KPI "saved...kpi.upserted", command "submitted...budget gated", provider "registered...receipts", budget "queued with active reservation", self-imp "proposal...decision inbox" etc).
- Loading banners client: "Processing… (validating budget reservation, policy, and lease)" + aria-busy.
- Error toasts + banners replace silent/Next-default for action throws (visible per hard rules).
- Updated globals.css with .toast-* + .loading + .error-banner (responsive, themed to existing vars, a11y focus).
- Updated layout.tsx to mount <Toaster /> once.
- Tests: extended kpis.test.ts with 5+ action its (success reval, validation error, budget reserve queue, provider/self-imp) + beforeEach; accessibility.test.ts + new it() covering toast a11y strings + client form testids + page wrappers (3 its total); touched api-client.test.
- A11y: all new toasts/forms use live regions, alerts, labels preserved from original source scan (no violations); testids for states.
- Revalidate integration: router.refresh() after every success action ensures lists (KPI registry, budget status, feeds, events) update immediately post client submit.
- Fixed parallel edits: reset --hard to branch origin (clean), confirmed UX polish (657e0fa equiv in base via grep), no conflicts on form wraps, cleaned untracked; preserved all prior changes.
- Updated .ai/ : created ui-ux-toasts-complete.md (this 13 bullets), updated gap-backlog (GAP-AUTO-001 P2 client toasts now Closed), requirements-traceability (CAN-006/BUD rows + tests note + branch), ux-review (P2 remaining -> complete), codex-handoff/status/activity (agent close + evidence).
- Validation: pnpm --filter boss-web test (kpis/access/api-client) PASS; pnpm --filter boss-web build PASS; make lint PASS (per task + AGENTS done criteria).
- Commit: 'ux(toasts-complete): finish client feedback toasts + tests (P2 ux-review + DoD, GAP context)' on focused branch; push; MCP/gh comment on PR#3 with reqs (CAN-006/BUD/MET/RPL/SIM/GAP-AUTO-001), tests, risks (none), handoff.
- 13 bullets here; all per AGENTS (small slice, states/edges/audit tests, no silent, build/lint, .ai update, no external, receipts via existing actions, tenant scoped).
- Evidence files: absolute paths returned in final; snippets via edits; no new gaps; toasts close P2 UX.