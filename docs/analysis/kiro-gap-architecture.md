# Kiro Crew → BOSS: Architecture Gap Analysis

*Evidence gathered 2026-09-19 from `kiro.dev/blog/software-factory-1000-prs`, `kiro.dev/docs`,
`github.com/kirodotdev/KiroCrew` (Apache-2.0, cloned at ~3.1M LOC Python), and
`github.com/kirodotdev/Kiro`. BOSS side verified against this repo at `49b4fc45`.*

Companion document: [`kiro-gap-features.md`](kiro-gap-features.md). This one is about
**runtime shape** — the primitives that decide what the platform can physically do. The other is
about **capabilities an operator can name**.

---

## 0. The correction that has to come first

**Kiro is not open source. Kiro Crew is.**

| Repo | What it actually contains | Licence | Stars |
|---|---|---|---|
| `kirodotdev/Kiro` | `.github`, `docs`, `assets`, `scripts` — **no source**. An issue tracker and docs site for the closed AWS IDE. | **none** | 4,319 |
| `kirodotdev/KiroCrew` | The real thing: `src/kiro_crew/`, ~4,400 Python files | **Apache-2.0** | 4,014 |

So "adopt what Kiro does" means *Kiro Crew*. The IDE's concepts (Specs, Steering, Hooks) are
documented publicly but their implementation is not available — we would be reimplementing from
documentation, which is fine and is what this document assumes.

## 1. The framing that should govern every decision below

These are **not the same kind of system**, and a straight feature-port would damage BOSS.

|  | **Kiro Crew** | **BOSS** |
|---|---|---|
| Core noun | A *persistent workspace* | A *governed control plane* |
| Optimises for | Continuity and throughput | Evidence and accountability |
| Trust model | One owner, their hardware ("Slack access is owner-only") | Multi-tenant, roles, entitlements, SOC2, break-glass |
| Unit of work | A session that never ends | A workflow run with approval gates and receipts |
| Failure posture | Retry, checkpoint, keep going | Stop, record, require a human |
| Surface count | 11 messaging channels + desktop + web + CLI | Console + API + limited channels |
| Scale claim | 30–50 concurrent sessions on 6–10 machines | Single-container control plane |

Read that table in both directions. Kiro Crew has **no** risk tiers, no approval floor, no
dedupe keys, no receipts, no entitlements, no multi-tenancy, no SAML/OIDC, no canary/rollout
engine, no verifier debate, no FinOps reconciliation. BOSS has all of those and they are the
product. **We are not behind on governance — we are behind on continuity and scale-out.**

> Their own blog agrees on where the value is: *"Stage 1 to 2 is free. Stage 2 to 3 is the one
> that pays."* Stage 3 is **memory + dashboard + cron + workflows**. BOSS has the dashboard and
> workflows. It has **no memory**, and no scheduler for agent work.

**The recommendation of this document is therefore narrow:** import the *continuity and
scale-out primitives*, and import none of the trust model.

## 2. Measured position

What their numbers actually demonstrate (all self-reported in the blog):

- **1,000 PRs merged in 7 days**, ">120 a day, every one went through CI and review"
- **3 full-time engineers + ~500 community contributors**
- **30–50 concurrent sessions** across **6–10 machines**
- Sessions run "on laptops, on EC2 instances, on cloud dev desktops, and will be in Fargate"

And the community flywheel, measured directly from the API on 2026-09-19:

| Signal | Value |
|---|---|
| Stars since repo creation (2026-07-16) | 4,014 in ~9 weeks |
| Forks | 623 |
| **Open PRs** | **~570** |
| Open issues | 1,936 |
| Releases | daily `insider` builds; `v0.7.0-insider.5` on 2026-09-18 |

**What makes them popular is not the agent.** It is: one-line install onto your own hardware,
Apache-2.0, a desktop app with auto-update, 11 chat surfaces, and a visible daily release
cadence that makes ~570 open PRs feel worth filing. That is an *adoption architecture*, and it
is the part of their design most relevant to BOSS's own OSS initiative.

---

## 3. Architecture gaps, ranked by leverage

Each gap: what they have, what BOSS has today (verified), and what it would cost us.

### A1 — There is no memory plane. At all. `[CRITICAL]`

**Verified:** `db/schema.sql` defines **249 tables** and not one of them stores agent memory,
preferences, or lessons. `grep -rl "lesson" engine/boss_engine` → **1 file** (incidental).

Kiro Crew's memory is four distinct stores with different lifetimes:

| Store | Contents | Lifetime rule |
|---|---|---|
| `preferences.md` | Style, tools, communication | Consolidated ~every 30 messages |
| `projects.md` | Active project context, decisions, blockers | Consolidated alongside |
| `history/{date}.md` | Daily conversation summaries | **Graduated decay**: full 0–13d → first-entry+count 14–60d → count only 61–180d → on-disk-not-loaded 181–365d → pruned 365d+ |
| `lessons.jsonl` | Corrections you taught it | Durable, injected into later sessions |

The lesson mechanism is the one that matters: *"no, always run the frontend checks before
calling a change done"* becomes a **workspace-scoped lesson applied in future sessions**.

BOSS today re-derives everything from thread history every turn. This session is the proof — I
fixed the same class of relevance bug three times because nothing carried forward.

> **Note the design discipline we should copy:** the decay schedule. An unbounded memory store
> is a context-window bomb. Their answer is time-tiered fidelity, not summarisation-on-read.

**BOSS shape:** new tables `agent_memory` (kind: preference|project|lesson, scope:
org|project|actor|role, body, source_thread_id, confidence, created_at, expires_at) plus
`agent_memory_events` for provenance. Injected by `intent_router` at the same point
`_role_identity` is assembled. **Must be governed**: a lesson is a rule an agent will follow, so
it needs the same review path as a skill — which is a thing Kiro Crew does *not* have and we
already do.

### A2 — No session ledger, and no compaction at all `[CRITICAL]`

**Verified:** `grep -rl "compaction" engine/boss_engine` → **0 files**.

Kiro Crew's session ledger is "a small durable record of what a long-running session is doing:
the goal, the phase it has reached, the concrete next step, **the approaches already tried and
rejected**, and pointers to whatever it produced." Explicitly *"storage the agent reads and
writes, not a view you browse."*

Two details worth stealing verbatim:

1. **"Entries are written as intent, not status"** — *"add the Windows branch to the permission
   helper, the test already fails"* beats *"in progress"* for a cold resume.
2. **A ledger is marked finished** so it stops being re-injected. *"A stale ledger quietly
   steering a session that has moved on is the failure this avoids."*

BOSS has zero compaction strategy, which means long threads do not degrade gracefully — they
hit the window and the earliest context (usually the actual goal) is what falls out first.

**BOSS shape:** `agent_session_ledger` keyed by `thread_id` — goal, phase, next_step,
rejected[], artifact_ids[], finished_at. Written by the agent via a tool; injected ahead of
history. This is also the missing spine for the "agent forgot what it was doing" failures
already logged against the console.

### A3 — No subagent / fan-out primitive `[HIGH]`

**Verified:** `grep -rl "subagent" engine/boss_engine` → **0 files**.

Kiro Crew's `spawn_run` MCP tool takes `task` *or* `tasks[]` for parallel execution, optional
per-task `agent`/`agents[]`, and three context switches — `include_memory`, `include_lessons`,
`include_project` — so a subagent can be deliberately starved of parent context when a fresh
view is what you want. There is a `solo_reason` gate (`bulk_data` | `fresh_context`) forcing a
justification when spawning **one** subagent alone, which is a neat guard against using fan-out
machinery for something that should have been a tool call.

**Dynamic sizing** is the part I would not have thought of: `agent.max_subagents = 0` means
*auto*, computed at gateway start from **host memory and CPU plus a per-agent cost the system
learns from past runs**. Explicit values are floored at 3; a pin of 1–2 is normalised up and
emits a `config_bounds_clamped` event.

**BOSS shape:** BOSS already has `concurrency_governance`, `worker_autoscaling` and
`workflow_capability_runner`. The gap is a *conversational* fan-out: the agent deciding to
parallelise within one turn and synthesising results. Risk: this multiplies spend, so it must
land behind the existing budget cap and `MANUAL_APPROVAL_RISK_FLOOR`, not beside it.

### A4 — Control plane and work share one container `[HIGH]`

BOSS's own `scheduling.py` states the problem in its docstring:

> *"The control plane and the work it supervises run in the SAME container. `npm install`,
> `vite build`, `tsc` and Playwright execute as child processes of the API server — and those
> are among the most parallel workloads there are."*

Kiro Crew separates them deliberately: a long-running **Gateway** (access, sessions, memory,
schedules, approvals, apps) and **agent sessions** driving `kiro-cli` over **ACP**. A session is
*"a logical isolation boundary, not necessarily one OS process"* — backed either by a dedicated
ACP process or a handle on a **shared multiplexed ACP runtime**. A **warm pool** keeps processes
pre-spawned so a new session starts instantly (`grep warm.?pool` in BOSS → **0 files**).

**BOSS shape:** this is the most expensive item here and should not be attempted as one move.
The tractable first step is a **session-runtime interface** with the current in-container
implementation behind it, so a second implementation (separate process, then separate host) can
land later without touching callers. BOSS already has `execution_substrate`, `sandbox_provider`
and `container_runner` — the seam partly exists.

### A5 — No hook plane at the agent's own boundary `[HIGH]`

**Verified:** BOSS's 34 `hook` matches are all *webhooks* (inbound/outbound HTTP). That is a
different thing.

Kiro's hooks fire on the **agent's** actions, with a documented trigger vocabulary:

`PostFileCreate` · `PostFileSave` · `PostFileDelete` · `PromptSubmit` · `AgentStop` ·
**`PreToolUse` (can block execution)** · `PostToolUse` · `SessionStart` · `AgentSpawn` ·
`PreTaskExecution` · `PostTaskExecution`

Config is JSON in `.kiro/hooks/` with `trigger`, a regex `matcher`, and an `action` of type
`command` or `agent`. A deliberate limitation, stated plainly: *"File triggers respond only to
changes made by the agent."*

**Why this is the highest-principle item in the document:** the blog's four mandatory controls
include *"Permission control — enforced at the host level, not in prompts."* BOSS already
believes this — it is why `run_on_host` requires `_operator_confirmed` and why the write
allowlist exists. But BOSS has **no generic pre-tool interception point**; every guard is
hand-written at its own call site. A `PreToolUse` plane would let policy be declared once and
enforced uniformly, and would have caught at least two bugs found in this repo this month.

### A6 — Workflow stages wait on each other `[MEDIUM]`

Their Stage 4 is *"independent stages (triage, implement, review, merge) with message queues
between them. No stage waits for another."* Stage 5 adds an agent doing *"decomposition,
dispatch, patrol, acceptance, and sequencing."*

BOSS's `workflow_capability_runner` parks a step against one awaited id and sweeps. That is
closer to Stage 3–4: the graph is authored, the runner advances it. It also had the exact defect
their Stage 4 critique predicts — *"the pipeline is a fixed shape the human designed; it cannot
re-plan when a result invalidates the plan"* — which showed up here as a step following a dead
`code_task` id after a retry, fixed only for `kind == "code_task"` and **still unfixed for
meetings**.

**BOSS shape:** rather than import message queues wholesale, the cheaper win is *patrol*: a
sweeper that re-plans rather than just re-queues. BOSS has `shepherd_service` and
`task_shepherd` — the bones are there.

### A7 — No checkpoint/resume for long tasks `[MEDIUM]`

*"Give Kiro Crew a task spec and walk away. It plans steps, executes them, validates results,
retries failures, and resumes from checkpoints."* Invocable five ways — chat, dashboard, Slack,
`kirocrew run TASK.md`, and a `task_run` MCP tool that also accepts `__inline__:` content.

BOSS has retries, dead-letters and `recheck_workflow_run`, but no checkpointed **resume of a
spec**: a failed run restarts from its step, not from a saved position inside the step.

### A8 — Distribution is not an architecture we have `[MEDIUM, strategic]`

`curl -fsSL https://download.crew.kiro.dev/cli.sh | sh`, signed wheels, a desktop app with three
update channels, `.deb`/`.rpm`/AppImage for x86_64 **and** aarch64, a Docker image for always-on
servers, and an AppArmor profile shipped with the Linux packages.

BOSS installs as a compose stack you build from source. This session demonstrated the cost:
recovering `INFISICAL_ENCRYPTION_KEY` out of a running container by hand, and discovering that
`BOSS_SOURCE_SHA` is a build arg **no install ever populates** — so every BOSS instance reports
its own version as `unknown`.

If the OSS initiative is real (see `boss_oss_readiness`), this is the gap that decides whether
anyone gets far enough to see the governance we are proud of.

---

## 4. Where BOSS is ahead — do not regress these

Stated explicitly so the phased plan below is read as *additive*.

| Capability | BOSS | Kiro Crew |
|---|---|---|
| Risk tiers + manual-approval floor | Yes (`MANUAL_APPROVAL_RISK_FLOOR`) | No |
| Approval receipts, dedupe keys, quota guards | Yes | No |
| Multi-tenant orgs/projects, entitlements, editions | Yes | No — single owner |
| SAML / OIDC / SOC2 / break-glass | Yes | No |
| Canary + staged rollout + rollback engine | Yes | No |
| Verifier debate, mutation gate, visual gate | Yes | No |
| FinOps reconciliation, spend caps, budget failover | Yes | Partial (cost concern acknowledged only) |
| Governed PRD → panel → code-task pipeline | Yes | No |
| Secrets vault with `secret://` refs | **Both** | **Both** |

That last row matters: BOSS's `secret_refs` + AES-GCM store is at parity with their secrets
vault. It is the one place we should copy nothing.

---

## 5. Phased plan (architecture)

Sequenced so each phase is independently shippable and each unblocks the next. Estimates assume
the current team shape.

### Phase A0 — Continuity foundation `[3–4 weeks]`

**Goal:** an agent that remembers, and a long session that does not lose its spine.

| # | Item | Gap | Done when |
|---|---|---|---|
| A0.1 | `agent_memory` tables + write/read path | A1 | A correction in one thread changes behaviour in the next |
| A0.2 | Memory decay schedule (time-tiered fidelity) | A1 | Memory injection stays under a fixed token budget at 1,000 threads |
| A0.3 | Lesson review gate (reuse the skill-approval path) | A1 | A lesson cannot silently become a rule |
| A0.4 | `agent_session_ledger` + tool + injection | A2 | "Where are we on this?" answers from the ledger, not scrollback |
| A0.5 | Compaction strategy that preserves goal + ledger | A2 | A 200-turn thread still knows its objective |

**Risk:** memory that is wrong is worse than no memory. Ship A0.3 *with* A0.1, not after.

### Phase A1 — Long-horizon execution `[3–4 weeks]`

| # | Item | Gap | Done when |
|---|---|---|---|
| A1.1 | Checkpointed task runner over a spec file | A7 | A killed run resumes mid-step, not from step start |
| A1.2 | Monitor loop: same session, interval, exit condition, cycle cap | A6/F | "Watch this PR" survives a gateway restart |
| A1.3 | Scheduled agent work (cron), timezone-aware | A6 | "Every weekday at 9, summarise open work" runs unattended |
| A1.4 | Patrol sweeper that **re-plans**, not just re-queues | A6 | A retried meeting is followed, closing the `kind == "code_task"` hole |

### Phase A2 — Scale-out `[5–7 weeks]`

| # | Item | Gap | Done when |
|---|---|---|---|
| A2.1 | Session-runtime interface; current impl behind it | A4 | No caller knows where a session executes |
| A2.2 | Subagent fan-out with per-task context switches | A3 | One turn researches three options in parallel |
| A2.3 | Dynamic sizing from host resources + learned cost | A3 | Cap adapts to the box without a config edit |
| A2.4 | Budget + risk-floor integration for fan-out | A3 | Fan-out cannot outrun the spend cap |
| A2.5 | Warm pool | A4 | New session starts in <1s |

**Do not start A2 before A0.** Parallel sessions without memory multiply the re-explaining
problem by the number of sessions — which is precisely their Stage 2 complaint.

### Phase A3 — Policy and distribution `[4–6 weeks]`

| # | Item | Gap | Done when |
|---|---|---|---|
| A3.1 | `PreToolUse`/`PostToolUse` hook plane | A5 | A deny rule is declared once, enforced everywhere |
| A3.2 | Migrate existing hand-written guards onto it | A5 | `run_on_host`'s confirmation is a declared policy |
| A3.3 | File-event hooks for agent-made changes | A5 | Lint/format/test run without prompting |
| A3.4 | Populate `BOSS_SOURCE_SHA`/`BOSS_BUILT_AT` at build | A8 | No install reports `unknown` |
| A3.5 | One-line installer + signed artefact channel | A8 | A stranger runs BOSS without reading compose |

---

## 6. The one thing to take away

Their whole argument reduces to a sentence worth pinning above this plan:

> *"Trust does not come from an agent saying it did the right thing but can come from history
> being able to track."*

BOSS already believes this more thoroughly than Kiro Crew does — receipts, audit events,
evidence requirements, verifier debate. What BOSS lacks is the **other** half of the same idea:
history the agent can *read back*, so it stops re-deriving, re-asking, and re-making the same
mistake. That is Phase A0, and everything else is downstream of it.
