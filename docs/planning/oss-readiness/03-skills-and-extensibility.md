# Skills & Extensibility: Marketplace + System-vs-Customer Skill Loading

> **Implementation status (July 2026):** The P0 core (A1/A2/A3/B1/B2/B3/B4) is implemented. SkillSpecs now resolve real server executors from lease-owned `skill.execute` jobs or the governed agent loop, reconcile receipt-backed costs, persist per-project registry state, filter through effective edition packs, resolve per-task module/system/tenant unions, and pause high-risk agent skill calls for approval. Marketplace packaging, MCP gauntlet unification, and lazy on-demand enablement remain P1.
> · **User asks:** #4 (skills marketplace / extensibility) and #5 (system-provided vs customer-provided skill loading)
> · **Module:** Skills & Extensibility
> · **Priority:** **P0** — the loading model (ask 5) and a real executor registry (the wiring half of ask 4); **P1** — the marketplace surface (publishing, provenance, import/export) on top of MCP.
> · **OSS relevance:** the *skill-loading model* is an OSS-engine concern (self-hosters extend the OSS engine with their own skills/MCP servers). Marketplace *hosting/distribution* is the paid control-plane side.

---

## 1. Context & Goal

Two adjacent user asks:

- **Ask #4 — Skills marketplace & extensibility.** Users want to add capabilities (tools/skills) to BOSS without forking the repo, discover capabilities others have published, and trust what they install. Today the only no-fork extension path is registering an MCP server per project; skills require editing YAML in the repo.
- **Ask #5 — System vs customer skill loading.** Users want a clear split between skills BOSS *ships and always loads* ("system" skills) and skills a *tenant enables* on their own project ("customer" skills), plus on-demand loading so an agent isn't handed every tool for every task.

The core tension in the codebase: **there are two parallel tool systems that don't talk to each other.**

1. The **SkillSpec system** — a rich governance shell (permissions, cost, retry, risk-tier, data-boundary, policy) that is inert: it can be listed and *mock*-invoked but never really runs, and nothing on the real execution path consults it.
2. The **agent tool system** (`agent_loop.py`) — the thing that actually runs, built from a hardcoded Python `TOOLS`/`WRITE_TOOLS` list plus **live MCP** tools injected per project.

The goal of this spec is to (a) collapse those two systems by making SkillSpecs *executable* through a real registry that the agent loop and worker share, (b) give skills the same per-project storage + registration lifecycle MCP already has, (c) implement a proper system/customer/on-demand **resolver** that decides which skills reach an agent for a given task, and (d) elevate MCP to a first-class, publishable, provenance-checked marketplace citizen — building the marketplace on the boundary that already works rather than the one that doesn't.

---

## 2. Baseline State at Decomposition (historical ground truth)

### 2.1 SkillSpec is an inert governance shell

- **Registry / loader.** `entitlements.py:90` `_skill_files()` does `(specpack_root / "skills").glob("*.yaml")` — a **global filesystem glob**. `list_skills()` (`entitlements.py:93-118`) reads all 14 files in `packages/specpack/skills/*.yaml`, validates each (`validate_skill_spec`, `entitlements.py:120-171`), and returns a flat list. There is **no `project_id` parameter** and **no pack/tier filter** — every project sees every skill.
- **SkillSpec shape** (e.g. `packages/specpack/skills/github.yaml`): `id`, `kind`, `risk_tier` (0-3), `data_boundary`, `input_schema`/`output_schema`, `cost.{estimator,category}`, `retry.{max_attempts,backoff}`, `permissions.{allowed_callers,required_permissions}`, `policy.{requires_receipt, requires_approval, allowed_targets, dedupe_key_template, ...}`. Rich enough to drive execution — but nothing executes it.
- **Runtime.** `skill_runtime.py` `SkillRuntimeService.invoke()` runs the full gauntlet: permission gate (`_caller_allowed`, :162), budget reserve in the skill's cost category (:87, real mode only), retry loop up to `retry.max_attempts` (:108), records to `skill_invocations`, emits `skill.invoked`. It accepts an `executor: Callable[[dict], dict]` for real mode — **but nobody ever passes one.** In real mode with `executor is None` it raises (`skill_runtime.py:113-115`).
- **API is mock-only.** `main.py:6415` `GET /v1/skills` lists specs; `main.py:6428` `POST /v1/skills/{id}/invoke` **hard-rejects `mock=false`** with HTTP 400 (`main.py:6433-6438`): "Real skill invocation must be driven by the worker runtime." `GET /v1/skills/invocations` (`main.py:6422`) reads the ledger.
- **The worker never wires it.** `apps/worker/boss_worker/` (`main.py`, `runner.py`, `control_plane.py`, `config.py`) contains **zero** references to `skill_runtime` / `SkillRuntimeService`. The promised "worker runtime passes an executor" does not exist. Real-mode invocation is unreachable end-to-end.
- **The real agent loop ignores SkillSpecs entirely.** `agent_loop.py:151` defines `TOOLS` and `:358` `WRITE_TOOLS` as hardcoded Python dicts; `:207` `_TOOL_SCHEMAS` and `:425` `_WRITE_SCHEMAS` are derived from them. The live schema list is `list(_TOOL_SCHEMAS) + list(_WRITE_SCHEMAS)` (`agent_loop.py:610`) plus MCP tools — **SkillSpecs are not in it.** Tool dispatch goes through `_dispatch` (`:210`) / `execute_agent_tool` (`:463`) / `_WRITE_FNS` (`:422`), a wholly separate namespace from skill_ids.
- **Role `allowed_skills` is advisory / unenforced on the hot path.** `role_governance.py:137-153` (`check_scope`) can match a skill against a role's `allowed_skills`/`forbidden_skills` patterns and adjusts trust — but it's a **standalone endpoint** (`main.py:5995`), never called by `skill_runtime.invoke` or `agent_loop`. In `agent_runtime.py:247-250` `allowed_skills` is merely **appended to a prompt checklist** ("Role allowed skills: …"). The skill ids referenced by roles are not reconciled against real SkillSpec ids.

**Net:** skills today are a *catalog + a governance simulator*. Impressive on paper, executes nothing.

### 2.2 MCP is the real, working plug-in boundary

- **Per-project storage.** `mcp_registry.py` backs the `mcp_servers` table keyed by `project_id`: `register_mcp_server` (:37), `list_mcp_servers(enabled_only=…)` (:25), `set_mcp_enabled` (:88), `delete_mcp_server` (:105), `ensure_mcp_server` (idempotent, :116). Each row carries `url`, `transport`, `risk_tier`, `data_boundary`, `enabled`, `description`.
- **Client.** `mcp_client.py` speaks JSON-RPC over HTTP: `initialize` / `tools/list` / `tools/call`.
- **Live injection into the agent.** `agent_loop.py:612-629`: for each **enabled** MCP server it calls `mcp.list_tools(url)`, namespaces each tool as `mcp_<server>_<tool>` (:618), appends a function schema to the live `schemas` list, and records it in `mcp_map`. Dispatch at `:666-672`: if a tool call name is in `mcp_map`, it routes to `mcp.call_tool(...)` and emits `agent.mcp_invoked`. **This is the extensibility path that actually fires.**
- **CRUD API.** `/v1/mcp/servers` (`main.py:3878`).
- **Built-ins seeded per project.** `bootstrap.py:19-40` `BUILTIN_MCP_SERVERS` (boss-devops-reference, web-intel, doc-render) registered idempotently via `ensure_mcp_server`.
- **Gaps vs. a marketplace:** no publisher/provenance metadata, no signing, MCP calls **bypass** the skill_runtime cost/retry/permission gauntlet (they're wrapped only in a bare try/except at `agent_loop.py:668-671`), risk-tier is per-*server* not per-*tool*, and there's no packaging/import-export.

### 2.3 System-vs-customer, packs, and the missing resolver

- **Packs are metadata, not a runtime filter.** `packages/specpack/packs/*.yaml` (`community_edition`, `cloud_edition`, `enterprise_edition`, `core_v1/v2`, …) list `installs.skills: [...]` per edition (e.g. `community_edition.yaml:33` → `[model_gateway, github, software_factory, model_providers]`). But `list_skills()` **ignores pack membership** and globs everything. Packs describe intent; nothing enforces it.
- **The one real per-role default.** `agent_runtime.py:77` surfaces `runtime.default_skill_ids` from an agent-module spec — a genuine per-role skill default, but it flows into a compiled prompt, not into an executable tool set.
- **MCP already models the split** BOSS wants for skills: **default** (built-in seed), **on-demand** (`POST` register), **enabled/disabled** toggle (`set_mcp_enabled`). Skills have none of this — no per-tenant storage (skills are global FS only), no enabled toggle, no lazy-load, no per-task resolver.
- **Marketplace concept exists but is hosted-gated & blocked.** `/v1/marketplace/*` (`main.py:6169+`) is specced for **agent-modules/packs** (`commercial.list_marketplace_pack_listings`, sync-runs, `/v1/marketplace/sync`) and requires `BOSS_ACCOUNT_CONNECT_URL` — blocked in OSS/self-host. Local import/export is aspirational (`TECHNICAL_SPEC-v2.0.md:299`), not built. There is **no** third-party *skill* packaging: adding a skill means committing YAML to the repo.

**Summary of the split we must build:** MCP has {default, on-demand, enabled, per-project} but no marketplace polish; Skills have {rich governance, risk-tier, cost} but no execution, no per-tenant storage, no loading model. The design below borrows each side's strength.

---

## 3. Target Design & Approach

### 3.1 Executor registry — make SkillSpecs run (Ask 4, wiring half)

Introduce a **skill executor registry**: an in-process map `skill_id → callable(project_id, inputs, ctx) -> outputs`. The SkillSpec YAML stays the declaration of governance (permissions/cost/retry/risk/policy); the registry supplies the *implementation*.

- New module `services/skill_executors.py`:
  - `register_executor(skill_id, fn)` and a `@skill_executor("github.create_pr_real")` decorator; a module-level `EXECUTORS: dict[str, SkillExecutor]`.
  - Built-in executors that adapt existing adapters to SkillSpec ids (e.g. `github.create_pr_real` → `github_adapter.create_pull_request`; `deployment.*` → provider adapters). These are thin — they reuse existing code, they don't reimplement it.
  - `resolve_executor(skill_id) -> Optional[Callable]`.
- `skill_runtime.invoke()` changes: when `mock=false`, instead of requiring the caller to pass `executor`, it **looks up** `resolve_executor(skill_id)` (caller may still override). Everything else (permission gate, budget reserve, retry, receipt) already works.
- **Worker wiring.** The worker gets a `SkillRuntimeService` and calls `invoke(..., mock=False)` for skill-typed steps, closing the "driven by the worker runtime" gap that `main.py:6432` and the `skill_runtime.py` docstring both promise but never deliver.
- **Bridge to the agent loop.** Add a bridge so SkillSpecs appear as agent tools alongside `_TOOL_SCHEMAS`/MCP:
  - In `_drive_run` (`agent_loop.py:598`), after MCP injection, append schemas for **resolved** skills (from the resolver in §3.2) named `skill_<skill_id>`, built from `input_schema`.
  - In dispatch, before the `_dispatch` fallthrough (`agent_loop.py:688`), add an `elif name.startswith("skill_")` branch that routes through `skill_runtime.invoke(mock=False)` — so a skill invoked *by the agent* gets the **same** permission/cost/retry/risk-tier gauntlet as a skill invoked by the worker. Risk-tier maps onto the existing approval threshold (`agent_loop.py:673-685`): `risk_tier > agent_autonomous_risk_max` ⇒ `create_blocking_decision` and pause, identical to `_WRITE_FNS`.

This collapses the two parallel systems: one governance definition, one execution gauntlet, three entry points (API mock, worker real, agent real).

### 3.2 System / customer / on-demand loading model (Ask 5)

Add a **per-project skill store** mirroring `mcp_servers`, plus a **resolver**.

**New table `project_skills`** (mirror `mcp_registry.py`):
`id, project_id, skill_id, source ('system'|'tenant'|'module'), enabled bool, on_demand bool, config jsonb, publisher jsonb, created_at`. Functions in a new `services/skill_registry.py`: `list_project_skills`, `enable_skill`, `disable_skill`, `register_skill`, `ensure_skill` (idempotent, for system seeding).

**The `system` flag.** Each SkillSpec gains an optional `system: true` field (and, for on-demand, `load: "on_demand"` default `"eager"`). System skills are **always loaded** for every task on every project (e.g. read-only reference skills). They are seeded into `project_skills(source='system', enabled=true, on_demand=false)` at bootstrap — the same way `bootstrap.py:19-40` seeds built-in MCP servers.

**The resolver — `skill_registry.resolve_for_task(project_id, task) -> list[ResolvedSkill]`.** For a given task it returns the union of:

```
resolved_skills(project, task) =
      system skills            (SkillSpec.system == true; always on)
    ∪ module default_skill_ids  (agent_runtime.py:77, for the task's matched agent-module/role)
    ∪ enabled tenant skills     (project_skills where source='tenant' AND enabled AND NOT on_demand)
  then filtered by:
    – pack/tier membership       (project's edition pack installs.skills — §3.3)
    – role allowed/forbidden     (role_governance patterns — now enforced, not advisory)
  on-demand skills are NOT pre-loaded; they are lazy-attached on first reference.
```

In prose: **system** skills are the floor (always present). **Module defaults** add the skills a role is expected to use. **Enabled tenant** skills add what the customer opted into. That set is then **narrowed** by the project's edition pack (so Community doesn't see enterprise-only skills) and by the role's `allowed_skills`/`forbidden_skills`. On-demand skills sit in the catalog but are not handed to the agent until the agent (or an operator) explicitly requests one, at which point it's lazily resolved and its executor loaded.

**Make `list_skills` pack/tier-aware.** Change `entitlements.list_skills()` (`entitlements.py:93`) to accept an optional `project_id`; when present, intersect the global glob with the project's edition-pack `installs.skills` (from `packages/specpack/packs/*.yaml`) so the "governance shell" stops lying about availability. `GET /v1/skills` (`main.py:6415`) gains a `project_id` query param and returns per-project state: `{system, enabled, on_demand, source}` per skill.

**Enforce role `allowed_skills` against real ids.** The resolver calls the existing `role_governance` matching logic (`role_governance.py:137-153`) as a **filter** on the hot path (not just a standalone endpoint / prompt checklist). Add a validator that reconciles every role's `allowed_skills` ids against the real SkillSpec id set at load time and surfaces mismatches (today they silently don't match).

**On-demand toggle + lazy load.** `POST /v1/skills/{id}/enable` and `/disable` (per project), plus a `load` toggle. Enable = row in `project_skills`. On first agent reference to an on-demand skill, the bridge in §3.1 lazily appends its schema and resolves its executor for the remainder of the run.

### 3.3 Marketplace built on MCP + local packaging (Ask 4, marketplace half)

Rather than resurrect the hosted-gated `/v1/marketplace/*` pack machinery for skills, **build the extensibility marketplace on the boundary that already executes: MCP** — and give skills the same publish/provenance treatment.

- **Elevate MCP to a first-class marketplace citizen.**
  - **Publisher metadata.** Add `publisher jsonb` (name, homepage, contact) and `provenance jsonb` (source, version, checksum, signature) columns to `mcp_servers`; thread through `register_mcp_server` (`mcp_registry.py:37`).
  - **Route MCP calls through the gauntlet.** Today MCP calls at `agent_loop.py:668-671` skip cost/retry/permission. Wrap each MCP tool as a synthetic SkillSpec (`kind: mcp`, risk from per-tool metadata) so that MCP invocation goes through `skill_runtime.invoke` — inheriting budget reserve, retry policy, receipts, and the risk-tier approval pause. MCP becomes *one flavor of skill executor* whose callable is `mcp.call_tool`.
  - **Per-tool risk tier.** `tools/list` responses carry only a name/description/schema today; add an optional risk annotation and store a per-tool override table (fallback to server `risk_tier`) so a high-risk tool on an otherwise-low-risk server can be gated individually.
- **Local packaging + import/export (the OSS-safe half — Account-Connect-free).**
  - Define a portable **capability bundle** format: a signed tarball / JSON manifest containing SkillSpec YAML(s) and/or MCP server descriptors + publisher/provenance. New `services/skill_packaging.py`.
  - `POST /v1/skills/import` and `GET /v1/skills/{id}/export` (and the MCP equivalents) operate **entirely locally** — no `BOSS_ACCOUNT_CONNECT_URL`. This is the self-host extension path: drop a bundle in, register it into `project_skills`/`mcp_servers`, no repo fork. Mirrors the aspirational `import_export` capability listed in `community_edition.yaml:24`.
  - Hosted discovery/distribution (browse a central registry, one-click install, ratings) stays behind the existing hosted `/v1/marketplace/*` + Account Connect seam — **paid**. OSS gets local import/export of the same bundle format.
- **Trust / provenance (signing).** Bundles carry a detached signature over the manifest checksum. On import, verify signature against a configured trust set (`BOSS_SKILL_TRUST_KEYS`); unsigned/untrusted bundles are import-allowed only with an explicit operator override and are marked `provenance.trusted=false`, which the resolver can be configured to exclude. Emit `skill.imported` / `mcp.imported` events with provenance for the audit ledger.

### 3.4 What ships where (OSS vs paid)

- **OSS engine:** executor registry, skill/MCP execution through the gauntlet, `project_skills` store, the resolver + system/customer/on-demand loading model, per-project pack/tier filtering, role enforcement, **local** bundle import/export, signature verification. These are how a self-hoster extends the engine.
- **Paid control-plane:** hosted marketplace catalog, cross-tenant distribution/sync (`commercial.py` + Account Connect), curated/verified publisher program, ratings/discovery UI.

---

## 4. Implementation Tickets

### Epic A — Extensibility marketplace (skills executable + MCP as marketplace citizen)

**A1. Skill executor registry + built-in executors** — *P0, M*
Scope: new `services/skill_executors.py` with `register_executor`/decorator/`resolve_executor`; adapt existing adapters (github, deployment, provider ops) to their SkillSpec ids as thin wrappers. Key files: `services/skill_executors.py` (new), `services/github_adapter.py`, `packages/specpack/skills/*.yaml`.
- [x] `resolve_executor(skill_id)` returns a callable for every registered non-mock SkillSpec with a safe real backing, `None` otherwise.
- [x] GitHub PR and Deployment Registry skills have working executors that reuse existing adapter/service code (no reimplementation).
- [x] Unit tests cover registry lookup, context fail-closed behavior, GitHub and deployment happy paths, Receipt creation, and concurrent-effect dedupe.

**Implementation evidence (BOSS-45).** `skill_executors.py` provides direct/decorator registration, deterministic lookup, explicit server context, Receipt-backed GitHub PR effects, and Resource Plane deployment bindings. Unsafe/unbacked skills resolve to `None`; an existing in-progress Receipt prevents a duplicate GitHub provider call. Real-mode worker wiring remains BOSS-46/A2.

**A2. Wire real-mode `skill_runtime` (API + worker)** — *P0, M*
Scope: `skill_runtime.invoke()` looks up `resolve_executor` when `mock=false`; worker imports `SkillRuntimeService` and drives real invocations; lift the `main.py:6432` `mock=false` rejection to allow real invocation *from the worker path only* (API stays mock unless caller is trusted worker). Key files: `services/skill_runtime.py:49-137`, `apps/worker/boss_worker/runner.py`, `main.py:6428`.
- [x] Worker can execute a real skill end-to-end: permission gate + budget reserve + retry + receipt + `skill.invoked` event all fire.
- [x] `skill_invocations` row shows `mode='real'`, real cost reconciled.
- [x] API `/v1/skills/{id}/invoke` with `mock=false` no longer 400s when invoked on the worker path; still refuses unbacked skills with a clear error.

**Implementation evidence (BOSS-46).** `skill.execute` is an explicit worker capability. The control-plane endpoint validates the active lease and exact project/skill/caller/input scope before constructing server-only executor context. `SkillRuntimeService` resolves registered executors, applies permission/budget/retry gates, records real-mode invocations, and consumes receipt-backed reservations into cost receipts; unbacked skills fail closed before reserving budget. A replay of the same locked job returns its original invocation without a second reservation or executor call.

**A3. Bridge SkillSpecs into the agent loop** — *P0, M*
Scope: inject resolved skills as `skill_<id>` tools into the live agent schema list and dispatch them through `skill_runtime.invoke(mock=False)` with risk-tier→approval mapping. Key files: `services/agent_loop.py:610` (schema build), `:657-689` (dispatch), reuse `:673-685` approval pattern.
- [x] Agent can call a system skill; the call goes through the full gauntlet (not the raw `_dispatch`).
- [x] A `risk_tier > agent_autonomous_risk_max` skill triggers `create_blocking_decision` and pauses the run exactly like `_WRITE_FNS`.
- [x] MCP, native tools, and skills coexist in one schema list without name collisions.

**Implementation evidence (BOSS-47).** The durable agent loop consumes BOSS-54's eager resolver output, converts SkillSpec input declarations to collision-safe `skill_*` function schemas, and dispatches through `SkillRuntimeService.invoke(mock=false)` with a server-only executor context. Tier-above-threshold skills create a blocking decision and persist enough typed pending state to revalidate project/task availability before approved resume. Native, MCP, and SkillSpec names share one deterministic occupied-name set; MCP discovery failures now emit visible audit events.

**A4. Elevate MCP: publisher/provenance + per-tool risk + route through gauntlet** — *P1, M*
Scope: add `publisher`/`provenance` to `mcp_servers`; per-tool risk override; wrap MCP calls as `kind: mcp` skills so they inherit cost/retry/receipts. Key files: `services/mcp_registry.py:11-85`, `services/agent_loop.py:666-672`, `services/mcp_client.py`.
- [ ] MCP tool invocations record a `skill_invocations`-equivalent receipt with cost + attempts.
- [ ] A high-risk tool on a low-risk server is gated individually.
- [ ] `list_mcp_servers` returns publisher/provenance; bootstrap built-ins carry a `publisher='boss'` provenance.

**A5. Local capability bundle: packaging + import/export** — *P1, M*
Scope: `services/skill_packaging.py` defining the bundle manifest; `POST /v1/skills/import`, `GET /v1/skills/{id}/export`, and MCP equivalents — all Account-Connect-free. Key files: `services/skill_packaging.py` (new), `main.py` (new routes), `services/skill_registry.py` (A6).
- [ ] A skill exported from one project imports cleanly into another self-hosted instance with no hosted dependency.
- [ ] Import registers into `project_skills` / `mcp_servers` and emits `skill.imported` / `mcp.imported`.
- [ ] Round-trip export→import is byte-stable for the SkillSpec payload.

**A6. Trust & provenance (signing)** — *P1, S/M*
Scope: detached signature over manifest checksum; verify on import against `BOSS_SKILL_TRUST_KEYS`; mark untrusted bundles and allow resolver exclusion. Key files: `services/skill_packaging.py`, `config.py`, `services/skill_registry.py`.
- [ ] Signed bundle from a trusted key imports as `provenance.trusted=true`.
- [ ] Unsigned/untrusted bundle requires explicit operator override and imports as `trusted=false`.
- [ ] Resolver can be configured to exclude untrusted skills.

### Epic B — System-vs-customer skill loading model

**B1. `project_skills` store + registry service** — *P0, M*
Scope: new table + `services/skill_registry.py` (`list_project_skills`, `enable_skill`, `disable_skill`, `register_skill`, `ensure_skill`) mirroring `mcp_registry.py`. Key files: `services/skill_registry.py` (new), migration, `services/mcp_registry.py` (pattern to mirror).
- [x] CRUD for per-project skills with `source`, `enabled`, `on_demand`, `config`, `publisher`.
- [x] `ensure_skill` is idempotent across restarts (like `ensure_mcp_server`).
- [x] Events emitted on register/enable/disable.

**Implementation evidence (BOSS-51).** `project_skills` is project-scoped with a unique `(project_id, skill_id)` key and source constraint. `skill_registry.py` provides list/register/enable/disable/ensure operations, preserves strict project scope, and emits `skill.registered`, `skill.enabled`, and `skill.disabled` events.

**B2. `system` + `load` fields on SkillSpec; seed system skills at bootstrap** — *P0, S*
Scope: add optional `system: bool` and `load: eager|on_demand` to SkillSpec + validator; bootstrap seeds `system` skills into `project_skills` for every project. Key files: `entitlements.py:120-171` (validator), `packages/specpack/skills/*.yaml`, `bootstrap.py:19-56`.
- [x] `system: true` skills are auto-present on a fresh project without any tenant action.
- [x] Validator accepts the new fields and rejects invalid `load` values.
- [x] At least one skill marked `system` and one marked `on_demand` for test coverage.

**Implementation evidence (BOSS-52).** SkillSpec loading now defaults `system=false` and `load=eager`, rejects invalid metadata, seeds `deployment.health.check` as an eager system skill for every project, and marks `github.create_pr_sandbox` for on-demand loading.

**B3. Pack/tier-aware `list_skills`** — *P0, M*
Scope: `entitlements.list_skills(project_id=None)`; when set, intersect global glob with the project's edition-pack `installs.skills`; `GET /v1/skills?project_id=` returns per-project state. Key files: `entitlements.py:90-118`, `packages/specpack/packs/*.yaml`, `main.py:6415`.
- [x] A Community-edition project does not see enterprise-only skills.
- [x] `GET /v1/skills?project_id=X` returns `{source, system, enabled, on_demand}` per skill.
- [x] Global `GET /v1/skills` (no project) still returns the full catalog (back-compat).

**Implementation evidence (BOSS-53).** `EntitlementBrokerService.list_skills` accepts optional project scope, recursively resolves the effective edition pack's inherited skill bundles, and overlays project registry state. The typed API/OpenAPI contract exposes source/system/enabled/on-demand state while the unscoped route retains every validated SkillSpec.

**B4. Per-task skill resolver** — *P0, M*
Scope: `skill_registry.resolve_for_task(project_id, task)` returning system ∪ module `default_skill_ids` ∪ enabled tenant skills, filtered by pack/tier and role allow/forbid; feed the result into the agent schema build (A3). Key files: `services/skill_registry.py`, `services/agent_runtime.py:77`, `services/agent_loop.py:610`, `services/role_governance.py:137-153`.
- [x] Resolver returns the correct union+filter for a task with a matched agent-module.
- [x] On-demand skills are excluded from the eager set but discoverable.
- [x] The agent loop consumes the resolver output (no more hardcoded-only tool set for skills).

**Implementation evidence (BOSS-54).** `skill_registry.resolve_for_task` unions pack-eligible system skills, canonicalized module defaults, and enabled tenant rows; suppresses disabled rows; enforces RoleSpec allow/forbid patterns; and separates eager from on-demand discovery. Legacy compact references such as `health.check` are deterministically reconciled to canonical dotted SkillSpec ids so existing modules work without widening their role scope.

**B5. On-demand enable/disable + lazy load** — *P1, S/M*
Scope: `POST /v1/skills/{id}/enable|disable` (per project) and `load` toggle; lazy-attach an on-demand skill's schema + executor on first agent reference within a run. Key files: `main.py` (routes), `services/skill_registry.py`, `services/agent_loop.py` (lazy attach).
- [ ] Enabling a skill makes it appear for subsequent tasks without restart.
- [ ] An on-demand skill referenced mid-run is lazily resolved and usable for the rest of that run.
- [ ] Disable removes it from resolver output.

**B6. Enforce role `allowed_skills` on the hot path + id reconciliation** — *P1, S/M*
Scope: call `role_governance` matching as a resolver **filter** (not just the standalone endpoint / prompt checklist at `agent_runtime.py:247-250`); add a load-time validator reconciling role `allowed_skills` ids against real SkillSpec ids. Key files: `services/role_governance.py:137-153`, `services/skill_registry.py` (resolver), `services/agent_runtime.py:247-250`, `services/specpack.py:158`.
- [ ] A skill outside a role's `allowed_skills` is filtered out of that role's tool set (enforced, not advisory).
- [ ] `forbidden_skills` matches block invocation and record the scope violation.
- [ ] Startup validation reports any role `allowed_skills` id that has no matching SkillSpec.

---

## 5. Dependencies, Sequencing & Priority

**Critical path (P0, do first — makes skills real and gives the loading model):**
1. **A1** executor registry → **A2** wire real runtime (API+worker) → **A3** agent-loop bridge. This trio collapses the two parallel systems; nothing else is meaningful until skills execute.
2. In parallel: **B1** `project_skills` store → **B2** system/load fields + seeding → **B3** pack-aware `list_skills` → **B4** resolver. B4 depends on B1-B3 and feeds A3.
3. **A3 consumes B4's resolver output** — land A1/A2 first, then B1-B4, then complete A3's schema injection against the resolver.

**P1 (marketplace polish + finer loading control), after the P0 core:**
- **A4** (MCP as marketplace citizen) depends on A2's gauntlet being live.
- **A5** local packaging depends on B1 (store to import into).
- **A6** signing depends on A5.
- **B5** on-demand/lazy depends on B4; **B6** role enforcement depends on B4.

**Suggested order:** A1 → A2 → B1 → B2 → B3 → B4 → A3 → (A4, A5, B5, B6) → A6.

**Priority rollup:** Executor + real runtime + resolver + loading model = **P0** (the substance of asks 4-wiring and 5). Marketplace surface (publish/provenance/import-export/signing, on-demand UX) = **P1**.

---

## 6. Open Decisions

1. **Kill the mock-only skill API, or keep it?** Once A2 lands, is `POST /v1/skills/{id}/invoke` still mock-only from the API, with real only via worker/agent? Recommendation: yes — keep the API as a canary/mock surface (matches the canary use-case in `skill_runtime.py` docstring) and route real execution exclusively through worker/agent.
2. **One namespace or two?** Should MCP tools be *renamed* into the skill namespace (fully collapse to one system) or remain `mcp_*` while sharing the gauntlet under the hood (A4)? Renaming is cleaner conceptually but breaks any prompt/transcript expecting `mcp_<server>_<tool>`. Recommend: keep the `mcp_*` surface name, unify the *execution* only.
3. **Pack filter authority.** Is a project's edition pack the sole authority for skill availability, or can a tenant enable a skill outside its pack (upsell path)? Affects B3/B4 filter order. Ties into the open PRD-v2.0 open-core divergence (whole control plane AGPL vs engine-only) flagged in the digest — resolve alongside the OSS-split decision.
4. **Signing trust model.** Self-signed + operator trust list (`BOSS_SKILL_TRUST_KEYS`) vs a BOSS-rooted CA vs sigstore/cosign. Recommend starting with a simple detached-signature + configured public-key set (A6) and leaving a hosted verified-publisher program to the paid side.
5. **Where do executors live for OSS extraction?** `skill_executors.py` will import adapters (github, deployment) that may themselves be paid/hosted-coupled. Need to confirm the built-in executor set is engine-clean (imports only engine modules) so the executor registry ships in the OSS engine while hosted executors register from the control plane.
6. **On-demand discovery UX.** How does an agent learn an on-demand skill exists to request it? Options: a lightweight "available on-demand skills" catalog line in the system prompt, or an explicit `list_available_skills` meta-tool. Affects B5.
