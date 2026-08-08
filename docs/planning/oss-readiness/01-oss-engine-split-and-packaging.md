# OSS Engine Split & Packaging

> **Summary:** Extract the BOSS *engine* (code agents, sandboxing, coding pipelines, adapters, MCP, specpack) into an installable, permissively-licensed open-source package, while keeping the control-plane APIs and the Next.js console as paid enterprise. The engine is already cleanly layered and carries no secrets, proprietary deps, or phone-home, but three hard couplings compile budget/entitlement governance, direct DB access, and SQLAlchemy-bound `core.py` into the engine hot path. This spec introduces port protocols with no-op OSS defaults, splits the `main.py` monolith into community vs enterprise routers, makes the nominal `boss_edition` flag load-bearing, and adds the packaging/licensing that does not yet exist. It also flags a strategy fork that must be resolved before any license is committed.
>
> **Module:** OSS Release & Packaging · **Priority:** P0-oss-blocker

---

## 1. Context & Goal

BOSS today ships as a single deployable: one FastAPI monolith (`apps/api/app/main.py`, ~9.3k lines, ~230 top-level routes, no `APIRouter`), a Next.js console (`apps/web/app/*-ui.tsx`), Postgres, and a lease-based job queue. There is no packaging metadata anywhere — no `pyproject.toml`, no `setup.py`, no `LICENSE` — and the API runs as the flat module target `apps.api.app.main:app` off a single `requirements.txt`.

**Product intent (per the user / repo owner):** open-source the *engine* — the parts that build software (code agents, sandbox, coding pipelines, adapters, MCP, specpack) — to earn trust, adoption, and developer mindshare, while keeping the *control plane* (multi-tenant APIs, governance, identity, console, ProviderOps, clustering) as paid enterprise/cloud.

**Goal of this spec:** make the engine cleanly extractable and independently installable as `boss-engine` under a permissive OSI license, without dragging in the control plane, and without leaking budget/entitlement/DB coupling. Success is: a downstream developer can `pip install boss-engine`, run a code task through a local sandbox against their own git repo and their own LLM key, and get a diff/PR — with **zero** dependency on Postgres, the events ledger, BudgetGovernor, EntitlementBroker, Account Connect, or the FastAPI app.

This is a **P0 OSS blocker**: nothing about the OSS launch (license, README, first `pip install`) can be finalized until the engine boundary is real. It also depends on resolving a **strategy divergence** (§6) between the PRD's documented open-core plan (whole Community control plane under AGPLv3) and the engine-only-OSS stance driving this spec.

---

## 2. Current State (ground truth)

### 2.1 What's already clean (low-risk to open-source)

The engine layer is genuinely well-factored. Verified candidates for extraction:

| Module | Path | Notes |
|---|---|---|
| Code agent | `apps/api/app/services/code_agent.py` (~524 lines) | Decoupled — imports only `config`/`schemas`/`llm_telemetry`/`redaction`/`sandbox`. Produces `.py/.js/.ts/.go/.java/.rb/.rs/.json/.md` (`code_agent.py:354`). |
| Sandbox | `apps/api/app/services/sandbox.py`, `sandbox_provider.py` | `make_sandbox_provider` (`sandbox_provider.py:235`) → `LocalSubprocessProvider` (default, temp-dir, no isolation) or `MicroVMProvider` (dispatches to external sandbox-service). |
| Pipeline profiles | `apps/api/app/services/pipeline_profiles.py` | Pure data. |
| Governed pipeline | `apps/api/app/services/governed_code_task_service.py` (~1022 lines) | VAG loop, per-stage micro-VMs. |
| Code task service | `apps/api/app/services/code_task_service.py` (~514 lines) | Retry/override/lineage. |
| Software factory | `apps/api/app/services/software_factory.py` (~2342 lines) | Deploy actuation, gates. |
| Adapters | `apps/api/app/adapters/*` (github, chat/LLM, sandbox validation) | Outbound integrations. |
| MCP | `apps/api/app/services/mcp_client.py`, `mcp_registry.py` | The real working plug-in boundary. |
| Specpack | `packages/specpack/**` | YAML skills/packs/roles; already a `packages/` subtree. |

**Cleanliness verified:**
- **No secrets committed.** `.env` is gitignored; credentials default to `None`.
- **No proprietary dependencies** in the engine import graph.
- **No phone-home.** Telemetry is local Postgres only; no outbound analytics.
- **No hardcoded internal hosts** in engine code. `goldeneye` / `sean.network` appear only in docstrings, ops scripts, and `docs/` — **this must be scrubbed before publish** (see BLK-03 / PKG-03), but it is not compiled into engine runtime paths.
- `packages/` already exists (`packages/specpack`, `packages/api-client`) — a natural home for the extracted package.

### 2.2 What's coupled (the three hard blockers)

**Blocker 1 — budget/entitlements compiled into the engine hot path.**
`SoftwareFactoryService.__init__` eagerly constructs governance objects (`software_factory.py:41` `self.budget = BudgetGovernor(settings)`; `software_factory.py:45`-ish `self.entitlements = EntitlementBrokerService(settings)`), and `reserve/release/settle` are threaded through ~15 call sites. The pipeline services call into it mid-run and abort on failure:
- `governed_code_task_service.py:389` → `sf.budget.reserve(c, project_id, category="code_task", ...)` inside a `begin(engine)` transaction.
- `code_task_service.py:~250` → `sf.budget.reserve(...)` for gate-override PRs.
- `code_task_service.py:~351` → `sf.budget.reserve(...)` to reserve model spend for the task.

Because the constructor builds these unconditionally, you cannot instantiate the engine without pulling in the full commercial governance stack.

**Blocker 2 — direct DB coupling.**
The pipeline and factory services `from ..db import begin` and execute raw `text()` SQL against Postgres directly; there is no data-access layer. The governance reservations above all run inside `with begin(engine) as c:` blocks, so DB and budget coupling are entangled at the same sites.

**Blocker 3 — shared `core.py` imports SQLAlchemy.**
`apps/api/app/services/core.py` provides `emit_event()` (`core.py:41`) and `create_blocking_decision()` (`core.py:96`), both of which import `from sqlalchemy import text` / `Connection` (`core.py:9-10`) and write directly to Postgres (`events` table, ~343 `emit_event` call sites platform-wide). Any engine module that emits an event or raises a blocking decision transitively imports SQLAlchemy and assumes a live connection.

**Broader coupling (secondary, same shape):** `intake.py`, `skill_runtime.py`, `eval_harness.py`, `resource_plane.py`, `dashboard`, `metatron`, `canaries` all import budget/entitlements; `specpack.py:13` and `pipeline_profile_service.py:72,101` import `commercial.CommercialService`. These are not all in the engine, but they show the same pattern the ports must break.

### 2.3 Edition gating is nominal, not load-bearing

`config.py:225` defines `boss_edition: str = Field(default="community", alias="BOSS_EDITION")`. It is read in exactly one place — `commercial.py:395` — to pick an edition label. It gates **nothing**: no route is disabled, no service is swapped, no feature is fenced by edition. SAML/OIDC/enterprise/commercial routes (`saml.py`, `oidc.py`, `enterprise.py` SCIM/clusters `main.py:2688-3337`, `commercial.py` account-connect) are flag-gated by their own config but not unified under `boss_edition`. Account Connect is *already* a separate HTTP service (`apps/account_connect/`, reached via `BOSS_ACCOUNT_CONNECT_URL`) — a real network seam we can lean on.

### 2.4 Packaging state

- **No `pyproject.toml`, no `setup.py`, no `LICENSE`** anywhere in the repo (verified).
- JS side is a pnpm workspace (`package.json` version `0.6.0`).
- Python runs as `apps.api.app.main:app` with a flat `requirements.txt`.

---

## 3. Target Design & Approach

### 3.1 Package boundary: `packages/engine`

Create a new installable Python package `boss-engine` under `packages/engine/` (sibling to `packages/specpack`, `packages/api-client`). It contains the §2.1 modules, re-homed under a `boss_engine` import namespace. The FastAPI app depends on `boss-engine`; the engine depends on **nothing** in `apps/api/app` except shared schemas (which also move into or beside the package).

```
packages/
  engine/                      # NEW — boss-engine (OSS, permissive license)
    pyproject.toml
    LICENSE                    # Apache-2.0 or MIT (see §6)
    src/boss_engine/
      code_agent.py
      sandbox.py  sandbox_provider.py
      pipeline_profiles.py
      code_task_service.py  governed_code_task_service.py
      software_factory.py
      adapters/  mcp_client.py  mcp_registry.py
      ports/                   # NEW — protocols (§3.2)
        budget.py  entitlement.py  persistence.py  events.py
      defaults/                # NEW — no-op / in-memory OSS implementations
  specpack/                    # existing, re-license Apache-2.0/MIT
  api-client/                  # existing
apps/
  api/app/                     # enterprise control plane (proprietary/AGPL — see §6)
    adapters_enterprise/       # BudgetGovernor, EntitlementBroker, Postgres ports
    routers/                   # split main.py (§3.4)
```

### 3.2 The ports (dependency inversion)

Break each hard coupling with a `Protocol` the engine depends on, plus a **no-op / in-memory default** shipped in the OSS package. The enterprise app injects the real Postgres/governance-backed implementations.

**`BudgetPort`** (breaks Blocker 1):
```python
class BudgetPort(Protocol):
    def reserve(self, project_id: str, category: str, amount_usd_micros: int,
                reason: str = "", expires_in_minutes: int = 15) -> Reservation: ...
    def release(self, reservation_id: str) -> None: ...
    def settle(self, reservation_id: str, actual_usd_micros: int) -> None: ...
```
OSS default `NoopBudgetPort` returns an always-granted, unmetered reservation. `software_factory.py` receives a `BudgetPort` via constructor injection instead of building `BudgetGovernor` itself.

**`EntitlementPort`** (breaks Blocker 1):
```python
class EntitlementPort(Protocol):
    def check(self, project_id: str, capability: str) -> Entitlement: ...
```
OSS default `AllowAllEntitlementPort` grants every capability.

**`PersistencePort`** (breaks Blocker 2):
```python
class PersistencePort(Protocol):
    def get_run(self, run_id: str) -> dict | None: ...
    def save_run(self, run_id: str, state: dict) -> None: ...
    def transaction(self) -> ContextManager: ...
```
The OSS in-memory persistence seam replaces selected pipeline-local `from ..db import begin` + raw `text()` SQL. It is useful for isolated execution tests, but is not the community product datastore: running governed work requires the self-hosted Postgres workspace that durably records tasks, Receipts, approvals, and leases.

**`EventSink`** (breaks Blocker 3):
```python
class EventSink(Protocol):
    def emit(self, kind: str, payload: dict) -> None: ...
class DecisionSink(Protocol):
    def create_blocking(self, ...) -> str: ...
```
`emit_event` / `create_blocking_decision` become methods on injected sinks. OSS default `LoggingEventSink` writes to a Python logger / local JSONL; the enterprise adapter writes the Postgres `events` table exactly as `core.py:41,96` do today. This removes SQLAlchemy from the engine's transitive import graph.

**Design rule:** every port has (a) a `Protocol` in `boss_engine/ports/`, (b) a no-op/in-memory default in `boss_engine/defaults/`, (c) an enterprise implementation in `apps/api/app/adapters_enterprise/`. The engine constructors take ports as arguments and default to the OSS no-ops, so the package is usable stand-alone out of the box.

### 3.3 Edition gating made load-bearing

Promote `boss_edition` (`config.py:225`) from a label to a **switch** with a single composition-root resolver:

```python
def build_ports(settings) -> EnginePorts:
    if settings.boss_edition == "enterprise":
        return EnginePorts(budget=BudgetGovernor(settings),
                           entitlement=EntitlementBrokerService(settings),
                           persistence=PostgresPersistence(engine),
                           events=PostgresEventSink(engine))
    return EnginePorts.oss_defaults()          # isolated pipeline/test composition only
```
The FastAPI app calls `build_ports(settings)` once and injects the result into engine services. `boss_edition` now genuinely determines whether governance/Postgres are active. Enterprise routers (§3.4) are mounted only when edition == enterprise.

### 3.4 Router split: community vs enterprise

`main.py` is a ~9.3k-line monolith with no `APIRouter`. Introduce `APIRouter` modules and split by edition:

- **`routers/community.py`** — engine-facing endpoints that a self-host OSS user needs: code-tasks (`main.py:4213`), retry/override (`main.py:4310`), previews (`main.py:4310-4427`), MCP servers CRUD (`main.py:3878`), skills list/invoke (`main.py:6415-6427`), events read (`main.py:9052`).
- **`routers/enterprise.py`** — mounted only when `boss_edition == "enterprise"`: SAML/OIDC (`saml.py`, `oidc.py`), SCIM/clusters (`enterprise.py`, `main.py:2688-3337`), account-connect/commercial (`commercial.py`), marketplace (`main.py:6170`), ProviderOps/resource-plane, staged-rollout self-improvement (`main.py:5594-5772`).

The community router set is what could ship in an OSS distribution *if* the strategy decision (§6) opens more than the engine package. At minimum the split lets us gate enterprise routes on the now-load-bearing edition flag and gives a clean line for what the paid control plane owns.

### 3.5 Packaging & licensing

**`packages/engine/pyproject.toml`** — new. PEP 621 metadata for `boss-engine`: name, version (seed from `0.6.0` line or independent), Python `>=3.11`, runtime deps limited to what the engine actually needs (LLM SDK, git, MCP JSON-RPC over HTTP, pydantic) — explicitly **not** SQLAlchemy, **not** FastAPI, **not** the governance stack. Console-script entry point for a minimal CLI (`boss-engine run-code-task`) is a stretch goal.

**LICENSE files** — new. Add a `LICENSE` to `packages/engine/` and `packages/specpack/` and `packages/api-client/`. Per the PRD's own recommendation (`PRD-v2.0.md:513`), SDKs/schemas/pack formats/example packs → **Apache-2.0 or MIT**. The engine package should be permissive (Apache-2.0 recommended for patent grant) — **pending the §6 decision**. The `apps/api/app` control plane license is the contested question.

**Repo-root packaging** — decide whether the API stays `apps.api.app.main:app` off `requirements.txt` (enterprise, unpackaged) or also gets a `pyproject.toml`. Minimum viable: engine is packaged; the app depends on it via a path/workspace dependency.

**Internal-hostname scrub** — before any publish, remove `goldeneye` / `sean.network` / ops-host references from docstrings, scripts, and any `docs/` shipped with the package (BLK-03 / PKG-03).

---

## 4. Implementation Tickets

> **Implementation resolution (2026-07-26):** v0.8 resolved the strategy fork
> in favor of publishing the complete `boss_engine` service graph under
> Apache-2.0, while keeping HTTP edition composition explicit. Consequently,
> the seam acceptance below applies to the software-factory/code-task hot
> paths named by each ticket; SQL-backed control-plane services remain
> available in the published engine as optional governed capabilities. The
> FastAPI app owns the Postgres adapters and paid-route mounting.

### SEAM-01 — Introduce BudgetPort + EntitlementPort with no-op OSS defaults · size M
**Scope:** Define `BudgetPort` and `EntitlementPort` protocols in `boss_engine/ports/`, with `NoopBudgetPort` / `AllowAllEntitlementPort` defaults. Change `SoftwareFactoryService.__init__` to accept injected ports instead of constructing `BudgetGovernor`/`EntitlementBrokerService`.
**Key files:** `software_factory.py:41,45` (+~15 reserve/release/settle sites), new `boss_engine/ports/budget.py`, `boss_engine/ports/entitlement.py`, `boss_engine/defaults/`.
**Acceptance:**
- [x] `SoftwareFactoryService` can be instantiated with no governance backend and the standalone code-task path remains usable.
- [x] All software-factory reserve/release/settle sites call the injected `BudgetPort`.
- [x] `NoopBudgetPort` grants an unmetered reservation; existing enterprise behavior is unchanged when the real governor is injected.
- [x] No `BudgetGovernor` / `EntitlementBrokerService` import remains in the software-factory hot path.

### SEAM-02 — Route pipeline budget calls through the port · size M
**Scope:** Replace `sf.budget.reserve(...)` calls in the pipeline services with `BudgetPort` calls, decoupling them from `sf` internals.
**Key files:** `governed_code_task_service.py:389`, `code_task_service.py:~250,~351`.
**Acceptance:**
- [x] Governed and non-governed pipelines reserve/release via the injected port.
- [x] With `NoopBudgetPort`, pipelines never abort on budget and never touch Postgres for budget.
- [x] Enterprise path still aborts mid-run on real budget exhaustion (regression test).

### SEAM-03 — PersistencePort to replace direct `db.begin` + raw SQL · size L
**Scope:** Define `PersistencePort` with an in-memory test composition. Replace `from ..db import begin` and raw `text()` SQL in engine services with port calls. This is the largest seam because run-state read/write is threaded through the pipelines and entangled with the budget transactions. The production/community runtime continues to require self-hosted Postgres; a full SQLite runtime would be a separately governed product effort.
**Key files:** `governed_code_task_service.py`, `code_task_service.py`, `software_factory.py` (all `with begin(engine)` blocks).
**Acceptance:**
- [x] The named code-task/software-factory pipeline services contain no direct `from ..db import` and no `text()` calls.
- [x] `InMemoryPersistence` supports the retry/override/lineage flow (parent/root/attempt) without Postgres.
- [x] `PostgresPersistence` adapter reproduces current behavior; existing pipeline tests pass against it.

### BLK-01 — Decouple `core.py` (emit_event / create_blocking_decision) via EventSink/DecisionSink · size M
**Scope:** Convert `emit_event`/`create_blocking_decision` into `EventSink`/`DecisionSink` methods; ship `LoggingEventSink` OSS default and `PostgresEventSink` enterprise adapter. Remove SQLAlchemy from the engine's transitive imports.
**Key files:** `services/core.py:9-10,41,96`, engine call sites that emit events.
**Acceptance:**
- [x] Importing the engine core does not transitively import `sqlalchemy`.
- [x] Engine emits events to the injected sink; enterprise sink writes the `events` table identically to today.
- [x] Blocking decisions raised by the engine surface through `DecisionSink` with no DB assumption.

### BLK-02 — Make `boss_edition` load-bearing via a ports composition root · size M
**Scope:** Add `build_ports(settings)` resolver keyed on `boss_edition`; wire the FastAPI app to inject OSS defaults for `community` and real adapters for `enterprise`.
**Key files:** `config.py:225`, new composition root (e.g. `apps/api/app/bootstrap_ports.py`), `commercial.py:395`.
**Acceptance:**
- [x] `BOSS_EDITION=community` runs the engine with all no-op/in-memory ports and no governance.
- [x] `BOSS_EDITION=enterprise` injects Postgres + BudgetGovernor + EntitlementBroker.
- [x] Flipping the flag changes runtime behavior (test asserts both branches).

### BLK-03 — Split `main.py` into community/enterprise routers + edition gating · size L
**Scope:** Introduce `APIRouter`s; move engine-facing routes to `routers/community.py`, paid routes to `routers/enterprise.py` mounted only when edition == enterprise. Scrub internal hostnames from any engine-shipped docstrings/scripts.
**Key files:** `main.py` (route ranges in §3.4), new `routers/community.py`, `routers/enterprise.py`.
**Acceptance:**
- [x] Enterprise routes (SAML/OIDC/SCIM/clusters/marketplace/account-connect) return 404/disabled under `community`.
- [x] Community routes (code-tasks/previews/MCP/skills/events) work under `community`.
- [x] No internal deployment hostnames remain in engine-shipped files.

### PKG-01 — Create `packages/engine` package with `pyproject.toml` · size M
**Scope:** Re-home §2.1 modules under `packages/engine/src/boss_engine/`; author `pyproject.toml` (PEP 621) with a minimal, governance-free dependency set; make the API depend on it via workspace/path dep.
**Key files:** new `packages/engine/pyproject.toml`, `packages/engine/src/boss_engine/**`, root `requirements.txt` / API dependency wiring.
**Acceptance:**
- [x] `pip install -e engine` succeeds in a clean venv; FastAPI remains an optional MCP HTTP extra.
- [ ] A stand-alone script imports `boss_engine`, runs a local-subprocess code task against a sample repo + a user-supplied LLM key, and produces a diff — with zero Postgres.
  *(Truth-up 2026-07-28, Docker verification + Plane #308: NOT yet real. The OSS
  default ports (in-memory persistence, no-op budget) are now composed at the CLI
  boundary when `DATABASE_URL` is unset, but `SoftwareFactoryService._scope` and
  the read tools still issue raw SQL, so a code task without Postgres fails.
  Remaining work: route scope/run lookups through `PersistencePort`.)*
- [x] The FastAPI app imports engine modules from `boss_engine`; its legacy service package is an identity-preserving compatibility shim.

### PKG-02 — Add LICENSE files across `packages/*` · size S
**Scope:** Add `LICENSE` to `packages/engine`, `packages/specpack`, `packages/api-client` per the resolved license decision (§6); add SPDX headers where practical.
**Key files:** new `LICENSE` files; `pyproject.toml` `license` field.
**Acceptance:**
- [x] Each published package carries an OSI-compatible Apache-2.0 LICENSE consistent with the resolved strategy.
- [x] `pyproject.toml` declares the license; no license mismatch exists between metadata and file.
- [x] The §6 strategy decision was resolved for v0.8 before the license files shipped.

---

## 5. Dependencies, Sequencing & Priority

**Priority:** SEAM-01 → SEAM-02 → SEAM-03 → BLK-01 are the critical path; the package cannot be extracted (PKG-01) until the engine imports no governance/DB/SQLAlchemy. BLK-02 and BLK-03 make the split real at runtime. PKG-02 is gated on the §6 strategy decision.

```
SEAM-01 ─┬─► SEAM-02 ─┐
         └─► SEAM-03 ─┼─► BLK-01 ─► PKG-01 ─► PKG-02*  (*blocked on §6)
                      │
BLK-02 ◄──────────────┘
BLK-03 (parallel to SEAM work; needs BLK-02 for edition gating)
```

- **SEAM-01** first — it establishes the port pattern the others copy.
- **SEAM-02** depends on SEAM-01 (uses `BudgetPort`).
- **SEAM-03** can start in parallel with SEAM-02 but must land before PKG-01 (removes DB from engine).
- **BLK-01** after the seams (needs the sink pattern) and before PKG-01 (removes SQLAlchemy).
- **BLK-02** provides the composition root the seams inject through; wire it as SEAM-01 lands.
- **BLK-03** (router split) can proceed in parallel but needs BLK-02 to gate enterprise routes.
- **PKG-01** is the payoff — only correct once SEAM-01/02/03 + BLK-01 remove all three couplings.
- **PKG-02** is trivial mechanically but **must not merge before the §6 decision**.

**Recommended order:** SEAM-01, BLK-02 (together) → SEAM-02, SEAM-03 (parallel) → BLK-01 → BLK-03 → PKG-01 → [§6 decision] → PKG-02.

---

## 6. Open Decisions

1. **STRATEGY FORK — engine-only-OSS vs whole-control-plane-AGPL (needs a product + legal decision).**
   The PRD (`docs/product/PRD-v2.0.md:496-554`, esp. `:512` "BOSS Community server/control plane: **AGPLv3** candidate") documents an open-core plan that open-sources the **entire Community control plane** (local auth, single-org control plane, Mission Control, Decision Inbox, receipts, basic Budget Governor, basic Software Factory, basic Entitlement Broker, import/export) under AGPLv3, with SDKs/schemas/packs under Apache-2.0/MIT. **This diverges from the user's stated engine-only-OSS stance** (open the engine package, keep the control-plane APIs + console paid). The two imply different license choices, different repo boundaries, and different competitive exposure (AGPL copyleft on the control plane vs a permissive engine SDK with a proprietary app). PKG-02 and the whole boundary in §3 cannot be finalized until this is resolved. **Flag for product + legal.**

2. **Engine license: Apache-2.0 vs MIT.** Apache-2.0 gives an explicit patent grant (recommended for an engine others build on); MIT is simpler and matches some ecosystem norms. Tied to decision (1).

3. **Does the community router set ship as OSS, or only the engine package?** If (1) resolves to engine-only, `routers/community.py` stays proprietary and the OSS surface is just `packages/engine` + `packages/specpack` + `packages/api-client`. If (1) resolves to the PRD plan, the community control plane also opens under AGPL. This determines how aggressively BLK-03 must clean the community router.

4. **PersistencePort default: in-memory vs SQLite file.** Resolved for GA: self-hosted Postgres is the durable community runtime. In-memory is reserved for isolated tests and catalog-only setup; a SQLite runtime must not be implied until it can preserve BOSS's workflow, receipt, lease, and approval invariants.

5. **API packaging scope.** Package only the engine (API stays flat `apps.api.app.main:app` + `requirements.txt`), or also give the API a `pyproject.toml`? Minimum viable is engine-only; revisit if the API is ever distributed.

6. **Where do shared schemas live?** `code_agent.py` imports `schemas`. Decide whether `schemas` moves into `boss_engine` or becomes a separate permissively-licensed `packages/schemas` shared by both engine and app (cleaner, matches the PRD's "schemas → Apache-2.0/MIT" line `PRD-v2.0.md:513`).
