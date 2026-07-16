# Automated CI/CD & Deployment for the Forward-Deployed-Engineer Use Case

> **Summary:** BOSS already ships three real canary mechanisms, tiered previews, real deploy actuation with health-gated rollout records, and an external-CI reader that gates merges. The FDE use case (user ask 8) is an *extend-and-generalize* effort, not a build-from-scratch: promote the self-improvement staged-rollout+auto-rollback engine into a first-class facility for arbitrary customer app deployments, generalize the GitHub check-run reader into a provider-agnostic pipeline adapter, add a CI-config *generator* to the coding pipeline, and stand up an explicit staging→prod promotion orchestrator surfaced in the console.
> · **User ask:** 8 (Automated CI/CD + deployment for the FDE) · **Module:** CI/CD & Deployment · **Priority:** P2 — enterprise

---

## 1. Context & Goal (FDE role framing)

A Forward-Deployed Engineer dropped into a customer environment does not get to redesign the customer's release process. They inherit whatever exists — a GitHub Actions workflow, a Jenkins job, a GitLab pipeline, a hand-rolled `deploy.sh` — and they make it work: they *author* pipeline config where none exists, *drive* the pipeline that does exist, watch it go green, promote a build from staging to production behind a smoke/canary gate, and roll back the moment health regresses.

**Goal:** make BOSS able to act as that FDE for a customer's application. Concretely, given a generated or customer-owned app repo, BOSS should be able to:

1. Emit CI/CD config for a repo that has none (a governed PR that adds `.github/workflows/*.yml` or a `Jenkinsfile`).
2. Discover and drive the pipeline that already exists — trigger it, poll it, read its verdict — regardless of provider.
3. Chain a **staging DeploymentRun → smoke/canary gate → production DeploymentRun** as one orchestrated, observable promotion, with automatic rollback on a health breach.
4. Surface all of the above in the console as a single deployment timeline, not as three disconnected primitives.

The design principle throughout: **reuse the primitives that already exist.** BOSS is not missing a deployment engine — it is missing generalization, a provider-agnostic pipeline seam, a config generator, and one orchestration flow that ties the pieces together.

---

## 2. Current State (ground truth)

This module is unusually far along. Being explicit about what exists prevents rebuilding it.

### 2.1 What already exists

**Three real canary mechanisms** (all in-tree, all persisting):

1. **Preview smoke canary** — `apps/api/app/services/preview_service.py:384` `smoke_test()`, described in-code as the "Tier-3 smoke canary": hits a live preview URL with a set of checks and asserts each. Exposed as `POST /v1/previews/{id}/smoke`.
2. **Invariant canary suite** — `apps/api/app/services/canaries.py` (`run_canary_suite:164`, `list_canary_runs:197`): a battery of platform-invariant probes (exactly-once `_canary_exactly_once:34`, no-drop outbox `:48`, job lifecycle `:63`, budget reserve/release `:82`, router fallback `:100`, skill mock `:128`). Persists to the `canary_runs` table for console display.
3. **Self-improvement canary + staged rollout with auto-rollback** — `apps/api/app/services/self_improvement.py` (`record_canary_result:323`, `start_rollout:467`, `advance_rollout:528`, `_breach_reason:656`, `rollback:782`), driven by the routes at `apps/api/app/main.py:5594-5772` (`record_self_improvement_canary`, `start_self_improvement_rollout`, `advance_self_improvement_rollout`). This is the crown jewel: *ship-narrow → widen*, each stage gated by a health observation, with **automatic rollback on a threshold breach** (`self_improvement.py:477` documents exactly this). **Today it only applies to BOSS's own self-improvement proposals.**

**Tiered previews / deploy substrates** — all in `preview_service.py`:
- Tier-1 "1-box" host preview (`start_preview:114`, module docstring line 1).
- Tier-2 dev-container preview (`start_container_preview:215` — builds a Docker image from the run's branch).
- Tier-3 smoke canary (`smoke_test:384`).

**Real deploy actuation** — `apps/api/app/services/software_factory.py:849-1215`: a `deployment-review` Decision gate (`_ensure_deployment_review_decision`, entered at `:849` when `settings.enable_sandbox_deploy`), a health verifier `_verify_deployment_health:1009`, an INSERT into the `deployment_rollouts` table `:1206`, and `list_deployment_rollouts:1266`. Deploy actuation is real, gated, and recorded.

**External CI reader** — `apps/api/app/services/github_adapter.py:319` `wait_for_commit_checks()`: polls `/repos/{repo}/commits/{sha}/check-runs` (`:330`), collects check-runs (`:332`), and evaluates them (`_evaluate_check_runs`, called `:343`) to gate the merge. Also present in the same adapter: `create_pull_request:183`, `merge_pull_request:223`, `create_deployment:240` (GitHub Deployments API), `upsert_file:148`.

**Preprod→prod policy** — `apps/api/app/services/resource_plane.py:3334` `_deployment_rollout_policy_for_target()`: enforces `preview` / `staging_allowed` / `production_allowed` gating (`:3385-3395`), consulted at `:1176` and `:3661`. The policy *decision* exists; the orchestrated *flow* that walks staging→gate→production does not.

**GitHub deployment state** is already the recorded source of deploy truth (CHANGELOG "GitHub-backed sandbox PR, merge, and deployment integrations"). Provider deploy skills exist for DigitalOcean app deploy, Cloudflare worker/pages, Supabase migration, and SSH-host workload.

### 2.2 Gaps (what "extend/generalize" means here)

| Gap | Evidence |
|-----|----------|
| Staged rollout + auto-rollback is **hard-wired to self-improvement proposals**, not available for arbitrary customer app DeploymentRuns | `self_improvement.py:467/528/782`; `deployment_rollouts` INSERT `software_factory.py:1206` records but does not run a health-verified staged rollout. CHANGELOG "Known constraints": *"Deployment is recorded through real GitHub deployment state, but not yet through a full health-verified staged rollout pipeline."* |
| BOSS **reads** external CI but does not **author or drive** a pipeline | `wait_for_commit_checks:319` only polls; there is no trigger/dispatch, and it is GitHub-only. |
| **No CI-config generation** — BOSS never emits `.github/workflows/*.yml` or a `Jenkinsfile` | `grep` for workflow/Jenkinsfile generation in the code-agent path returns nothing; code agents produce app source only. |
| **No non-GitHub build integration** — Jenkins/GitLab are unreachable | Only `github_adapter.py` / `github_app_adapter.py` exist. |
| **No single staging→prod orchestrator** — the pieces (preview, smoke, deploy, policy, rollout) are not chained into one observable promotion flow | policy at `resource_plane.py:3334` is consulted per-target; nothing sequences the two DeploymentRuns with the gate between them. |

---

## 3. Target Design & Approach

### 3.1 The two architectural options (the user's framing)

The user raised the core question directly:

- **Option (a) — BOSS *runs* a traditional CI/CD pipeline.** BOSS is the pipeline. It owns build/test/deploy steps itself: the coding pipeline builds, the preview tiers stage, `deployment_rollouts` + the generalized staged-rollout engine deploy and gate. External CI systems are irrelevant.
- **Option (b) — BOSS *learns and drives* an existing pipeline.** BOSS spawns an agent that discovers the customer's pipeline (Actions/Jenkins/GitLab), triggers it, reads its verdict, and treats it as the build/test authority; BOSS layers promotion + rollback orchestration *on top*.

### 3.2 Recommendation: **do both, seam-separated — (b) is the primary FDE surface, (a) is the fallback.**

The FDE reality is that the customer already has a pipeline and expects BOSS to respect it — so **(b) is the primary customer-facing capability.** But BOSS's *own* generated apps frequently start with *no* pipeline, and BOSS already possesses a full internal build/test/deploy chain — so **(a) is the right substrate when there is nothing to inherit**, and is also what the CI-config generator bootstraps a repo *into*.

The clean way to hold both without forking the codebase is a **`PipelineAdapter` seam** (mirroring how `sandbox_provider.py` already offers local-vs-microVM behind one interface):

- **`ExternalPipelineAdapter`** (option b) — generalizes `wait_for_commit_checks` into `discover()` / `trigger()` / `poll()` / `verdict()`, with concrete `GitHubActionsPipeline` (reuse `github_adapter.py:319` verbatim), then `JenkinsPipeline`, `GitLabPipeline`.
- **`NativePipelineAdapter`** (option a) — wraps BOSS's own chain: coding pipeline → Tier-2 container build → Tier-3 smoke → generalized staged rollout. This is what a repo runs on until/unless it has external CI.

The **promotion orchestrator** and the **generalized staged-rollout engine** sit *above* the adapter and are provider-agnostic: they consume a `PipelineVerdict` and a `DeploymentRun` regardless of who produced them. This means we build option (a)/(b) parity once and every future provider is just an adapter.

### 3.3 How each ticket reuses existing primitives

- **T1 (generalize staged rollout)** lifts `self_improvement.py:467/528/782` into a `deployment_rollout_engine` that operates on `deployment_rollouts` rows (`software_factory.py:1206`) instead of self-improvement proposals — same ship-narrow→widen→auto-rollback state machine, new subject.
- **T2 (pipeline adapter)** promotes `github_adapter.wait_for_commit_checks:319` and `_evaluate_check_runs` behind the `ExternalPipelineAdapter` interface; GitHub is the first concrete impl and requires almost no new logic.
- **T3 (CI-config generator)** is a new code-agent capability that emits workflow YAML / Jenkinsfile, wired into the governed pipeline's PR stage (same PR-authoring path the code agent already uses via `github_adapter.upsert_file:148` / `create_pull_request:183`).
- **T4 (promotion orchestrator)** chains two `deployment_rollouts` runs across the `resource_plane._deployment_rollout_policy_for_target:3334` staging/production gate, with a smoke/canary (`preview_service.smoke_test:384` or `canaries.run_canary_suite:164`) in between.
- **T5 (console surface)** joins `deployment_rollouts` + `canary_runs` + pipeline verdicts into one deployment timeline in `apps/web/app/*-ui.tsx`.

---

## 4. Implementation Tickets

### T1 — Generalize the staged-rollout + auto-rollback engine to customer DeploymentRuns
**Scope:** Extract the ship-narrow→widen→health-gated→auto-rollback state machine currently locked inside self-improvement into a reusable `deployment_rollout_engine` that operates on arbitrary `deployment_rollouts` rows. Preserve the self-improvement callers by having them delegate to the new engine (no behavior change for BOSS-internal rollouts). Closes the CHANGELOG "not yet full health-verified staged rollout for app deploys" gap.
**Key files:** `apps/api/app/services/self_improvement.py:467/528/656/782` (source of the state machine), `apps/api/app/services/software_factory.py:1009/1206/1266` (`deployment_rollouts` schema, health verifier `_verify_deployment_health`), new `apps/api/app/services/deployment_rollout_engine.py`, `apps/api/app/main.py:5594-5772` (route pattern to mirror for app deploy: `.../rollout/start`, `.../rollout/advance`).
**Acceptance criteria:**
- [x] New engine module exposes `start_rollout` / `advance_rollout` / `rollback` operating on a `deployment_rollouts` row keyed by DeploymentRun id.
- [x] Stage advance consumes a health observation and calls an injected health source; breach → automatic `rollback` (parity with `self_improvement._breach_reason:656`).
- [x] Self-improvement and customer deployments delegate deterministic stage transitions to the shared rollout engine with **zero behavior change** for existing self-improvement persistence/routes.
- [x] New `POST /v1/deployment-runs/{run_id}/rollout/{start,advance,rollback}` routes are guarded by project membership plus `providerops_manage`.
- [x] Rollback is idempotent and records a `deployment_rollouts` state transition + emits a `deployment.rolled_back` event.

**Implementation evidence:** `deployment_rollout_engine.py` owns the shared transition and DeploymentRun persistence; `self_improvement.py` delegates stage creation, advance, and breach evaluation to it. API and DB acceptance coverage lives in `test_cicd_promotion.py`.
**Size:** L

### T2 — Provider-agnostic PipelineAdapter (drive an existing pipeline)
**Scope:** Define a `PipelineAdapter` interface (`discover` / `trigger` / `poll` / `verdict`) and refactor the GitHub check-run logic into the first concrete `GitHubActionsPipeline`. This is option (b)'s foundation. Ship GitHub first (near-zero new logic — it already exists), then Jenkins, then GitLab as follow-on adapters.
**Key files:** `apps/api/app/services/github_adapter.py:319` (`wait_for_commit_checks`, `_evaluate_check_runs:343`), new `apps/api/app/services/pipeline_adapter.py` (interface + `GitHubActionsPipeline`), `apps/api/app/services/sandbox_provider.py:235` (`make_sandbox_provider` — the pattern to mirror for a `make_pipeline_adapter` factory + setting).
**Acceptance criteria:**
- [x] `PipelineAdapter` protocol defined with `discover(repo) -> PipelineDescriptor`, `trigger(ref) -> RunHandle`, `poll(handle) -> RunStatus`, `verdict(handle) -> PipelineVerdict`.
- [x] `GitHubActionsPipeline.verdict` reuses the GitHub check evaluation contract; `wait_for_commit_checks` delegates to poll-until-terminal over the same endpoint.
- [x] `GitHubActionsPipeline.discover` lists workflows via the Actions API and reports whether the repo has a pipeline (feeds T3's "generate if absent" decision).
- [x] `make_pipeline_adapter(settings)` factory + `pipeline_provider` setting (default `github_actions`), mirroring `make_sandbox_provider:235`.
- [x] Merge gate in `software_factory` consumes `PipelineVerdict` instead of calling `wait_for_commit_checks` directly.
- [x] Adapter interface documented so Jenkins/GitLab are pure-additive (stub classes with `NotImplementedError` land in this ticket).

**Implementation evidence:** `pipeline_adapter.py` contains the protocol, result types, GitHub Actions implementation, factory, and additive Jenkins/GitLab seams. `github_adapter.py` retains compatibility by delegating its legacy wait method to the adapter.
**Size:** M

### T3 — CI-config generator (author a pipeline where none exists)
**Scope:** New code-agent capability that generates CI/CD config for a repo — `.github/workflows/ci.yml` (build+test+deploy) first, `Jenkinsfile` second — and proposes it through the governed pipeline's PR stage. Triggered when `PipelineAdapter.discover` reports no pipeline. This is what bootstraps a bare generated app onto option (a)'s native substrate or emits Actions YAML for option (b).
**Key files:** `apps/api/app/services/code_agent.py` (add a CI-config generation role/capability), `apps/api/app/services/governed_code_task_service.py` (PR stage wiring), `apps/api/app/services/github_adapter.py:148/183` (`upsert_file` / `create_pull_request` — reuse for authoring), `packages/specpack/skills/*.yaml` (register the capability as a skill spec).
**Acceptance criteria:**
- [x] Given a repo language/framework signal, the generator emits a valid `.github/workflows/ci.yml` covering install → test → build → deploy handoff stages.
- [x] Generated config is proposed as a governed PR (never pushed to a default branch directly), reusing the existing PR-authoring path.
- [x] Generator is a no-op when `PipelineAdapter.discover` reports an existing pipeline (idempotent, no duplicate workflow files).
- [x] Jenkinsfile emission is behind the same capability.
- [x] Generated workflow YAML is parsed and its required jobs are validated before the PR opens.

**Implementation evidence:** `ci_config_generator.py`, `coding_pipeline.generate_ci_config`, and `POST /v1/pipeline-config/proposals` generate deterministic config and hand it to `SoftwareFactoryService.propose_pull_request`, preserving budget, receipt, and draft-PR controls. The deploy job creates evidence only; Resource Plane remains the only provider-actuation path.
**Size:** M

### T4 — Staging→prod promotion orchestrator
**Scope:** A single flow that chains **staging DeploymentRun → smoke/canary gate → production DeploymentRun**, honoring the existing preprod→prod policy and using T1's staged-rollout engine for the production leg. This is the explicit orchestration the user asked for; today the policy exists but nothing walks the two runs with a gate between them.
**Key files:** `apps/api/app/services/resource_plane.py:3334` (`_deployment_rollout_policy_for_target` — the gate to honor), `apps/api/app/services/software_factory.py:849/1009/1206` (DeploymentRun creation + health verify), `apps/api/app/services/preview_service.py:384` (`smoke_test`) and `apps/api/app/services/canaries.py:164` (`run_canary_suite`) for the mid-promotion gate, new `apps/api/app/services/promotion_orchestrator.py`, `apps/api/app/main.py` (new `POST /v1/deployments/promote` route).
**Acceptance criteria:**
- [x] `promote(project, ref)` creates a staging DeploymentRun, blocks on its configured health gate, and only then creates a production DeploymentRun via the T1 staged-rollout engine.
- [x] Production leg is refused unless the existing deployment rollout policy returns `production_allowed`.
- [x] Gate failure aborts the promotion before any production effect and records the reason.
- [x] Each leg emits events (`deployment.staging_ok`, `deployment.promoted`, `deployment.promotion_blocked`) for the console timeline.
- [x] Full promotion is a single addressable entity (a `promotion` id) linking both DeploymentRun ids + the gate result.

**Implementation evidence:** `promotion_orchestrator.py` and the `deployment_promotions` table persist the pipeline verdict, both target/run links, staging gate, production rollout, terminal reason, and events. The production run does not exist until the staging health gate passes.
**Size:** L

### T5 — Console deployment timeline
**Scope:** Surface the promotion as one observable timeline in the console: staging run → gate result → production run → rollout stages → rollback (if any). Joins `deployment_rollouts`, `canary_runs`, and pipeline verdicts.
**Key files:** `apps/web/app/*-ui.tsx` (new deployment/promotion view alongside the existing preview `code-tasks-ui.tsx:30` controls), `apps/api/app/main.py` (read route `GET /v1/deployments/{promotion_id}` aggregating the three sources), `apps/api/app/services/software_factory.py:1266` (`list_deployment_rollouts`), `apps/api/app/services/canaries.py:197` (`list_canary_runs`).
**Acceptance criteria:**
- [x] Console shows a promotion as an ordered timeline: pipeline verdict → staging deploy → gate → production rollout stages → terminal state.
- [x] Auto-rollback events render distinctly with the breach reason surfaced.
- [x] Timeline data comes from one aggregating read endpoint (no client-side join across three calls).
- [x] In-progress promotions poll through the existing server-render refresh path every five seconds.

**Implementation evidence:** `GET /v1/deployment-promotions/{promotion_id}` returns the complete ordered timeline, and `deployment-promotion-timeline.tsx` renders it in the ProviderOps deployment card with active-state refresh.
**Size:** M

### T6 (follow-on) — Jenkins + GitLab pipeline adapters
**Scope:** Concrete `JenkinsPipeline` and `GitLabPipeline` implementations of the T2 interface (trigger via job/API, poll build status, map to `PipelineVerdict`). Pure-additive once T2 lands.
**Key files:** `apps/api/app/services/pipeline_adapter.py` (new concrete classes), settings for Jenkins/GitLab base URLs + credentials.
**Acceptance criteria:**
- [ ] `JenkinsPipeline` triggers a job, polls to a terminal state, maps to `PipelineVerdict`.
- [ ] `GitLabPipeline` does the same over the GitLab pipelines API.
- [ ] Both selectable via `pipeline_provider` setting with no changes to the orchestrator or merge gate.
**Size:** M

---

## 5. Dependencies, Sequencing & Priority

**Dependency on spec 05 (IaC / infrastructure provisioning):** the promotion orchestrator (T4) deploys *into* environments. Where those staging/production environments are **provisioned** — the resource-plane ChangeSets and any future IaC path — is owned by spec `05` (Infrastructure-as-Code / provisioning). T4 assumes the target environments already exist and are addressable via a healthcheck URL; standing them up is out of scope here and tracked in 05. If 05 defines an environment abstraction, T4's `promote()` should consume it rather than inventing its own.

**Internal sequencing:**

```
T2 (PipelineAdapter) ──┐
                       ├─► T4 (Promotion orchestrator) ─► T5 (Console timeline)
T1 (Rollout engine) ───┘                                   ▲
                                                           │
T3 (CI-config generator) ──────────────────────────────────┘ (feeds/observed by console)

T6 (Jenkins/GitLab) depends only on T2, can land anytime after.
```

- **T1 and T2 are independent** and can be built in parallel — they are the two load-bearing generalizations.
- **T4 depends on both T1 (production leg) and T2 (verdict source).**
- **T3 depends on T2** (needs `discover()` to know whether to generate).
- **T5 depends on T4** for the promotion entity; can begin against T1's rollout data earlier.
- **T6** is fully deferred; ship GitHub-only first.

**Priority within P2:** T1 → T2 first (they unblock everything and close the headline CHANGELOG gap), then T4, then T3, then T5, then T6. If forced to a minimal slice: **T1 + T4** deliver "BOSS promotes an app staging→prod with auto-rollback," which is the core FDE demo.

---

## 6. Resolved Decisions

1. **Primary architecture posture per customer:** an existing external pipeline is authoritative; config generation is idempotently skipped when discovery finds one. A bare repo receives a governed config PR, after which the same adapter and promotion path applies.
2. **Health signal source:** app rollouts accept the shared health-observation contract with an optional HTTP verifier; staging promotion gates use the health checks configured on the DeploymentTarget.
3. **Default promotion gate:** the staging DeploymentRun's configured customer-app health checks are the default. Platform invariant canaries remain separate evidence and are not silently substituted.
4. **Generated deploy scope:** the generated deploy job packages commit evidence and hands off to BOSS. It receives no provider credentials and performs no provider actuation.
5. **Merge-gate authority:** the external `PipelineVerdict` feeds the existing governed merge validation. Local sandbox validation, data-boundary policy, decision, budget, and receipt requirements still apply where configured.
6. **Promotion persistence:** `deployment_promotions` is a first-class table; `deployment_rollouts.promotion_id` links its production rollout without overloading the rollout ledger.
