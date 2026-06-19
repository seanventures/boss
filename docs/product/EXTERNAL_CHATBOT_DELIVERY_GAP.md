# BOSS Capability Gap & Plan — Delivering an External Client Chatbot

Status: Draft for execution
Date: 2026-06-18
Owner: param
Scope: Enable BOSS to build, deploy, and operate a RAG-backed, DB-connected, role-governed
chatbot for an external client website (reference target: `internationalbuddy2018.web.app`),
and to deploy BOSS-built apps onto self-owned / client-owned infra (Kaappu use case).

---

## 1. Goal

Use BOSS as the delivery harness for a customer engagement where:

1. BOSS modifies the client's website codebase (per a PRD) to add a chatbot UI.
2. The chatbot connects to the client's database at runtime for real-time information and
   actions (e.g. booking appointments, listing volunteer activities, sign-ups), with
   role-based authorization so AI agents cannot act outside their assigned role.
3. BOSS provisions a Neo4j instance in **customer infra** (a microVM/container on a
   customer-provided compute instance), and **initiates** a RAG ingestion process whose
   embeddings (Azure) run **in customer infra** — not in the BOSS sandbox — to populate the
   Neo4j graph from the client's documents/data.

The bar for "ready to contact the customer": every BOSS-side capability below is present and
verified (Section 6 checklist).

---

## 2. Architecture model — two layers (this decides scope)

BOSS is an **autonomous software factory**: a control plane of role-scoped AI workers that
**build + deploy + operate** software. It is not itself a chatbot runtime. Work splits cleanly:

- **Layer A — BOSS *writes* product code.** The sandbox coding agent
  (`apps/api/app/services/code_agent.py` → `ClaudeCodeAgent`, runs Claude Code headless;
  `sandbox-host/` Docker→microVM) clones a repo, edits, runs tests, returns a diff → PR.
  Source/data never leave the sandbox. **BOSS can write the RAG pipeline, Neo4j integration,
  DB connectors, chatbot UI, and the product's end-user RBAC with no new BOSS subsystem.**
- **Layer B — BOSS *connects to / operates* external systems** during build, deploy, and ops
  (provision compute, deploy workloads, trigger remote jobs, run migrations). **This is where
  the real gaps are.**

**Data-plane runs in customer infra.** RAG retrieval, Azure embeddings, and the chatbot's
live DB access all execute in customer infra. BOSS orchestrates; it does not host the data
plane. This respects data residency, keeps Azure cost in the customer account, and matches
BOSS's "sandbox returns a diff only" design.

### Responsibility boundary

| Concern | Where it lives | BOSS change needed? |
|---|---|---|
| Chatbot UI built into website repo | Product code (Layer A) | No — native factory |
| Runtime DB connector + agent action tools | Product code, runs in customer infra | No |
| End-user / agent RBAC (parent/volunteer/admin) | Product code, runs in customer infra | No |
| RAG pipeline + embedding worker code | Product code (Layer A) | No |
| Embeddings execution + vector/graph store | Customer infra (Neo4j) | No (BOSS deploys + triggers) |
| Provision Neo4j / workers on customer compute | **BOSS (Layer B)** | **Yes — host deploy adapter** |
| Trigger + monitor the ingestion run | **BOSS (Layer B)** | **Yes — remote-job skill** |
| PR against the client's repo | **BOSS (Layer B)** | **Yes — repo allowlist** |
| Scoped creds for customer infra/Azure | **BOSS (Layer B)** | Minor — `SecretRef` pattern exists |

---

## 3. Capability gap inventory (evidence-based)

| Capability | In BOSS today | Verdict | Evidence / extension point |
|---|---|---|---|
| Sandbox code execution (clone/edit/test/diff) | Yes | ✅ Native | `code_agent.py`, `sandbox-host/in_guest_agent.py`, `claude_cli_adapter.py` |
| Open PR against a repo | Yes, but allowlisted to `seanheiney/boss-sandbox` | 🟡 Config | `packages/specpack/skills/github.yaml`, `roles/software_engineer.yaml` |
| Azure OpenAI **inference** | Yes | ✅ | `openai_adapter.py` routes to Azure when configured |
| Azure OpenAI **embeddings** | No (no embedding calls anywhere) | ❌ | Add embeddings path to adapter **only if BOSS embeds**; here embeddings run in customer infra, so this is product-worker code |
| RAG (chunk/embed/index/retrieve) inside BOSS | No | ❌ → **N/A** | Intentionally pushed to customer infra; BOSS writes + deploys + triggers it |
| Graph DB / Neo4j as a provisioned product resource | No | ❌ | No graph support; provision via new host adapter |
| Connect to **client datasource** at runtime | No (in BOSS) | ❌ → **N/A** | Runtime connector is product code; BOSS does not query client DB itself |
| Deploy to **arbitrary external host** (SSH/BYO box) | No (only DO/Cloudflare/Supabase) | ❌ | `resource_plane.py` `SUPPORTED_PROVIDERS`; `provider_account` enum already lists aws/azure/gcp/local but no adapters |
| Trigger + monitor a remote job in customer infra | No | ❌ | New orchestration skill |
| Agent RBAC (BOSS's own agents) | Yes, enforced kernel | ✅ (reference only) | `roles/*.yaml`, `policies/autonomy_ladder.yaml`, `check_scope()` — prompt-injected + post-hoc audit, not the product's end-user RBAC |
| Autonomous merge + deploy | PR-then-human-gated by design | 🟡 Config | `policies/default_safety.yaml` `github_real_merge_allowed:false`; `enable_sandbox_merge`/`enable_sandbox_deploy` env flags, off by default |

**Net:** the only *new BOSS subsystems* required are the **host/compute deploy adapter** and a
**remote-job orchestrator**. Everything labeled N/A above is product code BOSS writes (Layer A),
not a BOSS change.

---

## 4. Remaining BOSS backlog

| ID | Capability | Why | Effort |
|---|---|---|---|
| A | Repo allowlist for external/client repos | Factory PRs against the website code (Goal #1) | Config |
| B | **Host/compute deploy adapter** — given a compute instance (IP+creds), launch a workload (Neo4j container/microVM, ingestion worker, chatbot) via SSH + Docker | Central piece behind Goal #2-deploy and #3-provision | **Medium** |
| C | Remote-job orchestration skill — kick off the ingestion/embedding run in customer infra, poll status, receipt the result | "BOSS initiates RAG, embeddings run there" (Goal #3) | Low–Medium |
| D | Scoped-credential handling for customer infra (SSH key / cloud creds / Azure embedding key as `SecretRef`) | Agents never hold raw secrets (Goals #2, #3) | Low |
| E | *(optional)* Per-tenant autonomy gates for self-owned repos | No-human merge/deploy on Kaappu repos only | Config + 1 policy edit |

**Out of scope for BOSS (Layer A — BOSS writes as product code, not a BOSS feature):** RAG
pipeline, Neo4j schema/queries, runtime DB connectors, chatbot UI, product end-user RBAC.

---

## 5. Execution plan (phased)

Each phase ends with an acceptance test so "done" is observable.

### Phase 0 — Decision lock (blocking, ~0.5 day)
- Resolve the compute fork (Section 7): does the customer hand BOSS a **running host**
  (path i) or must BOSS **create the cloud VM** (path ii)? Default assumption: **path (i)**.
- Confirm credential model (SSH key vs cloud IAM) and Azure embedding account ownership.
- **Acceptance:** decision recorded in this doc; if path (ii), add a cloud-provisioning
  adapter sub-task to Phase 2.

### Phase 1 — Repo access (A) (~0.5 day) — **mostly config; de-risked**
- Finding: the runtime already targets an arbitrary repo via `settings.target_github_owner` /
  `target_github_repo` (env `BOSS_TARGET_GITHUB_OWNER` / `BOSS_TARGET_GITHUB_REPO`,
  default `seanheiney/boss-sandbox`). The hard-coded `boss-sandbox` in
  `default_safety.yaml:66` + skill specs is a declarative default, not a runtime lock.
- Remaining: set those env vars to the real client/Kaappu repo, provide a token/GitHub-App
  install with access, and widen the declarative `allowed_github_targets` list to match.
  Pending the actual repo name (post customer contact).
- **Acceptance:** BOSS opens a no-op PR against the target repo and it appears on GitHub.

### Phase 2 — Host/compute deploy adapter (B + D) (~2–4 days)
- Implement `SSHHostAdapter` following the `DigitalOceanAdapter` pattern; register in
  `resource_plane.py` `SUPPORTED_PROVIDERS`.
- Add `packages/specpack/skills/ssh_host.yaml` with `ssh_host.deploy`,
  `ssh_host.health_check`, `ssh_host.teardown` (rsync/scp + Docker run / compose / systemd).
- Wire host creds + Azure key as `SecretRef`; enforce ChangeSet + ResourceReceipt + budget.
- **Acceptance:** BOSS deploys a Neo4j container to a provided test host and the health
  check returns green; a `ResourceReceipt` is written.

### Phase 3 — Remote-job orchestration (C) (~1–2 days)
- Add a skill to launch an ingestion/embedding job on a deployed host and poll to completion,
  receipting start/finish; no embeddings run in the BOSS sandbox.
- **Acceptance:** BOSS triggers a sample ingestion worker on the test host that writes N
  embedded nodes into Neo4j; BOSS reports completion + a receipt.

### Phase 4 — ~~Optional self-tenant autonomy (E)~~ **DROPPED 2026-06-18**
- Decision: do **not** relax autonomy gates for a "self-tenant." Dogfooding must follow the
  **exact customer configuration** — all default safety gates ON, ChangeSet → approval →
  budget → receipt — because relaxing them would bypass the precise approval/access path a
  real customer depends on and would *hide* the security/access bugs we want to surface.
- Replacement: **Phase 4' — governed-path dogfood.** Run the full customer governance flow
  locally (Postgres-backed, per `BOSS_Local_Run_Guide.md`) with no shortcuts:
  create a scoped `ProviderAccount` (provider `ssh_host`), submit an
  `ssh_host.workload.deploy` ChangeSet, clear the risk-tier-3 CEO/CTO approval gate, actuate,
  and verify a persisted `ResourceReceipt` + `external_resource` row + the live container.
  Probe access control: unknown/under-permissioned actors must be rejected; the agent must
  never see the raw key.
- **Acceptance:** a real receipted deploy completes only after a genuine approval; an
  unauthorized actor is denied; teardown is likewise governed + receipted.

### Phase 5 — End-to-end dry run (~1 day)
- Against a throwaway repo + test host, run: PR chatbot UI → deploy Neo4j → trigger ingestion
  → deploy a stub chatbot that queries Neo4j + the test DB under a role-limited agent.
- **Acceptance:** all six checklist items in Section 6 pass.

---

## 6. "Ready to contact the customer" checklist

All must be ✅ before sending the PRD to the client:

- [ ] BOSS can open a PR against an external (non-sandbox) repo.
- [ ] BOSS can deploy a Neo4j workload to a customer-provided compute instance and verify health.
- [ ] BOSS can deploy a worker/app workload to the same host with scoped, `SecretRef`-held creds.
- [ ] BOSS can trigger a remote ingestion/embedding job in customer infra and confirm completion.
- [ ] Embeddings demonstrably run in customer infra, not the BOSS sandbox.
- [ ] (If self-deploy needed) BOSS completes an autonomous build→deploy on a self-owned repo.

---

## 7. Open decisions

1. **Compute provisioning fork** (Phase 0): **RESOLVED 2026-06-18 → path (i)** — customer
   provides a running host (IP + SSH key). Adapter B = SSH+Docker, Medium. Cloud-provisioning
   (path ii) deferred to a separate adapter if/when a client needs BOSS to create the VM.
2. **Workload runtime on host:** **RESOLVED 2026-06-18 → Docker / docker-compose** for
   Phase 2. microVM (Kata/Firecracker, reusing `sandbox-host` concepts) revisited later for
   multi-tenant isolation.
3. **Where do DB-connector + RBAC integration tests run?** The BOSS sandbox cannot reach the
   live client DB; these run in customer staging post-deploy as part of the verify step.
4. **Azure embedding account ownership / cost** — confirmed customer-side.

---

## 8. Status log

- 2026-06-18 — Initial gap analysis and plan drafted (this document).
- 2026-06-18 — Phase 0 decisions locked: compute path (i) customer-provided host; runtime Docker/docker-compose.
- 2026-06-18 — Phase 1 finding: external-repo targeting already supported via env; config-only.
- 2026-06-18 — Phase 2 first cut on branch `feat/external-delivery-adapters`: `SSHHostAdapter`
  (`apps/api/app/services/ssh_host_adapter.py`, ssh+docker, key as 0600 temp SecretRef),
  `ssh_host` registered in `SUPPORTED_PROVIDERS` + `DEPLOYMENT_SKILL_IDS`, config gates
  (`BOSS_SSH_HOST_DEPLOY_ALLOWED` / `BOSS_SSH_HOST_PRIVATE_KEY`), skill spec
  `packages/specpack/skills/ssh_host.yaml` (added to core_v1/core_v2 packs). Skill specs pass
  `validate_skill_spec`; ruff clean. **Not yet wired:** changeset/receipt actuation routing in
  `resource_plane` for the new capabilities.
- 2026-06-18 — Phase 2 **live adapter test PASSED** against AWS prod host `3.94.66.139`
  (ubuntu, `~/.ssh/kaappu-demo.pem`, ARM64/2GB, Docker 29.3.0). Drove `SSHHostAdapter`
  directly: connection_test → deploy (`alpine:3.20`, no published ports) → health_check
  (running=true) → teardown (removed=true) → verify (running=false, zero leftover
  containers/images). Used a throwaway alpine container, not Neo4j, to avoid load on the
  2GB prod box; pulled image removed afterward. **Still pending for full acceptance:** the
  ChangeSet→`SSHHostAdapter`→`ResourceReceipt` dispatch path in `resource_plane`, so the
  test exercised the adapter (the harness's execution boundary), not the receipted flow.
- 2026-06-18 — Phase 2 **actuation dispatch wired**: `resource_plane` now routes
  `ssh_host.workload.{deploy,health_check,teardown}` to `SSHHostAdapter` via
  `_ssh_host_workload_*` methods returning the standard managed-resource result dict
  (consumed by `_persist_managed_resource_from_result`). `ssh_host` added to the
  `ProviderAccount` provider enum (code gate via `_normalize_provider`/`SUPPORTED_PROVIDERS`
  already accepted it). Live-tested the actuation layer against `3.94.66.139`:
  deploy→health(running)→teardown(removed), zero leftover containers/images. ruff + syntax
  clean. **Remaining for full Phase 2 acceptance:** exercise the end-to-end
  `apply_changeset` flow (budget reservation → approval gate → actuation → `ResourceReceipt`
  row) — needs a Postgres-backed run, not available in this local check.
  Phase 2 status: **code complete; receipted e2e run pending a DB environment.**
- 2026-06-18 — Phase 3 (remote-job orchestration) implemented: `SSHHostAdapter` gains
  one-shot job primitives (`run_job` / `job_status` / `job_logs` / `remove_job` /
  `run_job_to_completion` — start, poll-to-exit, collect logs+exit code, auto-cleanup).
  Wired `ssh_host.job.run` into `resource_plane` (`_ssh_host_job_run`, capability +
  dispatch) and the skill spec (risk_tier 2, ChangeSet + budget + ceo approval + receipt).
  This is the "BOSS initiates ingestion; embeddings + graph population run in customer
  infra; BOSS only sets up and observes" primitive. The continuous re-ingest-on-new-files
  trigger is handled by a watcher container BOSS *deploys* via Phase 2, not by per-file
  BOSS calls. Live-tested the job-run actuation against `3.94.66.139`: an alpine job ran to
  completion, BOSS captured exit_code=0 and logs (`ingest:start/ingest:done`), container +
  image auto-cleaned. ruff + all 4 skill specs validate. **Same caveat as Phase 2:** the
  receipted `apply_changeset` e2e run needs a Postgres environment.
  Phase 3 status: **code complete; receipted e2e run pending a DB environment.**
- 2026-06-19 — Phase 4' **governed-path dogfood PASSED** (full customer config, all safety
  gates ON, local Postgres stack). Flow against AWS prod host `3.94.66.139`:
  create scoped `ProviderAccount` (ssh_host) → deploy `ChangeSet` → CEO/CTO approval gate →
  apply → actuation → `ResourceReceipt` (`rrcpt_87c1cbba…`, succeeded) + `external_resource`
  (container, active/running, env=production) + **live container confirmed on host** →
  governed teardown `ChangeSet` (receipt `rrcpt_60a028f9…`) → host clean. This closes the
  Phase 2 receipted-e2e caveat through the exact path a customer would use.

  Access-control probes (all correct): apply-before-approval → 422 "must be approved";
  approve by non-member `qa` → 403; agent never receives the raw key (env SecretRef only).

  Governance gates surfaced (expected customer onboarding/config steps):
  1. Production-affecting ops require a `rollback_plan` (422 until provided).
  2. Deployment ChangeSets require an **active** `deployment-environment-policy` for the
     target env (production → `runtime_mode: production_allowed`, `allowed_target_kinds`
     incl. `container`); absent → 422 `missing_active_environment_policy`.

  ⚠️ FINDING to upstream: the skill declares `requires_approval: [ceo, cto]` (dual control),
  but a single `ceo` approval flipped the ChangeSet to `approved` and apply succeeded — the
  changeset gate appears to enforce a generic `requires_approval` boolean, not the per-skill
  multi-approver list. For security-sensitive deploys a customer expecting two distinct
  approvers would not get dual control. Worth a fix/issue in BOSS core.

- 2026-06-19 — **Dual-control FIXED** (decision: enforce true two-person rule for all
  skills). `resource_plane.approve_changeset` now accumulates approvals in `policy_result`
  and only finalizes (`approved`) once every role named in the operations' skills'
  `requires_approval` lists has approved; partial approval stays `pending_approval` and apply
  stays blocked. The approve endpoint (`main.py`) now authorizes a changeset's *named*
  approver roles via membership+role (no generic `providerops_manage` shortcut) — which both
  lets `release_manager`/`cto` provide the second approval AND tightens it (a generic
  providerops manager can't satisfy a dual-control gate). A second gap surfaced and was also
  fixed: `release_manager` lacked `providerops_manage`, so dual-control on
  `[ceo, release_manager]` skills (DO app / Cloudflare / Supabase deploys) was previously
  uncompletable. Tests: updated the provider/deployment changeset tests to supply both
  approvers + a `half_apply` assertion proving one approval can't apply. Full suite: the only
  failures are 7 pre-existing, environment-caused (`.env` has real creds, so
  `*_blocks_without_credentials` tests invert) + known ordering flakiness — identical with
  and without this change (verified by stash diff). No regressions introduced.
