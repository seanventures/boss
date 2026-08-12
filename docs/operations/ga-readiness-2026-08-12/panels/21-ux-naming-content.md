# Panel 21 — UX naming & content hierarchy (GA readiness)

**Date:** 2026-08-12  
**Role:** Content Design / Naming Expert  
**Scope:** Mission Control console labels, nav hierarchy, and operator vocabulary — **rename and re-group only; no feature removal**  
**Primary surfaces:** `apps/web/app/ConsoleShell.tsx` AREAS, panel UIs, design system language, PRD terms (Mission Control, Software Factory, ProviderOps, Evidence, Assurance)  
**Related panels:** `06-mission-control-ux.md`, `09-assurance-autonomy.md`, `14-ops-observability.md`, `PUBLIC_SITE_UX_AUDIT.md`

---

## 1. Problem statement

Operators currently meet **overlapping product nouns** that describe different jobs of work:

| Collision cluster | Surfaces today | Operator confusion |
| --- | --- | --- |
| **Home / snapshot** | Command Center · Mission Dashboard | Two “mission” homes; which do I open first? |
| **Software work** | Code Tasks · Coding Pipeline · Software Factory (docs/marketing) · CI/CD | Is “pipeline” a run, a policy, or the whole factory? |
| **Health & recovery** | Monitoring · Reliability · Operations · Task Shepherd | Three ops desks + a shepherd; who owns an incident? |
| **Proof** | Evidence · Assurance · receipts / scorecards | Is a receipt “evidence” or do I need an Assurance Case? |
| **Work lists** | Task Board · Work Allocation · Approvals | Board vs portfolio vs inbox — where is *my* work? |
| **Config** | System Configuration · Capabilities · Enterprise · GitHub · Sandboxes | Settings scatter across Platform with work-like names |

Marketing already simplified the **promise loop** to:

> **Outcome → Plan → Work → Evidence → Decide**

(`PUBLIC_SITE_UX_AUDIT.md`). The console must mirror that loop without collapsing distinct control-plane objects.

**Hard constraint:** No feature removal. Every current area keeps its capability; we change **labels, group placement, subtitles, and in-panel hierarchy** so Settings vs Work vs Evidence read as three clear planes.

---

## 2. Three planes (content hierarchy)

Give every nav item exactly one **primary plane**. Secondary planes may appear as deep-links or tabs, never as peer top-level names for the same job.

```text
┌─────────────────────────────────────────────────────────────────────────┐
│  WORK          — create, assign, execute, unblock, deploy               │
│  GOVERN        — decide, budget, prove, communicate, bound autonomy     │
│  SETTINGS      — connect systems, configure policy, identity, editions  │
└─────────────────────────────────────────────────────────────────────────┘
```

| Plane | Operator question | Typical verbs | Must not pretend to be |
| --- | --- | --- | --- |
| **Work** | “What should the company do next, and is it moving?” | Assign, run, resume, deploy, remediate | Settings or formal proof programs |
| **Govern** | “Is spend, risk, and proof under control?” | Approve, reserve, verify, gate, score | Day-to-day task execution |
| **Settings** | “How is this instance wired?” | Connect, configure, install, enable | Live fleet or proof completeness |

### 2.1 Recommended shell groups (relabel existing Run / Govern / Platform)

Today `GROUP_ORDER` is Platform → Run → Govern (`ConsoleShell.tsx`). That elevates design-system and config above the operating loop.

| Today | Recommended label | Role |
| --- | --- | --- |
| **Run** | **Work** | Primary operating loop (CEO / COO day job) |
| **Govern** | **Govern** (keep) | Budget, policy, proof, roster, comms |
| **Platform** | **Settings** | Connections, execution plane, editions, design foundations |

**Default order for GA:** **Work → Govern → Settings** (Platform last). Keep Foundations/Components under Settings, optionally collapsed behind an “Internals” disclosure for community GA.

### 2.2 Home rule (one primary, one secondary)

| Role | Surface | Job |
| --- | --- | --- |
| **Primary home** | **Command Center** | Situation room: what changed, top risks, next actions, outcome composer |
| **Secondary snapshot** | **Mission Dashboard** | Cross-phase KPI / budget / queue / health strip — read-mostly |

Do **not** use both “Mission” and “Command” as equal homes. Operators land on Command Center; Dashboard is the metrics companion (or a tab/section inside Command Center if a later UX pass nests them — nesting is optional; **rename clarity is required even if both stay top-level**).

---

## 3. Glossary (canonical operator language)

Use these terms in nav labels, eyebrows, empty states, docs, and PR descriptions. Parentheticals are **internal/code** names that may remain in APIs.

### 3.1 Product & shell

| Term | Definition | Use | Avoid saying |
| --- | --- | --- | --- |
| **BOSS** | Business Operating System Sentinel — durable control plane for AI labor | Product name | “the chatbot”, “OpenClaw” |
| **Mission Control** | The operator console product surface | Marketing + shell chrome | Calling a single nav item “Mission Control” |
| **Command Center** | Situation room: changes, risks, next actions, command intake | Primary home area (`overview`) | “Mission Dashboard” for the same job |
| **Mission Dashboard** | Live KPI / health / queue snapshot | Metrics companion (`dashboard`) | “Command Center” |
| **Metatron** | Executive/operator conversational interface | API/docs; UI: **BOSS Agent** | Exposing “Metatron” as nav without gloss |
| **BOSS Agent** | Chat surface that routes into governed workflows | Nav (`agent`) | Implying chat is the system of record |

### 3.2 Work objects

| Term | Definition | Use | Avoid saying |
| --- | --- | --- | --- |
| **Outcome** | Operator intent that becomes plans/tasks | Composer, Discovery | “ticket” as the only word for intent |
| **Task** | Durable unit of work with lifecycle | Task Board | “job” when meaning the human-visible work item |
| **Job** | Leased execution unit in the worker plane | Ops queue, internals, receipts | “task” for lease/heartbeat objects |
| **Decision** | Explicit approval gate before real effects | Approvals inbox | “approval” for the object id if API says decision |
| **Approval** | Human action on a decision | Buttons, nav label **Approvals** | Using “Approvals” for the Task Board |
| **Plan** | Structured decomposition before code/execution | Code task plans, Software Factory | “pipeline” for a single plan |
| **Artifact** | Durable output (spec, diff, report, packet) | Evidence trail | Treating artifacts as receipts |

### 3.3 Software delivery cluster

| Term | Definition | Use | Avoid saying |
| --- | --- | --- | --- |
| **Software Factory** | **Department / product capability** — intent → code → tests → review → release | Docs, marketing, skill packs | A third nav peer next to Code Tasks + CI/CD |
| **Code Task** | One governed coding assignment in a sandbox → gated PR | Nav **Code Tasks** (`code_tasks`) | “pipeline run” as the task itself |
| **Coding Pipeline** (policy) | Verification-amplified generation **policy**: profiles, tiers, model cost dials (VAG) | Govern surface — see rename table | “Code Tasks” or “CI/CD” |
| **CI/CD** | Deploy path: code task / PR → deployment run steps | Nav **CI/CD** (`delivery`) | “Monitoring” for deploys |
| **Sandbox** | Isolated execution environment for agent code work | **Sandboxes** settings + Code Tasks deep link | “CI/CD target” for sandbox console |

### 3.4 Health, recovery, control-plane ops

| Term | Definition | Use | Avoid saying |
| --- | --- | --- | --- |
| **Monitoring** | Health of **deployed apps** + metrics stack (Grafana etc.) | App SLOs, probes, dashboards | Control-plane deadletters |
| **Reliability** | Incident lifecycle — detect, RCA, governed fix, learn (AI SRE) | Incidents, remediations | Generic “ops health” |
| **Operations** | **Control-plane** operator desk: deadletters, audit, replay, canaries, self-improvement programs | BOSS kernel ops | App Grafana as “Operations” |
| **Task Shepherd** | Reasoning/remediation for **stuck work** (tasks/jobs) | Stuck-work panel | Full SRE incident desk |

### 3.5 Proof cluster

| Term | Definition | Use | Avoid saying |
| --- | --- | --- | --- |
| **Receipt** | Durable record of an effect with unique `dedupe_key` | Hard-rule language, Evidence detail | “log line” |
| **Evidence** | Operator-facing **proof of what happened**: receipts, throughput, cost, scorecards, review packets | Nav **Evidence** (`reports`) | Formal Assurance Case completeness |
| **Assurance** | Formal **proof program**: claims, ground truth, adversarial verification, policy evaluation | Nav **Assurance** | Throughput dashboards |
| **Evidence bundle** | Packaged artifacts for a software/unit of work | Software Factory DoD | Empty “healthy system” chrome |
| **Scorecard** | Scored dimensions over evidence (may leave gaps unscored) | Evidence / benchmarks | “Assurance Case” |

### 3.6 Work prioritization

| Term | Definition | Use | Avoid saying |
| --- | --- | --- | --- |
| **Task Board** | Kanban of tasks (Backlog → Done) + related jobs/receipts | Day-to-day execution board | Portfolio ranking |
| **Work Allocation** | Ranked priorities, quotas, leases, tickets, delivery posture | Portfolio / capacity | Replacing Task Board |
| **Lease** | Time-bounded worker ownership of a job | Ops, allocation detail | “assignment” without TTL semantics |

### 3.7 Money, policy, identity, providers

| Term | Definition | Use | Avoid saying |
| --- | --- | --- | --- |
| **Budget** | Caps, reservations, consumption, pressure | Govern **Budget** | FinOps cloud bills only |
| **Budget reservation** | Hold before spendy work | Hard rule copy | Silent spend |
| **ProviderOps** | Elastic resource plane for external providers | System Configuration suite / docs | “System Configuration” alone for all of BOSS |
| **ChangeSet** | Planned provider write with rollback | Infra write path | Raw provider API calls |
| **Resource receipt** | Receipt for provider-side effects | ProviderOps / Evidence | Generic receipt without provider lineage |
| **Capabilities** | Skills & MCP tools agents may use | Settings inventory | Agent Studio builder |
| **Agent Studio** | Build/compose/observe multi-agent work | Authoring surface | Runtime Task Board |
| **Agent Roster** | Roles, packs, modules, access | Govern org surface | Studio catalog |
| **Enterprise** | Editions, identity, account connect | Settings / commercial | Implying community has full cloud IDP |

---

## 4. Rename table (no feature removal)

**Rules**

1. **Stable `id` / route keys stay** (`overview`, `dashboard`, `code_tasks`, …) unless a separate engineering ticket renames them — labels and subs change first.  
2. Every row keeps the same backend feeds and panels.  
3. Subtitles are operator-facing one-liners (replace current `sub` strings in `ConsoleShell.tsx`).  
4. Where “keep label” is recommended, the **subtitle and plane** still change for clarity.

### 4.1 Nav areas — full map

| Area `id` | Today label | Recommended label | Plane | Recommended subtitle | Rationale |
| --- | --- | --- | --- | --- | --- |
| `overview` | Command Center | **Command Center** (keep) | Work (primary home) | Situation room — changes, risks, next actions | Distinct from metrics dashboard |
| `dashboard` | Mission Dashboard | **Mission Snapshot** *or keep* **Mission Dashboard** | Work | KPIs, budget pressure, queue depth, health | Drop competing “command” connotations; optional rename reduces dual-home feel |
| `agent` | BOSS Agent | **BOSS Agent** (keep) | Work | Chat intake into governed workflows | Avoid elevating chat above durable state |
| `decisions` | Approvals | **Approvals** (keep) | Work *or* Govern* | Decide before real-world effects | Inbox is operational; policy lives under Govern |
| `work` | Task Board | **Task Board** (keep) | Work | Tasks by state — backlog to done | Canonical execution board |
| `portfolio` | Work Allocation | **Portfolio** | Work | Ranked outcomes, quotas, leases, delivery posture | “Allocation” sounds like settings; Portfolio is work prioritization |
| `discovery` | Discovery | **Discovery** (keep) | Work | Research signals → dossiers & candidate outcomes | Clear upstream of Task Board |
| `meetings` | Meetings | **Meetings** (keep) | Work | Multi-agent panels → PRD drafts | Keep; not a settings surface |
| `code_tasks` | Code Tasks | **Code Tasks** (keep) | Work | Sandbox coding → tests → gated PRs | Operational Software Factory work units |
| `delivery` | CI/CD | **Deployments** *or keep* **CI/CD** | Work | PR → deploy steps, logs, where it stopped | “CI/CD” is familiar; **Deployments** better matches step model |
| `monitoring` | Monitoring | **App Health** | Work | Deployed app probes, metrics, dashboards | Disambiguates from control-plane Operations |
| `shepherd` | Task Shepherd | **Stuck Work** | Work | Shepherd reasoning on blocked tasks & jobs | Outcome-named; keeps feature, clearer job |
| `reliability` | Reliability | **Incidents** | Work | AI SRE — incidents, RCA, governed fixes | Verb-object clarity; “Reliability” is a property not a desk |
| `reports` | Evidence | **Evidence** (keep) | Govern | Receipts, cost, throughput, scorecards | What happened — not formal proof program |
| `budget` | Budget | **Budget** (keep) | Govern | Caps, reservations, live spend pressure | Core moat language |
| `pipeline` | Coding Pipeline | **Pipeline Policy** | Govern | Verification profiles, tiers, model cost dials | Not a run queue; not Code Tasks |
| `comms` | Communications | **Communications** (keep) | Govern | Outbox discipline before external sends | Keep; empty-state honesty separate |
| `ops` | Operations | **Control Plane Ops** | Govern | Deadletters, audit, replay, improvement programs | Not app monitoring |
| `assurance` | Assurance | **Assurance** (keep) | Govern | Claims, ground truth, proof gates | Formal proof — link *to* Evidence, not replace it |
| `org` | Agent Roster | **Agent Roster** (keep) | Govern | Roles, packs, modules, access | Staffing/governance not Studio |
| `github` | GitHub | **GitHub** (keep) | Settings | Repos, branch truth, sync, merges | Connection + policy for SCM |
| `infra` | System Configuration | **Providers & Resources** | Settings | Connections, inventory, ChangeSets, FinOps | Matches ProviderOps; less vague than “System” |
| `capabilities` | Capabilities | **Skills & MCP** | Settings | Tools agents may call — inventory & audit | Concrete; “Capabilities” is abstract |
| `studio` | Agent Studio | **Agent Studio** (keep) | Settings *or* Work† | Build, test, compose multi-agent systems | Authoring; not the Task Board |
| `sandboxes` | Sandboxes | **Sandboxes** (keep) | Settings | Execution plane — inspect & governed console | Settings for plane; Code Tasks for work |
| `enterprise` | Enterprise | **Edition & Identity** | Settings | Editions, IdP, account connect | Honest about commercial/settings nature |
| `foundations` | Foundations | **Design Foundations** | Settings (internals) | Tokens, type, motion, status language | Mark as internal/dev |
| `components` | Components | **UI Components** | Settings (internals) | Component library showcase | Mark as internal/dev |

\* **Approvals:** Prefer **Work** if the CEO’s daily path is Command Center → Approvals → Task Board; badge counts belong next to work. Policy docs stay Govern.  
† **Agent Studio:** Default **Settings** for community GA (authoring/config); enterprise multi-agent ops teams may pin it under Work later without renaming.

### 4.2 Collision clusters — before / after story

#### A. Mission Dashboard vs Command Center

| | Before | After |
| --- | --- | --- |
| Primary home | Ambiguous dual “Mission/Command” | **Command Center** only |
| Metrics | Mission Dashboard peers as home | **Mission Dashboard/Snapshot** = read companion; subtitle never says “next actions” |
| Optional later UX | Two top-level items | Dashboard as **tab/section** inside Command Center (feature preserved via deep link `?area=dashboard`) |

#### B. Code Tasks vs Coding Pipeline vs Software Factory

| Concept | Operator home | Not |
| --- | --- | --- |
| **Software Factory** | Product story + skill pack; optional eyebrow on Code Tasks (“Software Factory”) | Separate nav item |
| **Code Tasks** | Work: run coding assignments | Policy or deploy history |
| **Pipeline Policy** (was Coding Pipeline) | Govern: VAG profiles / cost dials | A queue of PRs |
| **CI/CD / Deployments** | Work: release steps | Model tier configuration |

Cross-links (keep features, fix mental model):

- Code Tasks → “Open Pipeline Policy” / “View deployment for this PR”  
- Pipeline Policy → “Active code tasks using this profile”  
- Evidence → “Receipts & review packets from code tasks”

#### C. Ops vs Reliability vs Monitoring

| Surface | Owns | Does not own |
| --- | --- | --- |
| **App Health** (Monitoring) | Deployed app probes, metrics stack | Deadletters, BOSS job leases |
| **Incidents** (Reliability) | Incident records & governed remediations | Routine deploy stepper |
| **Control Plane Ops** (Operations) | DLQ, audit, replay, canaries, improvement programs | Grafana for customer apps |
| **Stuck Work** (Task Shepherd) | Why *this* task/job is stuck | Org-wide SEV program |

Empty-state copy should state the plane: e.g. Monitoring “No deploy targets configured” ≠ Operations “No deadletters”.

#### D. Evidence vs Assurance

| | Evidence | Assurance |
| --- | --- | --- |
| Question | “What did the system do, and what did it cost?” | “Have we proved claims against ground truth?” |
| Objects | Receipts, scorecards, throughput, cost | Cases, claims, measurements, adversarial checks |
| Empty meaning | Quiet period / no runs yet | **Proof program not started** (must not look “healthy”) |
| Link | Evidence rows link into Assurance claims when attached | Assurance cites Evidence/receipts as supporting material |

#### E. Work Allocation vs Task Board

| | Task Board | Portfolio (Work Allocation) |
| --- | --- | --- |
| Grain | Individual tasks | Ranked outcomes / capacity / leases |
| Metaphor | Kanban | Portfolio / WIP governance |
| Primary user moment | “Move this card” | “What should we fund next?” |

---

## 5. In-panel content hierarchy patterns

Apply consistently so renames stick.

### 5.1 Page skeleton

```text
Eyebrow (plane)     e.g. WORK · SOFTWARE FACTORY
Title (area label)  e.g. Code Tasks
One-line job         matches nav subtitle
Primary CTA          one per page above the fold
Secondary links      to sibling planes (Policy · Evidence · Deployments)
Data / boards
Honesty footer       edition, empty, or host-not-connected
```

### 5.2 Shared cross-links (features stay, navigation teaches)

| From | Offer |
| --- | --- |
| Command Center | Approvals count · Stuck Work · Budget pressure · top Evidence anomalies |
| Code Tasks | Pipeline Policy · Sandboxes · Deployments · Evidence packet |
| Deployments / CI/CD | App Health · related Code Task · Receipts |
| App Health | Incidents · Deployments |
| Incidents | Control Plane Ops (if kernel) · related Task |
| Evidence | Assurance case · Budget · specific Receipt |
| Assurance | Supporting Evidence · Policy snapshot |
| Portfolio | Task Board filtered by outcome |
| Providers & Resources | Budget · ChangeSet approvals |

### 5.3 Status language (align design system)

Keep design-system status words; do not invent parallel sets per panel:

- Verified / Awaiting / Blocked / Live (design tokens)  
- Receipt states stay effect-oriented (created, applied, failed, deduped)  
- Never use green “healthy” chrome for **unconfigured** Assurance, Monitoring, or ProviderOps

### 5.4 Settings vs Work chrome

| Signal | Work | Settings |
| --- | --- | --- |
| Primary CTA | Assign / Approve / Deploy / Resume | Connect / Save / Install / Enable |
| Live pulse | Encouraged on queues & health | Prefer static config + last-sync |
| Badge counts | Pending decisions, DLQ, stuck | Connection errors only |
| Empty state | “No tasks yet — create outcome” | “Not connected — add provider” |

---

## 6. Marketing ↔ console alignment

| Marketing loop step | Console home | Supporting surfaces |
| --- | --- | --- |
| Outcome | Command Center composer · Discovery | Portfolio |
| Plan | Code Task plan · Meetings PRD | Pipeline Policy (how hard we verify) |
| Work | Task Board · Code Tasks · Stuck Work | Agent Roster, BOSS Agent |
| Evidence | **Evidence** | Receipts inside task/deploy detail |
| Decide | **Approvals** | Budget gates, Assurance gates |

**Software Factory** remains the **story name** for the coding department on the public site; in-console it is an **eyebrow / section label** over Code Tasks + Deployments + Pipeline Policy — not a fourth competing top-level name.

---

## 7. Implementation guidance (copy-only first)

Phased so GA can ship clarity without feature rewrites.

| Phase | Scope | Risk |
| --- | --- | --- |
| **P0 — Labels** | Update `label` + `sub` in `ConsoleShell.tsx`; match H1/eyebrows in panel UIs | Low — tests that assert exact strings need updates (e.g. Work Allocation) |
| **P0 — Group order** | Work → Govern → Settings; move `overview` under Work | Low–med — muscle memory |
| **P1 — Cross-links** | Sibling links in Code Tasks, Monitoring, Evidence, Assurance, Ops | Low |
| **P1 — Empty copy** | Plane-specific honesty (Assurance ≠ healthy; Monitoring ≠ kernel ops) | Low |
| **P2 — Optional nest** | Mission Dashboard as Command Center section; Foundations/Components under Internals | Med UX only |
| **P2 — Route cosmetics** | User-visible path aliases if ever introduced | Only with redirects; keep `id`s |

**Do not** rename API resources (`decision`, `receipt`, `ChangeSet`, job lease fields) in this initiative — glossary maps UI → API.

### 7.1 Test / string touch list (known)

- `apps/web/app/ConsoleShell.tsx` — AREAS labels/subs/groups  
- `apps/web/tests/work-orchestration.test.ts` — asserts `"Work Allocation"`  
- Panel unit tests that snapshot titles (GitHub Mission Control, etc.)  
- Docs that teach nav by label (`06-mission-control-ux.md`, design QA, marketing assets)

---

## 8. Rename decision summary (recommended defaults for GA)

If only one package of renames ships:

| Change | From → To |
| --- | --- |
| Group | Run → **Work**; Platform → **Settings** |
| Order | **Work → Govern → Settings** |
| `portfolio` | Work Allocation → **Portfolio** |
| `pipeline` | Coding Pipeline → **Pipeline Policy** |
| `monitoring` | Monitoring → **App Health** |
| `reliability` | Reliability → **Incidents** |
| `ops` | Operations → **Control Plane Ops** |
| `shepherd` | Task Shepherd → **Stuck Work** |
| `infra` | System Configuration → **Providers & Resources** |
| `capabilities` | Capabilities → **Skills & MCP** |
| `enterprise` | Enterprise → **Edition & Identity** |
| `dashboard` | Mission Dashboard → **Mission Snapshot** (optional but preferred) |
| Keep | Command Center, Task Board, Code Tasks, Evidence, Assurance, Budget, Approvals, BOSS Agent, Agent Roster, Agent Studio, Sandboxes, Discovery, Meetings, GitHub, Communications |

**Software Factory:** docs + Code Tasks eyebrow only — **no nav deletion, no new nav peer.**

---

## 9. Acceptance criteria (naming)

- [ ] Every nav item maps to exactly one plane (Work / Govern / Settings) in a published table (this doc).  
- [ ] No two top-level labels answer the same operator question (dual-home test: Command Center vs Dashboard).  
- [ ] Software work cluster: operator can explain Code Tasks vs Pipeline Policy vs Deployments in one sentence each.  
- [ ] Health cluster: App Health vs Incidents vs Control Plane Ops vs Stuck Work are non-overlapping in empty states.  
- [ ] Evidence vs Assurance: empty Assurance cannot read as “system healthy.”  
- [ ] Task Board vs Portfolio: grain (card vs ranking) is obvious from subtitle alone.  
- [ ] Settings CTAs never use Work verbs as primary (no “Deploy” as the only CTA on a connect-provider page).  
- [ ] Marketing Outcome→…→Decide maps to named console homes without inventing a second loop.  
- [ ] String assertion tests updated; no feature flags removed; no areas dropped from `AREAS`.

---

## 10. Out of scope

- Removing Enterprise, Communications, Foundations, or any area  
- Changing hard-rule object names in the API (`BudgetReservation`, `dedupe_key`, leases)  
- Full information architecture rebuild of every panel layout (cross-links and titles only for GA)  
- Plane issue workflow / ops credentials copy

---

## 11. Sources

- `apps/web/app/ConsoleShell.tsx` — AREAS + GROUP_ORDER  
- `docs/operations/ga-readiness-2026-08-12/panels/06-mission-control-ux.md`  
- `docs/design/BOSS_DESIGN_SYSTEM.md`  
- `docs/product/UI_REDESIGN_PLAN.md`  
- `docs/product/PRD-v2.0.md` (Mission Control, Software Factory, ProviderOps, receipts)  
- `docs/operations/ga-readiness-2026-08-12/PUBLIC_SITE_UX_AUDIT.md`  
- `design-qa.md` — Command Center / Task Board / Evidence language  

---

## 12. Verdict

**GA naming posture: Conditional go** — chrome density is a product risk, but **label collisions are fixable without feature removal**. Ship the **P0 label + group order** package so Settings vs Work vs Evidence (Govern proof) are unmistakable; keep Software Factory as the departmental story over Code Tasks rather than a rival nav name.
