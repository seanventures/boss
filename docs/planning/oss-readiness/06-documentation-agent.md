# 06 — Feature Documentation & Continuous Docs Agent

> **Summary:** A docs-completion agent that keeps a public feature wiki current: when a feature completes (PR opened / merged, discovery promoted, plan dispatched), an allowlisted `docs.update` job reads the code-task diff + spec artifacts and drafts/updates a Markdown feature doc, which the existing code_agent → `github_adapter.upsert_file` → `create_pull_request` path lands as a **human-gated docs PR**. Feature docs are published to a static site (mkdocs). · **User ask 1** (continuous docs kept in sync with feature completion) · **Module:** Documentation & Knowledge · **Priority:** P1-oss-launch

---

## 1. Context & Goal

Documentation is a first-order gate on open-source adoption. For an OSS launch, the public surface — "what does BOSS do, what features exist, how do they work" — is what a prospective contributor or evaluator reads *before* the code. Today BOSS's documentation is entirely hand-written internal design docs; there is no published site, no per-feature reference, and nothing that keeps docs in sync as the engine ships features into itself. Because BOSS's whole thesis is an autonomous software factory that opens PRs against repos, the highest-leverage move is to point that same machinery at **its own documentation**: when a feature completes, an agent drafts the doc and opens a docs PR, exactly like any other change.

**Goal.** Stand up (a) a living **feature catalog** derived from completion events + `TRACEABILITY.md`, (b) a `docs.update` job kind triggered on allowlisted completion events (via the spec-00 event dispatcher), (c) a **docs agent** that drafts/updates a Markdown feature doc from the run diff + spec artifacts and opens a **docs PR** through the existing GitHub write path, and (d) a **public docs site** (mkdocs) generated from those feature docs. The human-gated model is preserved end to end: docs land via reviewed PR, never a silent write.

**Non-goals.** Auto-generating API reference from code annotations (the manual `api/openapi.yaml` stays authoritative for now); replacing the internal PRD/spec docs; auto-merging docs PRs; translating docs.

---

## 2. Current State (ground truth)

**Docs are all hand-written; no generation, no site, no agent.**
- ~30 hand-maintained Markdown docs live under `docs/` — PRDs (`docs/product/PRD-v2.0.md`, `PRD-v1.1.md`, `PRD-v1.0.md`), technical specs (`docs/product/TECHNICAL_SPEC-v2.0.md`, `TECHNICAL_SPEC-v1.1.md`), status/traceability (`docs/product/IMPLEMENTATION_STATUS.md`, `docs/product/TRACEABILITY.md`), and `docs/user-stories.md` (74 KB).
- `TRACEABILITY.md` is already a **structured feature ledger**: a Markdown table keyed by requirement label (e.g. `MET-001`, `DEC-002`, `BUD-003`), with columns `Requirement label | Source | Implementation files | Test coverage | Status` and a status vocabulary (`Implemented` / `Partial` / `Planned` / `Blocked`). This is the natural seed for a feature catalog. Last hand-updated "June 3, 2026" — it drifts because nothing keeps it fresh.
- `IMPLEMENTATION_STATUS.md` is a hand-written prose summary of "what is implemented now" — also drifts.
- `api/openapi.yaml` (4,439 lines, `openapi: 3.1.0`, version `0.2.1`) is **maintained by hand**, not generated from the FastAPI app.
- **No docs generator** (no `mkdocs.yml`, no Docusaurus, no TypeDoc/Sphinx), **no wiki**, **no published docs site**, **no feature-completion trigger**, and **no docs agent** anywhere in the tree.

**Completion signals already emit — but nothing consumes them.**
- `emit_event(connection, project_id, event_type, actor, entity_type, entity_id, data)` at `apps/api/app/services/core.py:41` appends to the immutable `events` table. It is a **poll-only ledger**: the only read surface is `GET /v1/events` (`apps/api/app/main.py:9052`). There is no pub/sub, no subscriber, no webhook-out. **This is the load-bearing missing primitive — provided by spec 00 (event-dispatch foundation).**
- Real completion events already fire (verified event-type strings):
  - `code_task.pr_opened` — `apps/api/app/services/code_task_service.py:495` and `apps/api/app/services/governed_code_task_service.py:1003`; payload `{pull_request_url, decision_id}`.
  - `software_factory.pull_request_merged` — emitted from the software-factory merge path.
  - `discovery.completed` — `apps/api/app/services/discovery_service.py:184` (the prompt's "discovery.promoted"; the actual emitted string is `discovery.completed`).
  - `plan.dispatched` — `apps/api/app/services/intake.py:154`.
  - Also present: `code_task.completed_no_pr`, `meeting.completed`, `task.completed`, `self_improvement.rollout_completed`.

**The write path the docs agent will reuse already exists.**
- `github_adapter.py`: `upsert_file(branch_name, path, content, message)` (`:148`), `create_pull_request(branch_name, title, body)` (`:183`, opens a **draft** PR and is idempotent per head branch), `merge_pull_request` (`:223`), `create_deployment` (`:240`). Outbound-only; no inbound webhooks.
- `code_agent.py`: `CodeAgent` protocol (`:98`) with `run` (`:286`) and `run_role` (`:233`, `:402`); already emits `.md` files (allowed extension set includes `".md"`, `code_agent.py:354`). A docs draft is just Markdown — well within its existing output surface.
- **Worker job dispatch** is a flat `if job.kind == …` chain in `apps/worker/boss_worker/runner.py` (`dispatch` around `:541`–`:709`, terminating in `raise ValueError(f"Unsupported job kind: {job.kind}")` at `:709`). Existing kinds include `code.task` (`:568`), `software_factory.propose_pr` (`:578`), `github.pull_request.merge` (`:598`), `comms.outbox.deliver` (`:668`). A new `docs.update` kind slots into this same chain.
- **Job enqueue/lease** is `apps/api/app/services/jobs.py` (`enqueue` `:108`, capability-filtered `lease` `:206`). Jobs carry a JSON `payload` and a `kind`.

**Net:** every ingredient exists except (1) an event → job trigger (spec 00), (2) a feature catalog, (3) a `docs.update` handler + docs agent, and (4) a published site.

---

## 3. Target Design & Approach

### 3.1 Flow

```
completion event (code_task.pr_opened / .pull_request_merged / discovery.completed / plan.dispatched)
        │  emit_event → events table (core.py:41)
        ▼
spec-00 event dispatcher  ──(allowlist match)──►  enqueue job  kind="docs.update"
        │                                          payload = { event_id, event_type, entity_type,
        │                                                       entity_id, project_id, source_ref }
        ▼
worker leases docs.update  (runner.py dispatch chain)
        │
        ├─ 1. resolve feature  → FeatureCatalog.resolve(entity) → catalog entry (label, doc path, status)
        ├─ 2. gather context   → code-task run diff/PR files + spec artifacts (PRD/discovery dossier) + prior doc
        ├─ 3. draft            → docs_agent.draft(feature, context) → Markdown (code_agent.run_role, docs role)
        ├─ 4. write            → github_adapter.upsert_file(branch="boss/docs/<feature>", path, content, msg)
        └─ 5. PR               → github_adapter.create_pull_request(branch, title, body) → DRAFT docs PR
        ▼
human reviews & merges docs PR  ──►  CI builds mkdocs site  ──►  published public docs
```

### 3.2 Feature catalog (living registry)

A `FeatureCatalog` service (`apps/api/app/services/feature_catalog.py`) is the index of "what BOSS can do, and where its doc lives." It is **derived, not hand-authored**:

- **Seed** from `docs/product/TRACEABILITY.md`: parse the requirement table into catalog rows keyed by label (`MET-001`, `DEC-002`, …), carrying `source`, `implementation_files`, `test_coverage`, `status`, and a derived `doc_path` (`docs/features/<slug>.md`).
- **Augment** from completion events: when a `docs.update` fires, the triggering `entity_type`/`entity_id` (e.g. a `code_task_run`, a `discovery_run`, a `plan`) is mapped to a catalog entry. If the run's task/spec references a requirement label, attach to that entry; otherwise open a **new** catalog entry ("emergent feature") flagged `needs_label` for a human to reconcile back into `TRACEABILITY.md`.
- **Persist** as a `feature_catalog` table (project-scoped): `id, project_id, label (nullable), slug, title, doc_path, status, source_refs (jsonb), last_documented_event_id, last_documented_at, updated_at`. This makes the agent idempotent (skip if `last_documented_event_id` already covers the trigger) and lets the console show doc coverage.
- **Reconciliation:** because `TRACEABILITY.md` remains the human system-of-record for requirement status, the docs PR that touches a labelled feature **also** proposes the `TRACEABILITY.md` status/`last updated` edit in the same PR — closing the drift loop rather than forking a second ledger.

### 3.3 `docs.update` job kind

- **Trigger (allowlist).** Spec 00's dispatcher owns the event→job mapping. This spec contributes an allowlist config (e.g. `docs.trigger_events`) naming the completion events that produce a `docs.update` job: `code_task.pr_opened`, `software_factory.pull_request_merged`, `discovery.completed`, and (behind a flag, coarser) `plan.dispatched`. Non-allowlisted events are ignored. Debounce/coalesce: multiple events for the same `entity` within a window enqueue at most one `docs.update` (dedupe on `entity_id`), so a chatty pipeline doesn't spawn redundant docs PRs.
- **Handler.** Add `if job.kind == "docs.update":` to the `runner.py` dispatch chain (before the `raise ValueError` at `:709`), delegating to a control-plane endpoint (mirroring how `code.task` calls `client.run_code_task`). The handler:
  1. Loads the catalog entry via `FeatureCatalog.resolve(entity_type, entity_id)`.
  2. Idempotency check against `last_documented_event_id`.
  3. Gathers context (§3.4).
  4. Calls the docs agent (§3.5).
  5. Opens the docs PR (§3.6).
  6. On success, emits `docs.pr_opened` and updates the catalog row. On failure, standard lease retry/deadletter (`jobs.py` `:206`+) applies.
- **Budget/governance.** The docs draft is one bounded LLM call; reserve a small budget line (reuse the existing reserve/settle pattern) so a docs storm can't blow the cap.

### 3.4 Context assembly

For a code-task-triggered doc, the agent needs the *change*, not the whole repo:
- **Diff / PR files** from the triggering `code_task_run` (the run already retains its proposed `files`; `software_factory.propose_pr` payload carries `files`, `runner.py:578`). Use these as the primary "what changed" signal.
- **Spec artifacts** attached to the task: the originating command/PRD/discovery dossier text (discovery runs carry a cited dossier). These supply the *why*.
- **Prior doc** (`doc_path` current contents via `github_adapter` read / repo checkout) so the agent updates in place rather than regenerating from scratch — preserving human edits.
- **Catalog metadata**: label, `implementation_files`, `test_coverage` from `TRACEABILITY.md`.

### 3.5 Docs agent

A thin `docs_agent.py` (`apps/api/app/services/docs_agent.py`) that reuses `code_agent.run_role` with a **docs role** (a new `RoleSpec` under `packages/specpack/roles/`, e.g. `docs_writer`). The role prompt constrains output to a fixed feature-doc template:

```
# <Feature title>   (<label if any>)
> status: <Implemented|Partial|Planned> · last verified: <date> · source: <PRD/discovery ref>
## What it does           (1–3 sentence user-facing summary)
## How it works           (mechanism, key files, entry points)
## API / interface        (relevant endpoints, job kinds, events — links to openapi.yaml)
## Configuration          (env vars / flags)
## Status & limitations    (from TRACEABILITY + known gaps)
```

Output is Markdown only (already an allowed extension, `code_agent.py:354`). The agent must **diff against the prior doc** and preserve human-authored prose it wasn't asked to change (append/patch sections rather than clobber). No new LLM plumbing is needed — this rides the existing `code_agent` + role machinery.

### 3.6 Docs PR (human-gated, reuses GitHub write path)

- Branch: `boss/docs/<feature-slug>` (stable per feature, so repeated updates reuse one branch/PR — `create_pull_request` is already idempotent per head, `github_adapter.py:183`).
- `upsert_file(branch, doc_path, content, message)` for the feature doc (+ optional `TRACEABILITY.md` / `mkdocs.yml` nav edits).
- `create_pull_request(branch, title, body)` opens a **draft** PR; body links the triggering event, run, and spec artifact for reviewer context.
- **No auto-merge.** Docs land only when a human merges — identical to the code-task human-gated model. This is the guardrail against the docs agent hallucinating into the public site.

### 3.7 Public docs site (OSS surface)

- **Pick mkdocs (Material for MkDocs).** Rationale: Python-native (matches the repo), single `mkdocs.yml`, Markdown-in / static-HTML-out, trivial CI + GitHub Pages publish, and it renders the exact `docs/features/*.md` the agent produces with zero transform. (Docusaurus is heavier/JS-toolchain; TypeDoc/Sphinx are API-reference tools, not a feature wiki.)
- `mkdocs.yml` at repo root; nav auto-includes `docs/features/` (either explicit nav edited by the docs PR, or `awesome-pages`/`literate-nav` for zero-touch nav). Landing page = generated overview from the feature catalog ("BOSS features" index with status badges).
- CI: a `.github/workflows/docs.yml` builds `mkdocs build --strict` on PRs (catches broken links/nav) and publishes on merge to the default branch (GitHub Pages or the existing static host). Strict build failing **blocks the docs PR**, so the site never breaks.
- **OSS angle:** this is the public front door. Every merged feature PR yields (via its trailing docs PR) an updated public feature page — so an OSS evaluator always sees current capabilities, and contributors get a per-feature "how it works" without reading 9k-line `main.py`. The site is generated from files in-repo, so it stays open-source-native (no proprietary docs SaaS, consistent with the engine-OSS split).

---

## 4. Implementation Tickets

> **Cross-cutting dependency:** every ticket that consumes a completion event depends on **spec 00 (event-dispatch foundation)** for the event→job trigger. Until spec 00 lands, tickets 2–3 can be developed against a manual `POST /v1/docs/update` enqueue and wired to the dispatcher last.

### DOC-1 — Feature catalog service + table
**Scope.** Parse `TRACEABILITY.md` into a `feature_catalog` table; add `FeatureCatalog` service with `seed_from_traceability()`, `resolve(entity_type, entity_id)`, `upsert_entry()`, and idempotency via `last_documented_event_id`.
**Key files.** `db/schema.sql` (new `feature_catalog` table), `apps/api/app/services/feature_catalog.py` (new), `apps/api/app/services/core.py` (reuse), `docs/product/TRACEABILITY.md` (parsed input).
**Acceptance criteria.**
- [ ] `seed_from_traceability()` produces one catalog row per requirement label with `source`, `implementation_files`, `test_coverage`, `status`, derived `doc_path`.
- [ ] `resolve()` maps a `code_task_run` / `discovery_run` / `plan` entity to a catalog entry, creating a `needs_label` entry when no label matches.
- [ ] Idempotency: re-resolving the same `last_documented_event_id` is a no-op.
- [ ] Unit tests in `apps/api/tests/` cover seed, resolve-existing, and resolve-emergent.
**Size.** M.

### DOC-2 — `docs.update` job kind + trigger allowlist
**Scope.** Register `docs.update` as a worker job kind; add the completion-event allowlist config and (via spec 00's dispatcher) enqueue `docs.update` on allowlisted events with entity-scoped dedupe/debounce. Add a manual `POST /v1/docs/update` for testing before the dispatcher lands.
**Key files.** `apps/worker/boss_worker/runner.py` (new `if job.kind == "docs.update"` branch before `:709`), `apps/api/app/services/jobs.py` (enqueue, `:108`), `apps/api/app/main.py` (manual endpoint + client method), config (`docs.trigger_events` allowlist), spec-00 dispatcher wiring.
**Acceptance criteria.**
- [x] Allowlisted event (`code_task.pr_opened`, `software_factory.pull_request_merged`, `discovery.completed`) enqueues exactly one `docs.update` job carrying `{event_id, event_type, entity_type, entity_id, project_id}`.
- [x] Non-allowlisted events enqueue nothing.
- [x] Two events for the same `entity_id` within the debounce window enqueue at most one job.
- [x] Handler failure follows existing lease retry/deadletter; success emits `docs.pr_opened`.
- [x] **Depends on spec 00** for the automatic trigger; manual endpoint works standalone.
**Size.** M.

### DOC-3 — Docs agent + context assembly + docs role
**Scope.** Add `docs_agent.py` that assembles context (run diff/PR files + spec artifact + prior doc + catalog metadata) and calls `code_agent.run_role` with a new `docs_writer` role to emit a templated feature doc; preserve human-edited sections on update.
**Key files.** `apps/api/app/services/docs_agent.py` (new), `packages/specpack/roles/docs_writer.yaml` (new `RoleSpec`), `apps/api/app/services/code_agent.py` (reuse `run_role` `:233`/`:402`, `.md` already allowed `:354`), `apps/api/app/services/discovery_service.py` (dossier read).
**Acceptance criteria.**
- [x] Given a code-task run + prior doc, produces valid Markdown matching the feature-doc template.
- [x] On update, sections not targeted by the change are preserved (diff-and-patch, not clobber).
- [x] Emits Markdown only; no non-`.md` artifacts.
- [x] `docs_writer` RoleSpec validates against the specpack loader.
- [x] Unit test with a fixture run + fixture prior doc asserts template sections and preservation.
**Size.** L.

### DOC-4 — Docs PR path + TRACEABILITY reconciliation
**Scope.** Wire the docs agent output through `github_adapter.upsert_file` + `create_pull_request` on a stable per-feature branch; in the same PR, propose the `TRACEABILITY.md` status/`last updated` edit for labelled features. No auto-merge.
**Key files.** `apps/api/app/services/docs_agent.py` (PR step), `apps/api/app/services/github_adapter.py` (reuse `upsert_file:148`, `create_pull_request:183`), `docs/product/TRACEABILITY.md` (patched in-PR), `apps/api/app/services/feature_catalog.py` (update `last_documented_*`).
**Acceptance criteria.**
- [x] Opens a **draft** docs PR on branch `boss/docs/<slug>`; repeated updates reuse the same branch/PR (idempotent).
- [x] PR body links the triggering event, run, and spec artifact.
- [x] Labelled feature → PR includes a `TRACEABILITY.md` status + `Last updated` edit.
- [x] Catalog `last_documented_event_id`/`last_documented_at` set on PR open.
- [x] No merge is performed by BOSS (human-gated).
**Size.** M.

### DOC-5 — mkdocs site + CI publish
**Scope.** Add `mkdocs.yml` (Material theme) rendering `docs/features/*.md` + a generated feature index; add `.github/workflows/docs.yml` to `mkdocs build --strict` on PRs and publish on merge.
**Key files.** `mkdocs.yml` (new, repo root), `docs/features/index.md` (generated overview), `.github/workflows/docs.yml` (new), `requirements-docs.txt` (mkdocs-material pin).
**Acceptance criteria.**
- [x] `mkdocs build --strict` passes locally and in CI; generated-catalog drift and broken links/nav fail the build (and thus the docs PR).
- [x] Feature pages render with status badges sourced from the catalog/TRACEABILITY status.
- [x] Merge to default branch publishes the site through the reviewed-main-only GitHub Pages workflow.
- [x] Landing page lists BOSS features with status — usable as the OSS front door.
**Size.** M.

---

## 5. Dependencies, Sequencing & Priority

**Hard dependency.** **Spec 00 (event-dispatch foundation)** provides the event→job trigger that turns `emit_event` (`core.py:41`) from a poll-only ledger into a subscriber that can enqueue `docs.update`. DOC-2's automatic trigger cannot land without it; everything else can be built and tested against the manual `POST /v1/docs/update` endpoint in the interim.

**Soft dependencies / reuse (already present, no new work):** `code_agent.run_role` + `.md` output (`code_agent.py:233/354/402`); `github_adapter` write path (`:148/:183`); worker dispatch chain + jobs enqueue/lease (`runner.py`, `jobs.py`); `TRACEABILITY.md` as catalog seed.

**Sequencing.**
1. **DOC-1** (catalog) — foundational, no spec-00 dependency; unblocks everything.
2. **DOC-3** (docs agent) — buildable in parallel with DOC-1 against fixtures.
3. **DOC-4** (docs PR path) — needs DOC-1 + DOC-3.
4. **DOC-2** (job kind + trigger) — manual endpoint early; **automatic trigger gated on spec 00**.
5. **DOC-5** (site + CI) — needs the first real `docs/features/*.md` from DOC-4; can be scaffolded anytime.

**Priority: P1-oss-launch.** Public docs are a launch gate for OSS credibility; the feature wiki is the first thing evaluators and contributors read. Ships after spec 00's foundation but before the OSS launch milestone.

---

## 6. Open Decisions

1. **mkdocs vs. Docusaurus.** Recommended: **mkdocs (Material)** for Python-nativeness and zero-transform Markdown. Confirm no requirement for React-based interactive docs that would favor Docusaurus.
2. **Trigger granularity.** Document on `code_task.pr_opened` (feature proposed, not yet merged) or only on `software_factory.pull_request_merged` (feature actually shipped)? Merged is more truthful but lags; pr_opened is timelier but may document unmerged/abandoned work. Recommend **merged as default**, pr_opened behind a flag.
3. **Emergent (unlabelled) features.** When a completion event maps to no `TRACEABILITY.md` label, do we auto-create a `needs_label` catalog entry + doc, or hold for human labelling first? Recommend auto-create as **draft** so nothing is lost, flagged for reconciliation.
4. **Human-edit preservation.** How aggressively should the agent patch vs. regenerate? Options: section-scoped patch (safer, more complex) vs. regenerate-with-review (simpler, risks clobbering prose). Recommend section-scoped patch with the PR diff as the safety net.
5. **openapi.yaml relationship.** Keep `api/openapi.yaml` manual and merely *link* it from feature docs (this spec's assumption), or fold API-reference generation into the docs agent later? Out of scope here; note as a follow-on.
6. **Site hosting.** GitHub Pages vs. the existing static host (Cloudflare Pages is already in the resource plane). Pick per OSS-launch infra decisions.
7. **Docs PR reviewer / auto-merge policy.** Docs are lower-risk than code — do we ever allow auto-merge for docs-only PRs that pass `mkdocs build --strict`, or keep strictly human-gated? Recommend human-gated for launch, revisit post-launch.
