# Kiro Crew → BOSS: Feature Gap Analysis

*Evidence gathered 2026-09-19. Companion to [`kiro-gap-architecture.md`](kiro-gap-architecture.md),
which covers runtime shape. This document covers **capabilities an operator can name and ask
for** — the things that show up in a demo, a changelog, or a competitive comparison.*

Sources: `kiro.dev/docs` (Specs, Steering, Hooks, MCP, Permissions, Custom agents, Agent
Skills, Powers, Cloud sessions, Compaction, Kiroignore, Checkpoints and rewind),
`src/kiro_crew/docs/index.md` in the cloned Apache-2.0 repo (the complete internal feature
inventory), and the `software-factory-1000-prs` blog. BOSS verified at `49b4fc45`.

---

## 1. Scoring key

| Mark | Meaning |
|---|---|
| ❌ | BOSS has nothing equivalent |
| ◐ | BOSS has a partial or adjacent mechanism |
| ✅ | BOSS has it, at parity or better |
| ⬆ | **BOSS is ahead** — Kiro Crew has no equivalent |

Priority reflects *value to BOSS's customer*, not fidelity to Kiro.

---

## 2. Feature-by-feature

### 2.1 Memory and learning

| Feature | Kiro Crew | BOSS | P |
|---|---|---|---|
| Cross-session preferences | `preferences.md`, auto-consolidated ~30 messages | ❌ | **P0** |
| Active-project context | `projects.md` | ❌ | **P0** |
| Daily history with graduated decay | 0–13d full → 14–60d first+count → 61–180d count → 181–365d on disk → pruned | ❌ | **P0** |
| **Lessons from corrections** | *"no, always run the frontend checks before calling a change done"* becomes a workspace-scoped rule | ❌ | **P0** |
| Per-session memory modes | persistent / incognito / temporary | ❌ | P2 |
| Memory visible and editable by the user | Dashboard pages | ❌ | **P1** |

This block is the single largest gap and the one their own blog says pays. **249 BOSS tables,
zero memory tables.**

The editable requirement is not cosmetic: a wrong lesson silently steering every future session
is a worse failure than having no lessons. BOSS should go further than Kiro here and put lessons
through the existing skill-review path — a lesson *is* a rule.

### 2.2 Skills and knowledge

| Feature | Kiro Crew | BOSS | P |
|---|---|---|---|
| Markdown skill packs | `SKILL.md` in a directory; nested; aux scripts alongside | ✅ specpack + authored skills | — |
| Always-on / on-demand / triggered loading | `always: true`, summary-then-load, `skills.max_triggered` | ◐ `load: on_demand` exists; no trigger matching | P2 |
| **Skills synthesised from repeated patterns** | *"Synthesize reusable skills from repeated patterns, then inspect, refine, or remove"* | ❌ | **P1** |
| Auto-generated skills need approval | Setting: whether a generated skill needs your approval | ⬆ BOSS's review model is stronger | — |
| Skill chosen by a small fast model | "Jev skill selection" — cheap model picks, falls back to trigger matching | ❌ | P3 |
| Knowledge library, semantic search over own docs | Yes, incl. generated artifacts | ◐ `knowledge_graph`, `knowledge_context` (packing/redaction, not search) | P2 |
| **Steering files** | `.kiro/steering/*.md`, YAML front-matter, four inclusion modes: `always`, `fileMatch` (glob), `manual` (`#name`), `auto` (name+description). Defaults: `product.md`, `tech.md`, `structure.md` | ◐ `platform_standards`, `mandate_standard` — DB-held, not file-scoped, no fileMatch | **P1** |

**Steering deserves attention.** BOSS's standards are mandated through the workforce and stored
in the database — better governance, worse ergonomics. Kiro's `fileMatch` mode
(`"components/**/*.tsx"`) means a convention loads *only* when relevant, which is how they keep
always-on context small. BOSS's equivalent would be scoping a standard to a path glob.

### 2.3 Autonomy and long-running work

| Feature | Kiro Crew | BOSS | P |
|---|---|---|---|
| **Task runner from a spec file** | `kirocrew run TASK.md`; also chat, dashboard, Slack, `task_run` MCP tool with `__inline__:` | ◐ workflows exist; no spec-file entry, no checkpoint resume | **P1** |
| Checkpoint + resume | "plans, executes, validates, retries, **resumes from checkpoints**" | ❌ | **P1** |
| **Monitor loops** | Same conversation wakes on an interval until an exit condition; deadline-preserving; 24-cycle default cap; wall-clock budget alternative | ❌ | **P1** |
| Goal control in the composer | 🎯 shows active loop, cycle number, countdown; set interval/cap/stop | ❌ | P2 |
| **Cron / recurring agent work** | Natural language → timezone-aware recurring job, delivered to a chosen surface | ❌ (BOSS `scheduling.py` is preemption, not cron) | **P1** |
| Reactive triggers | Authenticated inbound webhooks, messaging events | ◐ `webhooks`, `plane_webhooks` — not agent-turn triggers | P2 |
| Proactive "another pass" | AutoNudge, goal-loop skills | ❌ | P3 |
| Subagent fan-out from chat | `spawn_run` with `tasks[]`, per-task agent, context switches | ❌ | **P1** |
| Research campaigns | Multi-cycle scoping → adaptive execution → exportable report | ◐ `discovery_service` is close | P3 |

The monitor-loop detail worth copying exactly: **deadline-preserving intervals**. Your own
messages defer a due wake until your turn finishes but do **not** restart the clock, so a loop
stays on schedule in a conversation you are actively using.

### 2.4 Session experience

| Feature | Kiro Crew | BOSS | P |
|---|---|---|---|
| **Checkpoints and rewind** | Undo agent changes, or **fork a conversation** | ❌ | **P1** |
| Compaction | Documented strategy to keep long sessions productive | ❌ (0 files) | **P0** |
| Agent questions | Agent pauses mid-turn, asks a **clickable multiple-choice** question | ◐ `proposed_actions` are buttons, but the agent cannot *ask* | P2 |
| Queued-message editing | Edit, reorder, cancel a queued message before it runs | ◐ queue shipped; no edit/reorder | P2 |
| Cooperative stop | Cancel first, hard-kill only after a budget, so state survives | ◐ Stop shipped; not cooperative | P2 |
| Follow-up suggestions | Proposed next steps above composer: **start in a new git worktree**, add to session, skip | ❌ | P2 |
| Artifacts: version and revert | "Save, version, and revert generated UI and documents" | ◐ `artifacts` table, no versioning/revert | P2 |
| Kiroignore | Keep secrets/sensitive files away from the agent | ◐ redaction + sensitive-path checks | P3 |
| Warm pool | Pre-spawned processes for instant start | ❌ | P2 |
| Snapshot / restore | Portable state export for upgrade or machine migration | ❌ | P2 |
| Feature tips / intro videos | Personalised nudges toward unused features | ❌ | P3 |

**Checkpoints and rewind is the sleeper item.** "Fork a conversation" turns an agent session
into something you can explore from — try an approach, rewind, try another. For BOSS it maps
naturally onto workflow versions, which are already immutable.

### 2.5 Surfaces and reach

| Feature | Kiro Crew | BOSS | P |
|---|---|---|---|
| Messaging channels | **11**: Slack, Discord, Telegram, Teams, Webex, WeCom, WeChat, WhatsApp, Feishu, iMessage, + CLI | ◐ Slack, Mattermost, email adapters | P2 |
| Channel-neutral core | "a capability a channel lacks degrades gracefully rather than failing the turn" | ❌ | P2 |
| Desktop app | Electron shell, 3 update channels, hotkey, tray badge | ❌ | P3 |
| Web dashboard | ✅ | ✅ Console | — |
| CLI | `chat`, `run`, `cron`, `spawn`, `security` | ◐ `ops.py` is deploy-only | P2 |
| Multi-instance hub | Manage several remote gateways from one dashboard | ❌ | P3 |
| Mobile | Preview | ❌ | P3 |
| Browser control | Navigate, snapshot, click, type, screenshot | ❌ | P3 |
| Computer use | Drive native apps via accessibility layer; **off by default** | ❌ | P3 |
| Voice / streaming STT | Local Whisper or AWS Transcribe | ❌ | P3 |

I would resist most of this block. Eleven chat channels is a *consumer* adoption strategy; BOSS's
buyer is an organisation that wants an audit trail. The two worth taking are the
**channel-neutral core** (a design principle, cheap) and a **real CLI** (`chat`, `run`, `cron`,
`spawn` — an operator surface BOSS genuinely lacks).

### 2.6 Extensibility

| Feature | Kiro Crew | BOSS | P |
|---|---|---|---|
| MCP servers | ✅ | ✅ `mcp_registry`, `mcp_client`, `mcp_gateway` | — |
| **Hooks** (agent-action events) | 11 triggers incl. `PreToolUse` (blocking), JSON in `.kiro/hooks/`, regex matcher, `command` or `agent` action | ❌ (BOSS "hooks" are HTTP webhooks) | **P1** |
| Custom agents | Model, prompt, tools, MCP servers, avatar | ✅ roles + `agent_versions` | — |
| Powers | "Tools with built-in knowledge that activate on demand" | ◐ overlaps skills + capabilities | P3 |
| Installable Apps | Dashboard pages + scoped Gateway APIs + events + lifecycle hooks; **deny-by-default, every load audited** | ❌ | P2 |
| MCP Apps | Render interactive MCP output (diagrams, viewers, forms) in chat | ❌ | P3 |
| Inbound webhooks → agent turn | Named tokens, HMAC signing, reversible off switch, ephemeral sessions | ◐ webhooks exist, don't start turns | P2 |
| Configuration sync | Global steering syncs across installs | ❌ | P3 |

### 2.7 Security and governance — **where BOSS leads**

| Feature | Kiro Crew | BOSS |
|---|---|---|
| OS sandbox for agent process, AppArmor profile shipped | ✅ | ◐ `sandbox_provider`, `preview_sandbox_service` |
| Credential redaction on LLM output paths | ✅ | ✅ `redaction` |
| Denied-command rules at a `PreToolUse` gate | ✅ | ❌ — hand-written per call site |
| Secrets vault, `secret://` refs, refused to agent | ✅ | ✅ `secret_refs` + AES-GCM |
| Prompt-injection exfiltration protection | ✅ | ◐ |
| Audit log the agent cannot edit | ✅ | ✅ `events`, `audit` |
| **Risk tiers + manual-approval floor** | ❌ | ⬆ |
| **Approval receipts, dedupe keys, quota guards** | ❌ | ⬆ |
| **Multi-tenant orgs, entitlements, editions** | ❌ (owner-only by design) | ⬆ |
| **SAML / OIDC / SOC2 / break-glass** | ❌ | ⬆ |
| **Canary, staged rollout, rollback** | ❌ | ⬆ |
| **Verifier debate, mutation gate, visual gate** | ❌ | ⬆ |
| **FinOps reconciliation, spend caps** | ◐ acknowledged as an open problem | ⬆ |

Their blog names four mandatory controls: coordination, memory, **permission control enforced
at the host level not in prompts**, and an unedittable trail. BOSS satisfies three. The one it
misses is the `PreToolUse` gate — a principle BOSS already holds but implements ad hoc.

---

## 3. What actually makes them popular

Worth separating from the feature list, because the answer is mostly **not** features.

1. **Zero-friction install onto your own hardware.** One curl line, or a signed desktop app with
   auto-update. Local-first is also a privacy story: *"Conversation history, memory, and
   knowledge indexes remain on that host."*
2. **Apache-2.0 with real governance.** `GOVERNANCE.md`, `MAINTAINERS.md`, `TENETS.md`,
   `SECURITY.md`, semgrep, jscpd, a vulnerability-exception schema. It reads like a project you
   can contribute to — hence **~570 open PRs and ~500 contributors in nine weeks**.
3. **Visible velocity.** Daily `insider` releases. Contributors see their work ship.
4. **A concrete, checkable claim.** "1,000 PRs in 7 days, every one through CI and review" is a
   better ad than any feature list.
5. **They meet people where they already are** — 11 chat surfaces.
6. **The pitch is continuity, not intelligence.** *"Most agent sessions end when the chat closes."*
   Everyone has felt that. Nobody markets a context window.

**The lesson for BOSS's OSS initiative:** we are not losing on capability. BOSS does more than
Kiro Crew on almost every governance axis. We lose on *install friction, contribution surface,
and a story told in one sentence.*

---

## 4. Phased plan (features)

Aligned with the architecture phases so the two can be executed as one programme.

### Phase F0 — "It remembers" `[with A0, 3–4 weeks]`

The demo sentence: *"Tell BOSS once, and it holds."*

| # | Feature | Acceptance |
|---|---|---|
| F0.1 | Lessons from corrections | Say "always run frontend checks first" → the next run does |
| F0.2 | Preferences + project memory | A new thread opens already knowing the project |
| F0.3 | Memory page: view, edit, delete, scope | A wrong lesson is removable in one click |
| F0.4 | Lesson review gate | A lesson becomes binding only after review |
| F0.5 | Compaction that preserves goal + ledger | A 200-turn thread still knows its objective |

### Phase F1 — "It keeps working" `[with A1, 3–4 weeks]`

| # | Feature | Acceptance |
|---|---|---|
| F1.1 | Monitor loops with exit condition + cycle cap | "Watch this PR" survives restart, stops on merge |
| F1.2 | Cron in natural language, timezone-aware | "Every weekday at 9, summarise open work" |
| F1.3 | Checkpointed task runner over a spec | Killed run resumes mid-step |
| F1.4 | Goal control in the composer | Loop state and countdown visible, stoppable |
| F1.5 | Agent questions (clickable MCQ mid-turn) | Agent asks instead of guessing |

### Phase F2 — "It scales and extends" `[with A2/A3, 5–7 weeks]`

| # | Feature | Acceptance |
|---|---|---|
| F2.1 | Subagent fan-out with context switches | One turn researches three options in parallel |
| F2.2 | Hooks plane (`PreToolUse` first) | A deny rule declared once, enforced everywhere |
| F2.3 | Steering with `fileMatch` scoping | A convention loads only for paths it governs |
| F2.4 | Skill synthesis from repeated patterns → review | A recurring fix is offered as a skill |
| F2.5 | Checkpoints and rewind / fork a thread | Explore an approach, rewind, try another |
| F2.6 | Artifact versioning and revert | A regenerated doc keeps its history |

### Phase F3 — "It reaches further" `[4–6 weeks, optional]`

| # | Feature | Acceptance |
|---|---|---|
| F3.1 | Channel-neutral core; degrade not fail | A channel lacking buttons still completes the turn |
| F3.2 | Operator CLI: `chat`, `run`, `cron`, `spawn` | BOSS is usable without the console |
| F3.3 | Inbound webhooks that start an agent turn | CI failure opens an investigation unprompted |
| F3.4 | Installable apps, deny-by-default + audited | A team ships a page without forking BOSS |
| F3.5 | Snapshot / restore | State moves between machines |
| F3.6 | One-line install + signed artefacts | A stranger runs BOSS without reading compose |

---

## 5. Recommendation

**Do Phase 0 and Phase 1. Treat Phase 2 as the real roadmap. Be sceptical of Phase 3.**

The gap that matters is not that BOSS lacks eleven chat channels. It is that **BOSS forgets.**
Every session starts cold, every correction is spent the moment it is given, and every long
thread eventually loses the goal it started with. Kiro Crew's entire pitch is that it does not
do those things, and their own advice is that the memory step *"is the one that pays."*

Everything BOSS is genuinely better at — governance, evidence, approvals, multi-tenancy — gets
*more* valuable once the thing being governed stops repeating itself.
