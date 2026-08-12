# Mission Control UI/UX IA recommendations

**Date:** 2026-08-12  
**Scope:** Content / settings organization + simplification **without losing functionality**  
**Surface:** `apps/web` ConsoleShell (28 areas today)  
**Constraint:** Every existing `AreaId` stays deep-linkable (`?area=`), palette-reachable, and SSR-renderable  
**Living plan:** [`../GA_LIVING_PLAN.md`](../GA_LIVING_PLAN.md)

### Expert panel (6)

| # | Lens | Report |
|---|---|---|
| 16 | Founder / CEO IA | [panels/16-ux-ia-founder.md](panels/16-ux-ia-founder.md) |
| 17 | Settings organization | [panels/17-ux-settings-org.md](panels/17-ux-settings-org.md) |
| 18 | Workflow loop UX | [panels/18-ux-workflow-loop.md](panels/18-ux-workflow-loop.md) |
| 19 | Cognitive load | [panels/19-ux-cognitive-load.md](panels/19-ux-cognitive-load.md) |
| 20 | Edition-aware IA | [panels/20-ux-edition-ia.md](panels/20-ux-edition-ia.md) |
| 21 | Naming / content hierarchy | [panels/21-ux-naming-content.md](panels/21-ux-naming-content.md) |

---

## Consensus verdict

**Do not cut features.** Reorganize:

1. **Primary nav ≤ 8** founder jobs (daily path)  
2. **Hubs + nested children** for power surfaces  
3. **One Settings hub** for all wiring/config  
4. **Edition-aware chrome** (Community vs Enterprise)  
5. **⌘K palette** remains the complete map  
6. **Design system** out of production default nav  

**Density is fine** (mission-control design language). **Equal peer status for 28 areas is not.**

---

## Current problem (inventory)

| Group (today) | Count | Issues |
|---|---|---|
| Platform | 9 | Foundations/Components lead the list; Enterprise peer; System Config vs GitHub vs Capabilities split |
| Run | 14 | Full workflow stages compete as separate products |
| Govern | 6 | Budget/ops/assurance/roster mixed with runtime |

**28 top-level peers** · GROUP_ORDER starts Platform → design system is `AREAS[0]` · Command Center buried · Settings scattered · Community still sees Enterprise as a normal settings peer.

---

## Recommended information architecture

### A. Primary sidebar (default Community CEO · 8 items)

| # | Primary | Job | Contains / deep-links (all still exist) |
|---|---|---|---|
| 1 | **Home** | Situation | `overview` (+ optional KPI strip from `dashboard`) |
| 2 | **Ask BOSS** | Direct | `agent` |
| 3 | **Decide** | Approvals | `decisions` |
| 4 | **Work** | Do the work | `work`, `portfolio`, `discovery`, `meetings`, `shepherd`, `comms` |
| 5 | **Ship** | Factory + runtime | `code_tasks`, `delivery`, `github`, `sandboxes`, `pipeline`, `monitoring`, `reliability` |
| 6 | **Evidence** | Proof | `reports`, `assurance` |
| 7 | **Budget** | Money | `budget` (live) + link to budget *policy* in Settings |
| 8 | **Settings** | Wire the system | `infra`, `capabilities`, `org`, `ops`, `studio`, `enterprise`, `foundations`, `components` |

**Mission Dashboard (`dashboard`)** stays reachable as a Home companion tab or palette item — not a second peer “home.”

### B. Marketing / product loop alignment

```text
Outcome  →  Plan  →  Work  →  Evidence  →  Decide
discovery/meetings   portfolio   work/code_tasks   reports/assurance   decisions
         ↑ substrate: github · sandboxes · pipeline · delivery
         ↑ control: budget · settings (providers, keys, roster)
```

**Current Work rail (recommended):** sticky focus for active task / code_task / decision with stepper through the loop + blockers + receipts — so users follow one job without re-hunting the nav.

### C. Settings hub (content organization)

Single **Settings** area with internal sections (tabs or left subnav):

| Section | Scope | Surfaces mapped (AreaIds) |
|---|---|---|
| **Connections** | Project | `infra` (providers, secret-refs, inventory, ChangeSets, FinOps), `github` *connect* half, sandbox host, observability setup |
| **Tools & agents** | Project/Org | `capabilities` (MCP/skills), `studio` |
| **Governance** | Project | `pipeline` profiles, delivery/safety policies, improvement programs (community-safe), budget *caps/allocations* |
| **People** | Org/Project | `org` roster, packs, access |
| **Operations** | Project | `ops` audit/replay/DLQ tools, canaries |
| **Edition & identity** | Org | `enterprise` — **upsell / status on Community**; live SSO/SCIM only when edition allows |
| **Lab** | Operator | `foundations`, `components` (hidden from production default nav) |

**Dual-home rule (no function loss):**  
- **Live** Budget / GitHub merges / Sandbox fleet / CI/CD **runs** stay on Work/Ship operational paths.  
- Each links **“Configure…” → Settings section**.  
- Settings never owns the only path to day-to-day execution.

**Scopes (label clearly):**

| Scope | Examples |
|---|---|
| **This project** | Keys, GitHub binding, budget caps, pipeline profile |
| **This org** | Members, packs, SSO (enterprise) |
| **You** | Nav density role, focus mode, live refresh — chrome only |

### D. Edition-aware chrome (Community)

| Tier | What user sees |
|---|---|
| **Default** | 8 primaries above (or ~12 expanded default per panel 20 if Work/Ship expanded) |
| **Advanced** | Collapsed drawer: Discovery, Meetings, Portfolio, Shepherd, Monitoring, Reliability, Assurance, Studio, Sandboxes, Pipeline |
| **Enterprise** | Renamed **Editions** · badge · capability list · upgrade CTA — **not** live SSO/SCIM/cluster forms that 404 |
| **Hidden controls** | Paid POST/actions (Account Connect, marketplace, SCIM) — routes remain, buttons gated |

**ProviderOps / ChangeSets stay Community** (whole workflow) — do not park under Enterprise.

### E. Naming (high-signal renames — AreaIds stable)

| Today (label) | Recommend | Why |
|---|---|---|
| Approvals | **Decide** (or keep Approvals, sub: “decide before effects”) | Matches loop step |
| Mission Dashboard | **Mission Snapshot** | Not a second home |
| Coding Pipeline | **Pipeline policy** | Not the same as Code Tasks |
| CI/CD | **Delivery** | Less false Grafana promise |
| Monitoring | **App health** | Distinguish from control-plane ops |
| Reliability | **Incidents** | AI SRE entry |
| Operations | **Control plane ops** | DLQ / audit / replay |
| Task Shepherd | **Stuck work** | Plain language |
| Work Allocation | **Portfolio** | Capacity/leases vs kanban |
| System Configuration | **Providers & resources** (under Settings) | Clearer |
| Capabilities | **Skills & MCP** | Clearer |
| Enterprise | **Editions** (Community) | Honesty |
| Assurance | Keep; empty state must not look “healthy” | Proof gates vs Evidence receipts |

Full glossary: [panels/21-ux-naming-content.md](panels/21-ux-naming-content.md).

---

## Full AreaId map (nothing deleted)

| AreaId | Today label | Primary home | Secondary / nested |
|---|---|---|---|
| overview | Command Center | **Home** | — |
| dashboard | Mission Dashboard | Home companion / palette | KPI strip |
| agent | BOSS Agent | **Ask BOSS** | — |
| decisions | Approvals | **Decide** | — |
| work | Task Board | **Work** | default child |
| portfolio | Work Allocation | Work | Advanced |
| discovery | Discovery | Work → Outcome | Advanced |
| meetings | Meetings | Work → Outcome | Advanced |
| shepherd | Task Shepherd | Work | Advanced |
| comms | Communications | Work | Advanced |
| code_tasks | Code Tasks | **Ship** | default child |
| delivery | CI/CD | Ship | — |
| github | GitHub | Ship + Settings/Connections | dual-home |
| sandboxes | Sandboxes | Ship + Settings | dual-home |
| pipeline | Coding Pipeline | Settings/Governance | link from Ship |
| monitoring | Monitoring | Ship → Operate | Advanced |
| reliability | Reliability | Ship → Operate | Advanced |
| reports | Evidence | **Evidence** | — |
| assurance | Assurance | Evidence | Advanced until seeded |
| budget | Budget | **Budget** | policy half in Settings |
| ops | Operations | Settings/Operations | live health chips on Home |
| org | Agent Roster | Settings/People | staffing chip on Work |
| infra | System Configuration | Settings/Connections | — |
| capabilities | Capabilities | Settings/Tools | — |
| studio | Agent Studio | Settings/Tools | Advanced until seeded |
| enterprise | Enterprise | Settings/Editions | upsell Community |
| foundations | Foundations | Settings/Lab | hide_nav production |
| components | Components | Settings/Lab | hide_nav production |

**Aliases:** keep `?area=infra` etc. working forever (or one release with redirect to `?area=settings&section=connections`).

---

## Progressive disclosure rules

1. **Default = CEO Community job list** (≤8 primaries).  
2. **Advanced drawer** for power users; remember expand state per browser.  
3. **Role presets** (CEO / Eng / Admin / Builder / Full) change *sidebar density only*, not API permissions.  
4. **Empty states:** Setup CTA when unconfigured; quiet empty when legitimately zero; never fake health.  
5. **Focus mode:** collapse sidebar + slow live refresh; palette still has full map.  
6. **Edition:** Community never presents paid forms as broken settings.  
7. **⌘K:** primary discovery for all 28 areas + synonyms + attention (pending decisions, DLQ count).

---

## What we explicitly do **not** do

- Remove AreaIds, routes, or panels  
- Merge unrelated data models into one API  
- Move live Approvals/Code Tasks *only* under Settings  
- Market Assurance/Studio as populated when empty  
- Put ChangeSets/ProviderOps behind Enterprise  

---

## Implementation phases (recommended)

| Phase | Ship | Effort | Risk |
|---|---|---|---|
| **P0** | Labels/subs renames in `ConsoleShell`; default area `overview`; Foundations/Components `hide_nav` on dogfood; Enterprise → Editions badge | Small | Low |
| **P1** | 8-primary nav + nested groups; Settings hub shell with section router embedding existing panels | Medium | Medium |
| **P2** | Current Work rail + loop stepper deep-links | Medium | Medium |
| **P3** | Role presets, focus mode, server prefs | Medium | Low |
| **P4** | Optional deeper policy/runtime splits inside Settings | Large | Higher |

**No Phase requires deleting functionality.**

---

## Success metrics

| Metric | Target |
|---|---|
| Default Community primary nav count | ≤ 8 |
| Area coverage via palette | 100% of current AreaIds |
| Deep links `?area=` | Stable |
| Time-to-approve (CEO) | Home → Decide ≤ 2 clicks |
| Time-to-start code task | Home or Ship ≤ 2 clicks |
| Settings discoverability | All config surfaces under Settings + dual-home links |
| Community Enterprise chrome | No 404-as-settings |
| Feature loss | **Zero** |

---

## Suggested next PR (if implementing)

`ai/codex/mc-ia-phase-p0` — ConsoleShell labels, GROUP_ORDER Work→Govern→Settings, hide Lab nav, Editions badge, default overview.  
Follow with Settings hub shell + nested Ship/Work.

---

## Sign-off

| Panel | Stance |
|---|---|
| Founder IA | 8 primaries + Settings hub |
| Settings | Single hub, project/org/you scopes |
| Workflow | Hubs along Outcome→Decide + Current Work rail |
| Cognitive load | Hide-until-needed, palette complete |
| Edition | Default / Advanced / Editions upsell |
| Naming | Work vs Govern vs Settings language |

**Overall recommendation:** **Adopt the 8-primary + Settings hub IA.** Implement in phases P0→P2 first for GA trust and founder usability without any capability loss.
