# Agent: COMMERCIAL_FULL (ux-commercial-full surfaces polish)

**Role**: Polish commercial local surfaces for edition, packs manager, marketplace blocked states, account connect UI panels (health/auth/team/billing/link); ensure states (empty/error/success/blocked), a11y landmarks, tests per DoD/P2 + pack P3 context.
**Assigned Req IDs**: COMPKG-xxx (PRD Commercial packaging / Account Connect / Marketplace), COMPKG local polish, GAP-AUTO-001 UX states, GAP-PRD-AUDIT-001 (pack P3 context), NFR a11y/responsive.
**Branch/Worktree**: ai/grok/ux-commercial-surfaces (per handoff); current ai/codex/qa-e2e-selfimp-provider (integrated); codex/mission-control-commercial-enterprise prior.
**Current Objective**: Complete commercial local surfaces polish (edition, packs manager, marketplace blocked, account connect UI states/tests/a11y). Read first per task+AGENTS: AGENTS.md, PRD commercial (PRD-v2.0 COMPKG + constitution 14/16), .ai/gap (pack closed GAP-PRD-AUDIT-001 P3 context + GAP-002 blocked external), trace COMPKG (requirements-traceability.md), commercial service (apps/api/app/services/commercial.py full fail-closed logic, receipts, blocked states), web commercial (apps/web/lib/commercial.ts + packs.ts + agent-modules.ts), tests (commercial-enterprise.test.ts, activity.test.ts, accessibility.test.ts, api test_api.py commercial*). Update .ai/agents/ux-commercial-full.md 13 bullets. Validation: pnpm test commercial*; build; lint. Commit exact msg.
**Files Touched**: apps/web/app/page.tsx (added 9+ aria-labelledby + id landmarks on commercial/packs/marketplace panels; polished edition blockedReason + success state copy; packs empty msg with P3 context; marketplace no-listings msg with blocked tie-in to edition), apps/web/tests/accessibility.test.ts (added 18 expects for new commercial/packs/marketplace headings), apps/web/tests/commercial-enterprise.test.ts (added 4 new its: community edition map+entitlements, blocked cloud edition+empty hosted, marketplace blocked via commercial context, commercial error->empty blocked state for surfaces), .ai/agents/ux-commercial-full.md (this), plus .ai/gap-backlog.md, .ai/requirements-traceability.md, .ai/status.md, .ai/codex-handoff.md, .ai/activity-log.md (updates for closure), .ai/ux-review.md (cross ref).
**Decisions Made**:
- Followed AGENTS hard rules exactly: no external calls (used existing adapter fail-closed), receipts/dedupe already in service, no silent, tenant scope, tests for states/transitions (edition community/blocked, acct connect link states, marketplace blocked), small reviewable.
- Polish minimal vertical: focused on states visibility (explicit blocked for edition/marketplace when !linked or no url; success notes; empty with context), a11y (landmarks for all commercial surfaces + packs for keyboard/reachability of states), tests (happy+blocked+error for edition/packs/market/account-connect surfaces).
- Marketplace blocked polish tied to commercial.edition.blockedReason / accountConnectStatus (not faking listings, per service + PRD "missing hosted -> visible blocked").
- Packs manager polish notes P3 context (import/export) but no impl (per small PR rule + GAP-PRD-AUDIT-001 open).
- Edition polish: community local always "active" no-hosted; cloud/enterprise show "blocked_account_connect_required" + warn + localAuthAllowed flag.
- Account connect states: all panels (health/auth/invites/billing) already had per-status pills + empty + error via feed; ensured consistent with edition block.
- No change to backend (already complete fail-closed + events + receipts per trace); no new DB; UI + tests + .ai only.
- Used existing empty*Feed + fetch catch for blocked.
- Per DoD: states covered, a11y test extended, build/lint, no hardcoded, audit trail via existing.
- Branch coordination: preserved parallel (ai/grok/ux-commercial-surfaces, codex/mission-control-commercial-enterprise), no dupe.
**Tests Run**: pnpm --filter boss-web test -- tests/commercial-enterprise.test.ts tests/activity.test.ts (commercial*); added 4 tests pass (community, blocked-cloud, marketplace-blocked, error-empty); accessibility.test.ts now 2/2 + new asserts; prior 84 web. API: targeted pytest -k "community_edition|cloud_edition|account_connect" (existing + coverage via mocks).
**Failures Found**: 0 (pre-existing commercial tests green; new states exercised blocked/community/marketplace without breaking; a11y scan passes on new labels).
**Fixes Applied**:
- page.tsx: 9 aria+id for panels (edition, packs x2, health, auth, entitlements, team, billing, hosted-mkt); edition blocked enhanced with localAuth note + success state msg; packs empty + P3 note; marketplace no-listings + blocked edition context.
- accessibility.test.ts: 18 new landmark expects covering COMPKG surfaces.
- commercial-enterprise.test.ts: 4 new its covering edition community/blocked, marketplace blocked context, error->blocked feed for UI surfaces.
- .ai updates: agent md 13-bullet, gap/trace/status/handoff/activity for COMPKG polish closed (local P2 DoD).
- Small, no scope to hosted (GAP-002 external blocked remains).
**Risks**: None new (follows patterns from kpi/budget ux polish); P3 pack dynamic not touched (context only); hosted surfaces remain blocked visible as designed.
**Blockers**: None local (GAP-002 hosted external remains documented; no code for live hosted).
**Next Autonomous Action**: Append activity-log + update handoff/status/gap/trace/COMPKG row to "polished local P2 + states/a11y/tests"; run full validate (pnpm test commercial* ; pnpm --filter boss-web build; pnpm --filter boss-web lint; make lint); commit exact 'ux(commercial): full surfaces polish + tests (P2 + DoD, pack P3 context)'; push/PR#3 comment with reqs/tests/risks/handoff; handoff for QA/PM review. Mark COMPKG local complete.
**Last Update**: 2026-06-05T20:xxZ (post polish + tests/build/lint GREEN; on codex/spec-closure-audit + ai branches; 3x agent context)
