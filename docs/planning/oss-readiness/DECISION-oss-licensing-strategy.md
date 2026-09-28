# DECISION — How much of BOSS goes open source (fork #1)

> **Status: DECIDED — Apache License 2.0 for the reviewed full Community repository at `seanventures/boss`, recorded 2026-09-28; counsel reference `01049940`.**
> This is the single decision that determines the repo boundary, the license, the monetization
> model, and the competitive exposure of the whole open-source initiative. Everything in
> [01-oss-engine-split-and-packaging.md](01-oss-engine-split-and-packaging.md) hangs off it —
> in particular BLK-03 (how aggressively to split `main.py` into community vs enterprise routers)
> and PKG-01/02 (what gets a `pyproject.toml` + which LICENSE).

> Historical analysis below predates this decision. The product owner confirmed counsel approval on 2026-09-28. The approved scope is recorded in [OSS_LICENSING_DECISION.md](../../operations/OSS_LICENSING_DECISION.md). Cloud and Enterprise code remain outside the Community export. BOSS-300 and public publication remain open until implementation and the separate release gates are complete.

## 1. Why this is blocking

Two different plans are on record and they draw the OSS boundary in different places:

- **Your stated stance (this initiative):** open-source the **engine**; keep the **control-plane APIs
  and the console** as paid enterprise.
- **The existing PRD** (`docs/product/PRD-v2.0.md:496–554`): open-source the **entire Community
  control plane** — a self-hostable single-org BOSS with local auth, Mission Control, Decision Inbox,
  receipts, a basic Budget Governor, basic Software Factory, and import/export — under **AGPLv3**,
  with SDKs/schemas/packs under Apache-2.0/MIT, and monetize hosted Cloud + Enterprise.

You can't clean the module boundary or pick a license until these are reconciled, because they
cut the codebase in different places and imply different copyleft obligations.

## 2. The real question is a spectrum, not a binary

"Open-source the engine" and "open-source the control plane" are the two ends of a dial. The dial is:
**how much of a runnable product do we give away for free?**

```
  LESS OPEN  ───────────────────────────────────────────────►  MORE OPEN
  ┌───────────────┐   ┌────────────────────────┐   ┌───────────────────────────┐
  │ A. Engine only │   │ C. Engine + thin        │   │ B. Whole Community control │
  │   (a library)  │   │    runnable community   │   │    plane (PRD plan)        │
  │                │   │    control plane        │   │                            │
  └───────────────┘   └────────────────────────┘   └───────────────────────────┘
   not self-hostable    self-hostable, minimal        self-hostable, full single-org
   as a product         "it actually runs"            product
```

The tension underneath: **what is BOSS's real moat?**
- If the moat is the **engine** (the VAG verification harness — cheap models + strong verification =
  frontier quality), then open-sourcing it is giving away the crown jewels, and you'd want to keep it
  closed. But then there's nothing compelling to open-source, and the whole initiative loses its point.
- If the moat is the **operational-trust layer** (governance, budget control, multi-tenant, compliance,
  managed workers, managed ProviderOps, SLA) — the things companies actually pay for — then the engine
  is the *adoption driver* you give away, and you sell the operations. This is the classic, durable
  open-core logic (open the commodity, sell the operations), and it matches the PRD.

Resolving fork #1 is really answering: **"Is our defensible value the engine or the operations?"**
The recommendation below assumes the latter, which is also what the VAG thesis implies — the harness
*pattern* being widely adopted is a tailwind, not a threat, because the paid value is running it at
scale with trust.

## 3. The three options, concretely

### Option A — Engine-only OSS (your stated stance)

**Ships OSS:** `packages/engine` = `code_agent`, `sandbox`/`sandbox_provider`, `pipeline_profiles`,
`governed_code_task_service`, `code_task_service`, `software_factory`, model adapters,
`mcp_client`/`mcp_registry`, `bakeoff`/`eval_harness`; plus `packages/specpack` and
`packages/api-client`. **License:** Apache-2.0 (permissive).
**Stays proprietary:** the entire FastAPI control plane (`main.py` + orchestration + budget +
multi-tenant), the console, everything else.

| Axis | Assessment |
|---|---|
| Adoption / mindshare | **Weak.** An engine without a control plane is a *library*, not a self-hostable BOSS. Developers can `pip install boss-engine` and embed it, but they can't run "BOSS". |
| "Open source BOSS" narrative | **Dishonest-adjacent.** "We open-sourced our engine" ≠ "BOSS is open source." The PRD explicitly warns: *be honest about open source.* |
| Monetization protection | **Strongest.** Nobody can host a competing BOSS; the whole product stays closed. |
| Competitive moat exposure | **Highest engine exposure** (the VAG harness is fully public), **lowest product exposure.** |
| Legal complexity | **Lowest** — permissive library + proprietary app is well-trodden. |
| Engineering cost | **Lowest** — only the port decoupling (SEAM-01..03), no need to make a control plane standalone. |

### Option B — Whole Community control plane, AGPLv3 (the PRD plan)

**Ships OSS:** a self-hostable single-org BOSS — local auth, local control plane, local runner, core
objects, basic Metatron/Mission Control/Decision Inbox/receipts, basic Budget Governor, basic Software
Factory, basic Model Entitlement Broker, read-only ProviderOps inventory, role/skill/pack formats,
import/export. **License:** AGPLv3 for the server; Apache-2.0/MIT for SDKs/schemas/packs.
**Stays paid:** Account Connect (hosted identity/billing), Cloud SaaS, Enterprise (SSO/SCIM/RBAC,
clustering/HA/K8s, managed workers, managed gateways, write-capable ProviderOps, marketplace, support).

| Axis | Assessment |
|---|---|
| Adoption / mindshare | **Strongest.** A real, runnable, self-hostable product. Matches GitLab/Sentry/n8n/Supabase-style success. |
| "Open source BOSS" narrative | **Honest and strong.** BOSS genuinely is open source (Community edition). |
| Monetization protection | **Good, via AGPL copyleft** — a SaaS competitor hosting a fork must open their modifications, which most won't. Cloud/Enterprise convenience + trust features are the paid wedge. |
| Competitive moat exposure | Control plane is public, but the *operational trust* layer (the paid part) isn't. Engine is public too. |
| Legal complexity | **Higher.** AGPL scares some enterprises (their legal blocks copyleft); needs a CLA for contributions; "basic vs enterprise" feature line must be defended continuously. |
| Engineering cost | **Highest** — the community control plane must be genuinely standalone (no Account Connect dependency, clean edition gating), which is most of BLK-02/BLK-03. |

### Option C — Engine permissive + thin runnable community control plane (recommended middle)

**Ships OSS:** `packages/engine` (Apache-2.0) **plus** a *minimal but runnable* community control plane
— enough to self-host a single-org BOSS that actually does coding tasks/meetings/discovery with local
auth and SQLite/Postgres — under **AGPLv3** (or evaluate FSL/BSL "fair-source" if AGPL is too
permissive for your comfort; note fair-source is **not** OSI open source during the restriction window,
so it can't be marketed as "open source"). **Stays paid:** the console, Account Connect, Cloud,
Enterprise, and the advanced control-plane features (clustering, managed workers, write-capable
ProviderOps, marketplace hosting, compliance).

This is the PRD plan **narrowed**: give away a runnable product (so adoption is real and the narrative
is honest), but keep the community control plane *thin* and keep the console + all operational-trust
features paid. It reconciles your "keep the console paid" instinct with the PRD's "ship a real product."

| Axis | Assessment |
|---|---|
| Adoption / mindshare | **Strong** — it runs; developers can self-host and evaluate. |
| Narrative | **Honest** if AGPL; **caveated** if FSL (must say "source-available"). |
| Monetization protection | **Strong** — console + operations + cloud are paid; copyleft deters hosted forks. |
| Engineering cost | **Medium** — thin control plane is less work than the full PRD Community edition. |

## 4. Decision framework — answer these and the option falls out

1. **Moat:** Is our durable, defensible value the **engine** or the **operations/trust layer**?
   (If operations → open the engine freely; if engine → don't run this initiative at all.)
2. **Runnable product:** Do we need self-hosters to run a *real BOSS*, or is a *library* enough for our
   goals? (Real product → B or C; library → A.)
3. **Console:** Is the console a paid differentiator we must keep closed? (Yes → A or C, not full B.)
4. **Copyleft tolerance:** Are we OK shipping AGPL (max adoption, some enterprise friction), or do we
   want a non-compete/fair-source license (protects hosting, but not "open source")?
5. **Honesty bar:** Are we willing to call it "open source" only if it's OSI-licensed? (The PRD commits
   to this; it rules out marketing a fair-source build as "open source.")
6. **Contributor model:** Will we accept external contributions (needs a CLA + governance), or is this
   source-available with no external PRs? (Ties to the GitHub community agent, spec 08.)

## 5. Recommendation

**Option C**, unless a specific reason pushes to A or B:

- Open **`packages/engine` under Apache-2.0** (explicit patent grant; right for a library others build
  on and embed — this is the mindshare play for the VAG paradigm).
- Ship a **thin, runnable community control plane under AGPLv3** so "BOSS Community" is an honest,
  self-hostable product — but keep it minimal.
- Keep the **console, Account Connect, Cloud, and all operational-trust/enterprise features paid.**
- Evaluate **FSL/BSL only if** the board wants hard protection against hosted competitors and is willing
  to accept the "source-available, not open source" framing (and its community cost).

Rationale: it captures the adoption/mindshare upside of a runnable OSS product and an honest narrative,
keeps the console and the operations paid (your instinct), and uses AGPL copyleft — not secrecy — as the
anti-competitive-hosting mechanism. It also matches proven open-core companies.

## 6. Cascading sub-decisions (unblocked once #1 lands)

From [spec 01 §6](01-oss-engine-split-and-packaging.md):

| # | Sub-decision | Default recommendation |
|---|---|---|
| 2 | Engine license: Apache-2.0 vs MIT | **Apache-2.0** (patent grant) |
| 3 | Does a community router ship OSS, or engine-only? | Falls out of A vs C/B |
| 4 | PersistencePort default: in-memory vs SQLite file | **SQLite file** (credible self-host) |
| 5 | Package only the engine, or the API too? | **Engine only** to start |
| 6 | Where do shared `schemas` live? | Separate **`packages/schemas`** (Apache/MIT), shared by engine + app |

## 7. Next steps

1. **You + Sean** answer the six framework questions in §4 (a 30-minute call). Legal review of the
   chosen license before anything is published (the PRD mandates this).
2. Record the decision at the top of this file (Status → DECIDED, with the chosen option + date).
3. That unblocks **#25 (OSS engine split)** — specifically BLK-03's router split and PKG-01/02's
   package + LICENSE choices — and lets the rest of the OSS-blocker work proceed.

No code should be cut for the split until §7.1 is done — otherwise BLK-03 gets built against the wrong
boundary and has to be redone.
