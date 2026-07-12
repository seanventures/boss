# Robust Preview, Sandbox/VM Clarity & a Feedback-Iteration Agent

> **Summary:** Make app previews survive an API redeploy and render real apps (not just `fetch`-only pages), collapse the three conflated execution substrates into one auditable "where did this run" story, and add a conversational feedback agent that owns a per-task thread and drives `retry_code_task` with structured fixes.
> **User ask:** 6 · **Module:** Preview, Sandbox & Feedback · **Priority:** P0 (preview robustness + substrate clarity) + P2 (feedback agent)

---

## 1. Context & Goal

BOSS produces a green PR and then lets an operator *see the working website* before merging (`preview_service.py` docstring, lines 1-9). That preview loop is the human's primary confidence signal, but today it is fragile in three compounding ways, and the machinery underneath it (the sandbox/VM substrate that built the code) is conflated with three different meanings that no UI or doc disambiguates. Separately, the mechanism to *act on* preview feedback already exists (`retry_code_task`) but there is no agent that converses about a preview, decides what to change, and drives that mechanism.

This spec covers three tightly-related surfaces under user ask 6:

- **(A) Preview robustness (P0).** Previews are subprocess children of the API process, so any redeploy kills every live preview; the HTML proxy shim only rewrites `fetch()`/form `action`, so anything with `<a href>`, `<img src>`, `<link>`, a WebSocket, or client-side routing renders broken; run-command detection is a fixed heuristic; and previews run on the *API host*, not the substrate that built the task — so "preview passes" does not prove "the built environment works."
- **(B) VM-vs-sandbox clarity (P0).** Three substrates — local subprocess sandbox, external Firecracker sandbox-service ("the VM"), and the preview host — are conflated, governed by two independent settings that both default to `local`, with no UI or doc telling an operator *where a given run or preview actually executed*.
- **(C) Feedback iteration (P2).** `retry_code_task` already lets even a **completed** task receive operator feedback and extend on the same branch/PR. What's missing is a conversational **feedback agent**: a per-task thread that asks clarifying questions, decides tweak-vs-rerun, and drives `retry_code_task` with a structured `fix_note` — plus first-class "extend a completed task" and preview-anchored feedback capture.

**Goal.** After this work: (A) a preview outlives an API redeploy and renders a normal SPA/multi-page app faithfully; (B) every run and preview carries an unambiguous, badge-visible "ran on X substrate" provenance, documented once; (C) an operator can talk to a feedback agent from the preview, and the agent produces a scoped, structured retry instead of the operator hand-writing a `fix_note`.

---

## 2. Current State (ground truth)

### 2A. Preview robustness

- **Two modes + `auto`.** `preview_service.py` has process mode (`start_preview`, line 114 — git clone + heuristic `_detect_command` line 43 + `subprocess.Popen` line 158) and docker mode (`start_container_preview`, line 215 — synthesizes a Dockerfile via `_generate_dockerfile` line 187). The route picks `auto` (docker-then-process) at `main.py:4338` (`/v1/code-tasks/{run_id}/preview`).
- **Killed on every restart.** Previews are `subprocess.Popen(..., start_new_session=True)` children of the API process (`preview_service.py:158`). The lifespan hook bulk-marks them dead on boot: `UPDATE app_previews SET status='stopped' WHERE status='running'` (`main.py:643-652`, comment: "a restart/recreate kills them all"). Every API redeploy invalidates every live preview.
- **Fragile proxy shim.** Previews bind to `127.0.0.1:<port>` (`preview_service.py:171`); only the API can reach that loopback port, so `proxy_preview` (line 338) reverse-proxies them at the **auth-exempt** route `/v1/previews/{id}/web/{path}` (`main.py`, served through `proxy_preview`). HTML responses get `_inject_shim` (line 316), which rewrites **only** root-relative `fetch()` URLs and form `action` attributes (lines 321-322). It does **not** rewrite `<a href>`, `<img src>`, `<link href>`, `<script src>`, `srcset`, CSS `url(...)`, WebSocket URLs, or history-API/SPA client routing — so any non-trivial app renders with broken assets, links, and live connections.
- **Brittle port discovery.** `_discover_working_port` (line 102) reads the app's log for a printed URL/port (`_candidate_ports`, line 76) then falls back to the injected `PORT` env (line 98), health-checking each (`_http_ok`, line 66). Apps that neither print a port nor honor `PORT`/`HOST` env are undiscoverable; discovery deliberately avoids process-tree `lsof` to dodge neighboring services (line 80-81), so there is no fallback for the silent case. `_http_ok` treats **any** HTTP response including 4xx as "serving" (line 71) — a startup error page counts as up.
- **Runs on the API host, not the build substrate.** `start_preview` re-clones the branch fresh (`preview_service.py:146`) and runs it on the API host — it never touches the micro-VM or the rootfs that built the task. So the preview environment ≠ the build environment; a task that built and passed inside a micro-VM can fail to preview (or vice versa) purely from host drift.
- **Failure UX is a dead end.** On a gone process the proxy self-heals the DB row and returns a static "Preview is no longer running … click Run preview" HTML (`proxy_preview`, lines 365-378). A failed start returns `{status:"failed", error: "<tail 300 chars>"}` (`_fail`, line 134) with no streamed log surface.
- **UI.** `PreviewControls` (`apps/web/app/code-tasks-ui.tsx:30`) shows a "▶ Run preview" button (line 36) and is rendered per-run (line 381), gated to after PR open.

### 2B. VM-vs-sandbox conflation

Three distinct things all get called "the sandbox" / "the VM":

1. **Build sandbox** — chosen by `make_sandbox_provider` (`sandbox_provider.py:235`). Two providers:
   - `LocalSubprocessProvider` (line 56) → per-task `EphemeralSandbox` (temp clone + allowlisted subprocess **on the API host**, no isolation; `acquire`, line 66).
   - `MicroVMProvider` (line 70) → dispatches the **whole** task to an external sandbox-service over an async submit→poll→release contract (`run_code_task`, line 148; submit body, line 111). It has no granular `acquire()` (raises, line 106).
2. **"The VM"** — the external sandbox-service at `settings.sandbox_agent_url` (default `http://127.0.0.1:8088`, `config.py:436`). On **success** the control plane releases it (`MicroVMProvider.release`, line 92); on **failure** it is intentionally **retained** so the rootfs survives ~2h for inspection (line 93-94, and the `run_code_task` NOTE at line 196-198).
3. **Retained-rootfs console** — `sandbox_console.py` proxies file/tree/log browsing of a retained VM (`get_tree` line 70, `read_file` line 95, `get_logs` line 103), but live exec is **hard-blocked**: `exec_command` (line 123) returns `{"blocked": True, ...}` unconditionally — "no live process to exec into" (line 126-129).
4. **Preview host** — always the API host (§2A), re-clones the branch, never touches any VM.

**Two independent settings, both default `local`:**
- `settings.sandbox_provider` (`config.py:434`, default `"local"`) read by the native pipeline at `code_task_service.py:387`.
- `settings.governed_sandbox_provider` (`config.py:427`, default `"local"`) read by the governed pipeline at `governed_code_task_service.py:480`; when microvm, it runs **per-stage** micro-VMs (`_guest_stage`, line 487, with git as the state carrier — see call sites lines 604, 655, 798, 834).

Nothing records or surfaces *which substrate a given run actually used*, and the two pipelines can disagree. An operator cannot tell whether a run executed on the API host or in a micro-VM, nor where its preview ran.

### 2C. Feedback iteration

- **The mechanism already exists.** `retry_code_task` (`code_task_service.py:134`, endpoint `POST /v1/code-tasks/{id}/retry`, `main.py:4310`) starts a **new attempt in the same lineage** (parent/root/attempt tracked in `finalize_ctx`, line 210), reuses the branch `boss/code/{root}` (line 149) so the model **iterates on the existing code** rather than regenerating (feedback block, lines 168-182), and threads the operator's `fix_note` (line 175-176) plus prior adversarial findings into an `=== ITERATION FEEDBACK` block (line 190). `feedback_history` accumulates per attempt (lines 195-199).
- **Completed tasks can already be extended.** There is **no status guard** — a completed run can be retried. `retry_code_task` deliberately does **not** carry prior findings for a completed run (line 156: "a completed attempt's reviews passed"), and instead lets the operator's feedback drive the iteration (line 154). The UI already reflects this: `RetryControls` (`code-tasks-ui.tsx:54`) shows "↻ Iterate with feedback" for completed runs vs "↻ Retry with feedback" otherwise (line 66), posting a free-text `fix_note` textarea (line 60).
- **`override_code_task`** (`code_task_service.py:220`) is a different lever: waive gates + open a PR from the preserved diff, **no re-run** (requires `awaiting_gate_override`).
- **The gap.** There is no conversational feedback agent (grep confirms none). The operator hand-writes a raw `fix_note` into a textarea; nothing (a) asks clarifying questions, (b) decides scoped-tweak vs. full-rerun, (c) structures the fix, or (d) anchors feedback to *what the operator saw in the preview* (a specific URL/element/interaction).

---

## 3. Target Design & Approach

### 3A. Preview robustness (P0)

Four moves, roughly independent:

1. **Detach preview lifecycle from the API process (persistent/supervised previews).** Previews must not be children of the API. Two viable substrates, in preference order:
   - **Serve the preview from the retained build artifact / micro-VM rootfs (~2h).** When a task built in a micro-VM (§2B), the sandbox-service already retains the rootfs on failure and can be *held* on success (mirror `sandbox_console.hold`, line 112). Extend the sandbox-service contract with a "serve" verb: boot the retained rootfs, run the app inside it, and expose its port back through the existing proxy. **Preview env == build env** — this is the highest-value fix and the one the user explicitly called out. Guarded by whether the run used the microvm substrate (§3B provenance).
   - **Supervised local process (fallback for local-substrate runs).** Launch the preview under a supervisor that is *not* the API process — a detached `systemd --user`/`launchd` unit, a long-lived `boss-preview-supervisor` sidecar, or (simplest) a container in docker mode (`start_container_preview`, line 215) which already survives an API restart. Remove the blanket `UPDATE app_previews SET status='stopped'` on boot (`main.py:643-652`); instead, on boot **reconcile**: re-probe each `running` preview's port/container and only mark dead ones stopped.
2. **Harden the proxy — or give each preview a subdomain.** Two options (Open Decision D1):
   - **(preferred) Per-preview subdomain / path-transparent routing.** Route `preview-<id>.<host>` straight to the preview's port with no HTML rewriting at all. Apps then use their natural root-relative URLs and everything (SPA routing, WS, assets) "just works." Requires wildcard DNS/TLS on the preview host (tailnet-friendly).
   - **(fallback) A real rewriting proxy.** If we must keep the path prefix, replace `_inject_shim` (line 316) with proper handling: inject a `<base href="/v1/previews/{id}/web/">` tag and a broader client shim that also patches `history.pushState`/`replaceState`, `WebSocket`, `EventSource`, `<a>`/`<img>`/`<link>`/`<script>` root-relative attributes, `srcset`, and CSS `url(...)`; rewrite `Location` headers on redirects; and pass through non-HTML byte-for-byte (already the case, line 379). This is strictly a stopgap — SPAs will still have edge cases.
3. **Robust run-command detection + streamed logs + actionable failures.** Extend `_detect_command` (line 43): honor `package.json` `scripts.dev`/`scripts.start`, `Procfile`, `Makefile` `run`/`dev` targets, and a repo-level `boss.preview.command` hint; when a framework is detected (Next/Vite/CRA) prefer its dev server. Make `_http_ok` (line 66) distinguish a real app response from an error page (don't treat all 4xx as "up" for the *root* health probe; allow explicit health paths). Stream the preview log to the UI over the existing events/SSE surface instead of returning only a 300-char tail (`_fail`, line 134). On failure, classify the cause (clone failed / no command detected / port never bound / crashed on boot with log excerpt) and return an actionable message + a "retry with docker mode" / "provide run command" affordance.
4. **Provenance stamp.** Every preview row records *which substrate it ran on* (rootfs-served vs docker vs local process vs API host) and *which run/build substrate it corresponds to*, feeding the §3B badge.

### 3B. VM-vs-sandbox clarity (P0)

1. **Single source of truth for "where."** Introduce one normalized notion of **execution substrate** with a small enum — e.g. `local_subprocess` (API host), `microvm` (sandbox-service VM), `preview_host`, `preview_rootfs`. Record it, per run and per preview, at the moment execution is chosen: in the native pipeline where `make_sandbox_provider` is called (`code_task_service.py:387`) and in the governed pipeline where `use_microvm` is decided (`governed_code_task_service.py:480`). Persist the resolved substrate (plus the sandbox-service `sandbox_task_id` when microvm — already captured as `MicroVMProvider.last_sandbox_task_id`, line 87) onto the run record, so it is auditable rather than re-derived from settings.
2. **Reconcile the two default-`local` settings.** Document why two settings exist (native vs governed pipeline) and make their relationship explicit: either collapse to a single `BOSS_SANDBOX_PROVIDER` with a governed override, or keep both but validate/surface them together at startup and in the badge. Emit a clear boot log line stating the effective substrate for each pipeline.
3. **Console badge.** Add a substrate badge to the run and preview views in `apps/web/app/code-tasks-ui.tsx` showing exactly where each run built and where its preview is running (e.g. "built: micro-VM `sb_...` · preview: retained rootfs" vs "built: API host · preview: docker"). This closes the "operator can't tell where it ran" gap.
4. **Documentation.** One canonical doc page (this file plus an operator-facing note in `docs/`) that names the three substrates, the two settings, the retained-rootfs console (read-only, exec hard-blocked — `sandbox_console.py:123`), and the preview host, with a decision table for which combination applies.

### 3C. Feedback agent (P2)

Build a conversational **feedback agent** on top of the existing `retry_code_task` mechanism — do not reinvent the iteration engine.

1. **Per-task feedback thread.** A durable thread keyed by root run id (align with `finalize_ctx.root_run_id`, `code_task_service.py:147`). Messages: operator feedback, agent clarifying questions, agent decisions, and links to resulting retry attempts. Reuse/extend the events ledger and a `feedback_threads`/`feedback_messages` store.
2. **Clarifying-question loop.** When feedback is vague ("the button doesn't work"), the agent asks targeted questions (which button, expected vs actual, on which preview URL) before dispatching — instead of forwarding an ambiguous `fix_note`.
3. **Tweak-vs-rerun decision.** The agent classifies each request as a **scoped tweak** (small, localized — a light fix on the same branch) vs a **full rerun** (re-engage the governed pipeline). Both terminate in `retry_code_task` with a well-structured `fix_note`; the classification tunes `profile`/`repair_retries` (already plumbed through `retry_code_task`, lines 212-214) and sets expectations in the thread.
4. **Structured `fix_note`.** The agent emits a structured fix instruction (observed behavior, expected behavior, anchor URL/route, severity, suggested scope) rather than raw prose, and passes it as the `fix_note` to `retry_code_task`. This lands in the `## Feedback to address (from the operator)` block (line 176).
5. **First-class "extend a completed task."** Surface completed-task extension as its own flow (not just a repurposed retry button): the agent frames it as "add/change behavior on the existing PR" and relies on the existing no-status-guard path (line 156) that carries operator feedback rather than stale findings.
6. **Preview-anchored capture.** From the preview UI, let the operator capture feedback *in context* — current URL/route, optionally a selected element or screenshot — and route it into the feedback thread pre-populated. This makes the anchor field in the structured `fix_note` automatic.

---

## 4. Implementation Tickets

> Sizes: S ≈ ≤1 day, M ≈ 2-4 days, L ≈ 1-2 weeks.

### T1 — Previews survive an API redeploy (reconcile, don't kill) · **P0 · M**
**Scope.** Stop bulk-killing previews on boot; run a reconciliation pass that re-probes each `running` preview and only marks the genuinely-dead ones. Move local-process previews under a supervisor that is not a child of the API (detached session already exists via `start_new_session=True`, `preview_service.py:159`, but the DB is wiped regardless).
**Key files.** `apps/api/app/main.py:643-652` (replace blanket UPDATE with reconcile), `apps/api/app/services/preview_service.py` (add `reconcile_previews()` using `_http_ok`/`_docker_rm`), `app_previews` table.
**Acceptance criteria.**
- [ ] After an API restart, a preview whose process/container is still alive stays `running` and is reachable through the proxy.
- [ ] A preview whose process actually died is marked `stopped`/`expired`, not left `running`.
- [ ] No blanket `UPDATE app_previews SET status='stopped'` on boot remains.
- [ ] Reconcile is idempotent and bounded (probes with timeout, reaps expired per `list_previews` line 456).

### T2 — Serve preview from the retained build artifact / micro-VM rootfs · **P0 · L**
**Scope.** For runs built on the microvm substrate, add a "serve" path so the preview boots inside the retained rootfs (preview env == build env) and exposes its port back through the existing proxy. Extend the sandbox-service contract (submit/hold analog) and wire a `start_rootfs_preview()` alongside `start_preview`/`start_container_preview`.
**Key files.** `apps/api/app/services/preview_service.py` (new serve path), `apps/api/app/services/sandbox_provider.py:70-109` (MicroVMProvider — add serve/hold verbs, reuse `last_sandbox_task_id` line 87), `apps/api/app/services/sandbox_console.py:112` (`hold` analog), sandbox-service side contract.
**Acceptance criteria.**
- [ ] A task built in a micro-VM can be previewed from its retained rootfs without re-cloning on the API host.
- [ ] The preview's substrate is recorded as `preview_rootfs` (feeds T6 badge).
- [ ] Rootfs preview honors the ~2h retention window and is released/held explicitly, not leaked.
- [ ] Falls back cleanly to host/docker preview when the run was built on the local substrate.

### T3 — Faithful preview rendering: subdomain routing or a real rewriting proxy · **P0 · L**
**Scope.** Implement Open Decision D1. Preferred: per-preview subdomain routing with no HTML rewriting. Fallback: replace `_inject_shim` with a `<base>` tag + broadened client shim (history API, WS, EventSource, `a/img/link/script`, `srcset`, CSS `url()`) and `Location`-header rewriting.
**Key files.** `apps/api/app/services/preview_service.py:316` (`_inject_shim`) and `:338` (`proxy_preview`), `apps/api/app/main.py` (preview web route + any DNS/TLS/routing config for subdomains).
**Acceptance criteria.**
- [ ] A multi-page app with `<a href="/...">` links and `<img src="/...">` assets renders and navigates correctly through the preview.
- [ ] A client-side-routed SPA (pushState) deep-links and reloads correctly.
- [ ] A WebSocket/EventSource-using app connects through the preview.
- [ ] Non-HTML responses remain byte-identical (regression guard on `proxy_preview` line 379).

### T4 — Robust run-command detection + streamed logs + actionable failures · **P0 · M**
**Scope.** Extend `_detect_command` to cover `package.json` `dev`/`start` scripts, `Procfile`, `Makefile` targets, framework dev servers, and a `boss.preview.command` repo hint. Tighten `_http_ok`/`_discover_working_port` so an error page isn't "up." Stream the preview log to the UI; classify failures with actionable next steps.
**Key files.** `apps/api/app/services/preview_service.py:43` (`_detect_command`), `:66` (`_http_ok`), `:102` (`_discover_working_port`), `:134` (`_fail`), `apps/web/app/code-tasks-ui.tsx:30` (`PreviewControls` — log stream + failure affordances).
**Acceptance criteria.**
- [ ] A Vite/Next/CRA app and a `Procfile`/`Makefile`-based app all start without hand configuration.
- [ ] A boot that only produces an error page is reported as failed, not "running."
- [ ] The operator sees streamed startup logs (not just a 300-char tail).
- [ ] A failed preview shows a classified cause + a concrete next action (e.g. "retry in docker mode", "set boss.preview.command").

### T5 — Execution-substrate as a single source of truth · **P0 · M**
**Scope.** Introduce a normalized substrate enum, resolve and persist it per run (native + governed pipelines) and per preview, and reconcile the two default-`local` settings with a documented relationship and a boot-time effective-substrate log line.
**Key files.** `apps/api/app/services/sandbox_provider.py:235` (`make_sandbox_provider`), `apps/api/app/services/code_task_service.py:387`, `apps/api/app/services/governed_code_task_service.py:480`, `apps/api/app/config.py:427,434`, run + `app_previews` schema.
**Acceptance criteria.**
- [ ] Each run persists the substrate it actually executed on (plus `sandbox_task_id` when microvm).
- [ ] Native and governed pipelines report substrate through one shared code path/enum.
- [ ] Startup logs the effective substrate for each pipeline.
- [ ] The two settings' relationship is documented and validated (no silent disagreement).

### T6 — Console substrate badge · **P0 · S**
**Scope.** Show, on each run and preview, exactly where it built and where the preview is running.
**Key files.** `apps/web/app/code-tasks-ui.tsx` (near `PreviewControls` line 30 / run detail line 381), run + preview API payloads (from T5/T4 provenance).
**Acceptance criteria.**
- [ ] A run shows "built: micro-VM `sb_...`" vs "built: API host."
- [ ] A preview shows "preview: retained rootfs | docker | host process."
- [ ] Badge reflects reconciled state after a restart (consistent with T1).

### T7 — Feedback thread + conversational feedback agent · **P2 · L**
**Scope.** A durable per-task feedback thread keyed by `root_run_id` and an agent that asks clarifying questions, classifies tweak-vs-rerun, emits a structured `fix_note`, and drives `retry_code_task`. Reuses the existing iteration mechanism end-to-end.
**Key files.** new `apps/api/app/services/feedback_agent.py` + `feedback_threads`/`feedback_messages` store, `apps/api/app/services/code_task_service.py:134` (`retry_code_task`, unchanged entry point), `apps/api/app/main.py:4310` (retry endpoint) + new feedback endpoints, events ledger.
**Acceptance criteria.**
- [ ] Vague feedback triggers a clarifying question before any retry is dispatched.
- [ ] The agent classifies each request as scoped-tweak vs full-rerun and tunes `profile`/`repair_retries` accordingly (via `retry_code_task` args, lines 212-214).
- [ ] The dispatched retry carries a structured `fix_note` (observed/expected/anchor/severity/scope), landing in the `## Feedback to address` block (line 176).
- [ ] The thread links each operator message to the resulting attempt (`feedback_history`, lines 195-199).

### T8 — First-class "extend a completed task" flow · **P2 · S**
**Scope.** Surface completed-task extension as its own affordance framed as "change/add behavior on the existing PR," relying on the existing no-status-guard retry path.
**Key files.** `apps/web/app/code-tasks-ui.tsx:54` (`RetryControls` — distinct completed-task entry), `apps/api/app/services/code_task_service.py:154-156` (existing completed-task behavior, no change needed).
**Acceptance criteria.**
- [ ] Completed tasks present an "extend" action distinct from failure-retry, wired to the feedback agent (T7).
- [ ] Extension reuses the same branch/PR (`boss/code/{root}`, line 149) and does not carry stale findings (line 156).

### T9 — Preview-anchored feedback capture · **P2 · M**
**Scope.** Capture feedback in context from the preview (current URL/route, optional selected element/screenshot) and route it into the feedback thread pre-populated so the structured `fix_note` anchor is automatic.
**Key files.** `apps/web/app/code-tasks-ui.tsx` (preview UI, `PreviewControls` line 30), preview proxy (`preview_service.py:338`), feedback endpoints (T7).
**Acceptance criteria.**
- [ ] From a live preview, an operator can start feedback with the current route captured.
- [ ] The captured anchor pre-fills the structured `fix_note` anchor field (T7).
- [ ] Works for both subdomain-routed and proxy-prefixed previews (T3).

---

## 5. Dependencies, Sequencing & Priority

**Priority.** Part A (preview) and Part B (substrate clarity) are **P0** — they are the operator's confidence loop and the auditability that makes it trustworthy. Part C (feedback agent) is **P2** — the underlying mechanism already ships, so this is UX/agent leverage on top.

**Sequencing.**
1. **T5 (substrate SoT)** first — it is the provenance backbone that T2, T4, and T6 all read from.
2. **T1 (survive redeploy)** in parallel with T5 — self-contained, immediate operator win, and prerequisite for T6 badge consistency.
3. **T4 (detection/logs)** and **T3 (rendering)** next — the two things that make previews actually usable; independent of each other (do in parallel).
4. **T2 (rootfs preview)** depends on T5 (needs substrate provenance) and the sandbox-service serve verb; highest value but heaviest — schedule after T1/T4 land.
5. **T6 (badge)** after T5 + T1 (needs persisted substrate + reconciled state).
6. **T7 (feedback agent)** depends on nothing in A/B structurally, but is far more useful **after T3/T9** (anchored, faithful previews to react to). **T8** rides on T7. **T9** depends on T3 (routing model) + T7 (thread).

**Cross-spec dependencies.** Substrate provenance (T5) and the retained-rootfs serve path (T2) touch the same sandbox-service contract discussed in the sandbox/micro-VM security work referenced in MEMORY (`boss_microvm_sandbox`); coordinate the serve/hold verb there. The feedback agent (T7) should emit through the same events ledger other asks build on.

---

## 6. Open Decisions

- **D1 — Subdomain routing vs. rewriting proxy (T3).** Per-preview subdomains eliminate HTML rewriting entirely and are the only robust answer for SPAs/WS, but require wildcard DNS/TLS on the preview host. The rewriting proxy needs no infra but will always have SPA edge cases. Recommendation: subdomains where the host supports wildcard TLS (tailnet), rewriting proxy as the portable fallback. **Decide before starting T3.**
- **D2 — Collapse the two provider settings, or keep both?** `sandbox_provider` (native) and `governed_sandbox_provider` (governed) both default `local` (`config.py:427,434`). Collapsing to one with a governed override is simpler but loses the ability to run the two pipelines on different substrates during migration. Recommendation: keep both, validate + surface together (T5). **Decide during T5.**
- **D3 — Preview supervisor substrate for local runs (T1).** Detached process under `systemd --user`/`launchd`, a dedicated `boss-preview-supervisor` sidecar, or always-docker for previews. Docker is the least new machinery (already implemented, survives restart) but excludes hosts without Docker. **Decide during T1.**
- **D4 — Should live exec into a retained rootfs stay hard-blocked?** `sandbox_console.exec_command` is unconditionally blocked (`sandbox_console.py:123`) because the rootfs is a read-only snapshot. Serving a preview *from* that rootfs (T2) implies booting a live process — revisit whether a constrained, audited exec/serve is acceptable, and how it interacts with the security model. **Decide during T2.**
- **D5 — Feedback-agent autonomy boundary (T7).** How much can the agent dispatch a retry without operator confirmation? Options: always confirm before dispatch, auto-dispatch scoped tweaks but confirm full reruns, or fully autonomous with a kill-switch. Recommendation: confirm-before-dispatch for v1, given retries consume budget (reserve/settle in the pipeline). **Decide during T7.**
- **D6 — `boss.preview.command` hint format (T4).** A repo-committed file vs. a run-spec field vs. inferred-and-cached. Affects whether operators can fix undetectable apps without a code change.
