# Panel 06 — Mission Control UI/UX (GA readiness)

**Date:** 2026-08-12  
**Role:** Mission Control UI/UX Expert Panel  
**Scope:** `apps/web` — ConsoleShell, major `*ui.tsx` panels, empty-state honesty, edition honesty, hydration, community vs enterprise surfaces  
**Primary evidence:** code review of current tree + `docs/operations/LIVE_UI_FUNCTIONAL_AUDIT_2026-08-11.md` + `.ai/ux-review.md` + web tests (`delivery-empty-states`, `datetime-hydration`, `accessibility`, panel unit tests)  
**Edition under dogfood:** `community` (goldeneye)

---

## Rating scale

| Grade | Meaning |
|---|---|
| **Works** | Operator can complete the primary job; empty/error states are honest; not edition-dead |
| **Partial** | Core path or UX present, but incomplete loop, residual a11y/hydration risk, or noisy/empty data |
| **Shell** | Navigable UI with APIs mostly 200 and forms/tables that look live, but no real end-to-end product loop on dogfood |
| **Dead** | Surface looks product-complete while primary backend routes 404 / not mounted, or host plane is down with no safe story |

Grades combine **code intent** (what the UI does when data is healthy) with **live community dogfood truth** (what operators see today).

---

## Executive verdict

Mission Control is a **wide, navigable console** with a strong operator shell (grouped nav, ⌘K palette, live refresh guards, skip link, status chips). Core control-plane areas (auth scope, agent chat, dashboard, budget, command center signals, capabilities inventory) **work**.

GA UX risk is not “missing chrome” — it is **enterprise-looking panels that overpromise on community**, residual **hydration mismatch** paths, and **empty machinery** that still reads as a live fleet unless the newer honesty patches land on the deploy under test.

Since the 2026-08-11 live audit, the tree shows deliberate bake-outs:

- **CI/CD + Monitoring** empty-state honesty (BOSS-344) — no fake fleet chrome at zero targets  
- **Self-improvement** enterprise-only honesty — hide 404 CTAs; show improvement-programs path  
- **Provider configuration** community BYOK copy + edition notices (code claims kernel is community; **live deploy may still 404** — packaging skew)  
- **Sandboxes** host-not-connected banner instead of empty grid pretending to be fine  
- **Hydration** helpers (`lib/datetime.ts`, `ClientRelativeTime`) + tests (BOSS-346); code-task timers use `suppressHydrationWarning`  
- **Previews** routed through gateway `/web/` rather than raw `127.0.0.1` sandbox URLs  

Those are the right directions. **GA still blocked** until edition-aware nav, remaining hydration hotspots, and dogfood wiring (GitHub MC, sandbox host, deploy targets, studio seed) match the chrome.

---

## Area scorecard

| Area | Grade | Functional vs shell | Empty / edition honesty | A11y basics | Hydration risk | Community dead surface? | GA blocker? |
|---|---|---|---|---|---|---|---|
| Console shell / nav | **Works** | Shell is the product | N/A (chrome) | Skip link, `aria-current`, dialog palette, labelled nav | Low (client island; area from URL/localStorage) | No | No |
| Command Center (overview) | **Partial** | Functional snapshot + actions | Empty activity copy present | Landmarks mixed; long page | Medium (heavy SSR; relative wait labels elsewhere) | ProviderOps fan-out noise if edition 404 | Perf/noise |
| BOSS Agent | **Works** | Chat + workspace load | Empty thread CTAs honest | Forms functional; chat shell | Low (client-only load) | No | No |
| Mission Dashboard | **Works** | Live KPI snapshot | Unavailable card if null | Status text, not icon-only | Low | No | No |
| Approvals | **Partial** | Approve/reject real | Inbox noisy (expired history) | Labels on forms | **Medium** — `waitedLabel` uses `Date.now()` in SSR `page.tsx` | No | Hydration residual |
| Task Board | **Partial** | Tasks/jobs/receipts real | Empty states present | List structure OK | Medium (prior #418; datetime helpers mitigate) | No | Residual #418 verify |
| Work Allocation | **Partial** | Overview + forms | Explicit empty outcomes/jobs | Labels on forms | Low–med (`ClientRelativeTime`) | No | Product loop depth |
| Discovery | **Partial** | List/create present | Seed empty OK | Forms labelled | Low | No | E2E deepen/promote unproven |
| Meetings | **Partial** | Rooms/meetings exist | “No meetings yet” + CTA | Deep-link after mount (hydration-aware) | Low–med | No | Multi-agent PRD loop unproven |
| Code Tasks | **Partial** | Full operator UI | Empty list CTA good; host honesty partial | Labels/forms | Mitigated (`suppressHydrationWarning` on timers) | No | Execution flaky / DLQ / previews ops |
| CI/CD | **Partial** | Onboard + deploy when targets exist | **Honest** zero-target setup path | Tables/forms; health graph `aria-label` | Low (UTC `formatDateTime`) | Deploy flag fail-closed notice | Dogfood: 0 targets = setup-only |
| Monitoring | **Partial** | Health when configured | **Honest** combined unconfigured card | Setup copy clear | Low | Observability stack not deployed ≠ outage | Same as CI/CD setup |
| Task Shepherd | **Partial** | Overview present | Empty when quiet | OK | Prior #418 area — re-verify | No | Depth of remediation UX |
| Reliability | **Partial** | Incidents list | “No incidents” honest | OK | Low | No | Remediate path unproven |
| Evidence / Reports | **Partial** | Summaries/feeds | Empty proof sets | OK | Prior #418 area | No | Residual hydration |
| Budget | **Works** | Status, quote, allocations | Empty alloc + unavailable | Landmarks + labels | Low (UTC format) | No | Intensity integer mapping polish |
| Coding Pipeline | **Partial** | Profiles + run costs | **Edition honesty** for custom profiles (Enterprise only) | OK | Prior #418 | Custom profiles gated correctly | None major |
| Communications | **Shell** | Render/local draft | Empty inbox/outbox | Forms | Low | External send fail-closed (OK) | Looks complete, no channel loop |
| Operations | **Partial** | Deadletters/audit OK; SIM gated | **SIM Enterprise-only** honesty + programs panel | Mixed | Low | SIM routes not on community | Hide remaining dead CTAs if any |
| Assurance | **Shell** | Overview chrome + zeros | “No cases yet” but product feels empty | Status badges | Low | No | Empty proof = non-demoable |
| Agent Roster / Org | **Partial** | Roles/packs/modules | Empty install records | OK | Prior #418 on org | Marketplace/commercial bits | Residual hydration |
| GitHub MC | **Shell** | Full forms + easy connect | Empty connect/bind CTAs; not “broken fleet” | Labels on easy-connect | Low (UTC-ish formatTime) | Unwired dogfood (0 conn/bind) | Wire or demote nav until connected |
| System Configuration | **Dead*** | Full ProviderOps suite UI | Community BYOK notice in code | Provider suite labels scanned | Low | **Live audit: ProviderOps 404 on community** | **Yes** — edition mismatch |
| Capabilities | **Works** | Skills/MCP inventory + add | Empty MCP + CTA | Labels | Low | No | No |
| Agent Studio | **Shell** | Rich workbench + seed CTA | Empty library + starter install | Tabs, sr-only filter, aria | Low (`ClientRelativeTime`) | Empty catalog / A2A degraded | Seed or setup wizard default |
| Sandboxes | **Partial** | Console when host up | **Honest** host-not-connected | Break-glass reason required | Low | Host connection refused on dogfood | Host plane ops |
| Enterprise | **Dead** | Full commercial/SCIM/cluster UI | Edition card honest; cluster CTAs still present | Forms labelled | Low | Account-connect / enterprise APIs 404 | **Yes** — hide or badge nav |
| Foundations / Components | **Works** | Design system only | N/A | Showcase | N/A | No | No |

\* **System Configuration** grade: **Dead** against 2026-08-11 live community (`apiReachable=false`, routes 404). **Code** now treats ProviderOps write kernel as community BYOK and softens edition limitations — if goldeneye remounts routes, re-rate to **Partial/Works**. Until then, UI overpromises.

---

## Console shell deep dive

**Source:** `apps/web/app/ConsoleShell.tsx`

**What works**

- 26+ areas grouped Run / Govern / Platform with icons, badges (pending decisions, deadletters)
- ⌘K jump palette (`role="dialog"`, Escape closes, focus on open)
- `aria-current="page"`, skip link to `#decision-inbox`
- Live auto-refresh with **stacking guards** (no refresh while transition pending, hidden tab, or focused input) — important for heavy SSR fan-out
- Area deep links via `?area=` + `history.pushState` without full refetch
- Control-plane online/offline + budget tone chips

**Risks**

- **All areas remain in nav** regardless of edition — Enterprise, full System Configuration, and SIM-adjacent Ops read as first-class community product
- Live interval title says “7s” in one chip tooltip while interval is 15s (copy drift)
- Single-page mount of all SSR panels still means operators pay full fan-out cost on every refresh (perf UX)

**Grade: Works** (chrome) with **GA note:** edition-aware nav is still required.

---

## Empty-state honesty — trend line

### Improved (shipped in tree)

| Surface | Pattern |
|---|---|
| CI/CD | Zero targets → setup-only (`data-empty-state="no-targets"`); no deploy wizard fleet chrome |
| Monitoring | Combined “not set up yet” when health + observability empty |
| Health | Zero targets ≠ `● not_deployed` fleet dashboard |
| Sandbox deploy flag | Explicit fail-closed notice when `BOSS_ENABLE_SANDBOX_DEPLOY=false` |
| Self-improvement | Enterprise-only badge; CTAs hidden; improvement-programs panel |
| Pipeline custom profiles | “Enterprise only” with edition id, forms disabled |
| Sandboxes | Host not connected → explanatory panel, not empty sandbox grid |
| Agent Studio | Starter catalog install when library empty |
| GitHub | Easy-connect when 0 connections/bindings |
| Previews | Gateway URL, not sandbox `127.0.0.1` |

Tests: `apps/web/tests/delivery-empty-states.test.tsx`, self-improvement / provider / github unit tests.

### Still weak

| Surface | Issue |
|---|---|
| Assurance | Zero counts look like a quiet healthy system, not “unconfigured proof program” |
| Comms | Empty outbox/inbox looks like a real messaging product with external channels |
| Enterprise area | Full Account Connect / SCIM / cluster **forms** remain when APIs 404 — `blockedReason` text alone is not enough |
| ProviderOps (live) | If routes 404, suite “unavailable” is correct; SSR still may pay fan-out cost every page |
| Ops workflows / canaries / chaos | Empty lists with run CTAs — OK only if CTAs work; standing programs missing |
| Approvals | Historical expired decisions dominate inbox → operator trust erosion |

---

## Edition honesty — community vs enterprise

| Feature | UI honesty today | Live community | Verdict |
|---|---|---|---|
| Self-improvement proposals | Strong — hide CTAs, Enterprise badge, edition id | 404 / not mounted | Honest **if** deployed build includes this panel |
| Improvement programs | Surfaced as community alternative | Empty but API class exists | Partial honesty |
| Pipeline custom profiles | Explicit Enterprise-only | N/A | Good |
| ProviderOps | Code: community BYOK + notice | Live audit: **404** | **Packaging mismatch** (PRD gap doc agrees) |
| Account Connect / SCIM / clusters | Status meta says community local-first; forms still active | 404 | **Dead enterprise surface on community** |
| Commercial edition card | Shows `community` / local auth | Works | Good |
| GitHub MC | Not edition-gated; empty config | Unwired | Shell, not edition-dead |

**GA rule:** Community nav must not present **Cloud/Enterprise-only** areas as equal peers without a badge, gate, or collapse. Prefer:

1. Badge + short “Enterprise” lock copy in nav, **or**
2. Hide until `GET /v1/edition` allows the capability, **or**
3. Single “Upgrades / Enterprise” surface that does not look like a broken Settings page.

---

## Hydration risks

**Fixed / mitigated**

- `lib/datetime.ts` — fixed UTC strings (no `toLocaleString` SSR drift)
- `ClientRelativeTime` — absolute on SSR/first paint, relative after mount
- Delivery/monitoring timestamps use `formatDateTime`
- Code-task live durations: `suppressHydrationWarning`
- Meeting deep-link open after mount
- Tests: `tests/datetime-hydration.test.ts` (incl. MissionTaskBoard SSR)

**Residual**

| Location | Risk |
|---|---|
| `page.tsx` `waitedLabel()` for Approvals | Uses `Date.now()` during SSR → classic #418 if decision ages cross a bucket boundary between server and client |
| Live audit areas | `work`, `code_tasks`, `shepherd`, `reports`, `pipeline`, `org` reported React **#418** on deploy `view2` — re-crawl after datetime fixes |
| Any unconverted `toLocaleString` / relative age in SSR islands | Must stay on UTC helpers or client-only |

**GA blocker:** re-run Playwright crawl; Approvals `waitedLabel` should use `ClientRelativeTime` or absolute UTC only.

---

## Accessibility basics

| Check | Status |
|---|---|
| Form controls labelled (`label` / `aria-*`) | Static scan test covers `page.tsx` + `provider-configuration-ui.tsx` |
| Skip link + main landmarks | Present in ConsoleShell |
| Palette dialog | `role="dialog"` `aria-modal` |
| Agent Studio tabs | `role="tablist"` / `aria-selected` |
| Live relative times | Prefer `time` + `dateTime` when using `ClientRelativeTime` |
| Full axe/WCAG automation | **Not** in package (noted as P2 in tests) |
| Icon-only controls | Mostly paired with text; brand mark `alt=""` decorative |

**Grade for a11y:** baseline **acceptable** for internal console; not WCAG-certified GA polish. Keyboard path for sidebar on mobile (hamburger) exists; focus trap in palette is partial (no full roving tabindex).

---

## Dead / enterprise surfaces on community (summary)

| Surface | Why it hurts GA |
|---|---|
| Enterprise area (SCIM, clusters, billing portal session forms) | Buttons that 404 destroy trust |
| Self-improvement (pre-honesty builds) | Create/canary/rollout 404 |
| System Configuration / ProviderOps (live 404 edition split) | Every SSR refresh looks “broken” |
| Account Connect health / link flows | Cloud product on community SKU |

These must be **gated or honest-locked** before GA marketing of community Mission Control as complete.

---

## Panel notes on major `*ui.tsx` modules

### `delivery-ui.tsx` / `monitoring-ui.tsx` / `deploy-ui.tsx`

- Strongest empty-state work in the console  
- Clear separation: CI/CD = ship; Monitoring = health  
- Fail-closed sandbox deploy notice is correct  
- Grade: **Partial** (honest shell → works once targets exist)

### `github-operations-ui.tsx` + `repo-activity-ui.tsx`

- Substantial governed GitHub MC UI (connections, bindings, leases, sync, merges)  
- Easy-connect path with SecretRef / env key (no raw token paste) — good security UX  
- Dogfood empty → **Shell** until connection+binding  
- Grade: **Shell**

### `provider-configuration-ui.tsx`

- Mature suite: catalog, accounts, ChangeSets, health, worker pools  
- Community notice + edition limitations banners  
- Live edition mismatch → **Dead** until API mount matches UI  
- Grade: **Dead** (live) / re-rate when routes community-available

### `agent-studio-ui.tsx`

- Polished workbench (library / canvas / A2A inspector)  
- Empty → seed CTA (better than blank tables)  
- A2A health can show degraded — good signal  
- Still **Shell** until catalog + runs are non-empty by default or setup is forced

### `sandboxes-ui.tsx`

- Host-down honesty is GA-correct  
- Break-glass shell path is well copywritten  
- Grade: **Partial** (UX OK; plane broken on dogfood)

### `pipeline-ui.tsx`

- Edition gate for custom profiles is the template other Enterprise surfaces should copy  
- Grade: **Partial**

### `assurance-ui.tsx`

- Metric cards at zero without setup narrative  
- Grade: **Shell**

### `code-tasks-ui.tsx` / `agent-ui.tsx` / `dashboard-ui.tsx` / `work-orchestration-ui.tsx`

- Operator-grade functional UIs when backend healthy  
- Grades: **Partial** / Agent & Dashboard **Works**

---

## GA UX blockers (ordered)

1. **Edition-aware Mission Control** — nav + SSR fan-out skip dead enterprise feeds; badge or hide Enterprise / paid-only.  
2. **ProviderOps packaging** — either mount community BYOK routes or stop rendering a full broken suite (match code claim to deploy).  
3. **Enterprise surface honesty** — no live Account Connect / SCIM / cluster mutation forms on community 404.  
4. **Hydration re-verify** — fix Approvals `waitedLabel`; Playwright crawl of prior #418 areas.  
5. **Dogfood wiring honesty** — GitHub MC connect-or-hide; Studio seed; CI/CD remains setup-first until targets exist.  
6. **Sandbox host + preview gateway** — keep host-down honesty; never advertise unreachable URLs.  
7. **Approvals inbox hygiene** — filter expired / default to actionable pending.  
8. **Assurance / Comms empty product story** — setup cards, not zeroed ops dashboards.  
9. **SSR cost** — reduce fan-out so Live refresh does not feel like a full product rebuild.  
10. **A11y automation** — optional axe pass before GA marketing claims.

---

## What is ready enough for community GA demo

Show these as the spine (Works / Partial with real data):

- Command Center + Mission Dashboard  
- BOSS Agent chat  
- Budget  
- Task Board + Approvals (with inbox filter)  
- Capabilities  
- Code Tasks **when** worker/git/model healthy  
- CI/CD **as setup** then deploy once targets exist  
- Ops deadletters / audit (not SIM)  

Do **not** lead a community GA tour with Enterprise, full ProviderOps (until mount fixed), empty Assurance, empty Studio, or GitHub MC zeros without the easy-connect success path.

---

## Suggested acceptance for this panel (UX)

- [ ] Playwright crawl: zero React #418 on all 26 areas  
- [ ] Community edition: no mutation form whose primary API is edition-404  
- [ ] Nav badges or hide for Enterprise-only  
- [ ] CI/CD + Monitoring empty states asserted in CI (already started)  
- [ ] ProviderOps either 200 with BYOK path or locked panel with single setup message  
- [ ] GitHub: easy-connect succeeds on dogfood **or** area demoted  
- [ ] Studio: one-click seed leaves non-zero library  
- [ ] Approvals default view = actionable pending only  

---

## Sources

- `apps/web/app/ConsoleShell.tsx`  
- `apps/web/app/*-ui.tsx` (delivery, monitoring, github-operations, provider-configuration, agent-studio, sandboxes, pipeline, assurance, code-tasks, agent, dashboard, work-orchestration, …)  
- `apps/web/app/page.tsx` (area SSR composition, SIM honesty, enterprise commercial panels, `waitedLabel`)  
- `apps/web/lib/{datetime,self-improvement,providerops,enterprise,commercial}.ts`  
- `apps/web/tests/{delivery-empty-states,datetime-hydration,accessibility}.test.ts(x)`  
- `docs/operations/LIVE_UI_FUNCTIONAL_AUDIT_2026-08-11.md`  
- `docs/operations/PRD_CRITICAL_GAPS_2026-08-12.md`  
- `.ai/ux-review.md`  

---

## Panel summary line

**Mission Control chrome is GA-capable; many inner panels are still Shell/Dead on community dogfood. Empty-state and edition-honesty patches landed in code are necessary but not sufficient until deploy packaging, hydration re-crawl, and wiring match the UI.**
