# BOSS OSS-Readiness — triage, tickets & roadmap

> Triage of 9 strategic asks into a categorized, prioritized backlog for the open-source
> initiative: **open-source the BOSS engine; keep control-plane APIs + console as paid enterprise.**
> Every epic below is a Plane issue in the goldeneye project, assigned to a module, with a detailed
> spec in this directory. Generated 2026-07-11.

## How this is organized

The Plane project was restructured from one flat list into **12 functional modules**, native
**priority** (urgent/high/medium/low), and **labels** for the licensing boundary
(`oss-engine` / `control-plane` / `console-enterprise`), plus `ai-agent`, `foundation`, `has-spec`.

Each big feature is an **epic** (parent issue) with **child tickets** carrying acceptance criteria.
The full design for each lives in the linked spec — written to be picked up by an implementing agent.

## Priority tiers (gated on the OSS initiative)

| Tier | Meaning | Epics |
|---|---|---|
| **P0 — OSS engine blocker** (urgent) | The engine can't ship / first impressions fail without it | [01 OSS engine split](01-oss-engine-split-and-packaging.md) · [09 First-time output quality](09-first-time-output-quality.md) · [00 Event foundation](00-event-and-webhook-foundation.md) |
| **P0 — core** (high) | Core engine capability for a credible OSS release | [02 Preview/sandbox/feedback](02-preview-sandbox-and-feedback.md) · [03 Skills & extensibility](03-skills-and-extensibility.md) |
| **P1 — OSS launch** (high) | The community & adoption surfaces | [06 Docs agent](06-documentation-agent.md) · [07 Orchestration agent](07-orchestration-agent.md) · [08 GitHub community agent](08-github-community-agent.md) |
| **P2 — enterprise reach** (medium) | Paid enterprise value; not launch-blocking | [04 CI/CD & deployment](04-cicd-and-deployment.md) · [05 Infrastructure-as-Code](05-infrastructure-as-code.md) |

## The 9 asks → where they landed

| # | Ask | Module | Epic | Spec |
|---|---|---|---|---|
| 1 | Docs + auto-updating docs agent | Documentation & Knowledge | #71 | [06](06-documentation-agent.md) |
| 2 | Agent that initiates coding/meetings/discovery (many tasks) | Orchestration Agent | #77 | [07](07-orchestration-agent.md) |
| 3 | In-GitHub triage + PR-review agent for the OSS | GitHub Community Agent | #83 | [08](08-github-community-agent.md) |
| 4 | Skills / MCP marketplace (extensible) | Skills & Extensibility | #44 | [03](03-skills-and-extensibility.md) |
| 5 | System (default) vs customer (on-demand) skill loading | Skills & Extensibility | #44 | [03](03-skills-and-extensibility.md) |
| 6 | Robust preview + feedback agent (extend completed tasks) | Preview, Sandbox & Feedback | #34 | [02](02-preview-sandbox-and-feedback.md) |
| 7 | Robust first-time output (vs polsia.com) | Engine & Model Quality (VAG) | #90 | [09](09-first-time-output-quality.md) |
| 8 | Automated CI/CD, canaries, preprod→prod (FDE) | CI/CD & Deployment | #57 | [04](04-cicd-and-deployment.md) |
| 9 | Terraform / multi-cloud IaC | Infrastructure-as-Code | #64 | [05](05-infrastructure-as-code.md) |

Plus a foundational enabler surfaced during triage — **#19 Reactive event + webhook foundation**
([00](00-event-and-webhook-foundation.md)) — which unblocks asks 1, 2 and 3.

And a scale enabler surfaced when reviewing the engine language — **#97 Go concurrency services for
high-scale task execution** ([10](10-go-concurrency-services.md), module *Runtime & Concurrency (Go
services)*): the engine stays Python (I/O-bound orchestration), while the resource-bound **execution
substrate** (Firecracker sandbox, gateway, high-fanout dispatcher) goes to **Go** for 100s of concurrent
tasks — polyglot **by layer**, not a rewrite.

## Key triage finding: most asks are *extend*, not *build*

Ground-truth codebase research (not assumptions) reshaped the estimates. What already exists:

- **Feedback on completed tasks already works** — `retry_code_task` reuses the branch/PR. The gap is a *conversational* feedback agent, not the mechanism. (ask 6)
- **Canaries already exist** — three kinds, including a self-improvement staged-rollout with auto-rollback. The gap is *generalizing* them to customer app deploys. (ask 8)
- **The intent→many-tasks decomposer already exists** — `intake.propose_command`/`dispatch_plan`. Asks 2 & 3 are mostly wiring an intent router over live endpoints.
- **MCP is a working per-project plug-in boundary** already injected into the agent loop; the skills registry, by contrast, is an *inert governance shell with no executor wired*. The marketplace should build on MCP. (asks 4/5)
- **The engine is clean** of secrets, proprietary deps, and phone-home — but budget/entitlements are compiled into its hot path; the real OSS work is decoupling behind ports. (OSS split)

What is genuinely *missing* (net-new):

- No reactive event dispatch / no inbound webhook receiver (the one primitive under asks 1/2/3).
- No Terraform/IaC generation and no AWS/GCP/Azure resource adapters (ask 9).
- No frontend scaffolding, no design-aware prompts, no visual/UX gate — first-time output is design-blind (ask 7).

## Suggested sequencing

1. **Foundations first:** #19 event foundation (unblocks 3 agents) and the #25 OSS-split ports (unblocks a clean engine package).
2. **First impressions:** #90 first-time output quality and #34 preview robustness — these are what an OSS evaluator sees on day one.
3. **Extensibility:** #44 skills executor + loading model (customers extend the engine).
4. **Adoption surfaces:** #71 docs agent, #77 orchestration agent, #83 GitHub agent.
5. **Enterprise reach:** #57 CI/CD and #64 IaC.

## Open strategic decision (resolve before the OSS split)

- [DECISION — How much of BOSS goes open source (fork #1)](DECISION-oss-licensing-strategy.md) —
  engine-only OSS vs whole Community control plane (AGPL) vs the recommended middle; must be
  answered (product + legal) before #25's `main.py` router split is cut.

## Specs in this directory

- [00 — Reactive event + webhook foundation](00-event-and-webhook-foundation.md)
- [01 — OSS engine split & packaging](01-oss-engine-split-and-packaging.md)
- [02 — Preview, sandbox & feedback](02-preview-sandbox-and-feedback.md)
- [03 — Skills & extensibility](03-skills-and-extensibility.md)
- [04 — CI/CD & deployment](04-cicd-and-deployment.md)
- [05 — Infrastructure-as-Code](05-infrastructure-as-code.md)
- [06 — Documentation agent](06-documentation-agent.md)
- [07 — Orchestration agent](07-orchestration-agent.md)
- [08 — GitHub community agent](08-github-community-agent.md)
- [09 — First-time output quality](09-first-time-output-quality.md)
- [10 — Runtime & Concurrency: Go services for high-scale task execution](10-go-concurrency-services.md)
