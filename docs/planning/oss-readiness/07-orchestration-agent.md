# Orchestration Agent — Conversational Front Door to All of BOSS

> **Summary.** A conversational AI agent a user talks to in natural language that is aware of *every* BOSS capability (coding, meetings/PRDs, discovery, multi-phase commands) and turns one utterance into the right initiator call — including **fanning a single ask out into many dependency-sequenced tasks**. The heavy lifting already exists: the intent→many-tasks **decomposer is live** (`intake.CommandIntakeService.propose_command` → `dispatch_plan`). This spec is mostly **wiring**: a chat endpoint, a session/thread store, and a **surface-agnostic intent router** (an LLM that classifies an utterance and calls the *existing* endpoints as tools), plus a **capability manifest** so the agent stays current. The fan-out is **confirmed before dispatch**, preserving the human gate.
>
> · user ask 2 · module: **Orchestration Agent** · priority: **P1-oss-launch**

---

## 1. Context & Goal

Today BOSS is driven through ~230 discrete HTTP routes on the FastAPI monolith (`apps/api/app/main.py`). To start any unit of work you must already know *which* primitive you want and hit its endpoint directly: `POST /v1/code-tasks` for code, `POST /v1/meetings` / `POST /v1/meetings/prd` for deliberation, `POST /v1/discoveries` for research, `POST /v1/commands/propose` for a multi-phase program. There is no single natural-language surface that (a) understands "here's what I want" and picks the right initiator, and (b) explodes a broad ask into a coordinated set of tasks.

**Goal.** Add one conversational endpoint — `POST /v1/agent/chat` — backed by a persisted thread, that:

1. Accepts a free-form utterance in a project + actor context.
2. Runs an **intent router** (LLM with tools) that classifies the ask and calls the **existing** initiator endpoints — code-task vs meeting vs PRD vs discovery vs multi-phase command.
3. Is **capability-aware**: it is fed a live **capability manifest** (the BOSS feature catalog) so it never offers or invents a capability that isn't wired, and can explain what BOSS can do.
4. For broad asks, routes through the existing **`propose_command` → `dispatch_plan`** decomposer, shows the user the proposed fan-out (N child tasks, dependency order), and **only dispatches after explicit confirmation** — the human gate is never bypassed.

**Explicit non-goal.** We are **not** building an intent decomposer. It exists and works (§2). We are not replacing `agent_loop.py` (that is the *per-task* code-planning loop). This agent sits *above* the initiators and orchestrates *which* primitives fire.

---

## 2. Current State (ground truth)

### 2.1 The intent→many-tasks decomposer ALREADY EXISTS

This is the load-bearing fact for this spec. A natural-language command is already turned into an LLM-generated multi-phase plan and fanned out into one dependency-sequenced child task per phase:

- `CommandIntakeService.propose_command` — `apps/api/app/services/intake.py:220`. Takes a free-form `command` string + optional `constraints`, creates a parent "CEO command" task with `required_artifacts=["plan_draft"]`, and drives the LLM to produce structured `phases`.
- `CommandIntakeService.dispatch_plan` — `apps/api/app/services/intake.py:116`. "Turn a plan's structured phases into executable tasks: **one child task per phase**, parented to the plan's task and **sequenced** (each phase depends on the previous unless the phase declares its own `depends_on`)." This *is* the fan-out.
- HTTP surface, both live and permissioned:
  - `POST /v1/commands/propose` — `apps/api/app/main.py:4924` (perm `command_submit`, high-cost rate-limited).
  - `POST /v1/plans/{plan_id}/dispatch` — `apps/api/app/main.py:5194` (perm `task_create`), calling `intake.dispatch_plan`.
- Also present: `factory_scale.chunk_spec:121` (a second decomposition path for large specs).

**Implication:** propose returns a *plan* (draft phases); dispatch is a *separate* call. That two-step shape is exactly the confirm-before-fanout human gate we want — the agent just needs to surface the proposed plan conversationally and gate the dispatch on a user "yes."

### 2.2 All initiators are live endpoints (the router's tools)

| Intent | Endpoint | Line | Service |
|---|---|---|---|
| Code task | `POST /v1/code-tasks` | `main.py:4213` | `code_task_service.py` |
| Meeting (multi-persona deliberation) | `POST /v1/meetings` | `main.py:5222` | `meeting_service.py` |
| PRD authoring | `POST /v1/meetings/prd` | `main.py:3924` | `meeting_service.py` |
| Discovery (research) | `POST /v1/discoveries` | `main.py:4471` | `discovery_service.py` |
| Multi-phase command (fan-out) | `POST /v1/commands/propose` + `POST /v1/plans/{id}/dispatch` | `main.py:4924`, `5194` | `intake.py` |

Each already enforces its own permission (`require_project_actor(... "task_create" / "command_submit" / ...)`), so the router calling them inherits authorization for free — it does not need its own bespoke authz on the actuation, only on the chat surface itself.

### 2.3 There is an inbound-message path, but no free-form intent parser

- `POST /v1/comms/inbox/ingest` — `apps/api/app/main.py:7871` (→ `comms.ingest_inbox_message`).
- Its parser is **regex-only**: `comms._parse_inbound_message` — `apps/api/app/services/comms.py:1254`. It matches `\bdecision\b|\bdecision_id\b|\bdec_` and otherwise returns `{"kind": "message"}`. It classifies **decision replies vs plain messages** — it has **no** free-form intent→task capability. This is the gap the orchestration agent fills, and it is why we build a *new* surface rather than extend the regex parser.

### 2.4 A reusable LLM-tool-calling primitive already exists

We do **not** need to build LLM plumbing:

- `make_chat_adapter(settings)` — `apps/api/app/services/claude_cli_adapter.py:317` — returns a chat adapter exposing `create_chat_completion(messages, tools=...)` (`claude_cli_adapter.py:237`) with OpenAI-style function-tool support and usage accounting (`_usage:141`). `agent_loop.py:16` already imports it.
- The **tool-registry pattern** to copy verbatim: `agent_loop.py` defines `TOOLS` as a list of `{"fn": ..., "schema": {"type":"function","function":{...}}}` dicts, derives `_TOOL_FNS`/`_TOOL_SCHEMAS` (`agent_loop.py:206-207`), and routes calls through a `_dispatch(name, args)` with a SAVEPOINT per tool (`agent_loop.py:210+`). The intent router is the *same shape* with a different, smaller tool set (one tool per initiator).

### 2.5 What is missing (the deltas this spec delivers)

- **No conversation endpoint.** Grep for `/v1/agent/chat` = none.
- **No session/thread store.** No `agent_threads` / `agent_messages` tables. Events (`events` table, `emit_event` core.py:39) are a poll-only ledger, not a conversation.
- **No capability manifest.** No machine catalog of "what can BOSS do" for an LLM to reason over. (This is shared with spec 06 — see §5.)
- **No surface-agnostic router.** `agent_loop.py`'s loop is welded to *code planning* (repo-read tools, plan artifacts). Nothing routes "which primitive" from an utterance.

---

## 3. Target Design & Approach

### 3.1 Component overview

```
                          POST /v1/agent/chat  (main.py, new)
                                   │
                   ┌───────────────┴────────────────┐
                   │        agent_service.py         │   ← new service
                   │  (thread load/append + turn)    │
                   └───────────────┬────────────────┘
                                   │
        ┌──────────────────────────┼───────────────────────────┐
        │                          │                            │
  agent_threads /          intent_router.py            capability_manifest.py
  agent_messages           (surface-agnostic)          (feature catalog feed)
  (new tables)             LLM + tools over                shared w/ spec 06
                           EXISTING endpoints
                                   │
        ┌──────────┬──────────┬────┴─────┬───────────────────────┐
        ▼          ▼          ▼          ▼                        ▼
  create_code  create_    create_   run_discovery         propose_command
   _task       meeting     _prd                          (+ confirm → dispatch_plan)
   4213         5222       3924        4471                4924 / 5194
```

The **intent router** is the reusable core (shared with the GitHub community agent, spec 08 — see §5). It knows *nothing* about HTTP or chat; it takes `(messages, manifest, tool_registry)` and returns either an assistant reply, a tool call to execute, or a **proposed plan awaiting confirmation**. The *surface* (chat here, GitHub issue/PR comments in spec 08) only provides transport + the message history.

### 3.2 Chat endpoint + session/thread store

**Endpoint:** `POST /v1/agent/chat`

Request:
```json
{
  "project_id": "proj_...",
  "actor": "role_or_user_id",
  "thread_id": "athr_...",          // optional; omitted → new thread
  "message": "Ship a login page and research auth providers first",
  "confirm_plan_id": "plan_..."     // optional; present → user is approving a prior fan-out proposal
}
```

Response:
```json
{
  "thread_id": "athr_...",
  "reply": "Assistant natural-language reply.",
  "actions": [                       // structured record of what the agent did/proposes
    {"kind": "code_task_created", "task_id": "task_..."},
    {"kind": "plan_proposed", "plan_id": "plan_...", "phase_count": 3,
     "phases": [...], "requires_confirmation": true}
  ],
  "pending_confirmation": {"plan_id": "plan_...", "summary": "3 tasks, sequenced A→B→C"}
}
```

Permission: gate the chat surface on a single new capability `agent_chat` via `require_project_actor(connection, project_id, actor, "agent_chat")`. Actuation tools each re-check their own perms (§2.2), so a user who can chat but lacks `command_submit` gets a clean "you're not allowed to dispatch that" from the underlying endpoint rather than a silent failure.

**Session store — two new tables** (Postgres, same `begin(engine)`/`text()` conventions as the rest of the codebase):

- `agent_threads(id, project_id, actor, surface, external_ref, status, created_at, updated_at)`
  - `surface` ∈ `{chat, github}` — this is what makes the store reusable across spec 08. `external_ref` holds e.g. a GitHub issue URL for the community agent.
- `agent_messages(id, thread_id, role, content, tool_calls jsonb, actions jsonb, created_at)`
  - `role` ∈ `{user, assistant, tool}`. `tool_calls`/`actions` persist the structured router output for audit + replay.

A turn = load thread messages → call router → persist the user message, any tool/assistant messages, and the structured actions → return. Emit `emit_event("agent.turn_completed", ...)` (`core.py:39`) so turns land in the existing ledger for observability.

### 3.3 Surface-agnostic intent router (tools over existing endpoints)

`intent_router.py`, modeled directly on the `agent_loop.py` tool-registry pattern (§2.4). Each tool is a thin adapter over an existing initiator — **the router does not re-implement any initiator logic**, it calls the same service methods the HTTP handlers call (or the HTTP handlers internally, in-process).

Tool set (one per intent from §2.2):

| Tool name | Wraps | Behavior |
|---|---|---|
| `create_code_task` | `POST /v1/code-tasks` (`main.py:4213`) | Single well-scoped code change. Returns `task_id`. |
| `create_meeting` | `POST /v1/meetings` (`main.py:5222`) | Multi-persona deliberation on an open question. |
| `create_prd` | `POST /v1/meetings/prd` (`main.py:3924`) | Author a PRD for a fuzzy/large feature ask. |
| `run_discovery` | `POST /v1/discoveries` (`main.py:4471`) | Research task (market/tech/options). |
| `propose_command` | `POST /v1/commands/propose` (`main.py:4924`) | **Broad/multi-step** ask → generate a phased plan. **Does NOT dispatch.** Returns the draft plan for confirmation. |
| `dispatch_plan` | `POST /v1/plans/{id}/dispatch` (`main.py:5194`) | **Only callable after user confirmation** (see §3.5). Fans out the plan. |

The router LLM is given, in its system prompt: (a) the **capability manifest** (§3.4), (b) tool schemas, (c) the routing policy — when to prefer a single code-task vs a meeting vs discovery vs a multi-phase command. Heuristic encoded in the prompt: *narrow, unambiguous change → `create_code_task`; open design question → `create_meeting`; unspecified feature → `create_prd`; "should we / research / compare" → `run_discovery`; anything that reads as a program of ≥2 dependent steps → `propose_command`.*

Router interface (surface-agnostic — no FastAPI/HTTP types):
```python
def route_turn(
    connection, settings,
    project_id: str, actor: str,
    messages: list[dict],            # prior thread history + new user msg
    manifest: CapabilityManifest,
    confirm_plan_id: str | None = None,
) -> RouterTurn:                     # {reply, actions, pending_confirmation}
```
Spec 08's GitHub agent calls the *same* `route_turn` with `messages` reconstructed from an issue/PR thread and a different `surface`. Keep `route_turn` free of any chat-specific assumptions.

### 3.4 Capability manifest feed

`capability_manifest.py` produces a structured catalog the router injects into its system prompt so it (a) stays aware of every BOSS feature and (b) can answer "what can you do?" truthfully.

- **Source of truth = shared with spec 06.** Spec 06 (event foundation / feature catalog) defines the canonical feature registry. This spec **consumes** it; if spec 06's registry is not yet built, ship a minimal static manifest here (`CAPABILITIES: list[Capability]` with `id, title, summary, initiator_tool, enabled_flag`) and migrate to spec 06's registry when it lands. Do **not** fork a second permanent catalog.
- Each entry carries an **`enabled`** predicate resolved against config/entitlements at request time (e.g. discovery gated by a flag, marketplace gated by `BOSS_ACCOUNT_CONNECT_URL`). The router must never route to a disabled capability — it explains the gap instead. This keeps the agent honest about the many partially-wired features noted in the digest (skills-not-executed, marketplace-blocked, etc.).
- Rendered into the prompt as a compact table (id · what it does · which tool fires it · enabled?).

### 3.5 Confirm-before-fanout (respects the human gate)

The fan-out is a two-turn interaction and the gate is **explicit**:

1. User asks something broad. Router calls `propose_command` → gets a plan (`plan_id`, phases). Router **returns** `pending_confirmation={plan_id, summary}` and a reply that enumerates the proposed N tasks and their dependency order. **It does not call `dispatch_plan`.** No child tasks exist yet.
2. `agent_service` persists the pending plan on the thread.
3. Next user turn arrives with `confirm_plan_id` set (or the router recognizes an affirmative reply referencing the pending plan). Only then does the router call `dispatch_plan` (`main.py:5194`), which fans out and sequences the child tasks. A negative/edit reply loops back to `propose_command` with the amended constraints.

This maps cleanly onto the already-two-step propose/dispatch API (§2.1) — we are surfacing an existing gate conversationally, not inventing one. Single-initiator intents (code-task, meeting, discovery) fire immediately since each is one governed unit that already has its own downstream gates; only the *multi-task fan-out* requires the extra confirmation, because that is where one utterance becomes many committed units of work.

---

## 4. Implementation Tickets

### T1 — Session/thread store + `POST /v1/agent/chat` skeleton
**Scope.** Create `agent_threads` + `agent_messages` tables (migration in the project's migration path). Add `agent_service.py` with `start_or_load_thread`, `append_message`, `record_turn`. Add the `POST /v1/agent/chat` route in `main.py` gated on new `agent_chat` permission; for T1 it echoes and persists (router stubbed). Emit `agent.turn_completed`.
**Key files.** `apps/api/app/main.py` (new route near other `/v1/*` posts), new `apps/api/app/services/agent_service.py`, migration file, `apps/api/app/schemas.py` (request/response models), permission seed alongside existing perms.
**Acceptance.**
- [x] `POST /v1/agent/chat` with no `thread_id` creates a thread and returns its `thread_id`.
- [x] Second call with the `thread_id` appends and returns the full-turn structure.
- [x] User + assistant messages persisted in `agent_messages`; `agent.turn_completed` in `events`.
- [x] Missing `agent_chat` perm → 403 via `require_project_actor`.
**Size.** M.

### T2 — Surface-agnostic intent router core
**Scope.** New `apps/api/app/services/intent_router.py`: `route_turn(...)` per §3.3, tool registry modeled on `agent_loop.py:{TOOLS,_TOOL_FNS,_dispatch}`, using `make_chat_adapter` (`claude_cli_adapter.py:317`) for tool-calling. Tools for the single-initiator intents only (code-task, meeting, prd, discovery) call the existing services in-process. No chat/HTTP coupling in this module.
**Key files.** new `intent_router.py`; call sites into `code_task_service.py`, `meeting_service.py`, `discovery_service.py`; wire into `agent_service.py`.
**Acceptance.**
- [x] "Fix the typo in the footer" routes to `create_code_task` and returns a real `task_id`.
- [x] "Should we use Postgres or Mongo?" routes to `create_meeting` (or `run_discovery`) — not a code task.
- [x] Each tool re-enforces its own `require_project_actor` perm; a caller lacking `task_create` gets a clean refusal surfaced in the reply.
- [x] `route_turn` has zero imports of FastAPI/`Request` (verified surface-agnostic for spec 08 reuse).
**Size.** L.

### T3 — Capability manifest feed
**Scope.** `capability_manifest.py` exposing `build_manifest(connection, settings, project_id) -> CapabilityManifest`, each capability carrying an `enabled` predicate resolved against config/entitlements. Consume spec 06's feature registry if available; else a static minimal catalog. Inject rendered manifest into the router system prompt.
**Key files.** new `apps/api/app/services/capability_manifest.py`; consumed by `intent_router.py`; cross-ref spec 06 registry.
**Acceptance.**
- [x] Router prompt includes the manifest table; "what can you do?" yields a truthful capability list.
- [x] A capability whose `enabled` predicate is false is never routed to; agent explains the gap.
- [x] Manifest built once per turn (not per tool call).
**Size.** M.

### T4 — Multi-task fan-out with confirm-before-dispatch
**Scope.** Add `propose_command` + `dispatch_plan` tools to the router. `propose_command` returns `pending_confirmation` and never dispatches. `agent_service` persists the pending plan on the thread; a subsequent turn with `confirm_plan_id` (or affirmative reply) triggers `dispatch_plan`. Reuse `intake.propose_command` (`intake.py:220`) and `intake.dispatch_plan` (`intake.py:116`) unchanged.
**Key files.** `intent_router.py`, `agent_service.py`, `main.py` (`confirm_plan_id` in request), `schemas.py`.
**Acceptance.**
- [x] Broad ask returns a proposed plan with phase count + dependency order and `requires_confirmation: true`; **no child tasks created yet** (verified via task count).
- [x] Confirmation turn dispatches; child tasks appear parented + sequenced per `dispatch_plan`.
- [x] A "no, change X" reply re-proposes rather than dispatching.
- [x] Non-affirmative / ambiguous replies never auto-dispatch.
**Size.** L.

### T5 — Console chat surface (thin)
**Scope.** A conversational panel in the Next.js console (`apps/web/app/*-ui.tsx`) posting to `/v1/agent/chat`, rendering `reply`, the `actions` list (with links to created tasks/meetings/discoveries), and an explicit **Confirm plan** button when `pending_confirmation` is present (sends `confirm_plan_id`).
**Key files.** new `apps/web/app/agent-ui.tsx` (+ nav wiring), api-client addition in `packages/api-client`.
**Acceptance.**
- [x] User can hold a multi-turn conversation from the console.
- [x] Created tasks/meetings/discoveries are shown as clickable links.
- [x] Fan-out plan renders phases; nothing dispatches until Confirm is clicked.
**Size.** M.

---

## 5. Dependencies, Sequencing & Priority

**Shared building blocks:**

- **Intent router shared with spec 08 (GitHub community agent).** T2 must land the router as a **surface-agnostic** module (`route_turn(messages, manifest, ...)`, `surface` column on `agent_threads`). Spec 08 reuses the identical core, swapping only the transport (GitHub issue/PR comments via the currently outbound-only `github_adapter.py`, which spec 08 extends with an inbound webhook + comment methods). Do not let chat concerns leak into the router. If specs 07 and 08 are built in parallel, T2 is the joint dependency and should be owned once.
- **Capability manifest shared with spec 06 (event foundation / feature catalog).** T3 consumes spec 06's canonical feature registry. If spec 06 has not landed, ship the minimal static manifest in T3 and swap the source when spec 06 is ready — never maintain two catalogs.

**Internal sequencing:** T1 → T2 → (T3 ∥ T4) → T5. T1 unblocks everything (store + endpoint). T2 is the core and the spec-08 dependency; prioritize it. T3 and T4 are independent once T2 exists. T5 is presentation, last.

**External dependencies (all already live — this is why the spec is mostly wiring):** the five initiator endpoints (§2.2), the propose/dispatch decomposer (§2.1), `make_chat_adapter` (§2.4), `require_project_actor`, `emit_event`, `begin(engine)`.

**Priority:** **P1-oss-launch.** This is the primary human-facing front door for the OSS engine — it makes the existing capability surface usable without knowing 230 routes. It has no hard blocker (decomposer + initiators exist), so it is fast to land and high-leverage.

---

## 6. Open Decisions

1. **In-process calls vs internal HTTP.** Router tools should call the initiator *services* directly (`code_task_service`, `meeting_service`, etc.) to avoid an internal HTTP hop and re-serialization — but the permission checks currently live in the `main.py` handlers, not the services. Decide: (a) call services + replicate `require_project_actor` in each tool (chosen default in T2 acceptance), or (b) factor a shared `initiate_*` service function that both the HTTP handler and the router tool call, moving the perm check down. (b) is cleaner long-term; (a) ships faster.
2. **Affirmative-detection for confirm-before-fanout.** Rely on an explicit `confirm_plan_id` from the UI (deterministic, safe) vs also letting the router interpret a free-text "yes, go" as confirmation (better UX, risk of over-eager dispatch). Recommend: require `confirm_plan_id` from the console (T5), allow router-interpreted affirmation only for surfaces without a button (spec 08's GitHub), with a conservative classifier.
3. **Manifest freshness & caching.** Rebuild per turn (simple, always correct) vs cache per project with event-driven invalidation. Tie to spec 06 — if 06 provides a cached registry, inherit its invalidation.
4. **Thread ownership & multi-actor threads.** Is a thread single-actor, or can teammates share one `agent_threads` row? Affects the `actor` column and per-message attribution. Default: single-actor per thread for launch; revisit for shared/ops channels.
5. **Relationship to `comms.ingest_inbox_message`.** Should the regex inbox path (`comms.py:1254`) eventually route unmatched free-form messages into the orchestration agent (email/Slack → intent router)? Natural convergence, but out of scope here — flag for a follow-up so the two inbound paths don't diverge permanently.
6. **Cost/rate-limiting.** `/v1/commands/propose` is already high-cost rate-limited (`main.py:4926`). Does `/v1/agent/chat` need its own limit given each turn may trigger an LLM router call plus a downstream initiator? Recommend applying `_enforce_high_cost_rate_limit` to the chat endpoint.
