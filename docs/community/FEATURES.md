# Features

Everything in BOSS Community as of `v1.0-rc8`, grouped by area. Cloud and Enterprise capabilities are marked so the open-core boundary is explicit; the full v1 scope is tracked in [V1_FEATURE_LOCK.md](https://github.com/seanventures/boss/blob/main/docs/product/V1_FEATURE_LOCK.md).

**Legend:** ✅ Stable · ◐ Preview: works today, still being hardened · ○ Planned · ✦ Cloud / Enterprise only

## Control plane

| Feature | Status |
|---|---|
| Durable tasks, plans, jobs (Postgres) | ✅ |
| Job leases, heartbeat, fencing | ✅ |
| Deadletters and remediations | ✅ First-class remediations (`retry` / `requeue` / `escalate` / `hold`) on `/v1/deadletters/*` and Mission Control. Retry never silently drops. |
| Event / audit feed | ✅ |
| Global activity log | ◐ The Activity log: a query bar, facets, a histogram, a live tail, a drawer per line, and export. Credentials are masked whichever field they were written into. |
| Decision inbox (approve / reject / escalate) | ✅ Hold `reason` and `default_action` on the card; GET `/v1/decisions/{id}`. |
| Receipts with unique dedupe keys | ✅ |
| Budget quote → reserve → consume / release | ✅ |
| Artifacts and evidence packets | ✅ |
| Single org / workspace / project model | ✅ |
| Named accounts and API keys | ◐ See Identity below. |
| Govern: every limit in one place | ◐ Each limit's real value, what enforces it, and what it stopped; Spend by agent and by model; envelopes; a month-cap preview counted as the governor counts it. |
| Capacity-aware builds | ◐ Builds are admitted against free memory and queue instead of thrashing; workloads run in an envelope so the control plane keeps its memory. |
| Process ownership and reaping | ✅ Every process a build starts is owned, and Stop stops it. A reaper clears stale previews, orphans, temp dirs, images and disk on a clock. |
| System doctor | ◐ Shepherd and Metatron watch the machine, explain pressure in words, and offer choices. Work waiting on a person is never stopped. |

## Identity

| Feature | Status |
|---|---|
| Named accounts and the claim | ◐ Each person signs in as themselves; the first visit to `/claim` makes the first Owner. [Upgrade to named accounts](IDENTITY_UPGRADE.md). |
| Sign-in methods | ◐ Passkeys, password with an authenticator app, recovery codes, email links and codes (SMTP). [Sign in](CONSOLE_AUTH.md). |
| Single sign-on, social sign-in, SCIM | ◐ OIDC and SAML, GitHub/Google/Microsoft/Apple, SCIM 2.0, Require SSO for domains. [Sign-in setup](SIGN_IN_SETUP.md). |
| Access roles, groups, project access | ◐ Five built-in roles, custom roles with an impact preview and versions, groups, effective access. [People and access](PEOPLE_AND_ACCESS.md). |
| Service accounts and API keys | ◐ Keys shown once, scoped, expiring, with reminders and optional revocation when unused. |
| Agents acting for a person | ◐ Passports, delegation grants, run tokens, a revocation cascade. [Agents and access](AGENTS_AND_ACCESS.md). |
| GitHub through boss-egress | ◐ Builds hold no credential: a per-build GitHub App token, or the person's own. |
| Build sandbox | ◐ A user per build and a fenced network. [Build sandbox](SANDBOX.md). |
| Security audit log | ◐ Append-only, hash-chained, verifiable, exportable, 400 days. [Identity administration](IDENTITY_ADMIN.md). |
| CSV import/export, identity webhooks, several organisations | Not yet. |

## Console

| Area | Status |
|---|---|
| Command Center | ◐ One read: your team, what needs you, what is in flight, templates, apps built, and 14 weeks of activity. |
| New project | ◐ One screen for a new app, or a feature added to an existing app that builds onto that app's branch. |
| Research and Panels | ◐ Opened from a workflow step: the research, the panel's votes in words, and the PRD by section. |
| Factory and Apps | ◐ The Floor, a build's cockpit (the Line, Story, Outputs, Logs), and one table of apps with their crew, PR, deploy and spend. Stop answers at once. |
| Infra | ◐ Fleet, Deploys and Previews, and Cloud. Host deploys go through plan → approve → apply. |
| Observability | ◐ Pulse, per-app drawers, Models, System, and Sources. |
| Activity log and Govern | ◐ See Control plane. |
| Agent Studio and Organization | ◐ Agents with a Passport and Loadout (skills, MCP, instructions); Organization as Teams, Flow and Lines. |
| Setup | ◐ Repo and models, Model routing, and the next step for a new install. |
| Skills & MCP (Kit) | ◐ The Library and connections; a skill that calls an MCP server never carries the server's reach. |

## Interfaces

| Feature | Status |
|---|---|
| HTTP control-plane API (OpenAPI) | ✅ |
| CLI | ✅ |
| MCP server for agent clients | ◐ All 983 mounted Community operations are mapped: 851 use API policy, 64 need separate exact-request human confirmation, none remain held, and 68 protocol/internal routes are excluded. See [MCP coverage](MCP.md). |
| Self-hosted Mission Control console | ✅ |
| API-first clients (`packages/api-client`) | ✅ |

Every interface goes through the same HTTP API, so server-side policy and audit checks apply to terminal requests exactly as they do to other API clients.

- [Terminal API guide](TERMINAL_API.md): Metatron commands, direct `/v1/` calls, and one-shot JSON for scripts.
- [Terminal capability matrix](TERMINAL_CAPABILITY_MATRIX.md) and [MCP operation matrix](mcp-api-capability-matrix.json): how each mounted operation maps to each surface.

## Models & skills

| Feature | Status |
|---|---|
| Bring-your-own API keys (SecretRefs) | ✅ |
| Model router | ◐ Setup › Model routing. **Fixed** (the default) keeps today's behavior: the model you chose. **Smart** picks by tier with per-step floors, spends subscription headroom first and then OpenRouter, escalates on failure, and never strands a step when one source is down. Every step shows Why this model. |
| SecretRef consumption (factory / OpenAI / GitHub / Plane) | ◐ Fail-closed: a bound, usable ref wins; env is a last resort only when no ref is bound. Unresolvable bound refs never fall back. |
| Official QA /reports v1.1 | ◐ Honesty helper, inspect gate, and Mission Control scorecard island. Missing UI/UX stays `not_scored`; scores are never invented. Live official POSTs may still be in flight. |
| Local / OpenAI-compatible base URLs | ✅ |
| Local model entitlement broker | ✅ |
| Role packs and skill packs (specpack) | ✅ |
| Starter agent catalog | ◐ First-run seed installs 34 agents (including the mobile template), 3 panels, and specpack workflows. Templates default to side-effect-free briefs; mobile device execution requires a separate governed runner. A2A reports `not_configured` when Redis is unset rather than a false-red degraded state. |
| Pack YAML import / export in Mission Control | ✅ |
| Managed multi-tenant model gateway | ✦ Cloud |
| Hosted skill marketplace | ✦ Cloud |

### Plane integration (optional)

Community installs and runs without Plane, an ops token, or a private registry. If you connect a Plane instance you run, HTTPS certificate verification stays on by default.

- For a certificate authority you operate, set `PLANE_TLS_CA_FILE` to a PEM file readable by the API and worker.
- `PLANE_TLS_INSECURE_SKIP_VERIFY=true` is an explicit local-only fallback for when no CA you trust is available.

!!! warning
    Only use `PLANE_TLS_INSECURE_SKIP_VERIFY` against a host you operate.

## Software Factory

| Feature | Status |
|---|---|
| Outcome → governed code task | ✅ |
| Spec / architecture / implement / test phases | ✅ |
| Sandbox execution plane | ◐ Local subprocess is the Community coding path. Firecracker/microVM claims are **frozen** (`BOSS_MICROVM_CLAIMS_FROZEN=true`). The in-repo Go S1 sandbox is a fail-closed scaffold: not live, not GA. App templates are applied on the local sandbox only: on a micro-VM install an apply is refused (409 `template_tests_need_local_sandbox`) because the guest does not return the JUnit report the `template_tests` gate counts. Retained sandbox inspection requires project access; hold and delete controls stay disabled until their budget, approval, rollback, and receipt paths are complete. |
| Evidence-backed completion | ✅ |
| Discovery web research | ◐ Runs as a governed worker job: each research stage is budget-reserved, leased and receipted, and a retry replays stages already paid for instead of calling them again. Uses the Claude Code subscription; a local model cannot do web research. A workflow's research step runs it when that subscription can be used, and otherwise is recorded as skipped with the reason and a `workflow.research_skipped` event, and the run goes on to the panel. |
| Live debugging investigations | ◐ Past results remain readable. New Claude CLI investigations and legacy queued execution are paused until leased, budgeted model calls and governed host/database effects are complete. |
| Factory quality pass after green verify | ◐ One improve pass when UI/UX evidence is thin. Frozen tests stay frozen, and a failed pass restores the last green implementation. Unknown JTBD / `a11y_violations` stay `not_scored`. Stays partial. |
| PR / review packets (with GitHub) | ✅ |
| Browser / automated test evidence | ✅ |
| Deployment target metadata and recorded history | ✅ |
| Governed host deployment | ◐ Infra › Deploys plans a deploy to a verified SSH host as a ChangeSet; a person approves it, BOSS applies it, writes a receipt, and rolls back if the health check fails. Direct, unapproved host writes, cloud-account (registry) deploy runs, and auto-deploy stay paused in Community. |
| Agent host shell and host-backed preview sandboxes | ◐ Paused in Community until host changes have approved, budgeted, receipted execution. Simulated preview records remain available. |
| Managed elastic worker fleets | ✦ Cloud (planned) |

## GitHub

| Feature | Status |
|---|---|
| Project connection + repo binding | ✅ |
| Easy connect patterns | ✅ |
| Branch policies and immutable snapshots | ✅ |
| Fenced branch leases | ✅ |
| Safe sync and conflict resolution | ✅ |
| Merge authorizations | ✅ |
| Durable reconciler | ✅ |

## ProviderOps (your infra)

| Feature | Status |
|---|---|
| Provider accounts (model + infra keys) | ✅ |
| Read-only inventory sync | ✅ |
| ChangeSets (plan / approve / apply) | ✅ |
| Resource receipts and cost events | ◐ Explicit empty / unknown / unavailable labels; exact dollar amounts need billing-readable keys. |
| Worker pools and scale proposals | ✅ |
| Hosted connector marketplace | ✦ Cloud |

## Workforce

| Feature | Status |
|---|---|
| Role-scoped agents (Metatron, COO, PM, …) | ✅ |
| Focused always-on agents | ◐ Builder and duty loop (`/v1/focused-agents`). The worker ticks due always-on and hybrid agents every 60s; **Wake now** runs on-demand agents. Same validate-then-apply gates as the captain. until proven in live operation. |
| Metatron captain (persistent session / goals / watch) | ◐ Worker overwatch follows observe → measure → validate → apply-safe → verify. Decisions and deadletters stay human. |
| AI SRE kernel (awesome-ai-sre map) | ◐ Capability ledger at `GET /v1/ai-sre/capabilities` plus a Mission Control island. MCP adds list / diagnose / capabilities / verify / page. Observe-only worker tick at `POST /v1/ai-sre/overwatch` (scan / diagnose / dry-run page). Runbooks at `/v1/ai-sre/runbooks` (validate + Decision hold; `page_oncall` is dry-run only). Noise view at `GET /v1/ai-sre/noise` (clustered vs. raw, no invented percentages). `POST /v1/ai-sre/ask` returns a query plan only. Evidence-only incident postmortem at `POST/GET /v1/reliability/incidents/{id}/postmortem` (uses existing evidence only, never invents an RCA, never auto-resolves). Deterministic `GET /v1/ai-sre/bench` (InvestigationEngine fixtures). Never remediates, auto-applies Decisions, or merges PRs. **Preview.** |
| Work orchestration / portfolio | ✅ |
| Meetings / multi-agent panels | ✅ |
| Agent process handoffs | ◐ Authorable role sequences (natural-language compile, manual builder, Metatron/chat). Default sequence: PRD → architect/CTO/VP-PM review → improve → captain build → QA → UI/UX → captain verify → panel certify → captain accept. Simulated runs complete without spend. |
| Project Manager steward | ◐ Observes process runs, advances existing work, and proposes changes (`/v1/process-manager`, worker every 60s). It ensures the default template but does not automatically start simulated delivery runs. Explicit QA fixtures remain labeled as simulated; templates are never silently rewritten. |
| Assurance / ground-truth runtime | ✅ |
| Self-improvement automation routes | ✦ Enterprise mount |

### Metatron captain

Endpoints: one-shot `POST /v1/metatron/query`, plus `/v1/metatron/session|goals|watch|drive|overwatch`.

The captain is no longer a separate engine. A sentence sent to `/v1/metatron/session` is an Ask BOSS turn for the BOSS agent. The captain's watch, goals, drive, overwatch and theater are actions of that same agent (`captain_watch`, `captain_goals`, `captain_set_goal`, `captain_drive`, `captain_overwatch`, `captain_theater`). The routes are thin adapters that press those actions.

| Aspect | Behavior |
|---|---|
| Watch | The worker runs `captain_watch` on a schedule. It observes and scores goals and applies nothing |
| Overwatch, drive, theater | These apply moves, so they run only when a person asks: `/overwatch` in the terminal, the route, or pressing the agent's offer. The agent offers them and never runs them itself |
| Validation | Every captain action (goal, drive, overwatch apply) runs `validate_action` / `validate_move` first |
| Human gates | Decisions and deadletters never auto-apply |
| Changes to BOSS itself | Opened as a PR, never written silently |

!!! note
    The captain stays in preview until a validated apply has been proven in live operation on a release tag.

## Improve BOSS

| Feature | Status |
|---|---|
| Run scorecards, experiments and ratings | ◐ Behind one opt-in switch in Govern › Sharing, off by default. You see the exact payload before anything is sent. |
| Daily anonymous heartbeat | ◐ At most one a day. `BOSS_TELEMETRY=off` or `DO_NOT_TRACK=1` turns it off. There is no default collector: nothing is sent until you set a collector URL. |

## Open-core boundary

| Feature | Status |
|---|---|
| Full Community product without Cloud | ✅ |
| Optional Easy Connect to BOSS Cloud | ✅ |
| Hosted Account Connect & seats | ✦ Cloud |
| Self-hosted SSO (OIDC / SAML / SCIM) | ✅ Community |
| IdP catalog + registry (Okta, Entra, …) | ✅ Community |
| HA / multi-region clustering | ✦ planned Enterprise |
| Premium support SLAs | ✦ Enterprise |

## What “done” means in Community

A model saying a task is finished does not complete it. A task is done when:

1. Evidence exists (tests, artifacts, traces).
2. Required receipts exist for side effects.
3. Budget reservations settled correctly.
4. Any sensitive step has a recorded decision.
