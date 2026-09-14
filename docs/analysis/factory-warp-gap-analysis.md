# BOSS vs Factory.ai vs Warp — P0 gap analysis for the OSS MVP

**Date:** 2026-09-13 · **Revised:** 2026-09-14 (revalidated; Phase 0 implemented — see §10)
**Status:** P0-2, P0-3a and P0-4a implemented on branch `ai/codex/p0-native-coding-agent`; the rest planned
**Scope decision (2026-09-14):** the v1 execution substrate is a **local container**. Firecracker/microVM
is out of scope for the OSS MVP and is no longer treated as a gap — only as a claim that must stay frozen.
**Plane:** epic BOSS-406, children BOSS-407…BOSS-417
**BOSS baseline:** this repository, `main` @ `00c18aa4` (`v0.14.0-643`), read from code — not from docs, which overstate maturity in several places noted below.
**Competitor baseline:** `docs.factory.ai` (~45 pages, full sitemap crawl) and `docs.warp.dev` (~53 pages), fetched 2026-09-13. Source URLs inline.
**Companion:** [`emergent-gap-analysis.md`](./emergent-gap-analysis.md) — same method, different competitor (Emergent, a consumer app factory). That analysis is still valid but aims at a different buyer; see §2.

---

## 0. Verdict

1. **The category moved under us.** In August 2026 Warp shipped **Warp Factories** — marketed verbatim as *"a software factory that automates and orchestrates software development."* Factory.ai's product is literally called the **Software Factory**. Both converged on BOSS's exact architecture: *an orchestration control plane that drives someone else's coding agent.* We are no longer entering an empty category.
2. **Everything BOSS owns *around* the model is genuinely good** — arguably the best of the three on governance, evidence and auditability. The phase state machine, receipts, budget-as-a-gate, role separation, and the per-turn/per-call ledger have no real analogue at either competitor.
3. **Everything BOSS needs to *be* a coding agent is borrowed or missing.** The default coding lane shells out to the proprietary `claude -p` CLI on a Max plan. Remove that binary and quality collapses to `NativeCodeAgent` — a whole-file-JSON-rewrite loop with no tools, no exploration, and no iteration on file state.
4. **The differentiators are off by default.** `code_gate_never_block=True`, `code_review_panel_size=1`, `microvm_claims_frozen=True`. A first-time evaluator running stock config sees none of the governance story we lead with.
5. **The OSS artifact does not exist yet.** The repo is private, the `curl | bash` URL in the README 404s for the public, CI is red on `main`, and the licensing decision (BOSS-300) is still formally open while Apache-2.0 files already ship.
6. **The wedge is still there, and it is narrow and specific:** Warp's control plane is *always Warp's*, even self-hosted. Factory is proprietary and enterprise-licensed. **Nobody ships an open-source, self-hostable software-factory control plane.** That is the only positioning where BOSS starts ahead rather than behind — and it is worth more than feature parity.

---

## 0.1 P0 tracker

Live status. `◻` not started · `◐` in progress · `✅` done and tested · `⛔` blocked.
Branch `ai/codex/p0-native-coding-agent`. Plane epic **BOSS-406**.

| # | Item | Plane | Status | Evidence |
|---|---|---|---|---|
| P0-1 | Public artifact: repo flip, SECURITY.md, root community kit, BOSS-300 licensing | — | ⛔ | Blocked on a product + legal decision, not code. Out of scope for this branch. |
| P0-2 | CI green: missing make target, Python 3.9→3.11, engine unlinted | BOSS-412 | ✅ | Target added; Python pinned; real-bug gate over `engine/ apps/`. Found 4 live `NameError`s. |
| P0-2b | `make lint` passes | BOSS-418 | ✅ | **Green, both halves, rc=0.** Python: ruff.toml + 14 unused imports. TypeScript: all 24 `tsc` errors were test fixtures drifted from their types while vitest (no typecheck) kept passing. |
| P0-3a | Engine-owned tool-calling coding agent | BOSS-407 | ✅ | `code_agent_tools.py`; 39 tests incl. batching + turn-cap latency guards |
| P0-3b | Harness adapter registry | BOSS-409 | ✅ | `harness_registry.py`; 21 tests, full parity with the old `if/elif` |
| P0-4a | `AGENTS.md` / `CLAUDE.md` ingestion | BOSS-408 | ✅ | `load_repo_guide()`; 5 tests |
| P0-4b | Semantic retrieval / repo index | — | ◻ | Deferred by design: exploration tools reduce the need. Measure with P0-I before building it. |
| P0-5 | Container command execution + egress control | BOSS-410 | ◐ | `container_runner.py`, **live-verified**: egress blocked, non-root, host-visible writes, ~64ms/command, 1.5s container start once per task. **Opt-in** (`BOSS_SANDBOX_COMMAND_RUNTIME=docker`) until a live run on a real repo is recorded. 15 tests. |
| P0-6 | Autonomy levels + allow/deny/blocklist at the agent boundary | BOSS-411 | ✅ | 18 tests. Blocklist holds at every level, resolves `/bin/rm`, `./rm`, `RM`. |
| P0-7 | Honest Community defaults | BOSS-413 | ✅ | **Premise was wrong** — see §10.7. The defect was the doc, not the default. Doc corrected; 7 tests pin doc claims to runtime defaults. |
| P0-8 | Streaming turns + headless `boss code … -o json` | BOSS-414 | ✅ | `boss-engine code`, 3 output modes, real exit codes; 14 tests |
| P0-9 | Time-to-first-value ≤ 5 min | — | ◻ | Needs pre-built images; partly blocked by P0-1 |
| P0-10 | Hunk-level review feedback loop | — | ◻ | Largest remaining product gap vs Warp |
| P0-I | Brownfield multi-file bake-off task | BOSS-415 | ✅ | 14-file fixture with decoys, staged deterministically; its own suite verified green; 11 tests |
| — | OpenAPI route drift (197 routes) | BOSS-416 | ◻ | ~6,300-line generated diff; own PR |
| — | Router self-correction paths were dead | BOSS-417 | ✅ | 6 tests |
| — | Console feeds CORS-blocked — 21 areas silently empty | BOSS-420 | ✅ | Same-origin proxy; API key off the wire. 10 tests. |
| — | Screen checker never covered `/` | BOSS-421 | ✅ | 9/9 green while the console was unusable. 4 tests. |
| — | Console 5–10s TTFB on every area | BOSS-422 | ◻ | Root cause is the documented `shows()` trade |
| — | 42 buttons with no accessible name | BOSS-423 | ◻ | Plus fonts not matching the console's own spec |
| — | `github` area has no v2 destination | BOSS-424 | ◻ | Product decision; left failing on purpose |
| — | Advisory mode ships a regression | BOSS-419 | ◻ | Found while writing P0-7 tests. Recorded as a **strict xfail**, not fixed: adding a blocking condition changes delivery behaviour. |

**Regression budget.** The coding pipeline has had significant latency work
(`_PARALLEL_TOOLS_RULE`, idle-stall detection, concurrent review panel, process-group
kill, prompt-cache `--resume`). No P0 change may regress it. Every change below is
checked against §11.

---

## 1. What each product actually is

### Factory.ai — "The Autonomy Stack for Enterprise Teams"

One agent core (the **Droid**) exposed through many surfaces, with the CLI primary. Enterprise-first positioning; named customers include NVIDIA, EY, Adobe, Palo Alto Networks, Adyen, Blackstone.

| Dimension | Factory |
|---|---|
| Primary surface | **Droid CLI** (REPL + `droid exec` headless). SDKs shell out to `droid` on PATH |
| Other surfaces | Desktop app (Mac/Win, no Linux), web + mobile with live shareable session URLs, VS Code/Cursor/Windsurf/JetBrains/Zed, Slack, Linear, GH Actions / GitLab CI, REST API, TS + Python SDKs |
| Long-horizon unit | **Missions** — orchestrator → milestones → features → fresh worker sessions + validators |
| Execution | Local by default; **Droid Computers** = persistent 4 CPU / 8 GB cloud VMs *or* bring-your-own-machine over an outbound-only relay |
| Isolation | **Kernel-enforced**: macOS Seatbelt, Linux bubblewrap + seccomp, HTTP/SOCKS **egress filtering proxy** |
| Distinctive idea | **Agent Readiness** — a 1–5 maturity model scoring whether *your repo* is ready for autonomy, across 9 pillars, with `/readiness-report` **and `/readiness-fix` that implements the remediation**. Missions require Level 4+ |
| OSS | Core proprietary. SDKs, `droid-action`, ESLint plugin, VS Code extension are open |

The single most useful number in the whole research pass, from their Missions post: **median mission ≈ 2 h; 65% > 1 h; 37% > 4 h; 14% > 24 h; longest 16 days.** And they are candid that *"serial execution with targeted parallelization has worked better than broad parallelism"* — the category leader has **not** solved long-horizon orchestration.

### Warp — "The Open Platform for Automating Development"

Four layers: Terminal (open-sourced Apr 2026, AGPLv3 + MIT UI crates, ~65k stars — **client only**, server proprietary) → Warp Code (editor/LSP/diffs) → Automation Platform (cloud agents, ex-"Oz") → **Factories**.

| Dimension | Warp |
|---|---|
| Factory model | **Foreman + 4 specialists**: Triage → Spec → Implement → Review. Foreman skips stages adaptively |
| Human checkpoints | Exactly three: spec approval, clarification questions, **merge is always human** |
| Factory-as-code | `factory.yaml` (`v1alpha1`) + `agents/ automations/ runners/ benchmarks/ scorers/ skills/ webhooks/`, GitHub-backed with a `warp/factory-config` PR validation check |
| **Multi-harness** | First-class: every agent sets `harness.type: oz \| claude \| codex`. They recognise 15+ third-party CLI agents |
| Measurement | **Scorers** (LLM-judge, sample rate %), **Benchmarks** (model × runner on *your* tasks), **Self-improvement** (fires at 25 unreviewed failures or a 7-day-old one; opens PRs against your code *or the factory definition itself*) |
| Headline metrics | **Autonomy %** (merged PRs needing no human code push), **PR cycle time**, **cost per PR**. Flagship proof: **$80 → $30 per PR** |
| Execution | Warp-hosted sandboxes, or self-hosted `oz-agent-worker` on Docker / K8s / direct (Linux + glibc only) |
| OSS | Terminal client only. **Control plane always proprietary and always Warp-hosted** |

### BOSS — governed autonomous software factory

| Dimension | BOSS |
|---|---|
| Primary surface | **MCP server (86 tools)** + Mission Control console + HTTP API. The CLI is thin |
| Coding unit | Governed code task: `standards → spec → plan → review_plan → write_tests → implement → run_tests → review_panel → check_tests → final gate → PR` |
| Distinctive idea | **Separation of duties as an enforced control** — tester ≠ coder, reviewer ≠ coder, the implementer *cannot edit the tests*, BOSS re-runs the tests itself and never trusts the agent's self-report |
| Governance | Receipts per effect, budget quote→reserve→consume, decisions with risk tiers, dual-control at high risk, replay + compliance evidence bundles, data-boundary classification |
| Execution | Local subprocess in the worker container. microVM is a scaffold with claims frozen by default |
| OSS | Apache-2.0 on disk; repo private; public flip unapproved |

---

## 2. The category moved — and that changes the target

The `emergent-gap-analysis.md` written in August compared BOSS to a **consumer app generator** (prompt in, deployed app out) and concluded the critical gaps were hosting, domains, and a way to pay. That was the right read *for that competitor*.

Factory and Warp are a different animal: **repo-native SDLC automation for teams that already have a codebase.** Ticket → spec → PR, inside your existing GitHub org, with your existing CI. For that buyer, managed hosting and custom domains are irrelevant; what matters is whether the agent can navigate a 200k-line repo, whether it can be trusted to run commands, and whether a PR arrives that a human will actually merge.

**These two competitor sets pull in opposite directions and BOSS cannot chase both.** That is decision D1 in §8.

This document assumes the repo-native target, because that is what "software factory" now means in the market and what an OSS launch audience will evaluate BOSS against.

---

## 3. Capability matrix

Legend: ✅ real and competitive · ◐ present but materially weaker · ○ absent · 🔒 proprietary-only at that vendor

### 3.1 The coding agent itself

| Capability | Factory | Warp | BOSS | Evidence (BOSS) |
|---|---|---|---|---|
| Own tool-calling agent loop | ✅ Droid | ✅ Warp Agent | ○ | No BOSS-authored loop. `ClaudeCodeAgent` shells out to `claude -p` (`engine/boss_engine/services/code_agent.py:526`) |
| Fallback lane without a proprietary CLI | ✅ same droid, any model | ✅ same agent, any model | ◐ **whole-file JSON rewrite, no tools** | `NativeCodeAgent._EDIT_SYS` at `code_agent.py:1039`; apply loop `:1135`; max 4 iterations `:1197` |
| Multi-harness (run Claude Code / Codex / others as configured agents) | n/a (owns the agent) | ✅ `harness.type: oz\|claude\|codex` | ◐ hardcoded lane switch | `_build_code_agent` at `code_agent.py:1482` |
| File edit / patch tool | ✅ | ✅ | ◐ borrowed from the CLI only | `--allowedTools Read,Edit,MultiEdit,Write,Glob,Grep,Bash,TodoWrite` (`config.py:887`) |
| Repo exploration (grep/glob/read as agent tools) | ✅ | ✅ | ◐ Claude lane only; native lane has none | as above |
| Semantic codebase index | ○ (deliberate — privacy) | ✅ Codebase Context, embeddings, ≥5k files, multi-repo | ○ | `repo_retriever.py` is **113 lines of lexical overlap**; its own docstring: *"a semantic/embedding backend is the follow-on"* |
| Repo instruction file (`AGENTS.md`) | ✅ AGENTS.md + CLAUDE.md, 80k chars, nested | ✅ AGENTS.md (WARP.md legacy) + `.cursorrules` + CLAUDE.md + `.claude/skills/` | ○ **dead code** | `repo_guide` declared `code_agent.py:314`, injected `:550`, **never populated** — the only construction site `code_task_service.py:1752` omits it |
| Context compaction as configurable behaviour | ✅ `compactionTokenLimit`, `compactionModel`, `/compress`, `PreCompact` hook | ✅ auto-summarise + context meter + breakdown | ○ delegated to the CLI | fresh process per stage is the only mechanism |
| Session resume / fork / rewind | ✅ `--resume`, `--fork`, `/tree`, `/rewind-conversation` | ✅ `/fork`, `/fork-from`, `/rewind`, `--resume` | ◐ `--resume` for repair rounds only | `code_agent.py:659` |
| Streaming output to the user | ✅ | ✅ | ○ | SSE/WS exist only for preview logs (`apps/api/app/main.py:9684`, `:9730`). Runs are poll-only |
| Subagents | ✅ `.factory/droids/*.md`, light/medium/heavy routing (no recursion) | ✅ `/orchestrate`, one level deep, durable message bus | ◐ concurrent review panel only | `ThreadPoolExecutor` at `governed_code_task_service.py:3069` |

### 3.2 Safety, permissions, isolation

| Capability | Factory | Warp | BOSS | Evidence (BOSS) |
|---|---|---|---|---|
| Autonomy ladder | ✅ 4 levels (Off/Low/Medium/High), Ctrl+L cycles live | ✅ 6 permission dimensions × 3 levels, per Agent Profile | ○ at the agent boundary | Governance is at the *workflow* level (decisions/gates), not per tool call |
| Command allow / deny / **block**list | ✅ blocklist can never run even under `--skip-permissions-unsafe`; resolves wrapper shells, absolute paths, command substitution | ✅ denylist beats allowlist and beats "agent decides"; team denylist unbypassable | ◐ argv allowlist for *validation* commands only | `sandbox.run` `sandbox.py:365`, allowlist `config.py:645`. The coding agent's own Bash is unconstrained |
| Kernel-level sandbox | ✅ Seatbelt / bubblewrap + seccomp | ✅ Docker / K8s, non-root | ○ | `LocalSubprocessProvider` hardcoded at `governed_code_task_service.py:1493` — a temp dir inside the control-plane container |
| Network egress policy | ✅ HTTP/SOCKS filtering proxy | ◐ published CIDRs to allowlist | ○ | `config.py:889` states plainly: *"network egress is the deployment's responsibility"* |
| Secret isolation from the agent | ✅ 30-day auto-rotating OAuth, `${ENV}` expansion, Droid Shield blocks secrets at commit | ✅ client-side encrypted, write-only, never in logs | ✅ **strong** | `agent_env.py` deny-by-default allowlist with a NEVER set — written after the 2026-09-02 incident where an agent's test suite truncated 204 production tables |
| Multi-backend secret store | ◐ | ◐ | ✅ **ahead** | `secret_backends.py` — env / file / platform / **local_vault (AES-GCM)** / **HashiCorp Vault** / database |

### 3.3 Verification and gates

| Capability | Factory | Warp | BOSS | Notes |
|---|---|---|---|---|
| Independent review agent | ✅ `/review`, P0–P3 severity, CI action, auto-approves clean PRs | ✅ Review agent — **explicitly no merge authority** | ✅ severity-calibrated refute panel | BOSS ahead on enforcement |
| Security review | ✅ STRIDE + OWASP Top 10 + OWASP LLM Top 10 + supply chain | ○ | ✅ dedicated pass | roughly level |
| Test-first, author ≠ tester | ○ | ○ | ✅ **unique** | implementer cannot touch the tests; BOSS re-runs them itself |
| Mutation testing | ○ | ○ | ◐ **built but unwired** | `mutation_gate.py` (deterministic AST) reachable only from `apps/api/app/main.py:10116`; the pipeline uses an LLM-planted mutant (`governed:3131`) |
| Merge gate that fails closed | ○ | ○ **no merge gates at all** | ◐ **off by default** | `code_gate_never_block: bool = Field(default=True)` (`config.py:841`) |
| UI / visual verification | ✅ agent-browser + a11y tree + screenshots; **video proof via Remotion**; `/demo`, `/verify` | ✅ Computer Use, **up to 10 screenshots + video auto-attached to the PR** | ◐ shallow | `visual_gate.py` is a 126-line HTML string rubric — no screenshots, no vision model |
| Accessibility checks | ✅ a11y tree capture | ○ | ○ | rubric *fields* only; no axe-core |
| Quality measured over your own tasks | ◐ benchmarks page | ✅ **Scorers + Benchmarks + Self-improvement PRs** | ◐ internal only | `bakeoff.py` (267 L) + `verifier_meta_eval.py` are real but not a product surface |
| Outcome metrics | ◐ Agent Effectiveness (private preview) | ✅ **autonomy %, cost/PR, cycle time** on the dashboard | ○ | BOSS has the richest underlying data of the three and surfaces none of it this way |

### 3.4 Governance, audit, enterprise

| Capability | Factory | Warp | BOSS |
|---|---|---|---|
| SSO SAML/OIDC | ✅ WorkOS | ✅ WorkOS | ✅ **in the free tier** (`services/saml.py`, `services/oidc.py`) |
| SCIM | ✅ full directory sync | ◐ **JIT only, no group sync**; deprovisioning doesn't revoke live sessions | ✅ SCIM 2.0 CRUD (`main.py:4742-4950`) |
| RBAC | ◐ exactly 3 roles | ◐ 3 roles | ✅ permission-scoped role keys + risk-tier approver mapping |
| Hierarchical org policy | ✅ **best in class** — hard controls flow org→down and can only be tightened; session defaults flow user→up inside org ceilings; ~14 org-lockable keys | ✅ admin panel, three-tier enforcement | ◐ project/org members, no settings hierarchy |
| Audit log | ◐ documented, **retention + SIEM export undocumented** | ○ **no audit product** — only an Early-Access Analytics `events` endpoint | ✅ **ahead** — receipts, `audit/export`, replay bundles, compliance evidence bundles, data-boundary classification |
| Run replay / trace | ○ **no replay viewer** | ◐ run timeline | ✅ **ahead** — `read_run_turns`, `explain_run`, `get_run_activity`, `llm_calls` ledger with per-call cost |
| Budget as an enforcing policy | ○ credit multipliers per model | ○ opaque credit buckets | ✅ **unique** — quote→reserve→consume, parent/child envelopes, colour states that defer or halt work |
| Self-hostable **control plane** | ◐ airgap, enterprise licence, proprietary | ○ **control plane is always Warp's** | ✅ **the wedge** |
| Compliance certifications | ✅ SOC 2 II, ISO 27001, **ISO 42001**, EU residency | ✅ SOC 2 II, ZDR with all providers | ○ none |
| Self-instrumentation (OTel) | ✅ OTLP metrics + optional content spans to *your* collector only | ◐ Analytics API | ○ zero `opentelemetry` in any requirements file |

### 3.5 Surfaces, DX, extensibility

| Capability | Factory | Warp | BOSS |
|---|---|---|---|
| Headless structured-output exec | ✅ `droid exec -o json\|stream-json\|stream-jsonrpc`, exit codes, `--restrict-tools` | ○ **CLI is TUI-only; automation goes through API/SDK** | ◐ `boss-engine call create_code_task --args '{…}'` + poll |
| Interactive CLI / REPL | ✅ | ✅ | ◐ `chat` exists but drives the *ops* agent, not coding |
| Diff review UX | ✅ inline comments on diffs in app + mobile approve | ✅ **hunk-level accept/refine/edit/revert + batched inline comments fed back to the agent** (works with third-party harnesses) | ○ |
| IDE plugins | ✅ VS Code/Cursor/Windsurf/JetBrains/Zed | ✅ it *is* the terminal | ○ (MCP server covers the inverse case) |
| Slack / Linear / Jira trigger → run | ✅ | ✅ | ◐ work-tracker *adapters* (create issue) + Plane webhooks → code tasks |
| Repo hosts | GitHub, GitHub ES, GitLab (self-hosted). **No Bitbucket, no Azure DevOps** | GitHub, GitLab, **Bitbucket, Azure DevOps** | GitHub (deep, 1013 L adapter), Bitbucket (thin, 166 L), **no GitLab** |
| MCP client (consume external servers) | ✅ stdio + HTTP + SSE, 3 config scopes, org `mcpPolicy` | ✅ stdio + HTTP + SSE, personal/project scopes, env scrubbing on share | ◐ **HTTP JSON-RPC only, 69 lines**; `mcp_registry.py:44` rejects non-http schemes — **cannot launch stdio servers** |
| MCP server (be driven by other agents) | ○ | ✅ Factory MCP, 18 tools | ✅ **86 tools — best of the three** |
| Hooks | ✅ 9 events (PreToolUse … SessionEnd), `deny`/`ask`/`updatedInput` | ○ | ○ |
| Plugin / tool SDK | ✅ plugins + **internal marketplaces**, provenance + version pinning | ◐ skills, skills-as-agents | ○ all 86 tools hardcoded in `tools/builtin.py`; no entry-point discovery |
| Skills (SKILL.md) | ✅ progressive disclosure, `/create-skill` | ✅ cross-compatible with Claude Code skills | ◐ format supported, **only 3 skills exist** |
| Declarative config-as-code | ✅ `.factory/` tree, committed | ✅ `factory.yaml` + directory tree, **PR-validated** | ◐ specpack YAML lives in *our* monorepo, not the customer's repo |
| SDKs | ✅ TS + Python | ✅ TS + Python | ◐ `packages/api-client` (TS) |

---

## 4. P0 — what must ship before an OSS MVP is credible

P0 test used here: **without this, a competent engineer who installs BOSS on day one either (a) cannot run it, (b) gets a worse result than free alternatives they already have, or (c) cannot safely point it at a real repository.**

Ordered by whether they block the one *after* them.

---

### P0-1 · There is no public artifact to launch

**Gap.** The `curl -fsSL https://raw.githubusercontent.com/seanheiney/boss/main/install.sh | bash` in `README.md:13` and `docs/community/QUICKSTART.md:17` resolves to a **private** repository. `install.sh:14` clones the same private URL. Meanwhile `engine/COMMUNITY_READINESS.md` lists **nine unchecked hard gates**, including a full-history secret scan, a dependency/licence audit, and — explicitly — *"SECURITY.md blocks publication until an actual reporting destination is provided."*

Root-level community files do not exist: no `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `CODEOWNERS`, issue templates, or PR template at the repo root. They exist **only** under `engine/`, staged for a future extracted repo.

And BOSS-300, the licensing/OSS-boundary decision, is **still formally open** (`docs/planning/oss-readiness/DECISION-oss-licensing-strategy.md`) while `LICENSE` is already stock unmodified Apache-2.0 and `engine/pyproject.toml` already declares it. The record and the artifact disagree.

**Why P0.** Every other item is unobservable until someone can install the thing.

**Shape of the fix.** Record BOSS-300. Complete the engine extraction (Phase 4 of the engine README — the only phase still open). Move the community kit to the root of whatever repo goes public. Give `SECURITY.md` a real inbox. Re-point `install.sh` and the README at the public repo.

---

### P0-2 · CI on `main` is red, and will be red for the first external PR

**Gap.** Verified directly:

```
.github/workflows/ci.yml:40   python-version: "3.9"     # Makefile:29 asserts sys.version_info >= (3, 11)
.github/workflows/ci.yml:51   run: make openapi-validate

$ make -n openapi-validate
make: *** No rule to make target `openapi-validate'.  Stop.
```

The Makefile target is `openapi-live-qa-parity`. Step 3 of every push and PR build cannot pass.

**Why P0.** On flip day the first outside contributor's PR shows a red X caused by us. There is no faster way to lose an early contributor. Secondary: `make lint` runs `ruff` over `apps/api apps/worker` only — the 195k-LOC `engine/` tree, including the 5,468-line `tools/builtin.py`, is **unlinted**, and there is no mypy or pyright anywhere.

**Shape of the fix.** Fix the target name; pin Python 3.11+. Extend `ruff` to `engine/`. Add one job that matters more than all the unit tests: **run a real code task against a fixture repo end-to-end** with a stubbed model, and assert a diff and a gate row came out.

---

### P0-3 · The open lane cannot write code competitively

**Gap — this is the single most important item in this document.**

> **Correction (2026-09-14).** An earlier draft said quality "collapses" without the Claude CLI.
> That was too strong and it obscured the real shape. BOSS's *orchestration* is genuinely
> model-agnostic and is what the 46-run VAG study measured: BOSS re-runs the tests itself, the
> tester is not the coder, and failed verification loops back to `implement`. None of that
> depends on Anthropic, and DeepSeek-V3 as implementer scored 3/5 — equal to frontier-solo, at
> −24% cost. What is *not* model-agnostic is the per-stage agent: the pipeline calls `run_role`
> exclusively (never `NativeCodeAgent.run`, which is dead on the governed path), so one step was
> **one completion** on the native lane against **up to 90 tool-using turns** on the Claude lane.

`config.py` defaults `code_agent_engine` to `claude_code`. That lane shells out to the proprietary Claude Code CLI with a Max-plan OAuth token (`code_agent.py:526-677`). Two problems: it is a dependency an OSS user may not have, and `claude_cli_adapter.py:13-16` says in our own words that it is *"a prototype/dogfooding convenience, not a production inference path."*

The fallback, `NativeCodeAgent`, is not an agent. It packs a lexically-ranked slice of the repo into a prompt and asks for `{"files":[{"path","content"}]}` — **whole-file rewrites** — then loops at most four times against test output (`code_agent.py:1039`, `:1135`, `:1197`). There is no read tool, no grep, no bash, no incremental edit, no exploration. On any repo larger than a toy it will rewrite files it has only partially seen.

There is also **no Anthropic API lane at all** — no `anthropic` dependency anywhere in the tree — and no Gemini lane. Lanes are: `claude_code` (CLI), `native`/`openai`, `azure`, `openrouter`, `groq`, `deepseek`, `ollama`, `vllm`.

**What the competitors do.** Both made the same strategic call BOSS did — *be the control plane, rent the agent* — but they executed it properly. Warp makes the harness a **first-class configured field**: every factory agent declares `harness.type: oz | claude | codex`, and they recognise 15+ third-party CLI agents. Factory owns a real droid and exposes it everywhere identically.

**Why the bake-off could not see it.** `evals/bakeoff/tasks.json` says it plainly: *"Deterministic,
stdlib-only Python tasks so pass/fail measures the model+harness, not the environment."* All five
tasks are **"Create `<newfile>.py` with a function…"** — slugify, token_bucket, csv_report, md_toc,
interval_merge. Greenfield, single file, nothing to navigate. On that shape a whole-file JSON write
is the *natural* output and having no grep/read tools costs exactly zero. The suite measures the one
task shape where the defect is invisible. That is a coverage gap in the eval (BOSS-415), not an error
in the study.

**Why P0.** This is the (b) case in the P0 test. An evaluator with an OpenAI key and no Claude Max subscription gets output strictly worse than running `aider` for free.

**Shape of the fix — do both halves.**
- **(a) A real tool-calling loop on the OpenAI-compatible lane.** `read_file`, `write_file`, `edit_file` (string replacement, not whole-file), `glob`, `grep`, `bash`, `list_dir` — bounded by the existing `agent_env` allowlist and the existing `sandbox.run` argv allowlist. This is the no-dependency default and it is the difference between BOSS being evaluable and not.
- **(b) A declared harness contract.** Turn the hardcoded `_build_code_agent` switch (`code_agent.py:1482`) into a registered adapter interface, and ship adapters for `claude_code`, `codex`, `opencode`, `aider`. Then the governed pipeline's value proposition becomes *"bring whichever agent you already trust; BOSS governs it"* — which is both true and the only story that survives a frontier-model release cycle.

---

### P0-4 · The agent cannot orient itself in a real repository

Three separate defects that compound:

**(a) `AGENTS.md` is dead code.** `RepoContext.repo_guide` is declared at `code_agent.py:314` with the comment `# CLAUDE.md / AGENTS.md, if ingested`, and injected into the prompt at `:550` as `REPO GUIDE:`. The only place a `RepoContext` is constructed — `code_task_service.py:1752` — never sets it. The prompt branch cannot fire. Meanwhile `AGENTS.md` has become the cross-tool convention: Factory reads `AGENTS.md`/`agents.md`/`CLAUDE.md` with 80k-char limits and nested per-subtree files; Warp reads `AGENTS.md`, legacy `WARP.md`, `.cursorrules`, `CLAUDE.md`, and `.claude/skills/`. **This is roughly a day of work and it is currently zero.**

**(b) Retrieval is 113 lines of lexical scoring.** `repo_retriever.py` ranks by path-token overlap ×5 plus capped content overlap, pins design-system and Terraform files, and stops. Its own docstring concedes the point. `search_code` is a Python `re` walk. There are zero embeddings, zero vector stores, zero index anywhere in `engine/boss_engine`.

Note the asymmetry carefully: **Factory also has no repo index** — deliberately, because their privacy story forbids it (*"does not upload or index your codebase into a remote datastore"*). They get away with it because their agent can *explore* with Read/Grep/Glob. Warp does index (Codebase Context, embeddings, ≥5k files, multi-repo). BOSS's native lane has **neither** exploration nor an index — the worst of both positions.

**(c) `code_agent_context_files` defaults to 14.** Fourteen files, head-truncated, chosen lexically, is the entire world the native agent sees.

**Why P0.** Fixing P0-3(a) fixes (b) and (c) for free — an agent that can grep does not need a good pre-packer. Do (a) regardless; it is nearly free and it is what every evaluator will check first.

---

### P0-5 · Isolation is a temp directory inside the control plane

**Gap.** `make_sandbox_provider` returns `LocalSubprocessProvider` even when `BOSS_SANDBOX_PROVIDER=microvm`, because `microvm_claims_frozen` defaults to `True` (`config.py:1056`). The governed pipeline hardcodes it anyway (`governed_code_task_service.py:1493`). The in-repo Go sandbox service is an honest scaffold — `firecrackerLive: false // this scaffold never enables live Firecracker`. So a code task runs as a subprocess in a `mkdtemp` inside the API/worker container, with **full network egress** by explicit design (`config.py:889`).

We already know what this costs: `boss_prod_wipe_2026_09_02` — agent sandboxes inherited a production `DATABASE_URL` and 204 tables were deleted. `agent_env.py` was the fix, and it is a good fix, but it hardens *one* escape route rather than establishing a boundary.

**What the competitors do.** Factory: macOS Seatbelt, Linux bubblewrap + seccomp, and an HTTP/SOCKS **filtering proxy** for egress, with writes confined to cwd by default. Warp: Docker containers running as non-root, per-team concurrency limits, published egress CIDRs so customers can allowlist.

**Why P0.** This is the (c) case. We are about to publish software whose core instruction is *"point this at your repository and let it run commands."* Container-level isolation is the floor, not a differentiator.

**Shape of the fix (decided 2026-09-14).** **Firecracker/microVM is out of scope for the OSS MVP.**
It is not a gap to close, it is a claim to keep frozen (`BOSS_MICROVM_CLAIMS_FROZEN` stays `true`) and
a substrate to stop maintaining on the critical path. The v1 execution substrate is a **local
container**: ship **Docker-per-code-task** as the Community default using the `sandbox-host`
`ContainerBackend` (`sandbox_agent.py:96-258`), which already exists and is real code. Add an egress
allowlist. Keep the local subprocess as an opt-in fast path for trusted repos only.

This also simplifies P0-6: the permission model and the container boundary are then one story
(what the agent may run, and where) rather than two.

---

### P0-6 · There is no permission model at the agent boundary

**Gap.** BOSS's governance is excellent at the *workflow* level — decisions, risk tiers, dual control, budget states. Inside the sandbox it evaporates: the coding agent runs with `--permission-mode acceptEdits` and the CLI's full toolset. The `sandbox_validation_allowed_commands` allowlist (`config.py:645`) governs BOSS's own *verification* commands, not the agent's `Bash`.

**What the competitors do.** Factory: four autonomy levels, plus **three lists** — allowlist, denylist (beats allowlist), and a **blocklist that can never run even under `--skip-permissions-unsafe`**, which resolves wrapper shells, absolute paths and command substitution specifically to defeat bypasses. Warp: six permission dimensions × three levels per Agent Profile, regex allow/denylist where **denylist beats everything**, and team-enforced denylists that "Run until completion" cannot override.

**Why P0.** It is the missing half of the BOSS story. We claim governance as the differentiator; today that claim stops at the sandbox wall. It is also the cheapest credibility win in this list, because the enforcement point already exists — `sandbox.run` already refuses shell syntax and checks `argv[0]`.

---

### P0-7 · The differentiators ship switched off

**Gap.** Verified in `engine/boss_engine/config.py`:

```
:802   code_review_panel_size:  int  = Field(default=1)
:841   code_gate_never_block:   bool = Field(default=True)
:1056  microvm_claims_frozen:   bool = Field(default=True)
```

So out of the box: the "refute panel" is a single reviewer; the "final gate **FAILS CLOSED**" — the phrase is in bold in `GOVERNED_CODING_PIPELINE.md` — ships with dissent recorded rather than blocking; and the isolation claim silently degrades. `shepherd_autopilot_enabled` is also `False` (`:665`).

Every one of these defaults is individually defensible. Together they mean a first-time evaluator running stock config **sees none of the thing we are selling**, and then reads documentation that describes it in present tense. That is the fastest way to be dismissed as overclaiming — and it is the exact failure mode `COMMUNITY_READINESS.md` says must be avoided before launch.

**Shape of the fix.** Ship a Community profile where the governance is on: panel ≥ 2 at tier ≥ 2, gate blocks by default with an explicit `--advisory` escape, and the frozen-microVM fallback surfaced in Mission Control and in the run evidence rather than only in an env var. If a default is too expensive to be on, say so in the docs in the same sentence as the claim.

---

### P0-8 · No streaming, and no headless structured exec

**Gap.** Two halves of the same problem — you cannot watch a run, and you cannot script one.

*Streaming:* SSE and WebSocket exist only for preview logs (`apps/api/app/main.py:9684`, `:9730`). Agent runs are observed by polling `read_run_turns` / `watch_code_task`. The CLI's `ask` and `chat` print a completed turn.

*Headless:* the engine CLI is `bootstrap | tools | status | call | ask | chat | serve-mcp`. There is no `boss code "<task>" --repo .`. Starting a code task from CI means `call create_code_task --args '{…}'` then polling.

**What the competitors do.** Factory: `droid exec -o text|json|stream-json|stream-jsonrpc`, `--restrict-tools`/`--additional-tools`/`--disabled-tools`, `--tag`, `--log-group-id`, exit codes 0/1/2. `stream-jsonrpc` is bidirectional and is explicitly how third parties build their own UIs on top. Warp is **weaker here than Factory** — their CLI is TUI-only with no JSON mode, automation goes through the API/SDK. So this is a place where matching Factory beats Warp outright.

**Why P0.** A software factory that cannot run in CI is a demo. And the console's credibility depends on a human being able to watch an agent work — "trust" is not established by a spinner and a final verdict.

---

### P0-9 · Time-to-first-value is 10–25 minutes and needs Postgres

**Gap.** Postgres is a hard, non-negotiable dependency: without `DATABASE_URL` the engine drops to "catalog-only mode" and every write tool fails closed. That is architecturally correct — the durable ledger *is* the product — but it is also the gate on the first five minutes. `.env.example` is 242 lines / 153 keys (62 empty); four are genuinely mandatory. The Docker build pulls Playwright Chromium and npm. `BOSS_DEFAULT_PROJECT_ID=boss-dogfood` is hardcoded as the default across CLI, MCP and web, so a new user's first experience is someone else's project.

**What the competitors do.** Warp's factory quickstart is *"~5 minutes"* through a wizard. Factory is `npm i -g` and you are in a REPL.

**Why P0.** It is the conversion funnel. `install.sh` is genuinely good — 460 lines of real preflight, port checks, health polling — but it is fighting a 25-minute image build.

**Shape of the fix.** Pre-built images on a registry so first run is a pull, not a build. Trim the required env to two (model key, repo). Seed a project named after the user's repo, not `boss-dogfood`. Publish the actual measured time-to-first-PR and hold it as a regression test — Warp publishes theirs.

---

### P0-10 · No diff-level human feedback loop

**Gap.** `steer_code_task` exists and queues guidance applied at the next stage boundary (`tools/builtin.py:2048`, `governed:203`). That is prose steering of a running task. There is no way to look at what the agent produced, mark up three specific hunks, and send those back.

**What the competitors do.** Warp's **Interactive Code Review** is the sharpest feature either competitor has: select changed lines, attach inline comments anchored to file+line, **batch them**, send all to the agent in one pass, get an updated diff, repeat — and it works with third-party harnesses. Plus hunk-level accept / refine-with-NL / edit / revert. Factory has inline review comments on diffs in the desktop app and mobile diff approval.

**Why P0 (borderline — argue me out of it).** BOSS's pitch is that a human stays in the loop at the points that matter. Today the human's only verbs are *approve*, *reject*, and *type a paragraph*. Both competitors have concluded that hunk-level feedback is the primary interaction of the whole product. I would rather ship this than three more governance surfaces.

---

### P0 summary

| # | Gap | Blocks | Rough size |
|---|---|---|---|
| 1 | No public artifact (repo private, SECURITY.md placeholder, BOSS-300 open) | everything | decision + extraction |
| 2 | CI red on `main`; `engine/` unlinted; no e2e pipeline test | contributors | small |
| 3 | **Open lane has no tool-calling agent; only good lane is a proprietary CLI** | product credibility | **large — start now** |
| 4 | `AGENTS.md` dead code; lexical-only retrieval; 14-file context | output quality | small (a) + free with 3 |
| 5 | Isolation is a temp dir in the control plane, full egress | safety | medium |
| 6 | No autonomy levels / command deny-blocklist at the agent boundary | safety + story | small |
| 7 | Gate, panel and sandbox defaults are all off | first impression | small |
| 8 | No streaming; no headless `-o json` exec | CI + trust | medium |
| 9 | TTFV 10–25 min, Postgres required, seeds `boss-dogfood` | conversion | medium |
| 10 | No hunk-level review/feedback loop | the core interaction | medium |

---

## 5. P1 — needed within a quarter of launch

| # | Gap | Why it matters |
|---|---|---|
| P1-1 | **Outcome metrics: autonomy %, cost per PR, cycle time** | Warp leads with `$80 → $30 per PR`. BOSS already has richer data than either competitor (`llm_calls`, `agent_turns`, phase timings, receipts) and surfaces none of it as an outcome number. This is a dashboard over existing tables, and it is the number a buyer repeats to their boss |
| P1-2 | **Scorers over your own runs** | Warp's `scorers/` + sample rate + pass threshold is their moat claim. `bakeoff.py` and `verifier_meta_eval.py` are 80% of the machinery already, sitting behind an internal API |
| P1-3 | **MCP client parity** | `mcp_client.py` is 69 lines, HTTP-only, and `mcp_registry.py:44` rejects non-http schemes — so BOSS **cannot launch stdio MCP servers**, which is how most of the ecosystem ships. Both competitors support stdio + HTTP + SSE. We are the best MCP *server* of the three and the worst MCP *client* |
| P1-4 | **Factory-as-code in the customer's repo** | Warp's `factory.yaml` + `agents/ scorers/ skills/` tree is PR-reviewed with a `warp/factory-config` check. BOSS's specpack YAML lives in *our* monorepo. Moving the definition into the customer's repo is what makes it *theirs* |
| P1-5 | **Wire the deterministic mutation gate** | `mutation_gate.py` is real AST-based mutation testing that the pipeline does not call. Neither competitor has anything like it. It is built, unused, and differentiating |
| P1-6 | **Visual/a11y verification with real evidence** | Factory attaches Remotion-rendered video; Warp attaches up to 10 screenshots + video to the PR. BOSS's `visual_gate.py` greps HTML strings. Playwright is already a dependency |
| P1-7 | **Ticket → PR from Slack / Linear / Jira** | The demo both competitors lead with. BOSS has tracker adapters that *create* issues and a Plane webhook path; the inverse trigger is missing for the trackers teams actually use |
| P1-8 | **OTel self-instrumentation** | Zero `opentelemetry` in any requirements file. Factory exports OTLP metrics and optionally message-content spans *to the customer's collector only* — a genuinely good pattern for a governance product |
| P1-9 | **Tool plugin SDK** | All 86 tools are hardcoded in `tools/builtin.py`; no entry-point discovery. Roles/skills/workflows/packs are genuinely extensible in YAML, which is more than it first appears — but "add a tool" means editing core. Either ship entry points or market MCP as the tool SDK and say so plainly |
| P1-10 | **GitLab** | Absent entirely. Warp has GitLab + Bitbucket + Azure DevOps; Factory has GitLab self-hosted |
| P1-11 | **Hooks** | Factory has 9 lifecycle events with `deny`/`ask`/`updatedInput`. Warp has none. Cheap way to beat one competitor outright |
| P1-12 | **Repo readiness assessment** | Factory's smartest GTM idea: score the repo 1–5 across 9 pillars, require Level 4 for autonomy, and ship `/readiness-fix` that implements the remediation. They sell the assessment before they sell the autonomy. BOSS's evidence model is a natural fit and this is a wedge, not a copy |

---

## 6. Where BOSS is already ahead — do not trade these away

1. **Budget as an enforcing policy.** Quote → reserve → consume, parent/child envelopes, colour states that defer or halt work. Warp abstracts spend into three opaque credit buckets with no published token→credit table. Factory has per-model credit multipliers. **Neither can stop a run because it would breach a budget.**
2. **Receipts, replay bundles, compliance evidence bundles, data-boundary classification.** Factory's audit log does not document retention or SIEM export and has **no run replay viewer**. Warp has **no audit-log product at all** — only an Early-Access analytics `events` endpoint. This is the biggest single lead BOSS holds.
3. **Separation of duties as an enforced control.** Tester ≠ coder, the implementer cannot edit the tests, and BOSS re-runs the tests itself rather than trusting a self-report. Warp's Spec/Implement/Review is the closest analogue and is weaker on all three counts — their Review agent explicitly has **no merge authority and no documented merge gates**.
4. **Agent panels / meetings → PRD before code.** Neither competitor has a multi-stakeholder deliberation artifact. Warp's Spec agent writes one spec into a draft PR and a human approves it. That is the whole of their product-definition story.
5. **SSO / SAML / SCIM in the free self-hosted tier.** Warp has **no SCIM group sync** and SSO deprovisioning does not revoke live sessions. Factory gates SSO at Business.
6. **Self-hostable control plane.** Warp's control plane is always Warp's — orchestration, session storage and inference routing all transit their servers *even in self-hosted mode*, with no air-gap. Factory offers airgap under an enterprise licence on proprietary code. **An Apache-2.0 self-hostable software-factory control plane does not exist. That is the launch story.**
7. **Secret handling.** Six backends including local AES-GCM vault and HashiCorp Vault/OpenBao, pointer-only storage in Postgres, and a deny-by-default agent env allowlist written from a real incident.
8. **86-tool MCP server.** The best "be driven by another agent" surface of the three — Warp's Factory MCP has 18 tools and ships with a documented warning that it has no read-only or per-factory scopes.
9. **Incident-derived engineering.** Dozens of modules document the exact production failure that motivated the current design — argv NUL bytes killing a review panel, a Playwright webServer holding a pipe for 18m43s, cost totals that reported four different numbers. That is not marketing; it is evidence the system has been run for real. It is also the most honest asset in the repo and it should be visible in the OSS docs, not buried in comments.

---

## 7. Recommended shape of the P0 cut

Sequenced by dependency, not by preference.

**Track A — make it launchable (parallel, mostly decisions)**
1. Record BOSS-300. Finish the engine extraction. Real `SECURITY.md`. Community kit at the repo root. Re-point `install.sh`.
2. Fix CI: the missing make target, Python 3.11, `ruff` over `engine/`, plus one end-to-end pipeline test against a fixture repo.
3. Pre-built images; two mandatory env vars; seed a project named after the user's repo.

**Track B — make it work (the long pole; start immediately)**
4. **The agent lane.** Tool-calling loop on the OpenAI-compatible path *and* a declared harness-adapter contract with `claude_code` / `codex` / `opencode` / `aider` adapters.
5. Wire `repo_guide` to `AGENTS.md` / `CLAUDE.md`. Half a day.
6. Streaming turns to console and CLI; `boss code "<task>" --repo . -o json|stream-json` with exit codes.

**Track C — make it safe (gates Track B going public)**
7. Docker-per-code-task as the Community default substrate; egress allowlist.
8. Autonomy levels + allow/deny/**block**list at the agent boundary, enforced where `sandbox.run` already enforces argv.
9. Flip the Community defaults so the governance we document is the governance that runs.

**Track D — make it stick (immediately after)**
10. Hunk-level review with batched comments fed back to the agent.
11. Autonomy % / cost-per-PR / cycle-time dashboard from tables that already exist.

Track B item 4 is the only one that cannot be compressed, and everything in Track A is a decision rather than engineering. **Start B4 and unblock A1 in the same week.**

---

## 8. Decisions only you can make

- **D1 — Which buyer?** `emergent-gap-analysis.md` optimises for "prompt → deployed app" (hosting, domains, credits). This document optimises for "ticket → PR in my existing repo" (agent quality, isolation, permissions). The P0 lists barely overlap. Factory and Warp have both chosen the second. Choosing both means shipping neither well.
- **D2 — Is the coding agent ours or rented?** If rented, the multi-harness adapter contract (P0-3b) is the *whole* product bet and should be first-class, versioned, and documented — Warp made exactly this bet and markets it as "open at every layer." If ours, P0-3a becomes a sustained investment, not a fallback, and we are competing with Anthropic and OpenAI on harness quality.
- **D3 — What does "Community" include?** `GTM_GAP_ANALYSIS.md` §G1 flagged the Mission Control tier divergence in July; today the engine ships a read-only Mission Control over MCP HTTP. Is the read-only console the Community answer, or does the full console go open? Warp open-sourced their *client* and kept the control plane. That is the obvious mirror — and it is the opposite of BOSS's advantage, since **our** control plane being open is the only thing neither competitor can match.
- **D4 — Do we publish benchmark numbers?** Factory publishes Terminal-Bench and Agent Arena leaderboards. Warp publishes $80→$30 cost-per-PR. `bakeoff.py` can produce a comparable number on our own task suite. Publishing an honest, self-defined number beats publishing none — but only if it survives contact with an outsider running it.
- **D5 — Firecracker.** Keep the freeze and ship Docker (recommended), or spend the time to make microVM real before launch? The honesty gate is currently doing its job; unfreezing on a "local fallback worked" signal would be exactly the overclaim `COMMUNITY_READINESS.md` exists to prevent.

---

## 9. Confidence and limits

- **BOSS claims** were read from this repository at `00c18aa4` and verified at file and line. Where docs and code disagreed, code won and the disagreement is noted. The three config defaults in P0-7, the two CI defects in P0-2, the dead `repo_guide` in P0-4, and the `NativeCodeAgent` whole-file rewrite in P0-3 were each confirmed by direct inspection.
- **Competitor claims** come from vendor documentation fetched 2026-09-13. Vendor docs describe intent as much as behaviour; neither product was run. Items the research could not confirm from a primary doc page were flagged in the source reports and have been kept out of this document's load-bearing claims.
- **Specifically unverified:** Warp's max run duration, concurrency limits, per-model credit multipliers, and `credentialStrategy: EXECUTOR|CREATOR` semantics; Factory's audit-log retention, SIEM export, and whether the hierarchical user/org memory described in their engineering blog is a shipped feature (it does not appear in current reference docs). Warp's Terminal-Bench / SWE-bench figures are press-sourced, not from their docs. Both vendors' benchmark leaderboards are self-published.
- **Warp Factories is Early Access and gated.** Its real-world maturity is unproven; the 800k-developer number is terminal users, not factory users. The window is genuinely open — but it is open for them too, and they are shipping weekly.
- **Effort sizes in §4 are shape, not estimates.** Nothing here has been costed against the actual codebase.

---

## 10. Phase 0 — what shipped (2026-09-14)

Branch `ai/codex/p0-native-coding-agent`. Plane epic **BOSS-406**.

### 10.1 P0-A — the native lane is now an agent (BOSS-407)

`NativeCodeAgent.run_role` was one chat completion returning whole-file JSON. It is now a real
multi-turn tool-calling loop. **One `run_role` call is one agent session, not one completion** —
which matters because the governed pipeline calls `run_role` once per step.

New: [`engine/boss_engine/services/code_agent_tools.py`](../../engine/boss_engine/services/code_agent_tools.py)

| Tool | Available in review steps? |
|---|---|
| `read_file` (line-numbered, windowed) | yes |
| `list_dir`, `glob`, `grep` | yes |
| `edit_file` (exact string replace) | **no** |
| `write_file` | **no** |
| `bash` | **no** |

Design decisions worth reviewing:

- **Confinement is the invariant.** Every path resolves and must land inside the sandbox root.
  Absolute paths, `..`, `~` and escaping symlinks are refused with a message the model can act on.
- **"The reviewer cannot edit" is now a property of the tool schema**, not a sentence in a prompt.
  Read-only steps are never offered the write half, and the toolset refuses them a second time at
  the call boundary.
- **One allowlist.** `bash` shares `EphemeralSandbox`'s executable allowlist and no-shell rule —
  `assert_allowed_executable` was lifted to module level in `sandbox.py` so there is exactly one
  copy of that rule. In the pipeline, `bash` is routed through the sandbox's own runner so an
  agent-run command gets the same tool caches, `node_modules/.bin` PATH and process-group kill
  that verification commands get.
- **A refused tool call is the next turn, not a dead end.** Errors come back as tool results.
- **Degradation is visible and first-turn-only.** An endpoint without function calling falls back
  to the legacy single-shot path with a `WARNING`; a rate limit, timeout or 5xx is explicitly *not*
  read as a capability gap, so a transient failure can never silently and permanently downgrade the
  lane. `BOSS_NATIVE_AGENT_TOOLS_ENABLED=false` makes the legacy path the default.
- **Bounded.** `BOSS_NATIVE_AGENT_MAX_TURNS` defaults to 40 — deliberately below the Claude lane's
  90, because a cheap model that has not converged in 40 turns is looping and the VAG economics
  depend on the cheap lane staying cheap. Hitting the cap is recorded in the summary, not hidden.
- Cost accounting is per turn via `record_llm_call`, so the `llm_calls` ledger and the
  $/verified-outcome maths stay honest.

### 10.2 P0-B — `AGENTS.md` is no longer dead code (BOSS-408)

`RepoContext.repo_guide` was declared, injected into the prompt, and never populated. `load_repo_guide()`
now reads `AGENTS.md` (or `CLAUDE.md`, for repos already set up for Claude Code) **from the checked-out
working tree**, so it reflects the branch actually being worked on rather than a value that would have
had to exist before the clone. An explicitly supplied `repo_guide` still wins. Capped by
`BOSS_REPO_GUIDE_MAX_CHARS` (32k; Factory caps the equivalent at 80k). Both lanes use it.

### 10.3 P0-F — two CI breakages, one fixed; a new gate found four live bugs (BOSS-412)

**CI was broken in two independent places, not one.** I claimed "CI is green" before checking the
step above the one I fixed; it is not, yet.

- `make openapi-validate` now exists. Every push and PR to `main` had been failing at a step that
  called a target that was never in the Makefile.
- CI Python is `3.11`, matching `Makefile:29` and `engine/pyproject.toml`. It was pinned to `3.9`.
- **New gate:** `ruff check engine/ --select E9,F63,F7,F82` — undefined names, broken f-strings,
  syntax errors. `make lint` covers `apps/api` and `apps/worker` only, so 195k LOC had no check at
  all. It found **three live `NameError`s in value position**:

| Bug | Consequence |
|---|---|
| `mutation_gate.py` — `low_priority` never imported | The deterministic mutation engine could not run a **single mutant**. It was already unwired from the pipeline; it was also broken. |
| `skill_executors.py` — `make_id` never imported | Every `sandboxed_command` skill run raised. |
| `intent_router.py` ×2 — `self.agent` does not exist, `actor` not a parameter | Two router self-correction paths were dead. See §10.4. |
| `apps/api/app/main.py:5077` — bare `project_id` in `onboard_org_endpoint` | **`POST /v1/orgs` raised `NameError`** — new-org onboarding returned 500. The first thing an OSS evaluator does. |

The gate therefore runs over `engine/ apps/`, not just `engine/`.

Two annotation-only findings (`specpack`, `console_driver`) were also fixed; neither could crash at
runtime because both modules use `from __future__ import annotations`.

**Not done (1): `make lint` still fails.** The `Lint` step *above* the one I fixed was already red
with 39 pre-existing findings — 24 × E402, 14 × F401, 1 × F841, concentrated in
`apps/api/tests/conftest.py` (22) and `apps/api/app/main.py` (10). The E402s are largely deliberate
late imports in a 24,796-line module, so the fix is a per-file-ignore decision rather than a blind
reorder. **CI is not green until these are cleared.** Tracked as **BOSS-418**.

**Not done (2):** OpenAPI route parity is not yet a CI gate. 197 implemented routes are missing from
`api/openapi.yaml`, in violation of the repo's own API-first rule. `sync_openapi_from_routes.py`
generates them but produces a ~6,300-line diff against a 49k-line document — that needs its own PR.
Tracked as **BOSS-416**.

### 10.4 Found while fixing CI: two dead quality features (BOSS-417)

`IntentRouter._answer` contains a **grounding re-ask** (a role answered without reading the project
record → make it answer again from the record) and an **action-claim reconcile** (the reply claims a
change no tool made → make it actually call the tool). Both called `self.agent.chat(...)`.
`IntentRouter` has no `agent` attribute. Both sat inside `except Exception: … = None`.

So both raised `AttributeError` on **every turn since they were written**, were swallowed, and the
features simply never ran. The bare except is why that went unnoticed — which is the precise failure
mode `AGENTS.md` forbids ("every failure must be visible").

Fixed: the agent object is `self._answer_agent()` (already the convention at four other call sites in
the same file), `actor` is threaded through `_answer` and its three call sites, and both failures now
log at `WARNING`.

### 10.5 Tests

`engine/tests/test_the_native_lane_can_actually_explore_a_repo.py` (35) and
`engine/tests/test_the_grounding_reask_actually_runs.py` (6) — 41 new tests, all passing.
108 adjacent existing tests (sandbox, agent env, turn capture, timeouts, branch handling, router)
re-run green.

The two tests that matter most are named for the invariants they hold:
`test_a_review_step_is_never_handed_a_way_to_write` and `test_a_path_outside_the_repo_is_refused`.

> **A trap worth knowing.** `Settings` sets `extra="ignore"` and does not set `populate_by_name`,
> so `Settings(native_agent_max_turns=3)` is **silently dropped** and you get the default. Tests must
> construct by alias (`BOSS_NATIVE_AGENT_MAX_TURNS=3`). Two tests initially passed for the wrong
> reason because of this.

### 10.6 Still open from P0 — next in order

| # | Item | Plane |
|---|---|---|
| P0-C | Harness adapter registry (`claude_code`/`codex`/`opencode`/`aider` declared, not a hardcoded `if/elif`) | BOSS-409 |
| P0-D | Container-per-code-task as the default substrate + egress allowlist | BOSS-410 |
| P0-E | Autonomy levels + allow/deny/blocklist at the agent boundary | BOSS-411 |
| P0-G | Honest Community defaults (panel ≥ 2, gate blocks) | BOSS-413 |
| P0-H | Streaming turns + headless `boss code … -o json` | BOSS-414 |
| P0-I | Brownfield multi-file bake-off task to put a number on §10.1 | BOSS-415 |

**Recommended next:** BOSS-415 before BOSS-409. The tool loop is built and tested, but its value on a
real repository is currently an argument, not a measurement — and the bake-off harness that would
settle it already exists.

---

## 11. Regression evidence (2026-09-14)

The coding pipeline has had real latency work behind it — parallel tool batching, idle-stall
detection, a concurrent review panel, process-group kill, `nice`/`ionice` for in-container
builds, prompt-cache `--resume`. The rule for this branch was that none of it may regress.

**Baseline, established before any change:** full engine suite on clean `main` —
`test_console_v2_parity::test_every_v1_area_has_a_recorded_destination` fails; everything
else passes. That one failure is pre-existing and unrelated (a missing `github` parity
entry).

**Final:** 3,787 passed · 1 xfailed (BOSS-419, deliberate) · 1 pre-existing failure. Plus
`ruff check apps/api apps/worker` green and `ruff check engine/ apps/ --select E9,F63,F7,F82`
green.

### 11.1 Three regressions I introduced, and the guards that caught them

| What I broke | Caught by | Why it mattered |
|---|---|---|
| The agent's `bash` ran builds at **normal priority** inside the API's container | `test_builds_yield_to_the_control_plane` | That guard exists because an unniced build took `/v1/code-tasks?limit=5` from **0.37s to 47.1s**. Now wrapped in `low_priority()`. |
| A comment named the operator's production platform | `test_ports::test_only_the_platform_integration_names_the_platform` | The OSS engine must not carry the operator's private hostnames or platform name. |
| `container_runner.start()` handed an `argv` to `subprocess.run` | `test_builds_yield_to_the_control_plane` again | Renamed `create_argv` with a comment, **not** added to the exclusion list: `docker run` starts the container, the build launch is in `run()` and it *is* niced. A meta-test (`test_the_exclusions_really_are_remote`) would have rejected the exclusion anyway — correctly. |

That guard suite is the most valuable thing in this repository. Every entry above is a
latency or isolation rule someone learned the hard way and then wrote down as a test.

### 11.2 Latency protections added, not just preserved

- **A review step now gets a tighter turn cap than a build step** (12 vs 40), mirroring the
  claude lane's 16 vs 90. A paced slot is held for a whole role invocation, and the review
  panel runs three checks concurrently against a per-provider in-flight cap of 3 — so a
  reviewer wandering for 40 turns starves the lane the other two need. Tested.
- **Parallel tool calls are executed as a batch.** The measured bug on the claude lane was
  1.00 tool calls per turn: 19 file reads cost 19 round trips and ~8 minutes of pure latency.
  `test_independent_reads_can_be_batched_into_one_turn` asserts three reads cost one turn.
- **Container overhead measured, not assumed:** 1.5s container start **once per task**,
  ~64ms per `docker exec`. A container per *command* would have added ~1.5s to every step;
  `test_one_container_serves_the_whole_task` pins that.

### 11.3 What is not covered

- `apps/api/tests` (187 files) were **not** run: they need Postgres, and this repo's pytest
  run wipes the database it points at. The one `apps/api` file I changed is a one-line
  `NameError` fix in `onboard_org_endpoint`.
- No live coding run against a real repository with real model keys. Every claim about
  *output quality* on the native lane remains an argument until BOSS-415 is executed — which
  is exactly why that task now exists.

---

## 12. Correction log

Things this document asserted that turned out to be wrong. Kept rather than edited away,
because a gap analysis that quietly revises itself is not evidence of anything.

| Claim | What was actually true |
|---|---|
| "Quality **collapses** without the Claude CLI" | Too strong. The *orchestration* is genuinely model-agnostic and is what the 46-run VAG study measured; DeepSeek-V3 as implementer matched frontier-solo at −24% cost. What is not agnostic is **per-stage agent capability**: one completion vs up to 90 tool-using turns. |
| "CI is green" (after fixing the missing make target) | Two independent breakages. The `Lint` step *above* the one I fixed was already failing, and its TypeScript half still is. |
| P0-7: "the differentiators ship switched off" | `code_gate_never_block=True` is a **considered autonomy default** with dissent recorded, re-queued as a follow-up task and reversible — documented at length in `resolve_gate_failure_action`. The defect was `GOVERNED_CODING_PIPELINE.md` promising a gate that fails closed. The doc was fixed; the default was not touched. |
| P0-5: "ship Docker-per-code-task as the **default**" | Shipped opt-in. A new isolation mechanism that silently becomes the default is exactly how delivery timelines break. It needs one live run on a real repository first. |

---

## 13. Console findings (2026-09-14)

Reported symptom: *"Application error: a client-side exception has occurred"* on one deep link.
It was not that link — **every** console URL behaved the same way, including `?area=overview`.

### 13.1 The client could never reach the control plane

Twenty-one client components fetch `${API_URL}/v1/…` through `lib/*` helpers. In a browser that
is cross-origin, and **the API has no CORS middleware anywhere in the repository** — zero matches
for `CORSMiddleware`, `allow_origins` or `Access-Control-Allow-Origin`. The preflight says it
plainly:

```
OPTIONS /v1/factory/flow  →  401, no access-control-allow-origin
```

Reproduced on every area checked — overview, delivery_workflows, code_tasks, decisions,
discovery, agent:

```
Access to fetch at 'https://boss-api.../v1/factory/flow?project_id=boss-dogfood'
from origin 'https://boss...' has been blocked by CORS policy
```

**Why nobody found it:** the helpers catch their own errors and return an empty feed. Those areas
render *blank*, not broken — which reads as "no data yet". A failure that looks like an absence
of data is the most expensive kind, because it never prompts anyone to look.

`app/api/preview-logs/route.ts` had already diagnosed exactly this, for one endpoint, in as many
words: *"the browser cannot reach the API at all in this deployment"*. There were **27**
hand-written route handlers solving it one endpoint at a time. `app/v1/[...path]` is the general
case, so a new client feed now needs no new handler.

It also takes the key off the wire: `API_KEY` fell back to `NEXT_PUBLIC_BOSS_API_KEY`, which Next
inlines into the bundle at **build** time — every visitor was handed a credential that can write
to the control plane. The key is attached on the server now, and the browser sends none.

### 13.2 The screen checker was green while the console was unusable

`V2_SCREENS` listed only `/v2/*`. Run as shipped against the broken deployment:

```
9 screens checked · broken: 0 · suspect: 0 · unrefuted: 9
```

Pointed at `/` — the page a bookmark, a share link and every "View in …" button actually land on:

```
BROKEN  /
        42 button(s) with no accessible name — unusable and undescribable
        9784ms to first byte, budget 3000ms
```

This is the same failure shape as BOSS-417: a check that passes for a reason unrelated to whether
the thing works. `/` is in the default sweep now, and a test stops any *other* screen opting out
of its identity assertion.

### 13.3 What is still wrong, and why I did not change it

| Finding | Plane | Why untouched |
|---|---|---|
| 5–10s TTFB on every area | BOSS-422 | Root cause is `page.tsx:554` `shows = () => true`, a **deliberate** revert (`877e13c3`) so a rail click does no server work. The comment records the operator complaint that drove it. Hydrating the ~8.7 MB tree froze a Chrome renderer for >45s in testing — the likely mechanism behind the intermittent client-side exception. Changing it is a product call. |
| 42 nameless buttons; fonts not matching the design spec | BOSS-423 | Neither is a crash; both are real. |
| `github` area has no v2 destination | BOSS-424 | `console_v2_parity.yaml` exists so a fold from 31 areas to 9 cannot be mistaken for a deletion. Inventing an entry is the thing it guards against. Left failing. |

**A middle option for BOSS-422 worth considering:** keep every area in the DOM so a rail click
still costs nothing, but stop shipping every area's *data* in the first payload. The ~950 KB
Discovery brief and 431 KB Shepherd feed are what make the tree expensive to hydrate — not the
markup.

### 13.4 Test pass, final

| Suite | Result |
|---|---|
| Engine | 3,792 passed · 1 xfailed (BOSS-419) · 1 pre-existing failure (BOSS-424) |
| Web (`vitest`) | **2,103 passed · 0 failed** · 1 skipped |
| `make lint` | **rc=0** — green both halves for the first time |
| `ruff` real-bug gate (`engine/ apps/`) | green |
| MCP server (stdio + HTTP) | initialize, tools/list, tools/call, `-32601` on unknown method; 54 read tools with no `DATABASE_URL`, write tools correctly hidden; Mission Control serves |
| Agent toolset live | 12/12 |
| Container substrate live | 9/9 — egress blocked, non-root, host FS invisible, nice=19, 68ms/cmd |

One correction from this pass: I first flagged MCP returning a *result* for an unknown tool as a
bug. It is not — MCP reports tool errors as `result` + `isError`, and reserves JSON-RPC errors for
protocol failures, which the server does correctly. My assertion was wrong, not the server.
