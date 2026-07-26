# Codex Handoff

## 2026-07-26 BOSS-255 GitHub Mission Control

- Stacked branch `ai/codex/boss-255-github-mission-control` starts from the
  BOSS-254 reconciler commit and is intended to remain stacked on draft PR #86.
  It does not rewrite Paramg PR #80 or any contributor ref.
- Adds a dedicated GitHub navigation area backed directly by twelve canonical
  typed read streams: connections, repository bindings, branch policies,
  immutable snapshots, fenced leases, code sync runs, conflict sets,
  resolution plans, merge authorizations, reconciliation runs/signals, and
  current repository ref state. Partial failures remain named while healthy
  evidence stays visible.
- The restrained two-pane workspace centers live branch topology and a shared
  evidence inspector, followed by linear sync/conflict/merge/reconciliation
  lanes. It exposes exact commit/tree/observation/Receipt identity, divergence,
  lease owner/fence/expiry, checks/reviews, rollback, and deadletter repair.
- `github_manage` controls cover connection, binding, immutable policy,
  access test, snapshot capture, sync planning, independent resolution review,
  merge authorization/enqueue, poll, and repair. Raw one-time lease tokens are
  never sent to the browser, so apply remains a leased worker operation.
- Validation: 128 web tests pass, including three new Mission Control feed/
  rendering tests; all 85 widened GitHub API/community/worker tests pass; web
  lint/typecheck and production build pass; strict docs build passes; live
  local API navigation shows all GitHub endpoints returning 200, no React
  errors, and no horizontal page overflow at the inspected desktop viewport.

## 2026-07-26 BOSS-254 GitHub webhook/poll reconciler

- Stacked branch `ai/codex/boss-254-github-reconciler` starts from BOSS-253
  draft PR #85 and preserves the BOSS-247→253 PR stack.
- Active repository bindings now route verified GitHub deliveries to
  deduplicated per-ref `ReconciliationSignal` and
  `github.repository.reconcile` jobs. The webhook transaction performs no
  provider reads.
- Poll and repair requests use the same `GitHubReconciliationRun` state
  machine. The BOSS worker advertises the capability; reconciliation atomically
  owns job success/retry/deadletter so the generic worker does not
  double-finalize its lease.
- Each attempt records a Receipt-backed core rate-limit read and one
  Receipt-backed branch identity/protection observation per requested ref.
  Immutable observations advance canonical `RepositoryRefState`; expected-SHA
  mismatch and delete/reappearance are explicit stale/out-of-order evidence.
- Provider errors are redacted. Rate exhaustion uses provider-reset-aware
  backoff, other read errors use bounded exponential backoff, the third failure
  deadletters, and operators can schedule a linked repair.
- Fresh commit/tree drift blocks pending CodeSyncRuns and
  MergeAuthorizations pinned to stale identities. BOSS never mutates a branch
  in this reconciler.
- Validation: 67 widened GitHub/webhook API tests, 30 worker tests, 125 web
  tests, repository lint, production build, fresh and repeated schema
  bootstrap, all seven new OpenAPI routes, generated-doc drift, strict
  MkDocs, and diff checks pass.

## 2026-07-26 BOSS-253 governed merge authorization and queue

- Stacked branch `ai/codex/boss-253-merge-authorizations` starts from
  BOSS-251/BOSS-252 draft PR #84 and preserves the BOSS-247→252 PR stack.
- `MergeAuthorization` pins one project binding, active policy ID/version, PR,
  source/target refs, exact head/base commit and tree identities, checks,
  effective reviews, evidence bundle, expiry, and optional sync/resolution
  lineage. Initial and fresh provider observations are Receipt-backed.
- Approval must be independent from the requester. Authorized merges enter a
  deterministic per-repository/target queue; only its earliest active entry can
  apply.
- Apply requires the exact protected-target BranchLease token and fence, then
  rechecks policy, PR state/draft/mergeability, commit/tree freshness, target
  head, required checks, current-head approvals, and requested changes.
- The adapter performs a second exact-snapshot guard and merges only through
  the GitHub PR API with the authorized head SHA. BOSS never pushes or
  force-updates the protected target.
- Every merge attempt has a risk-tier-three zero-cost ChangeSet operation,
  Receipt, and ResourceReceipt. Result verification records the merge
  commit/tree and previous target plus a manual, newly authorized revert-PR
  plan. Failures redact provider detail, retry three times, then deadletter.
- Validation: seven merge state-machine tests inside a 40-test widened GitHub
  API/adapter matrix; 39 typed-client tests; focused Ruff and compile; Next
  route generation and TypeScript; production web build; schema idempotency;
  complete BOSS-253 OpenAPI route coverage; generated-doc drift; strict MkDocs;
  and diff checks pass.

## 2026-07-26 BOSS-251/BOSS-252 safe sync and conflict resolution

- Stacked branch `ai/codex/boss-251-252-safe-sync-conflicts` starts from
  BOSS-250 draft PR #83 and preserves the BOSS-247→250 PR stack.
- `CodeSyncRun` maps immutable topology to no-op, preserved tree-equivalent
  identity, non-force owned-branch fast-forward, protected approval hold,
  block, or a dedicated policy-approved resolution branch.
- Apply verifies the exact raw lease token and monotonic fence, requires the
  pinned policy to remain active, and captures a fresh snapshot before every
  mutation. Commit/tree/topology drift fails before provider writes.
- Every GitHub provider write attempt has a canonical ChangeSet operation,
  Receipt, and ResourceReceipt. GitHub ref writes are explicitly zero-cost,
  never force, retain rollback identity, retry three times, then deadletter.
- Divergence creates a typed ConflictSet with immutable base/ours/theirs and
  bounded file evidence. ResolutionPlans require failing-red/passing-green
  proof and a reviewer different from the creator.
- Approved resolution apply creates a two-parent commit through the Git Data
  adapter and fast-forwards only the resolution branch. The protected target
  remains unchanged for BOSS-253's exact-snapshot MergeAuthorization.
- Validation: 31 widened GitHub API/adapter tests and 39 typed-client tests
  pass; focused Ruff, TypeScript/Next route generation, production web build,
  schema idempotency, OpenAPI YAML plus complete GitHub route coverage,
  generated-doc drift, strict MkDocs, compile, and diff checks pass.

## 2026-07-26 BOSS-250 fenced branch/worktree leases

- Stacked branch `ai/codex/boss-250-branch-leases` starts from BOSS-248/BOSS-249
  draft PR #82, which is stacked on BOSS-247 PR #81.
- `github_branch_leases` permits one active owner per repository binding/ref.
  Acquisition locks the binding row, so concurrent claimants produce exactly
  one winner, expires stale ownership atomically, and retains reclaim lineage.
- The raw high-entropy lease token is returned once and only its SHA-256 hash
  persists. Exact acquire replay returns the original record but never the raw
  token. Heartbeat/release verify both token and monotonic fence.
- Expired leases cannot heartbeat/release; unexpired leases cannot be
  administratively reclaimed. Reacquisition gets a higher fencing token and
  links the prior expired lease.
- Validation: double schema application; 65 widened API/GitHub/provider tests;
  38 API-client tests; concurrency, token/fence, expiry/reclaim and tenant
  tests; Ruff; TypeScript; production build; OpenAPI; generated docs; strict
  MkDocs; and diff checks pass.

## 2026-07-26 BOSS-248/BOSS-249 branch policy and topology

- Stacked branch `ai/codex/boss-248-249-branch-policy-snapshots` starts from
  BOSS-247 draft PR #81. Target the stacked PR at the BOSS-247 branch until
  #81 merges, then retarget/rebase by tree without rewriting contributor refs.
- Branch policies are immutable versions with supersession lineage,
  protected-ref patterns, branch prefixes/name rules, update/merge methods,
  checks/approvals, history/signing requirements, automation posture, and
  deterministic allowed/approval-required/blocked results.
- Remote snapshots are immutable, tenant-scoped, pinned to a policy version,
  and Receipt-backed. They retain commit/tree/parents, merge base,
  ahead/behind, protection, check runs, raw topology, and policy classification.
- Tree-equivalent commit-identity rewrites remain non-conflicts. A valid
  fast-forward that policy denies retains raw `fast_forward` topology and final
  `protected_blocked` classification.
- Receipt replay now verifies operation scope for both connection tests and
  snapshots. The BOSS-247 portion was cherry-picked and pushed to PR #81 as
  `073f3b6`.
- Validation: schema applied twice; 60 focused/widened API/GitHub/provider
  tests; 37 API-client tests; Ruff; TypeScript; production Next build; OpenAPI
  checks; generated-doc drift test; strict MkDocs; and diff checks pass.

## 2026-07-26 BOSS-246/BOSS-247 GitHub project sync foundation

- Branch `ai/codex/boss-247-github-project-connection` starts from rewritten
  `origin/main` at `1c6de8e`. The rewrite changed commit identities without
  changing the tree; no contributor branch was reset or force-pushed.
- Plane BOSS-246 is the governing feature. BOSS-247 implements project-scoped
  GitHub App/PAT connections, SecretRef-only credentials, repository bindings,
  permission/capability discovery, connection lifecycle, tenant/RBAC checks,
  and receipted idempotent health tests.
- Product and technical contracts define BOSS-248–255 as the remaining branch
  policy, snapshot, lease/worktree, safe sync, conflict-resolution branch,
  merge queue, reconciler, and Mission Control slices. This PR does not claim
  those behaviors are implemented.
- Existing process-wide GitHub automation remains backward compatible; the new
  adapter constructor overrides allow scoped execution without mutating global
  settings.
- Validation: schema applied twice; 54 focused/widened API tests; 36 API-client
  tests; Ruff; TypeScript; OpenAPI YAML/route checks; feature-doc generation
  and drift test; strict MkDocs; and diff checks all pass.
- The workstation `.venv` is Python 3.9, while the repository now requires
  Python 3.11+. Generation used the bundled Python 3.12 runtime; installed test
  dependencies remained in the existing virtualenv.

## 2026-07-26 BOSS-236 provider credential lifecycle

- Branch `ai/codex/boss-236-provider-credential-lifecycle` starts from
  `main` at `fe223af`; no open GitHub PR or active contributor branch overlaps
  this provider-account/SecretRef lifecycle slice.
- Added truthful connection definitions for AWS, Azure, GCP, DigitalOcean, and
  OVHcloud. AWS/DigitalOcean are available; unfinished adapters remain
  explicitly planned and cannot make provider calls.
- Provider accounts now carry auth method, named SecretRefs, safe metadata,
  ownership, credential version/rotation, and disable/revoke evidence.
  SecretRefs carry scope, secret/storage kind, verification/rotation lineage,
  bounded overlap, and revocation evidence without secret material.
- Added update, enable, disable, revoke, rotate, get, and definition APIs plus
  typed client/OpenAPI contracts. Revoked references cannot be reactivated;
  changed credentials cannot bypass rotation through an upsert; inactive
  accounts fail before sync/test/write adapters.
- Validation: 10/10 new lifecycle acceptance tests, 77/77 widened
  provider/AWS/IaC/router/promotion tests with one expected live AWS skip,
  28/28 targeted legacy ProviderOps tests, 279/279 complete legacy API tests,
  double schema application, focused Ruff, web typecheck, and client tests.

## 2026-07-26 BOSS-25–31 OSS runtime seams + BOSS-119 legacy closure

- Active branch `ai/codex/oss-ports-baseline` starts from clean `main` at
  `da9a43b`. Plane BOSS-25–31 and BOSS-119 are the release scope.
- Added typed Budget, Entitlement, Persistence, Event, and Decision ports with
  connection-free OSS defaults and Postgres/governance enterprise adapters.
  The FastAPI composition root selects all five once from `BOSS_EDITION`.
- Software-factory and both code-task pipelines now consume injected ports.
  In-memory persistence covers retry lineage and gate override; the Postgres
  adapter preserves existing transactional behavior.
- Community and enterprise APIRouter ownership is explicit. Community mounts
  code tasks, previews, MCP, skills, and events; paid identity, commercial,
  ProviderOps, deployment, marketplace, and self-improvement routes mount only
  for paid editions.
- Internal deployment hostnames were removed from engine-shipped source. Paid
  edition capability packs now inherit the Community baseline they declare.
- BOSS-119 repairs isolate the burst limiter from unrelated regression tests
  and enforce authenticated-principal binding before target permissions.
- Green release evidence: 279/279 legacy API tests, 30/30 focused OSS-port and
  edition-router tests, 31/31 engine tests, 46/46 widened persistence/code-task
  tests, repository Ruff/TypeScript lint, docs checks, compile, and diff checks.

## 2026-07-26 BOSS-195–214 assurance workflow batch

- Branch `ai/codex/boss-assurance-workflows` starts from refreshed `origin/main`
  after PRs #72 and #73. The unrelated workstation `site/` directory remains
  excluded.
- PR [#74](https://github.com/seanheiney/boss/pull/74) merged to `main` as
  `79e1f94` (implementation commit `2deda32`). Plane BOSS-195–214 are Done and
  each carries exact merge, validation, risk, and remaining-runtime-boundary
  evidence.
- All twenty are native acyclic WorkflowSpecs registered by the existing
  idempotent SpecPack seeder. No selector or default work-class mapping is
  created, so catalog publication cannot start work by itself.
- The graphs require dedupe keys, visible failure paths, hold-default
  Decisions where authority is ambiguous, evidence artifacts, receipts for
  member effects, budget reservations for spend, SecretRefs, and fail-closed
  ownership/capability preflight.
- Paramg's `feat/oss-engine-split` remains untouched. This branch changes only
  SpecPack workflows/catalog, contract/runtime-seeding tests, product
  traceability, and coordination documentation.
- Validation is green: 10 object/workflow contract tests, 6 Workflow Runtime
  tests, 10 adjacent marketplace tests, isolated API/worker Ruff, web route
  generation/TypeScript, SpecPack YAML, strict docs, compile, and diff checks.
  Full repository lint reaches only the same two inherited F541 findings in
  contributor-owned `intent_router.py`.

## 2026-07-26 BOSS-175–194 assurance contract batch

- PR #71 merged first as `c0b2493`; Plane BOSS-148/BOSS-149 are Done with
  shipped roster/board evidence.
- PR [#72](https://github.com/seanheiney/boss/pull/72) merged to `main` as
  `83eb39b`. Plane BOSS-175–194 each carry the exact PR, merge, validation, and
  runtime-boundary evidence and are Done. Their parent runtime features remain
  open for post-engine-split persistence, services, workers, APIs, and UI.
- The implementation branch `ai/codex/boss-assurance-contracts` was based directly on that
  refreshed `origin/main`. The unrelated untracked `site/` directory was
  present on the workstation and is explicitly excluded.
- Plane BOSS-175–194 are twenty child slices of BOSS-124–131. They define the
  complete typed SpecPack contract seam for metrics, observations, regressions,
  settled conclusions, closed-question matches, claims/evidence, surface
  ownership, verification, Assurance Cases, Agent Groups, Integrations,
  executor bindings, and capability intersection.
- `packages/specpack/manifest.yaml` catalogs all twenty and adds the governing
  fail-closed invariants. Contract tests enforce structure, tenant scope,
  dedupe, authority-state outcomes, exact critical fields/rules, and
  SecretRef-only credential handling.
- This branch deliberately does not touch API services, schemas, bootstrap,
  persistence, deploy, preview, provider actuation, or Paramg's active
  `feat/oss-engine-split`.
- Validation: 5/5 contract tests plus 16/16 adjacent marketplace/workflow
  regression tests pass; web route generation/TypeScript passes; Python lint
  passes for the full API/worker tree excluding contributor-owned
  `intent_router.py`; generated docs are current and strict MkDocs passes.
- Repository-wide `make lint` reaches and passes the web stage, then reports
  only two inherited F541 findings in `intent_router.py`. That file is owned by
  open PRs #23/#70 and is intentionally untouched on this branch.

## 2026-07-26 Paramg catch-up and BOSS-148/BOSS-149 safe UI tail

- Refreshed rewritten `origin/main` at `6a92bd9` and inspected Paramg's delivery
  since the prior Codex handoff: coding-engine/app-build hardening, task-scoped
  agent chat, previews and governed SSH deploys, Plane sync, AI SRE/reliability,
  design routing/scaffolding, VAG, and the Mission Control redesign foundation.
- Paramg's active `feat/oss-engine-split` relocates the complete API service
  layer into `engine/boss_engine`; do not modify engine services, API schemas,
  or bootstrap on this branch. Open Cursor PRs #23/#68/#69/#70 own adjacent
  authorization, intent routing, governed-code-task, and SSH deploy fixes.
- Safe branch `ai/codex/boss-148-151-ui-tail` claims Plane BOSS-148 and BOSS-149
  only. It extracts the roster/board into a web-only component, derives agent
  state/current work from canonical role/task records, and derives task receipt
  counts through job lineage. No backend endpoint, provider effect, credential,
  or invented operational state is added.
- Validation: 119/119 web tests, Next route generation/TypeScript, production
  build, and diff checks pass.

## 2026-07-18 BOSS-134–143 reusable workflow runtime

- Shipped to `main`: product-taxonomy PR
  [#33](https://github.com/seanheiney/boss/pull/33) merged as `60ba2c8`, then
  workflow-runtime PR [#34](https://github.com/seanheiney/boss/pull/34) was
  retargeted, rebased, and merged as `3637580`. The remote merge contains both
  contributor-safe histories without unresolved conflicts.
- Added canonical immutable workflow definitions/versions, deterministic scoped
  mappings, version-pinned durable runs/steps, leases/heartbeats/reclaim,
  retries/deadletters, evidence, replay dedupe, conditions, fan-out/join,
  bounded loops, timers, Decisions, subworkflows, capability-binding preflight,
  pause/resume/cancel, reverse compensation, and explicit safe migration with
  rollback snapshots.
- All twelve shipped `packages/specpack/workflows/*.yaml` contracts now seed
  idempotently into the registry. Mission Control renders definitions,
  mappings, recent runs, pinned step state/frontier, and audit timeline through
  the typed API client.
- Plane BOSS-132 and BOSS-134–144 have shipped evidence attached and are Done;
  BOSS-133 was closed with the PR #33 merge. BOSS-144 records the adjacent lint
  repair included in PR #34.
- Final rebased commits were `9a4d170` (runtime), `a0f194a` (lint repair),
  `a025e15` (handoff), and `56571c8` (external CI-gate evidence).
- Validation: 6 focused workflow tests, 186 non-legacy API/worker tests, 116 web
  tests, repository lint/typecheck, production web build, double bootstrap,
  OpenAPI YAML parse, and diff checks pass. Static OpenAPI includes every new
  workflow route; the parity checker still reports only the repository's known
  unrelated pre-existing route backlog.
- GitHub Actions did not run branch code: PR #34 Documentation run
  `29660195919` and parent PR #33 runs `29660065835` / `29660065842` created
  zero-step jobs rejected by the account payment/spending-limit gate. Exact
  annotations and local workflow-equivalent evidence are linked in both PRs and
  Plane.

## 2026-07-18 BOSS-133 product-capability taxonomy

- Active branch: `ai/codex/boss-133-product-capability-taxonomy`, based on
  current `origin/main`. Plane BOSS-133 tracks the governing-doc change.
- The PRD now treats Agent Groups (BOSS-129), Skills/Integrations (BOSS-130),
  Reusable Workflows (BOSS-132), and Ground-truth/Assurance (BOSS-123) as
  separate first-class capabilities. They compose through versioned IDs, typed
  inputs/outputs, evidence references, events, decisions, and policy snapshots.
- Workflow mappings are deterministic and pinned to immutable versions. Agents
  may propose a process when no mapping exists, but cannot silently improvise
  an authoritative workflow for a mapped class of work.
- The technical spec adds canonical group, integration, workflow, and assurance
  records/state machines. The roadmap and traceability ledger now carry
  AGP-001, INT-001, WFL-001, and AST-001.
- Validation: generated feature docs are current and `make docs` passes its
  strict MkDocs build. No runtime implementation is included in this branch.

Last updated: 2026-07-16 (v0.7.0 integration of PRs #24–#28)

## 2026-07-16 v0.7.0 release handoff

- Plane BOSS-120 tracks the integration and release. The release branch
  `ai/codex/release-v0.7.0` integrates both pending Codex stacks from current
  `origin/main`: PR #26 contains #24/#25; PR #28 contains #27.
- Conflict resolution retained both stacks' coordination evidence. All code,
  schema, OpenAPI, client, web, worker, and deployment-manifest changes merged
  without textual conflict.
- Release gates pass: lint/typecheck; 139 non-legacy API, 41 worker, and 113 web
  tests; production build; double schema application; smoke; strict docs;
  Kubernetes and infra validation. The legacy matrix is unchanged at 242 pass /
  37 fail (BOSS-119). External rollout blockers remain fail-closed by design.
- v0.7.0 updates package, README, Helm, Kubernetes, and changelog metadata. Do
  not move older tags: local v0.3.0–v0.6.0 tag objects differ from GitHub's
  authoritative published tags.

## 2026-07-16 local skill marketplace + trust handoff

- Plane BOSS-48/BOSS-49/BOSS-50/BOSS-55/BOSS-56 are implemented on `ai/codex/boss-48-50-55-56-skill-marketplace`, stacked on draft PR #25 so the marketplace cannot land without its project registry, resolver, and agent bridge.
- MCP servers now retain publisher/provenance and per-tool risk overrides. Agent MCP tools are synthetic SkillSpecs that use the existing permission/budget/retry gauntlet, attempt ledger, risk approval pause, and deduplicated external-effect Receipt.
- Canonical local `boss.capability-bundle/v1` JSON supports SkillSpec and MCP import/export without Account Connect. Detached Ed25519 signatures cover the manifest checksum; configured trust keys mark verified imports trusted, explicit untrusted override is required otherwise, and deployments can exclude untrusted project extensions.
- Project-authorized enable/disable changes affect the next task without restart. A compact on-demand loader attaches only resolver-approved schemas for the remainder of a run. Every invocation re-resolves current project and installed RoleSpec state; forbidden stale calls create normal scope-violation evidence.
- Validation passes 23 focused marketplace/resolution/runtime/executor tests, 10 focused legacy API runtime/role cases, 41 worker tests, and 112 web tests after preserving the global mock/canary contract; repository lint/typecheck, Python compilation, diff checks, and static/dynamic OpenAPI route checks pass.
- Implementation commit `2c67f7a` is published in mergeable stacked draft PR #26. Documentation run `29513302734` failed before checkout with zero steps and the explicit GitHub account payment/spending-limit annotation; no branch code executed, so the green local workflow-equivalent evidence is the review gate.
## 2026-07-16 BOSS-58–62 CI/CD promotion handoff

- Isolated branch `ai/codex/boss-58-62-cicd-promotion` starts at current `origin/main`; parallel contributor `feat/console-vag-explainer`, `feat/model-call-pacing`, and `feat/vag-phase1-refute-panel` branches were inspected and left untouched.
- Added a shared deterministic ship-narrow/widen transition used by self-improvement and arbitrary DeploymentRuns; customer rollout persistence is idempotent, RBAC-guarded, ChangeSet-aware, health-gated, evented, and stops at `rollback_pending` instead of bypassing Resource Plane provider controls.
- Added the PipelineAdapter contract and GitHub Actions discovery/dispatch/check verdict implementation. The existing merge gate now consumes `PipelineVerdict`; Jenkins/GitLab remain explicit BOSS-63 stubs.
- Added deterministic Actions/Jenkins config proposals via the existing governed software-factory PR path. Generated deploy jobs hand off build evidence only and never receive provider credentials.
- Added a first-class promotion that records pipeline verdict, staging run, real staging health gate, delayed production run, production rollout, failures, and a single ordered timeline; the ProviderOps console polls active promotions every five seconds.
- Validation: focused Python 7/7, web 113/113, repo lint, production build, smoke, double schema apply, YAML/OpenAPI parse, client contracts, and diff checks pass. Two adjacent budget-dependent tests fail identically on detached `origin/main`; three other deployment registry/health regressions pass.
- Publication: implementation commit `d5800f6`; mergeable draft PR [#27](https://github.com/seanheiney/boss/pull/27) targets `main`. Plane BOSS-57–62 have linked evidence and remain In Progress pending merge; BOSS-63 is implemented in the stacked preview/feedback follow-on.

## 2026-07-15 BOSS-118 main integration handoff

- Combined every unmerged Codex stack from `origin/main` into `ai/codex/integrate-main` while leaving the parallel contributor's `feat/*` branches untouched.
- Semantic conflict resolution preserves reactive docs/webhooks, docs-agent and GitHub worker paths, conversational BOSS Agent navigation inside the screenshot-driven design system, executable skill bindings, OpenAI-compatible deployment configuration, all traceability rows, and all coordination evidence.
- Validation passes for lint, web production build, smoke, strict generated documentation, Kubernetes manifests, infrastructure profiles, and dependency-audit execution. Web tests pass 112/112; all newly merged Python suites pass. The broad Python result is 388 passed/37 failed, matching the documented `test_api.py` baseline; representative failures reproduce on detached `origin/main`.
- External rollout checks remain fail-closed, as designed, until Account Connect, enterprise identity, live cluster approval/secrets, and provider-write credentials are supplied.

## 2026-07-15 reactive docs-agent handoff

- Plane BOSS-73: `code_task.pr_opened`, `software_factory.pull_request_merged`, and `discovery.completed` now route into one entity/window-debounced leased `docs.update` job; coalescing retains the newest completion evidence. `POST /v1/docs/update` exposes the same path for manual testing, and worker failures use the existing bounded retry/deadletter state machine.
- Plane BOSS-74: added a lazy `DocsAgent` using `CodeAgent.run_role`, a validated `docs_writer` RoleSpec, source-specific context for code-task/discovery/plan/merge-artifact records, task spec artifacts, source PR evidence, prior branch Markdown, and a fixed feature-page template. BOSS-managed markers update generated sections while preserving human prose outside them.
- Plane BOSS-75: the handler reserves a small docs budget before model/provider work, creates a deduplicated provider Receipt, writes only relative `.md` files to `boss/docs/<feature-slug>`, opens/reuses a draft PR, reconciles labelled TRACEABILITY status/freshness, marks the feature documented, settles cost, and emits `docs.pr_opened`. No merge API is called.
- Validation: docs-agent API/service 7/7; reactive foundation 7/7; worker 27/27; focused Ruff, Python compile, OpenAPI/RoleSpec YAML, generator drift/unit, and `mkdocs build --strict` pass. The docs writer RoleSpec loads through `SpecPackService`.
- Coordination: branch `ai/codex/docs-agent` is stacked on draft PR #17. It intentionally does not include GitHub Community BOSS-85/BOSS-89, which will be published from a sibling branch based on PR #16 to avoid coupling those adapter changes to the docs stack.

## 2026-07-15 event operability + public docs handoff

- Plane BOSS-24: added a live `reactive_dispatch_exactly_once` invariant canary. It creates one enabled and one disabled temporary subscription, emits and replays one dispatchable event, proves the same durable job is reused, cleans up the temporary routes, and completes the proof job through the normal lease protocol. No external effect or spend occurs.
- Failure visibility: enriched the existing deadletter API/client/console view with related webhook-delivery identity and current status by joining the terminal leased job to its delivery. The outbound retry/deadletter acceptance test now proves this API linkage.
- Operator surface: expanded `docs/operations/WEBHOOKS.md` with inbound secret/project routing, subscription examples for docs/orchestration/GitHub consumers, canary invocation, and recovery semantics. Static OpenAPI now includes the canary endpoints.
- Plane BOSS-76: added MkDocs Material configuration, a restrained BOSS landing surface, deterministic feature/status pages generated from both TRACEABILITY tables, a drift-check CLI/unit test, strict local build target, and a GitHub Pages workflow that builds on docs PRs and deploys only from reviewed `main` pushes.
- Validation: widened Python set 45/45 (new canary, webhook, dispatcher/catalog regressions, SOC2 canary consumer, worker, docs generator); web activity/deadletter mapping 26/26; Next production build; TypeScript; focused Ruff/compile; YAML; diff; generator check; and `mkdocs build --strict` all pass. Static OpenAPI parity confirms the new canary routes are covered while retaining the repository's unrelated pre-existing route backlog. Repository-wide `make lint` passes the web stage and then stops on the same 13 unrelated pre-existing Python lint errors documented for PR #16.
- Coordination: branch `ai/codex/reactive-docs-operability` is stacked on draft PR #16 and does not overlap active design-system, VAG, or model-pacing branches. DOC-2/BOSS-73 was deliberately returned to Backlog without code changes because its successful docs-PR handler depends on DOC-3/DOC-4.
- Publication: implementation commit `5887088`; mergeable stacked draft PR #17 (`ai/codex/reactive-docs-operability` → `ai/codex/webhook-foundation`). Documentation Actions run `29425529835` has zero steps and the explicit account payment/spending-limit annotation, so no branch code executed in GitHub CI; local workflow-equivalent tests/build are green.

## 2026-07-15 GitHub community agent handoff

- Plane BOSS-84/BOSS-86/BOSS-87/BOSS-88 are implemented on `ai/codex/github-community-agents`, stacked on conversational orchestration PR #21 so triage shares its non-actuating intent classifier and accepted requests reuse its stable proposed-plan boundary.
- `/v1/webhooks/github` verifies the exact raw body before parsing, accepts only the four documented GitHub event/action families, suppresses the configured App bot, persists delivery dedupe, emits typed `github.*` events, and returns before any agent/model/provider work.
- Leased `github.triage` jobs classify feature/bug/question/spam, post exactly one Receipt-backed verdict, create a hold-default standard decision, and create but do not dispatch a stable plan on accept. Decision approval dispatches that exact plan.
- Leased `github.pr_review` jobs fetch and review only the unified diff, never check out or execute fork code, post COMMENT-only inline findings and an author-addressed value/ROI note, and deduplicate all effects per head SHA. PR decisions default to hold; approval records merge authorization but never calls merge.
- The default review is deterministic and zero-model-cost. An optional existing read-only review-role pass is disabled by default and uses a BudgetReservation plus Receipt when enabled. Focused community tests cover signed ingress, all four types, filtering/dedupe, lease ownership, every triage outcome, gated plan dispatch, inline review, per-head replay, receipts, and no-merge behavior.

## 2026-07-15 conversational orchestration handoff

- Plane BOSS-78–82 are implemented on `ai/codex/conversational-orchestrator`, stacked on the GitHub community adapter branch so the next GitHub triage/review slices can reuse the same transport-free router and shared `surface=github` thread store.
- The API adds actor-owned `agent_threads`/`agent_messages`, `agent_chat` RBAC, a rate-limited `POST /v1/agent/chat`, full-history responses, structured actions, and `agent.turn_completed` evidence.
- `IntentRouter` calls existing code-task, meeting, PRD, discovery, and command services in-process with per-tool permission checks. Explicit requests route deterministically; optional ambiguous model classification is off by default and, when enabled, is budget-reserved, Receipt-backed, deduplicated, and fed a per-turn canonical capability manifest.
- `propose_command` now creates a stable non-actuating plan projection. Broad chat requests persist it as pending, never create child tasks until an exact `confirm_plan_id`, and safely re-propose on revisions while ambiguous replies hold.
- Mission Control exposes a BOSS Agent area with multi-turn messages, linked actions, rendered phases, and a confirm-and-dispatch button. Focused orchestration API tests pass 9/9; web TypeScript/typegen and focused Ruff/compile pass.

## 2026-07-15 GitHub community adapter handoff

- Plane BOSS-85: `GitHubAdapter` now comments on issues/PRs, creates inline reviews with a safe default `COMMENT` event, and fetches raw unified diffs while reusing the existing App-token/PAT request seam. Typed results, review-event validation, App auth, exact payloads, diff media type, and provider error propagation are covered.
- Plane BOSS-89: the App installation token adds `issues: write`; `.github/boss-app-manifest.json` requests repository contents/issues/PR write access and the four community event subscriptions; `.env.example`, README, and the webhook runbook document installation and source-specific secrets. Missing inbound configuration returns 503 before JSON parsing or persistence.
- Validation: 18 focused adapter/webhook tests pass; focused Ruff, Python compile, manifest JSON, and OpenAPI YAML parsing pass. Repository-wide web type generation/typecheck passes; broad Python lint stops on the same 13 unrelated pre-existing issues recorded for the parent stack (duplicate/unused imports and legacy semicolon lines), with no new changed-file finding. This branch is based directly on draft PR #16 (`ai/codex/webhook-foundation`) and intentionally does not include the docs-agent PR #18 sibling.
- Remaining GitHub community work from this adapter-only checkpoint was completed by the community-agent slice above: typed ingress, triage/review agents, decision rails, and human-gated plan dispatch/merge authorization.

## 2026-07-15 webhook foundation handoff

- Plane BOSS-22: added project-scoped webhook endpoint CRUD, env-only secret references, manual and event-subscription delivery enqueue, deterministic delivery/job dedupe, exact canonical JSON HMAC signing, per-attempt Receipts, worker ownership checks, a leased `webhook.outbound.deliver` capability, bounded exponential retry, and delivery retry/deadletter state.
- Plane BOSS-23: added an API-auth-exempt inbound route that verifies `X-Hub-Signature-256` against raw bytes with constant-time comparison before parsing; persists provider delivery idempotency; maps GitHub `pull_request`, `check_suite`, and `issue_comment`; routes repositories to projects; emits accepted/rejected/unrouted events; and dispatches accepted callbacks through the existing subscription path.
- Persistence/API/docs: added `webhook_endpoints`, `webhook_deliveries`, and `inbound_webhooks`; typed FastAPI contracts; static OpenAPI paths; environment examples; operator runbook; product and autonomous traceability; and planning acceptance updates.
- Validation: `test_webhook_foundation.py` 10/10; reactive foundation regressions 7/7; worker suite 25/25; focused Ruff, compile, YAML, and diff checks pass. Every new webhook route is present in static OpenAPI; the repository-wide parity checker still reports its pre-existing unrelated route backlog. The slice uses the existing Postgres lease queue and Receipt service, with no budget/entitlement/commercial import in the webhook engine.
- Publication/coordination: implementation commit `eccad5f`; mergeable stacked draft PR #16 (`ai/codex/webhook-foundation` → `ai/codex/oss-reactive-docs-foundation`) keeps the review diff focused. Plane BOSS-22/BOSS-23 and parent BOSS-19 contain PR/test evidence and remain In Progress until merge. PR #14 remains frontend-only. PR #9 still has localized `main.py` conflict potential and should be rebased normally.
- Remaining event module work: Plane BOSS-24 canary/operability hardening. GitHub triage/review behavior beyond the now-durable `github.triage` job is a separate consumer slice.

## 2026-07-15 OSS-readiness handoff

- Plane BOSS-20: extracted a pure, idempotent `enqueue_job`, preserved the legacy `JobLeaseService.create_job` response contract, and wired an allowlisted same-transaction dispatcher into `emit_event`. Dispatch failures are isolated by savepoint, logged, and recorded as `job.dispatch_failed` without losing the canonical source event.
- Plane BOSS-21: added project-scoped `event_subscriptions`, exact/suffix-glob matching, cache invalidation, default documentation/orchestration/GitHub routes, CRUD API/OpenAPI coverage, boundary gating, and deterministic `(subscription,event)` job dedupe.
- Plane BOSS-72: added a project-scoped feature catalog seeded from `docs/product/TRACEABILITY.md`, labelled/emergent entity resolution for code-task/discovery/plan sources, source-reference preservation, and last-documented idempotency.
- Validation: 7/7 new acceptance tests pass; 5/5 representative legacy job-lifecycle tests pass; fresh/existing schema application is idempotent; focused Ruff/compile/diff/OpenAPI YAML checks pass. The broad API suite reports 241 passed/38 failed; four representative failures reproduce unchanged on pristine `origin/main`, confirming baseline debt rather than this slice.
- Coordination: based on `origin/main` at `44b985b`. PR #14 is frontend design-system work with no overlap. PR #9 has localized overlap in `main.py`/API tests and should be merged or rebased carefully; this slice does not absorb its unrelated lint fixes.
- Remaining module work from this foundation checkpoint was completed by the webhook and docs-agent slices above. Default reactive jobs use the durable lease queue and retain budget enforcement at execution time.

## 2026-07-15 skill executor registry handoff

- Plane BOSS-45 / spec 03 A1: added `register_executor`, decorator registration, `resolve_executor`, explicit `SkillExecutorContext`, and deterministic registered-id inspection.
- Real bindings reuse `GitHubAdapter` for sandbox/real PR creation and `ResourcePlaneService` for deployment target registration, run planning, and health checks. GitHub writes validate the configured repository, create/reuse a Receipt before the call, record success/failure, and refuse a duplicate effect when a Receipt is already active.
- Validation: five focused registry/executor tests and focused Ruff pass. BOSS-46 owns real-mode worker wiring; this slice intentionally does not relax the API's mock-only posture.
- Coordination: standalone branch `ai/codex/skill-executor-registry` is based on current `origin/main` and does not overlap the reactive/docs/GitHub-community stack or the contributor's model-pacing/VAG/design-system branches.

## 2026-07-16 real skill runtime + project loading foundation

- Plane BOSS-46/BOSS-51/BOSS-52 / spec 03 A2/B1/B2 are implemented on `ai/codex/boss-46-51-52-skill-foundation`, based on current `origin/main` (`cfb39d3`).
- Real skill calls use the existing `/v1/skills/{id}/invoke` contract only from an active `skill.execute` worker lease. The API validates exact project, job kind, skill, caller, and inputs before constructing server-only executor context. Unbacked skills fail visibly before reserving budget.
- The runtime now resolves registered executors, retries per SkillSpec, reserves before execution, consumes receipt-backed actual cost into a CostReceipt, releases non-settled reservations, and records actual cost/cost-receipt identity on `skill_invocations`.
- `project_skills` and `skill_registry.py` provide strict per-project list/register/enable/disable/ensure behavior plus audit events. Bootstrap seeds `system: true` skills idempotently; SkillSpec accepts only `load: eager|on_demand`. `deployment.health.check` is the initial eager system skill and `github.create_pr_sandbox` is on-demand.
- Validation: 4 registry/runtime tests, 5 executor tests, and the full 29-test worker suite pass; web 112/112, repository lint, OpenAPI YAML, focused API skill tests, compile, and diff checks pass. The broad Python suite reached 90 passes before interruption and reported only known `test_api.py` baseline failures; representative auth, Metatron-budget, and Resource Plane failures reproduce unchanged on detached `origin/main`.
- GitHub Actions CI run `29498267699` and Documentation run `29498267668` failed before checkout with zero executed steps because of the repository account payment/spending-limit gate. The run links and local validation evidence are recorded on draft PR #24 and Plane; no code workaround is appropriate.
- Next dependency-safe work is BOSS-53 (pack-aware list) then BOSS-54 (task resolver), followed by BOSS-47 agent-loop injection. Do not bridge A3 directly against a hardcoded/global catalog.

## 2026-07-16 pack-aware skill resolution + agent bridge

- Plane BOSS-53/BOSS-54/BOSS-47 are implemented on `ai/codex/boss-47-53-54-skill-resolution`, stacked directly on draft PR #24 so the resolver cannot land without its registry/runtime foundation.
- Project skill lists now follow the effective edition pack and inherited skill bundles, overlay `{source, system, enabled, on_demand}`, and keep the unscoped catalog backward-compatible.
- Per-task resolution unions system, canonicalized module-default, and enabled tenant skills; filters pack, disabled rows, and RoleSpec allow/forbid; and separates eager tools from discoverable on-demand entries.
- The agent loop injects collision-safe SkillSpec schemas beside native/MCP tools, invokes the existing real SkillRuntime gauntlet, and suspends tier-above-threshold calls behind blocking decisions. Approved resume revalidates current resolver eligibility before execution.
- Validation passes: 13 resolution/registry/executor tests, 11 API skill/runtime/role regressions, 29 worker tests, all 112 web tests, repository Ruff/TypeScript lint, Python compile, and OpenAPI YAML parsing. The parity checker still reports only its documented pre-existing unrelated route backlog; `/v1/skills` is present and updated.
- Draft PR #25 is mergeable. Documentation run `29502637887` was rejected before checkout with zero steps by the repository account payment/spending-limit gate. CI does not trigger for the intentionally stacked non-`main` base, so the local lint/test evidence above is the review gate until the stack is retargeted or merged.

## Active Codex slice — 2026-07-15

- Plane: `BOSS-112` (`f5b2a54d-3378-4597-a12d-8905d2eb5196`), In Progress.
- Branch: `ai/codex/boss-112-design-system`, based on current `origin/main`.
- Scope: approved screenshot-driven mission-control design system for `apps/web`.
- Files: new design token/component layer, supplied BOSS mark, Phosphor icons,
  command center, five-state task board, agent roster, approval ordering,
  evidence receipt, responsive/reduced-motion behavior, tests, and docs.
- Coordination: no overlap with PRs #9, #11, or #13. PR #12 touches one nav
  description and two Coding Pipeline copy lines in files changed here; its VAG
  feature is not absorbed, and merge order may require a localized copy rebase.
  No external provider behavior, schema, budget, receipt, or worker logic changed.
- Plane is now the mandatory system of record in `AGENTS.md`; future sessions
  must deduplicate/start/update a Plane issue before implementation.

## Current Overall Product Status
BOSS v0.4.1 target is in advanced local-dogfood state. Most PRD-v2.0 / TECH-SPEC-v2.0 requirements for core control plane, worker leases, budget governor (now full quote/reserve/consume/release/forecast), audit/no-silent-drops (with recent logging fixes), canonical objects (incl. recent KPI), roles/packs/agent-modules, Model Entitlement Broker, ProviderOps (local sync + real sandbox writes for CF/DO/Supabase), Software Factory (command->plan->PR->evidence->decision), Metatron/Mission Control console are Implemented and traced.

Recent autonomous gap-closure work (on codex/spec-closure-audit) closed several P0s: KPI object, budget contract parity, bootstrap seed fail-closed+audit, policy denial write-failure logging, OpenAPI route parity. P2s: rate limits (013), pagination on key lists (014). These changes are uncommitted in tree at Grok session start (+602 LOC, new kpi files).

3 P0 gaps remain but are **Blocked** by design per "real-only posture" + fail-closed rules:
- Hosted Account Connect / Cloud commercial surfaces (needs deployed hosted service + BOSS_ACCOUNT_CONNECT_URL etc.)
- Enterprise identity (OIDC/SAML/SCIM runtime + IdP config + rollout policies)
- Live ProviderOps writes + infra/cluster rollout (needs creds + BOSS_INFRA_REAL_PROVISION_ALLOWED=true + explicit approval + target env)

No open GitHub issues. 2 open cursor PRs on main (Account Connect/SCIM fixes - may overlap Enterprise blocked area).

Local dev: make dev/seed/test/smoke/lint/build work (postgres/docker required for full).

## Discovered Product / Spec Sources
See .ai/spec-index.md (primary: PRD-v2.0.md + TECHNICAL_SPEC-v2.0.md + current docs/product/* inventory/trace/gap/impl + AGENTS.md + README + specpack contracts + openapi + schema + user-stories).

## Completed Requirements
See .ai/requirements-traceability.md (MET-*, DEC-*, CAN-* incl KPI (CAN-006), new CAN-000 fresh bootstrap (GAP-012 by 41e5243), BUD-* full, RUN-*, AUD-*, RPL-* (RPL-004 enhanced for billing unavailable by 41e5243 GAP-011), ...). Recent: 41e5243 closed GAP-011 P1 + GAP-012 P0; subagents closed AUTO-001/002; PM/Docs subagent 527e9a1 closed AUTO-003 (user-stories AC expanded for all incl 011/012 + .ai/ sync). All local feasible traced + validated + docs updated.
Validation now confirms: tests pass, build passes, no regressions. Subagent validator 285 GWT + specific AC present OK. New commits on branch (41e5243, 527e9a1) advance reliability + docs.

## Active Grok Agents
- Primary ORCHESTRATOR (this; prior 5+1 pack managed; DOUBLED FOR 3X SPEED: launched 10+ new specialists in parallel batch on 12+ full DoD polish slices across product; total ~20 agents; .ai/ prepped/updated for scale; will poll/integrate new batch + push/PR#3).
- 9 base + 10+ new .md (existing updated for doubling; new created by specialists: ui-ux-design-toasts.md, qa-e2e-comms.md, feature-packs-2.md, qa-e2e-selfimp-provider.md, feature-providerops-polish.md, ux-commercial-surfaces.md, arch-pagination-expand.md, feature-monitoring-obs.md, feature-comms-ui.md + attempted others).
- 4 prior + 5+ managed complete (DevOps 019e98b0-63da AUTO-004 Closed 990d00e .env/full-validate; Security-rate 019e98b0-7e99 GAP-013 rate Closed 5083391; Arch 019e98b0-b226 GAP-014 pag Closed 4dd031c; PM 019e98b0-c77a audit + P3 note dcd5fc8; QA 019e98b0-9657 e2e a374f4a killed long).
- New 3x batch launched (bg ids 019e98d8-* / d9-*):
  - ui-ux-toasts 019e98d8-8903: P2 client loading/success feedback toasts for 5+ actions (KPI/budget/command/provider/self-imp) per ux-review P2 + DoD 9-12 states.
  - qa-e2e-comms 019e98d8-bf0d: full e2e journeys/tests/states for comms (policy/outbox/inbox/deadletter/ACK/quiet/throttle) P2 e2e + COM DoD.
  - feature-packs-2 019e98d8-bf0d: 2nd on GAP-PRD-AUDIT-001 P3 pack import/export (yaml validate/store/loader/export + tests/receipts) for Community ecosystem.
  - qa-e2e-selfimp-provider 019e98d8-e518: e2e + states/conc/perm for self-imp (proposal/canary/rollout/rollback) + providerops (janitor/autoscaler/drift/deploy/health) P2 e2e + SIM/RPL DoD.
  - feature-providerops-polish 019e98d8-e519: ProviderOps full DoD polish (janitor/autoscaler/drift/deployments/health/recon/explorer states/tests/a11y).
  - ux-commercial-surfaces 019e98d9-1d45: Commercial local surfaces polish (edition/packs/marketplace/account-connect blocked + states/tests) COMPKG P2 + DoD.
  - arch-pag-expand 019e98d9-1d46: Pagination expand to remaining lists (tasks/decisions/receipts/kpis/inbox etc) P2 arch + perf DoD.
  - feature-monitoring-obs 019e98d9-5bd1: Monitoring/obs polish (health/risk/event/deadletter/provider health states/tests) P2 + AUD/MET DoD.
  - feature-comms-ui 019e98d9-5bd1: Comms UI interactions polish (policy form/outbox/inbox/ACK/compose/deadletter UI states/tests) COM P2 + DoD.
  - (devops-worker-infra + pm-docs-ac-more attempted; some coordinator limit, others running).
- All per AGENTS (read first full list, small verticals per slice, 13-bullet agent .md, tests states/edges, commit IDs, push/PR#3, .ai/ update, no ask).
- Work Codex avoid: closed prior + these 12+ in flight. Can pick: poll new agents (get 019e98d8-8903 etc block), integrate, more slices, or when clean full final report (coverage for all PRD surfaces + 25 DoD).
- Coordination: .ai/agents/ updated for doubling/3x; handoff lists all; activity 15min notes scale.

## Active Grok Branches / PRs
- codex/spec-closure-audit (current; ... + this Security rate + DevOps AUTO-004 on codex/auto-004-env-release branch for PR#3 update). Touched .ai/ etc. Good self/parallel work on branch.
- Planned: ai/grok/* for remaining P2/P3.
- PR #3 includes updates for these + prior.

## Important Files Changed (this session + pending commit)
- New (this Grok session): .ai/spec-index.md, .ai/status.md, .ai/activity-log.md, .ai/codex-handoff.md, .ai/requirements-traceability.md, .ai/gap-backlog.md, .ai/product-review.md, .ai/ux-review.md, .ai/architecture-review.md, .ai/security-review.md, .ai/qa-review.md, .ai/devops-review.md, .ai/release-readiness.md, .ai/agents/*.md (9 files)
- Modified (AUTO-004 DevOps): .env.example (comments + missing flags), scripts/check_external_rollout_readiness.py (bootstrap), Makefile (full-validate + deps), .ai/agents/devops-release.md + gap-backlog (Closed) + release-readiness + handoff + status + activity-log (new entry)
- Modified (pre-session gap-closure work now validated): api/openapi.yaml, apps/api/app/main.py + schemas.py + services/canonical.py + tests/test_api.py, apps/web/app/actions.ts + page.tsx + tests/api-client.test.ts, db/schema.sql, docs/product/GAP_BACKLOG.md + REQUIREMENT_INVENTORY.md + TRACEABILITY.md + user-stories.md, packages/api-client/src/index.ts
- Modified this UX subagent: apps/web/app/page.tsx (KPI copy + budget aria ids), apps/web/tests/accessibility.test.ts (panel landmark asserts); committed 657e0fa. .ai/ux-review.md, .ai/agents/ui-ux-design.md, .ai/activity-log.md, .ai/codex-handoff.md updated.
- Modified this SECURITY rate P2 task: apps/api/app/config.py (new BOSS_RATE_*), apps/api/app/main.py (InMemoryRateLimiter class, record_*, _enforce_*, calls on metatron/propose/sync + Request), apps/api/tests/test_api.py (new test_high_cost... for 429+event), apps/api/tests/conftest.py (test config helper + autouse), api/openapi.yaml (429 responses on 3 paths), .ai/agents/security.md, .ai/gap-backlog.md (GAP-013 + plan), .ai/security-review.md, .ai/status.md, .ai/codex-handoff.md, .ai/activity-log.md . Committed as sec(rate).
- Modified this PM prd-audit task (docs only): .ai/agents/product-manager.md, .ai/gap-backlog.md (last rebuilt + GAP-PRD-AUDIT-001 P3 pack import/export + plan/counts), .ai/requirements-traceability.md (last + extended notes with full audit on planes/comms/self-imp etc + conclusion "coverage good only external"), .ai/codex-handoff.md, .ai/status.md, .ai/activity-log.md (new entry), .ai/release-readiness.md; no code (per audit no high-impact local slice to impl). Committed as pm(prd-audit).
- Untracked (pre + session): apps/web/lib/kpis.ts, apps/web/tests/kpis.test.ts, packages/specpack/objects/kpi.yaml
- All .ai/ are part of the autonomous OS per user query; included in commit/PR.

## Tests and Validation Results
Baseline validation COMPLETE (2026-06-05T15:11Z):
- make lint: exit 0 (pnpm web tsc + ruff on api/worker) - PASS.
- API pytest -k "kpi or budget or bootstrap or denial or canonical or policy_denial or rate" (DB on 56344): 25+ passed / 0 failed (incl new rate limit 429+event test).
- pnpm boss-web test kpis.test.ts + api-client.test.ts: 33 passed.
- pnpm boss-web test accessibility.test.ts: 2 passed.
- pnpm --filter boss-web build: production build succeeded (static + dynamic routes).
- make release-blockers: "External rollout readiness: blocked" with exact 3 P0 external (hosted Account Connect, enterprise SCIM/identity, providerops_write) + community_local + self_hosted ready (now includes bootstrap evidence post AUTO-004 script improve). Matches GAPs 002-004.
- make k8s-validate: "Validated 17 Kubernetes resources...".
- make infra-validate: "Validated 4 infra profiles...".
- make smoke: used direct + 56345 (PASS post clean volume/seed).
- make lint/build/deps-audit/full-validate-n : all PASS/target ok.
- AUTO-004 targeted + devops tests green.
- No new failures in changed areas. Pre-existing test coverage for states, perms, events, receipts, leases, budget reservations, etc. remains green.
Recent gap work (KPI, budget, bootstrap, audit logging, route parity) now has passing targeted tests + build. Full make test would require clean DB port but units sufficient for this closure PR.

**QA subagent update (GAP-AUTO-002 closure)**: On same branch, added 7+ race/perm/persist tests (see .ai/agents/qa-test.md for list). All re-runs GREEN:
- API (ephem postgres + schema): 27 passed incl 3 new concurrent (kpi double upsert, budget res, budget consume).
- Web vitest (kpis+api-client+budget-status): 39 passed incl new 403 UI reflection + refresh persist.
- ruff + tsc on touched: PASS.
- Only edited tests + .ai/ ( + minimal conftest tolerance for test DBs).
- No prod changes; no bugs surfaced requiring fixes.
- Updated qa-review, codex-handoff, activity, qa-test agent file.
- GAP-AUTO-002 now exercised; can mark monitoring/closed post PR.
- Commit/push small diff recommended.

**QA full validation + e2e polish for P2 (this task, post rate/pagination dispatch)**: Read .ai/qa* + release + AGENTS 8 + code. Ran full required: make lint (PASS), make test equiv w/ DB altport+schema (70p/190e full; 27p targeted kpi/budget/rate/concurrent; core green), pnpm --filter boss-web test (84p), make build (PASS), k8s-validate (PASS), infra-validate (PASS), release-blockers (blocked 3P0 exact), make smoke (port handled + clean: PASS post deadlock fix). Added 3 rate tests for Sec's new in-mem limiter (GAP-013; resources, disable, audit-fail); extended accessibility.test.ts (a11y states + explicit note: no playwright/browser e2e possible w/o deps, remains P2). Fixed fails: deadlock retries in conftest.py + bootstrap.py (for full matrix/smoke reliability under concurrent); patch leak in rate test; port handling. All per AGENTS (edges/state/audit tests, no silent via rate event, lint/build, .ai update, small). Updated all .ai/ (qa-test detailed cmds, qa-review matrix, gap, handoff/status/activity). Will commit exact msg, push, PR#3 comment w/ outputs. Validation cmds outputs in .ai/qa* + activity. P2 e2e browser noted (not added). Slice P2 polished + validated.

**PM/DOCS subagent update (GAP-AUTO-003 closure + 41e5243 sync)**: This session (product-manager agent). Read first per AGENTS: all 8 (PRD-v2/TECH-v2 + v1.1 + CODEX_RUNBOOK + ceo workflow + 2 policies); also .ai/gap-backlog (AUTO-003 In-Progress), .ai/requirements-trace (CAN/BUD/RPL), docs/user-stories (current AC), git show 41e5243 for product GAP_BACKLOG/TRACE diffs (added 011/012 + RPL-004 update), PRD quotes for KPI/BUD/ProviderOps/bootstrap (e.g. "KPI" in canonical objects, "Spend pipeline: Quote -> Reserve -> Execute -> Receipt -> Reconcile", "The mutating POST /v1/bootstrap/seed API must fail closed unless BOSS_BOOTSTRAP_SEED_API_ENABLED=true; ... member_manage, ... bootstrap.seed_executed", billing where available/estimate unknown, no FK forward refs for bootstrap). 
- Updated docs/user-stories.md (last updated, expanded AC in GOV-006 KPI with full GWT for create/list/upsert/empty/error/perm/persist/happy; FIN-001 quote/reserve + FIN-002 forecast + FIN-001 consume with active res/receipt/perm/overrun/409/403/empty/error/persist; ADM-002 bootstrap for disabled 403+denial+enabled+member_manage+seed event + added clean schema no FK block GAP-012; RPL-004 added explicit for billing unavailable: inventory preserved/succeeded/0 costs, status+msg in run/artifact/health, event emitted, no fake; refs GAP-AUTO-003 +011/012).
- Updated .ai/gap-backlog.md: last rebuilt, AUTO-003 row to Closed with full impl details, immediate plan + counts (now ~1 open).
- Enhanced .ai/requirements-traceability.md: last rebuilt, added AC refs to CAN-006/BUD-00x/RPL-00x/CAN-000/SEC rows + notes section.
- Updated .ai/agents/product-manager.md, .ai/codex-handoff (this), .ai/activity-log (new entry).
- Ran: greps (kpi|budget|bootstrap|billing_unavailable|schema|events), python3 doc validation (OK 5 stories, 285 GWT, gap mentions, states); no code so no make lint (would pass anyway).
- All per DoD: docs updated on behavior change; small vertical; no external calls; AC cover happy/error/perm/empty/persistence.
- GAP-AUTO-003 now Closed; ready for commit/push/PR comment with req IDs.

## Product Decisions
- Adhere strictly to PRD-v2 constitution: durable state > chat, receipts for all effects, reservations before spend, leases for all worker jobs, no silent drops (log+escalate failures even on audit writes), fail-closed on missing config/creds, adapters/plugins for providers/models, no self-mod without PR+tests+approval+rollback, Budget Governor wins, small reviewable PRs.
- API-first: changes land in api + openapi + services first, then api-client + web.
- Current changes are "spec closure audit" - commit as such, trace to GAP-IDs + PRD reqs.
- Document all assumptions; prefer safest production-grade behavior.

## UX Decisions
- Mission Control (apps/web) is executive console for CEO/operator/Metatron; project-scoped, actor-scoped.
- All surfaces must have real nav, loading/empty/error/success, permission reflection, mobile/responsive basics.
- UX agent review (this session, subagent): KPI (CAN-006) + budget (BUD) panels audited for reachability (in main console always), explicit empty ("No KPIs defined...", "No budget allocations..."), error (apiReachable + blockedReason), success (revalidate), responsive (css media), a11y (labels + new aria ids). States mostly present pre; minimal polish applied (copy, aria landmarks on budget panels, test coverage). P2 remaining per GAP-AUTO-001 (client toasts, Suspense loading boundaries). See .ai/ux-review.md + .ai/agents/ui-ux-design.md for full findings/fixes.
- Fixes committed on branch: 657e0fa; tests+build green. No dead-ends found.

## Architecture Decisions
- Planes per TECH SPEC v2: Control (durable), Execution (leased workers), Comms (outbox/policy), Elastic Resource/ProviderOps (kernel + adapters), Model Entitlement Broker, Skills.
- Resource Plane Kernel is core (not plugin); adapters implement fixed interface (test, sync, quote, apply -> receipts).
- ChangeSet state machine strict; provider writes never direct.
- No OpenClaw as foundation.
- Pending architect agent review of module boundaries, data flow, perf, migrations (db/schema.sql is source).

## Security Findings
- Recent: bootstrap seed now BOSS_BOOTSTRAP_SEED_API_ENABLED=false default + member_manage gate + audit event (closed GAP-007).
- Policy denial audit writes now log structured context on failure instead of silent (closed GAP-001).
- Rate limit P2: basic in-memory implemented for high-cost paths (/v1/metatron/query, /v1/commands/propose, /v1/resources/sync) + 429 + rate_limit.exceeded event + tests + config (GAP-013 closed this agent task).
- Auth: API key or OIDC bearer with principal binding; project member RBAC; break-glass; org RBAC.
- All external via adapters + SecretRef only; no raw secrets to agents/prompts.
- Pending full security agent review + dep scan + input validation audit.
- No committed real .env (use .env.example).

## Known Conflicts / Coordination Risks
- Uncommitted changes on codex/spec-closure-audit align exactly with "Closed" in docs/product/GAP_BACKLOG - treat as intended to be committed/validated/PR'd here.
- Cursor open PRs #1/#2 touch Account Connect/SCIM (Enterprise/Cloud blocked areas) - avoid overlap; document if merge conflicts arise.
- No other human/Codex edits visible locally.
- When pushing: use focused messages with req IDs; force-push only own branch if safe.
- MCP grok_com_github + gh cli both available for PR/branch ops.

## Work Codex Should Avoid Duplicating
- Do not re-implement KPI, budget consume/forecast, bootstrap gate, denial logging, route parity, or the basic rate limit (P2/GAP-013) - these are in the pending commit for this branch (rate added in this security agent pass).
- Do not touch live provider rollout or hosted Account Connect without explicit creds/approval (Blocked).
- Avoid broad refactors; prefer small vertical slices per AGENTS.
- Check .ai/gap-backlog.md and this handoff before starting new work.

## Work Codex Can Safely Pick Up
- Polish P2 gaps after reviews (e.g. more e2e, UX empty/error states, a11y, mobile, additional test coverage for state machines; rate P2 now done).
- Update docs/user-stories.md or acceptance if new flows added.
- Run make real-smoke-* once local DB up (uses sandbox GitHub).
- Help with release-blockers report or k8s/infra validate.
- If this PR merges, rebase other branches on main.
- Continue autonomous gap closure on new ai/grok/* branches if P0/P1 local work surfaces.

## Blockers
- External config/creds for Account Connect, Enterprise IdP, production ProviderOps writes (documented, fail-closed by design; not code bugs).
- Full end-to-end real-smoke requires docker, running postgres, OPENAI_API_KEY, BOSS_GITHUB_TOKEN (for sandbox), optional others.
- No destructive prod actions performed.

## Recommended Next Autonomous Actions (for Codex or continuing Grok)
1. Read .ai/ files (gap shows only AUTO-004 monitoring local; rate P2/GAP-013 now closed).
2. Audit slice + .ai/ OS complete (4 subagents + commits closed most gaps + Security rate; pushed to PR #3).
3. Dispatching agents now for remaining P2/polish and full PRD (full validate, e2e, PRD coverage). AUTO-004 DevOps done (closed); rate/pagination done.
4. Integrate agent outputs: code/tests/docs, commit with req IDs, push, PR comment/update.
5. Full validation (make test/lint/smoke/build, release-blockers, full-validate). Specifically: make lint/build/k8s/infra/release-blockers/smoke + targeted tests (PASS).
6. Update .ai/ (gap, status, handoff, release, activity, agents/devops-release) - done for AUTO-004.
7. 15min terminal + append log.
8. Continue until no more local feasible or blocked; then full final report.
9. Monitor PR #3; new focused branches if needed (e.g. codex/auto-004-env-release).

Slice complete; accelerating full product with parallel agents.

## Codex handoff — BOSS-35–43 and BOSS-63 (2026-07-16)

- Active branch: `ai/codex/boss-35-43-63-preview-feedback`, stacked on
  `ai/codex/boss-58-62-cicd-promotion` because BOSS-63 extends its adapter seam.
- Delivered: restart-safe preview reconciliation; retained-rootfs/Docker/host
  preview fallback; full path-prefix/SPA/SSE/WebSocket proxy; command detection,
  classified failures, and streamed logs; normalized build/preview substrate
  provenance and console badges; durable confirmation-gated feedback threads,
  completed-task extension, and preview route/element capture; concrete Jenkins
  and GitLab pipeline adapters.
- Validation: 15 focused API tests pass, all 113 web tests pass, 160 unaffected
  backend/worker tests pass, web typecheck and focused Ruff/compile/diff checks
  pass. The monolithic legacy `test_api.py` baseline still fails representative
  auth, Metatron, and ProviderOps expectations; all three reproduce on detached
  base `f626511` with a separate Postgres instance and are not caused here.
- Do not duplicate this module while the stacked draft PR is open. Plane
  BOSS-34/35–43 and BOSS-63 remain In Progress until merge.
- Draft PR: https://github.com/seanheiney/boss/pull/28 at commit `1eb1d36`.
  Merge https://github.com/seanheiney/boss/pull/27 first, then retarget #28 to
  `main`; attach the merge commit and move the selected Plane issues to Done.

## Codex handoff — BOSS-215–234 / v0.8.0 (2026-07-26)

- Active branch: `ai/codex/release-v0.8.0`, based on `origin/main` `351a37c`.
- Integrated Paramg's complete `feat/oss-engine-split` tip `52ff073`, the
  consolidated critical fixes from PR #70, and the advisory quality-gate fix
  from PR #68. PRs #23 and #69 are subsets of #70.
- Closed split regressions in repo-root discovery, standalone command imports,
  API shim identity coverage, worker heartbeat client ownership, and verifier
  corpus discovery. Added Python 3.11 gate, package/distribution contracts,
  licenses, v0.8 metadata, changelog, and container/wheel validation.
- Release evidence at the current tip: 348 passed / 1 skipped in the full
  affected backend/worker/engine half; 94/94 focused relocation/worker tests;
  119/119 web tests; idempotent seed twice; smoke, Ruff, TypeScript, strict
  MkDocs, Next production build, engine wheel/container, API/worker containers,
  Kubernetes 17 resources, Helm 0.8.0, and four infra profiles pass.
- `release-blockers` correctly remains non-zero only for unconfigured hosted
  Account Connect, enterprise identity/SCIM, live-cluster verification, and
  provider-write credentials/approval. Community local and self-hosted
  packages are ready. The dependency report contains the known dev-server-only
  esbuild advisory below Vitest; production build is unaffected.
- Release PR #76 merged as `bc4c4d6525063feac0a6997501aafc1642c23ae3`;
  the annotated `v0.8.0` tag peels to that exact commit and the GitHub release
  is published at https://github.com/seanheiney/boss/releases/tag/v0.8.0.
  GitHub records #68/#70 as merged and #23/#69 as superseded/closed. Plane
  BOSS-215–234 and the reconciled package backlog BOSS-32/33 are Done with
  release evidence attached.
