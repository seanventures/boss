# BOSS Autonomous Delivery Status

## 2026-08-08 BOSS-333 toolchain intake status

- `ai/codex/model-agnostic-agent-toolchain` implements sanitized, model-agnostic
  intake for the seven requested upstreams. Raw source is discarded after
  hashing/scanning; activation always requires a native BOSS implementation.
- Restricted licensing, missing per-item licensing, executable imports,
  destructive commands, path escape, revision drift, and policy bypass fail
  closed. Authoritative context is never compressed or silently omitted.
- Focused validation is green: 18 toolchain/executor tests, Ruff, compile, and
  diff hygiene. PR #141 merged to `main` as `ad6964f`; Plane BOSS-333 can move
  to Done with that merge evidence.

**Last updated**: 2026-08-08 (GA readiness evidence refresh)
**Repo root**: /Volumes/DataSSD1/test/boss
**Git remote**: https://github.com/seanheiney/boss
**Current integration baseline**: `origin/main` at `32d6d9d` (v0.10.0)
**Base branch**: origin/main; use focused `ai/codex/*` branches for new work
**Product**: BOSS v0.9.1 released; GA closure remains in progress (no new GA release published)
**Mission**: Complete product to production-grade, tested, documented, pushed, Codex-coordinated, ready for real users. Grind until all discovered PRD/spec features satisfy world-class DoD or truly blocked.

## Discovered Spec Sources (see .ai/spec-index.md)
- Primary: docs/product/PRD-v2.0.md + TECHNICAL_SPEC-v2.0.md
- Supporting: REQUIREMENT_INVENTORY.md, TRACEABILITY.md, GAP_BACKLOG.md, IMPLEMENTATION_STATUS.md, README, AGENTS.md, user-stories.md, packages/specpack/*, api/openapi.yaml, db/schema.sql
- GitHub: draft PR #16 carries BOSS-22/BOSS-23 and is stacked on draft PR #15; other open PRs were inspected for overlap before implementation.

## Overall Product Status
- **BOSS-335 model gateway is implementation-complete pending merge.** The
  focused branch adds SecretRef-bound API/service credentials, owner-only
  official subscription sessions, job-subordinate model leases with TTL,
  heartbeat, fencing and concurrency, pre-spend reservations, effect Receipts,
  invocation settlement, capacity/cost/token telemetry, and explicit expiry
  reaping. Provider credentials are never returned to agents or public APIs.
- **2026-08-08 GA truth.** PR #136 (`8e8dd4e`) is the canonical integration
  of the critical authorization, secret-binding, delivery-verdict, and MCP
  HTTP hardening that superseded contributor PRs #105–#114. Full local
  evidence on that exact integration passes: 144 web tests and 1,088 Python
  tests (four intentional skips), plus repository lint and smoke. GitHub has
  no open BOSS PRs or issues as of this refresh.
- **GA is deliberately unreleased.** The Plane GA umbrella remains In
  Progress. The required private-sandbox `main` protection is not enabled:
  GitHub returned HTTP 403 when asked to require an approving review, stale
  review dismissal, conversation resolution, admin enforcement, and no force
  pushes/deletions. The owner must upgrade/move the private sandbox to a
  qualifying GitHub plan or explicitly authorize making it public. Do not run
  a live PR demo or publish a GA release until that gate is resolved. GitHub
  Actions remains globally disabled, so no status-check rule is requested.
- BOSS-282 integrates the contributor budget-settlement patch: completed-no-PR
  and repair outcomes settle before release, while resumed runs charge only
  their previously unbilled delta.
- BOSS-279 integrates the v0.9.0 source release. Clean, lint, docs, production
  build, manifests, infrastructure profiles, dependency audit, smoke, 143 web
  tests, and 1,011 Python tests pass locally; four live-provider tests skip as
  expected. Hosted identity/account, live-cluster, and provider-write rollout
  remain explicitly fail-closed pending external configuration and approval.
- BOSS-270–278 provide one ranked and quota-bounded path from Plane-backed
  intake to fenced agent execution. Plane creation is a leased,
  receipt-producing job; Mission Control embeds priority, quota, ticket, job,
  GitHub deployment, Mattermost channel, and bot-presence truth.
- BOSS-237 turns the ProviderOps surface into a coherent System Configuration
  workspace with persisted Connections, Resources, Scaling & Health, and Cloud
  FinOps views; truthful provider availability; named write-only credential
  slots; permission-aware account lifecycle controls; saved five-dimensional
  resource scope; evidence drawers; partial-feed resilience; and responsive
  table containment. Automated web tests and production build pass, and live
  desktop/mobile validation confirms no viewport overflow or browser errors.
- BOSS-246 defines full project-scoped GitHub integration as nine Plane-backed
  delivery slices. BOSS-247 implements project-scoped connections/bindings.
  BOSS-248/BOSS-249 add immutable versioned branch policies plus Receipt-backed
  commit/tree/merge-base/protection/check snapshots with deterministic
  equal/rewrite/fast-forward/behind/diverged/missing/protected-blocked
  classification. BOSS-250 adds serialized one-owner branch/worktree leases
  with hashed one-time tokens, monotonic fencing, heartbeat, release,
  expiry/reclaim lineage, tenant scope, RBAC and audit. BOSS-251/BOSS-252 add
  deterministic CodeSyncRuns, canonical ChangeSets, exact lease/fence and fresh
  topology gates, non-force owned-branch sync, protected-target holds, typed
  ConflictSets, dedicated resolution branches, mandatory red-to-green proof,
  independent review, and receipted two-parent resolution commits without
  writing the protected target. BOSS-253 adds expiring exact-snapshot merge
  authorization, independent approval, a deterministic per-target queue,
  protected-target lease/fencing, fresh policy/check/review/topology gates,
  guarded PR-only merge, receipted result verification, rollback evidence,
  bounded retry and deadletter. BOSS-254 adds binding-resolved enqueue-only
  webhook signals, scheduled poll and explicit repair convergence, leased
  Receipt-backed rate/ref reads, immutable observations and canonical ref
  state, stale/out-of-order handling, drift invalidation, reset-aware bounded
  retry, redaction and deadletters. BOSS-255 completes Mission Control with a
  project/repository selector, canonical ref topology, branch evidence and
  divergence inspector, lease/sync/conflict/merge/reconciliation lanes,
  Receipt/rollback visibility, independently degrading read streams, and
  `github_manage`-aware setup/review/poll/repair controls. Apply operations
  requiring one-time lease secrets remain worker-only.
- BOSS-236 implements the provider-account credential lifecycle foundation
  beneath BOSS-235: truthful initial-cloud auth definitions, multi-slot
  SecretRefs, safe metadata, rotation lineage/overlap, disable/revoke gates,
  typed API/client contracts, and fail-before-adapter enforcement. Azure, GCP,
  and OVHcloud remain explicitly planned until their adapter tickets ship.
- BOSS-25–31 now have a load-bearing edition composition boundary: typed
  Budget/Entitlement/Persistence/Event/Decision ports, connection-free OSS
  defaults, enterprise adapters, direct code-pipeline injection, and
  community-versus-paid router mounting. BOSS-119 replaces the historical
  failing legacy baseline with a locally enforced 279/279 green matrix gate.
- BOSS-195–214 add twenty native reusable WorkflowSpecs for Ground Truth,
  Assurance, Agent Group, and Integration lifecycles. They seed idempotently
  into the existing Workflow Runtime but create no implicit work-class
  mappings. Fail-closed Decisions, dedupe keys, evidence artifacts, ownership
  revalidation, independent verification, preserved dissent, bounded group
  loops, SecretRefs, and intersection-only capability checks are contract
  tested. The workflow orchestration layer is implemented; post-engine-split
  persistence, action executors, workers, API/client, and UI remain correctly
  Partial under the parent features. Validation is green across 26 focused and
  adjacent Python tests, native seeding/idempotency, isolated API/worker Ruff,
  web route generation/TypeScript, SpecPack YAML, strict docs, compile, and
  diff checks. Full repository lint reaches the same two inherited F541
  findings in contributor-owned `intent_router.py`.
- PR [#74](https://github.com/seanheiney/boss/pull/74) merged to `main` as
  `79e1f94`; Plane BOSS-195–214 are Done with exact merge, validation, risk,
  and remaining-runtime-boundary evidence attached.
- BOSS-175–194 implement twenty non-overlapping SpecPack contract slices under
  the existing Ground Truth, Agent Group, Integration, and Assurance parents.
  The v2 manifest now catalogs all contracts and makes evidence-backed claims,
  closed-question interruption, single surface ownership, deterministic policy
  composition, governance-preserving group consensus, and intersection-only
  capability resolution explicit product invariants. Runtime persistence/API/
  worker/UI remains correctly Partial until the active engine split lands.
  Validation is green across 21 relevant Python tests, web TypeScript/lint,
  isolated API/worker Ruff, strict docs, YAML, and diff checks; the only full
  lint failure is two inherited F541 findings in contributor-owned
  `intent_router.py` covered by open PRs #23/#70.
- PR [#72](https://github.com/seanheiney/boss/pull/72) merged as `83eb39b`;
  Plane BOSS-175–194 are Done with merge and validation evidence attached.
- PR #71 merged to `main` as `c0b2493`; BOSS-148/BOSS-149 are shipped and Done.
- Paramg's July 19–26 mainline delivery adds the design-aware app pipeline,
  task-scoped agent workspace, preview/deploy hardening, Plane closed-loop
  sync, AI SRE/reliability surfaces, and VAG extensions. His active
  `feat/oss-engine-split` owns the API service relocation, so Codex selected the
  non-overlapping Mission Control UI tail. Plane BOSS-148/BOSS-149 now complete
  the roster and five-column task board with canonical task state and real
  job-to-receipt lineage; 119 web tests, lint/typecheck, and production build
  pass.
- BOSS-134–143 implement WFL-001 end to end: canonical workflow persistence,
  immutable spec validation and specpack seeding, deterministic work-class
  mappings, version-pinned replay-safe runs, leased/recoverable steps,
  retry/deadletter visibility, branch/fan-out/join/bounded loops, durable
  Decisions/timers and run controls, fail-closed capability/subworkflow
  bindings, reverse compensation, explicit migration/rollback, typed API/client
  contracts, and a Mission Control registry/run timeline. Focused and widened
  validation is green. Product-taxonomy PR #33 merged as `60ba2c8`; rebased
  workflow-runtime PR [#34](https://github.com/seanheiney/boss/pull/34) merged
  to `main` as `3637580`. Plane BOSS-132–144 are closed with merge and validation
  evidence, including the adjacent BOSS-144 lint repair.
- BOSS-133 separates Agent Groups, Skills/Integrations, Reusable Workflows, and
  Ground-truth/Assurance in the governing PRD/technical spec while defining how
  they compose. Plane root features are BOSS-129, BOSS-130, BOSS-132, and
  BOSS-123 respectively; BOSS-131 remains the Assurance Case child of BOSS-123.
  Traceability IDs AGP-001, INT-001, WFL-001, and AST-001 are published in the
  generated docs catalog. Strict documentation validation passes.
- BOSS v0.7.0 integrates the complete PR #24–#28 delivery: project skills and
  signed local marketplace/MCP, provider-neutral CI/CD and promotions, durable
  attributable previews, and confirmation-gated conversational feedback.
  Release validation passes lint, non-legacy API/worker/web tests, build,
  schema idempotency, smoke, docs, Kubernetes, and infra checks. BOSS-119 tracks
  the unchanged 37-test legacy matrix baseline; external hosted/provider paths
  remain intentionally blocked until their real credentials and approvals exist.
- OSS skill marketplace P1 is implemented for Plane BOSS-48/BOSS-49/BOSS-50/BOSS-55/BOSS-56: MCP tools share the SkillRuntime gauntlet and receipt ledger; local canonical bundles import/export SkillSpecs and MCP descriptors; Ed25519 trust and explicit untrusted overrides are enforced; per-project enable/disable and lazy attachment are live; current installed RoleSpecs are filtered and rechecked on the invocation hot path with scope-violation evidence. Mergeable draft PR #26 is stacked on PR #25. Its Documentation job ran zero steps and was rejected by the external GitHub account payment/spending-limit gate; local workflow-equivalent validation is green.
- BOSS-58–62 implement the CI/CD module's first five dependency-ordered slices: a shared health-gated rollout state machine for arbitrary DeploymentRuns, a GitHub Actions implementation of the provider-neutral pipeline contract, deterministic CI/Jenkins config generation through the existing budgeted/Receipted draft-PR path, a fail-closed staging→health-gate→production promotion entity, and a live console timeline. Jenkins/GitLab execution remains the separately tracked BOSS-63 follow-on.
- Validation for this slice: 7 focused Python acceptance/regression tests, 113 web tests, repository lint, production web build, smoke, double schema application, YAML/OpenAPI parsing, and diff checks pass. Three adjacent ProviderOps tests pass; two budget-dependent legacy tests reproduce their unchanged `reservation_id=None` failure on detached `origin/main` and are not caused by this branch.
- Publication: implementation commit `d5800f6` is pushed in mergeable draft PR [#27](https://github.com/seanheiney/boss/pull/27). Plane BOSS-57–62 contain the commit, PR, validation, risk, and pending-merge evidence and remain In Progress until merge.
- BOSS-118 integrates every unmerged Codex feature stack from current `origin/main`: reactive docs/webhooks, docs agent/site, GitHub community automation, conversational orchestration, executable skill registry, BOSS-112 design system, and bare-metal deployment readiness. Parallel contributor `feat/*` branches remain excluded. Semantic conflicts were resolved as unions, including 10 default reactive subscriptions, both docs/GitHub worker paths, the agent panel inside the new Phosphor design navigation, and OpenAI-compatible chat fallback with retry telemetry.
- Integrated validation: `make lint`, production `make build`, `make smoke`, strict `make docs`, `make k8s-validate`, `make infra-validate`, and `make deps-audit` complete. Web tests pass 112/112; the Python run passes 388/425, with all 37 failures confined to the documented legacy `test_api.py` baseline. Representative auth, ProviderOps, and budget failures reproduce unchanged on detached `origin/main`. External release readiness remains deliberately fail-closed without hosted identity/provider credentials.
- Reactive feature documentation advanced on 2026-07-15: Plane BOSS-73/BOSS-74/BOSS-75 now provide allowlisted and manual entity-debounced `docs.update` jobs, evidence/context assembly, a validated `docs_writer` role, managed Markdown preservation, a budgeted/Receipted stable-branch draft PR path, TRACEABILITY reconciliation, feature freshness, worker retry/deadletter handling, and no automatic merge. Validation: 7 docs-agent tests, 7 reactive regressions, 27 worker tests, focused Ruff/compile/YAML, generator drift, and strict MkDocs all pass.
- Event/webhook operability and the public OSS docs surface advanced on 2026-07-15: Plane BOSS-24 now has a persisted reactive-dispatch exactly-once canary, related webhook-delivery identity in the shared deadletter feed, and a complete setup/recovery runbook. Plane BOSS-76 adds a deterministic TRACEABILITY-backed feature catalog site, strict local/CI MkDocs build, drift test, and reviewed-main-only Pages deployment. Focused/widened validation: 45 Python tests and 26 web tests pass; Next production build, TypeScript, focused Ruff/compile, YAML, diff, generator drift, and strict docs build pass. Repository-wide `make lint` reaches and passes the web lint/type stage, then reports the same 13 unrelated pre-existing Python lint errors documented on PR #16.
- Historical publication: implementation commit `5887088` was pushed in stacked draft PR #17; that stack is now absorbed into BOSS-118. GitHub Actions run `29425529835` created no steps and was rejected by the repository account payment/spending-limit gate; local workflow-equivalent validation is green.
- GitHub community automation is implemented for Plane BOSS-84/BOSS-86–88: strict signed typed ingress, loop/delivery guards, leased triage and PR-review agents, Receipt-backed provider effects, safe diff-only review, value/ROI feedback, hold-default standard decisions, approval-gated plan dispatch, and authorization-only/no-auto-merge PR approval. Focused API and worker acceptance tests pass; this stack is included in the BOSS-118 main release.
- Conversational orchestration is implemented for Plane BOSS-78–82: actor-owned persisted threads, `POST /v1/agent/chat`, a transport-free permission-rechecking intent router, a canonical feature-catalog manifest, explicit confirm-before-dispatch multi-task plans, and a thin Mission Control chat panel. Focused API acceptance tests and web type validation pass; this stack is included in the BOSS-118 main release.
- GitHub community enablement advanced on 2026-07-15: Plane BOSS-85/BOSS-89 add App-authenticated issue/PR comments, inline reviews, unified diff reads, scoped `issues` permission, a four-event App manifest, setup guidance, and 503/no-write behavior when no inbound secret is configured. Focused adapter/webhook tests pass; the triage and PR-review consumer slices are included in BOSS-118.
- OSS webhook foundation advanced on 2026-07-15: Plane BOSS-22/BOSS-23 now provide Receipt-backed signed outbound delivery with leased retries/deadletters and a raw-body-verified, idempotent inbound GitHub receiver that routes into the reactive event dispatcher. Ten focused API/integration tests, seven foundation regressions, and twenty-five worker tests pass.
- OSS-readiness foundation advanced on 2026-07-15: transactional allowlisted event dispatch, project subscription registry/CRUD/defaults, and TRACEABILITY-derived feature catalog are implemented for Plane BOSS-20, BOSS-21, and BOSS-72. Downstream webhook and docs-agent consumers are included in BOSS-118 and remain separately traceable in Plane.
- Current validation: 7 new acceptance tests + 5 representative legacy job tests pass on fresh Postgres; schema reapplication, focused lint/compile, diff checks, and OpenAPI YAML parsing pass. Full API baseline remains 241 passed/38 failed; selected failures reproduce unchanged on pristine origin/main.
- Coordination: mergeable draft PR #17 targets PR #16's branch, which targets PR #15; open PR #14 is frontend-only and non-overlapping; open PR #9 overlaps `main.py`/API-test regions and needs normal rebase conflict care.
- OSS executable-skills P0 core now spans Plane BOSS-45/BOSS-46/BOSS-47/BOSS-51/BOSS-52/BOSS-53/BOSS-54. The effective edition pack filters project catalogs; the task resolver unions canonicalized module defaults, system skills, and enabled tenant rows under RoleSpec filters; eager SkillSpecs enter the live agent schema beside native/MCP tools and invoke the same permission/budget/retry/receipt runtime. High-risk skills pause on blocking decisions and revalidate availability before approved resume. BOSS-47/BOSS-53/BOSS-54 are on stacked branch `ai/codex/boss-47-53-54-skill-resolution` above draft PR #24.
- Draft PR #24 is cleanly based on current main. GitHub Actions CI and Documentation runs `29498267699` / `29498267668` were rejected before checkout with zero steps by the account payment/spending-limit gate; local workflow-equivalent validation is green and the external blocker is linked from Plane.
- Audit slice (spec closure, .ai/ OS, docs AC, recent gaps 001-003/005/011/012) COMPLETE per DoD: local P0s closed, P2s addressed by 4 subagents + branch commits (41e5243, 527e9a1). Additional: Security agent closed rate P2 (GAP-013) with in-mem limiter for high-cost paths + tests + updates. Validations green, pushed to PR #3, .ai/ current.
- PM prd-audit (this) COMPLETE: full PRD-v2.0 + TECH audit vs impl/.ai/ (read thesis/planes/objects/journeys + code+openapi+trace); no other local implementable gaps/vertical slices not covered (comms/self-imp/commercial local/elastic covered fully local); noted GAP-PRD-AUDIT-001 P3; "coverage good, only external left". .ai/ updated + commit pending.
- Core product largely implemented locally (control, budget full, ProviderOps local+real sandbox, SWF, console, roles, MEB, auth, etc.) per IMPLEMENTATION_STATUS + PRD-v2.
- Local dogfood functional (make dev/seed/test/lint/build).
- Real-only enforced.
- Remaining local: P2 polish (e2e etc); rate 013 + pagination 014 closed; AUTO-004 (P3 .env/release-blockers + full-validate) CLOSED by DevOps agent (comments for all BOSS_*, script, target, validations run).
- External P0s (002-004) Blocked (creds/deploy needed; local fail-closed complete).
- No core TODOs/stubs (scan clean).
- .ai/ OS ready for ongoing autonomous work. See .ai/release-readiness for slice complete note.

## Active Grok Agents / Workstreams
(See .ai/agents/ for per-agent .md + updates)
- ORCHESTRATOR / TECH LEAD (primary; .ai/ + PR #3 + report + subagent launch complete).
- 9 agent .md created/updated.
- 4 subagents complete + pushed (UX 001, Sec, QA 002, PM/Docs 527e9a1 for AUTO-003). + Security rate P2 task closed GAP-013. + Arch pagination agent closed GAP-014. + DevOps AUTO-004 task closed (P3 .env/script/full-validate/validations). Branch commits 41e5243 (011/012) + 527e9a1 (AUTO-003) + rate/pagination + this AUTO-004. All local for slice (P0s + AUTO 001-005 + 011-014) closed/monitored. Validator + rate + pagination + devops tests OK. PR #3 current. Local P0 0; slice + rate + pagination + AUTO-004 complete. Next: full product or remaining P2 (e2e).
- More spawnable. Subagent outputs will drive P2 fixes + re-validate.

## Active Branches / Worktrees
- codex/spec-closure-audit (current; pushed 89166c2 with .ai/ OS + gitignore fix)
- codex/auto-004-env-release (DevOps AUTO-004 for commit/push + PR#3 comment)
- No additional worktrees yet.
- Planned: ai/grok/* for P2 polish if needed.

## Open PRs
- #3 (Grok mission): https://github.com/seanheiney/boss/pull/3 - "chore: spec closure audit - .ai/ autonomous OS + traceability for P0 gap closures (KPI, budget, security, audit)" (head codex/spec-closure-audit)
- #2 cursor/critical-bug-investigation-5450 (Account Connect entitlement drift/SCIM)
- #1 cursor/critical-bug-investigation-64e0 (Account Connect/SCIM lifecycle)
- PR #3 body has req IDs, user behavior, files, decisions (product/ux/arch/sec), tests (all GREEN), Codex notes, risks.

## Completed Requirements (high level, see .ai/requirements-traceability.md)
- All local: MET, DEC, CAN (incl KPI), BUD (full), RUN, AUD (incl logging), RPL (local), MEB, SWF, AGT, COM, SEC (incl gates), NFR.
- Traced, DoD satisfied, validated, PR #3, .ai/ matrix.
- External Blocked noted.

See .ai/requirements-traceability.md for full matrix + status "Yes" for local.

## P0/P1/P2/P3 Gap Counts (from docs + scan at start)
- P0 closed recently: 4 (audit log, bootstrap, budget contract, KPI, route parity)
- P0 remaining: 3 (all externally Blocked: Account Connect, Enterprise, ProviderOps live rollout)
- P1: 1 monitoring (placeholders - ok), some potential polish
- P2/P3: UX polish, more e2e coverage, docs refresh, mobile/accessibility edges per reviews. Rate P2 now closed (GAP-013).
- New gaps may surface from full validation + agent reviews.

## Tests and Validation Results (baseline to be run)
- Pending full run in this session. Known make targets: test, lint, smoke, build, real-smoke-*, k8s-validate, infra-validate, release-blockers.
- Current uncommitted changes include new tests (kpis.test.ts, api test updates, web tests).

## Product / UX / Architecture / Security / QA Decisions (initial)
- See .ai/*-review.md after agent runs.
- Key posture from PRD/AGENTS/README: real-only, API-first (web is client), receipts always, reservations for spend, leases always, no silent, fail-closed defaults, small reviewable PRs, state machine tests.

## Known Conflicts / Coordination Risks
- Current branch has uncommitted changes that align with "Closed" in GAP_BACKLOG - these must be committed with req IDs, validated, pushed, PR opened (or cleaned if conflicting with cursor PRs).
- Cursor PRs on main may conflict on Account Connect/SCIM areas (but those are Blocked anyway).
- No human/Codex concurrent edits detected in local tree at start.
- Git push/PR will use gh cli or MCP; auth confirmed.

## Blockers (true stops per rules)
- Missing external creds/secrets for hosted Account Connect, live providers (DO/CF/Supabase/OpenAI/Anthropic write), IdP.
- No paid service setup without approval.
- Destructive prod actions: avoided (use sandbox, fail-closed).
- GitHub perms: ok (gh logged in with repo scope).
- System deps: assume make works (postgres/docker needed for full dev/test).

## Recommended Next Autonomous Actions
1. Write full .ai/requirements-traceability.md + .ai/gap-backlog.md (P0 prioritized).
2. Run baseline validation: make lint, make test (with DB), make smoke, pnpm build where applicable; capture output.
3. Commit current coherent changes on ai/grok/spec-closure-audit or focused subbranch with proper messages (include req IDs like GAP-2026-06-05-009).
4. Spawn parallel agents via spawn_subagent for reviews + any missing slices.
5. Use worktrees if parallel feature work needed.
6. Push branch, open PR via gh or MCP with full DoD description (reqs, decisions, tests, risks).
7. Update codex-handoff.md.
8. Close any new P0/P1 that are local-code (not external-blocked).
9. Run full reviews, fix, re-validate.
10. Print 15-min terminal updates + append to activity-log.
11. Continue until all feasible work complete per world-class DoD or truly blocked; produce final report + .ai/release-readiness.md only at end.

## 2026-08-09 — BOSS-18/BOSS-123–131 implementation status

- Ten Plane tickets are In Progress on
  `ai/codex/boss-18-123-131-runtime`, based on contributor-current `main`.
- Canonical persistence, service/runtime behavior, authenticated APIs, typed
  client projections, and an Assurance Center UI are implementation-complete
  for Ground Truth/Assurance, Agent Groups, Integration Registry, and
  project-level model usage reporting.
- No provider credential enters the public API or UI. Spendy verifier/model
  actions retain Job leases, BudgetReservations, Receipts, dedupe, and audit.
- Affected-path validation is green: 21 focused API/runtime tests, 10 assurance
  contracts, 153 web tests, Ruff, TypeScript, strict docs, schema replay,
  OpenAPI parse, and production build. The branch is current with `origin/main`;
  commit, PR, and merge evidence remain before the tickets can move to Done.

## 2026-08-09 — BOSS-336 v0.11.0 release status

- Release candidate is based on contributor-current `main` `004b030`; local
  main was fast-forwarded cleanly by 15 commits before the release branch.
- Version/changelog metadata is aligned across application, engine, client,
  Helm, and Kubernetes surfaces.
- Release gates pass: 102 affected Python tests, 155 web tests, fresh schema
  replay, lint/typecheck, strict docs, production build, wheel import, smoke,
  Kubernetes/Helm, infra profiles, and web/API/worker/Account Connect images.
- Source/local/self-hosted release is ready. Hosted identity/support/provider
  writes and live-cluster rollout remain blocked by design pending external
  configuration and approvals. Commit/PR/tag/release evidence remains.

## Files Changed (session so far)
- Created: .ai/spec-index.md, .ai/status.md, .ai/...
- Modified this Security rate task: apps/api/app/config.py, apps/api/app/main.py, apps/api/tests/test_api.py, apps/api/tests/conftest.py, api/openapi.yaml, .ai/gap-backlog.md, .ai/security-review.md, .ai/agents/security.md, .ai/status.md, .ai/codex-handoff.md, .ai/activity-log.md (plus prior)

See activity-log.md for chronological.

## 2026-07-16 — preview/feedback/pipeline slice

BOSS-35–43 and BOSS-63 are implementation-complete on
`ai/codex/boss-35-43-63-preview-feedback`. Focused acceptance, full web, and all
backend/worker tests outside the inherited failing legacy API matrix are green.
Commit `1eb1d36` is published in stacked draft PR
https://github.com/seanheiney/boss/pull/28 (base: PR #27 branch). Plane evidence
is attached; tickets stay In Progress until merge. The inherited API matrix
baseline is tracked separately as BOSS-119.

## 2026-07-26 — v0.8.0 integration release

BOSS-215–234 are implementation-complete on `ai/codex/release-v0.8.0`.
Paramg's OSS engine split (`52ff073`) and the non-overlapping fixes from PRs
#68/#70 are integrated with relocation, packaging, runtime, security, and
release regressions closed. Current validation is green across the affected
backend/worker/engine suite (348 passed, 1 skipped), focused regression suite
(94 passed), web suite (119 passed), seed/smoke, lint/typecheck, strict docs,
production web build, isolated wheel/container imports, API/worker containers,
Kubernetes/Helm, and infra profiles. PR #76 merged as `bc4c4d6`; the annotated
`v0.8.0` tag peels to that exact commit and is published as the latest GitHub
release. Source PRs #68/#70 are merged and #23/#69 are closed as superseded.
Plane BOSS-215–234 plus stale shipped package tickets BOSS-32/33 are Done.
External hosted/provider rollout remains fail-closed by design.

## 2026-07-26 — BOSS-256–266 Agent Studio and global A2A fabric

Implementation is complete on `ai/codex/boss-256-agent-studio-a2a`.
Agent Studio now owns immutable agent/panel configuration and test/run
evidence while RoleSpec, project entitlements, policy, budget, and leases
retain authority. Twelve seeded specialists and three Panels are executable.
Its Workflow lane now exposes the canonical Workflow registry in the same
workbench with creation, editable graph preview, side-effect-free validation,
immutable publication, pinned execution, and recent-run visibility.

The A2A bus uses Postgres as canonical truth and Redis Streams only as the
delivery adapter. XADD/XACK are Receipt-backed; consumer loss uses XAUTOCLAIM
and bounded deadlettering; unknown schemas, incompatible boundaries, missing
leases, and unavailable live-model budget/provider paths fail closed.

Current evidence is green: 121 web tests; 938 Python tests with four
intentional live/external skips; 39 focused Agent Studio/A2A/worker/assurance
tests; Ruff, TypeScript, OpenAPI YAML, diff checks, optimized Next build,
desktop/responsive browser QA, and two real-Redis lifecycle proofs. Draft PR
#88 is cleanly mergeable against `main`; Plane BOSS-256–266 carry the commit,
PR, and validation evidence and remain In Progress until merge.

Post-workflow-lane evidence is also green: 12 focused Agent Studio/Workflow
runtime tests, all 121 web tests, Ruff, TypeScript, diff checks, and the
optimized Next production build. Draft validation proves graph reachability,
typed bindings, and bounds without publishing or executing work.

Contributor `main` tip `98ab5f4` (BOSS-267) was merged without conflict at
code/integration head `d0d3a6354a1214459db23e212c1467cd292d1506`;
22/22 combined Studio, Workflow Runtime, and repo-host tests pass, and GitHub
reports draft PR #88 cleanly mergeable.

## 2026-07-26 — BOSS-268 guided Agent Studio wizard

Implementation is complete on `ai/codex/boss-256-agent-studio-a2a` and extends
draft PR #88. Agent Studio now has a five-stage agent commissioning flow for
identity/RoleSpec, runtime and prompt layers, deterministic rules and requested
capabilities, scope/limits/output/A2A contracts, and final compile review.
Blank, builder, reviewer, and operator patterns remain editable and do not
grant authority. The live blueprint exposes the proposed RoleSpec, model
posture, capability requests, coordination topics, and runtime envelope.

Save Draft creates the stable definition plus immutable version. Validate +
Publish uses the existing server lifecycle in order and preserves partial
draft identifiers and errors on failure. The UI accepts integration references
only, never credentials. Plane BOSS-268 remains In Progress until PR #88
merges.

Validation is green: 125 web tests, seven focused Agent Studio/A2A API tests,
Ruff, TypeScript, generated feature-doc parity, strict MkDocs, optimized Next
build, and a real browser create/validate/publish/refresh journey. Browser QA
also verified the portal-based full-screen focus layer and all five stages.
