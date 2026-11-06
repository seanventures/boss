# Project experience: wireframes (BOSSAPP-913)

These are for approval before any build. Open `index.html`. Each screen is a static page in `screens/` built on the
console's real tokens (`console.css`, which is copied verbatim from `apps/web/app/design-system.css` `:root` and
`agent-ops/agent-ops.css`) and the avatar kit's real faces (`faces/*.svg`, rendered by `lib/agent-avatar.ts`
`avatarSvg`). Icons come from Phosphor, the set the console already uses: `ph-folders` is `<Folders/>` in
`@phosphor-icons/react`. The PNGs in `png/` come from `render.py` at 1600×1000 in dark and light, and at 390×844 on a phone.
Each page accepts `?theme=light`, `?nav=collapsed` and `?notes=0`. Walk the screens with → / ← (Esc returns to the index).
The API the screens build on: `docs/product/PROJECT_EXPERIENCE_CONTRACT.md` (on feat/project-experience; §11 Settings, §12 Health).

The sample project is **HelpDesk AI Demo** (`paramg/helpdesk-ai-demo`, staging `2.29.2.202:8004`). PR #1 (SLA timers) and
PR #2 (UI refresh) are merged. PR #3 (visual refresh finish) is waiting for a person to merge it. Helm is the captain,
Marshal ranks the backlog, and the crew is Juno, Forge, Vera and Beacon.

## Screens

| # | Screen | Purpose |
|---|---|---|
| 0a | First run: empty | A fresh install starts here. It shows "Add your first project" and absorbs the 3 steps of Get started. |
| 0b | First run: guided | A 6-step guide sits beside Helm's live onboarding diagram. It ends at suggested first work that leads to a PR. |
| 1 | Left nav | Projects is added. Factory is renamed Code Tasks. Get started is absorbed. Shows the collapsed rail and the phone drawer. |
| 2 | Projects | One card per project, with its journey strip, health ring, cost sparkline, crew and a needs-you badge. |
| 3 | Add a project | The user chooses New or Existing, picks a repo or folder, and Helm starts reading it live. Then the user names it, picks a captain and starts. |
| 4 | Overview | Shows the journey, Needs you, 4 tiles (open work, running, staging health, cost), the captain card, recent work and what's next. |
| 5 | Work | Every task is a row with a station strip (Plan · Decide · Build · Review · PR · Staging) and links. New work is added inline. |
| 5b | Work drawer | Opening a row shows each step, the agent that did it, the one action and every link. It opens in place, with no navigation. |
| 6 | Repo & knowledge | Shows repos with access checks, the dossier (stack, commands, tests, code map, hotspots), a KG snippet, docs and decisions, and what was learned. |
| 7 | Roadmap | Shows Now/Next/Later and ranked recommendations as evidence → detector → agent. Accept turns one into work; Reject asks for a reason in one tap. |
| 8 | Deployments | Shows the ship path, environment cards (URL, health, last deploy), demo logins with an audited reveal, and deploy history. |
| 9 | Ask the captain | A chat with Helm scoped to this project. Answers cite links and offer actions you can start. |
| 10 | Onboarding | The project is in onboarding. Shows Discover → Ready checklist → Backlog proposed, with what the agent is doing now. |
| 11 | Jump to (⌘K) | One keystroke reaches any run, PR, deploy, agent or action, scoped to the project. |
| 12 | Health | Per environment: uptime, error rate and p95 sparklines, SLO burn. Shows alerts, incidents as mini flows (alert → SRE squad → root cause → fix → recovered) linking to Agents at work, Beacon's monitoring pack, and Grafana / logs / Observability. |
| 13 | Settings | The project's own settings in one place: demo logins per environment (user names + reveal with step-up), deploy values (names only), targets & hosts, captain & squad, autonomy & ambient budget, repos, notifications. Old homes show "Moved to Project › Settings". |
| 14 | Infra › server | The shared server stays org-wide. "Projects on this server" links to each Project › Deployments. |
| 15 | Observability › apps | The cross-app view stays org-wide. Each app links to its Project › Health. |

## Key design decisions

1. **"Project" is BOSS's existing project.** It is the `project_id` scope with its own repos, agents, budget and
   workflows. Today the top-bar project menu reaches it, and `start-project/project-start.tsx` already gives it a page.
   The new Projects area is its home, and the top-bar menu becomes the rail list under Projects.
2. **One persistent project header on every tab.** The header has the name, the repo chip, the live staging chip, the
   stage, the captain (Ask) and **New work** (the single primary action). Below it is the **Needs you bar**, which is
   always visible, and then the tabs: Overview · Work · Roadmap · Repo & knowledge · Deployments · Health · Settings.
   A health dot sits beside the live-URL chip on every tab and opens Health.
3. **Diagrams, not prose.** The same three visual primitives repeat everywhere, so they become recognisable:
   - the **journey strip** for the project's life,
   - the **station strip** for one piece of work,
   - the **flow chips** for a path or a reason.
   Labels are at most 4 words. State is a badge plus a colour plus a word. Prose appears only in drawers and design notes.
4. **The captain is the project's agent.** Helm (Engineering Captain) takes over a project once it is created. Helm
   onboards it, keeps the dossier fresh, plans and proposes the roadmap, and answers in the captain drawer. Marshal
   (Chief of Staff) ranks backlogs across projects. The captain drawer is the existing Ask BOSS dock scoped to the
   project, talking as its captain. It is not a second chat (one agent path, BOSSAPP-759).
5. **Every tile, row and chip is a link.** Internal detail opens in a **side drawer** (5b, 9). An external target (PR,
   live URL) opens with ↗. A full page (Workflow run, Code Tasks build) is a chip inside the drawer.
6. **The onboarding agent's stages are the journey's first nodes:** Discover → Ready for work (the agent completes the
   checklist and asks only what it cannot do) → Propose backlog (open issues, failing CI and TODOs, ranked by Marshal)
   → Work it in a verified loop → Deploy (staging by policy, prod by a person).
7. **First run is part of Projects.** An unfinished install opens Projects (0a) instead of a separate Get started
   button. One strip covers the path from Connect GitHub to the first PR.
8. **Premium, quiet visuals.** Every section has an icon tile in its own colour. The screens use real agent faces, ring
   charts (health, progress, tests), sparklines (cost, latency), illustrated empty states (0a, prod not set up), generous
   spacing and one primary button per view.

9. **One project vs shared (operator, 2026-11-06).** Anything about ONE project lives on the Project page: its
   deployments, health, demo logins, deploy values, targets, captain, autonomy and budget, repos and notifications.
   Anything shared stays in its org-wide area: Infra (servers, cloud, plugins), Observability (all apps, BOSS's own
   platform, alert sources, contact points, Grafana) and Govern (org caps). The two are cross-linked both ways:
   Deployments › "runs on 2.29.2.202 ↗" ⇄ Infra › server › "Projects on this server"; Health › "Observability ›
   this app" ⇄ Observability › app › "Project › Health".
10. **Settings move, nothing is lost.** Each old home keeps working as a single line, "Moved to Project › Settings".
    Demo logins also show read-only (user + reveal) on Overview › Try it and Deployments.

### Motion (to build, not visible in PNGs)
- A live node (running step, current journey stage, an agent who is working) shows a soft ring pulse at 1.8s. This is
  the existing `aa-ring` animation, and agent faces use the kit's `working` state.
- A step that completes turns its edge green with a 200ms ease. A new row slides in at 150ms. Drawers slide from the right at 240ms
  (the rail's existing curve).
- A Needs-you item glows amber (`aa-glow`) once when it arrives, then holds still.
- `prefers-reduced-motion` removes all movement. The meaning stays in the colour and the word.

## Zero hunting: where do I find X

Everything about a project is at most 2 clicks from its page, and ⌘K reaches it in one.

| I want… | Where | Clicks from the project page |
|---|---|---|
| Status of my feature | Work row station strip, or Overview › Recent work | 0 (Overview) / 1 (Work) |
| The PR | `PR #n ↗` chip on the row, in the drawer, or in Needs you | 1 |
| The live URL | Staging chip in the header | 0 (always visible) |
| A demo login | Deployments › Demo logins, also Overview › Try it | 0 / 1 |
| Who is working on it | Faces on each row; captain and crew on Overview | 0 |
| What's blocked | The Needs you bar in the header, on every tab | 0 |
| Cost | Overview › Cost tile; per item in the row and drawer | 0 |
| What's next | Overview › What's next; Roadmap tab | 0 / 1 |
| Asking the captain | `Ask` on the header captain chip, or ⌘K "Ask Helm…" | 1 |
| Adding work | `New work` in the header, the inline box on Work, or the captain chat | 1 |
| Is it healthy / any incidents | The health dot in the header (every tab) → Health | 0 / 1 |
| A demo password | Settings › Demo logins › Reveal (step-up); read-only on Overview › Try it and Deployments | 1 |
| Which server it runs on | Deployments › "runs on 2.29.2.202 ↗" → Infra › server | 1 / 2 |
| A deploy value / secret name | Settings › Deploy values | 1 |
| Change captain, autonomy or budget | Settings › Captain & squad / Autonomy & budget | 1 |

Click map from the Overview:
```
Overview ─┬─ Needs-you item ─→ drawer (action inside)
          ├─ Journey node ───→ drawer (that stage's record)
          ├─ Tile ───────────→ Work / drawer / Deployments / Govern
          ├─ Work row ───────→ drawer ─→ Workflow run · Code Tasks build · PR ↗ · logs
          ├─ Captain › Ask ──→ captain drawer
          ├─ Health dot ─────→ Health ─→ incident flow → Agents at work graph · Grafana ↗ · Observability › app
          └─ Tabs ───────────→ Work · Roadmap · Repo & knowledge · Deployments · Health · Settings (each item → drawer)

Deployments ─ "runs on 2.29.2.202 ↗" ─→ Infra › server ─ "Projects on this server" ─→ back to each Project › Deployments
Health ─ "Observability › this app" ─→ Observability › apps ─ "Project › Health" ─→ back
Settings ─ Demo logins › Reveal ─→ "Confirm it's you" (step-up) ─→ password shown 30s
```

## Parity: today's Get started → where it lives now

From `apps/web/app/setup/welcome.tsx` and `lib/setup-stepper.ts`.

| Get started today | Where it lives now |
|---|---|
| Step 1 Connect GitHub (done state "Connected as X · N repos") | 0a step-1 card; 0b guide step 1 shows the login and repo count |
| "Add another account" | 0b guide footer `+ another account`; Add a project › Pick it › `+ account` |
| Step 2 Add a model (subscription / API key / local) | 0a step-2 card; 0b guide step 2 |
| "Add another model" | 0b guide footer `+ another model`; Repo and models (unchanged) |
| Step 3 Try a sample project / "Start the sample" | 0a step-3 `Try a sample`; Add a project header `Try a sample` |
| Last sample run pill, its PR link, "Open run" on failure | The sample becomes a project (Hello World on screen 2); its run and PR are rows on its Work tab |
| "Or open one small PR" (TryRow quick PR) | 0a step-3 `One small PR`; New work on any project |
| Import an existing repo | 0a `Existing repo`; Add a project › Existing |
| Start a new app from an idea | 0a `From an idea`; Add a project › New |
| Flow line Idea → Plan → Build → Review → PR → Live preview | 0a "What happens to each piece of work" diagram; Helm's plan strip in New work |
| Link to Repo and models | Rail (unchanged); 0a note "change anything later" |
| Stepper's collapsed "Who does what" / Budget defaults | Unchanged in Repo and models; per-project crew on 3 (Crew) and Overview |
| Command Center first-run help rows | Replaced by 0a when there are no projects; Command Center keeps its cross-project role |

## Parity: project settings that move to Project › Settings

Each old home keeps working and shows one line, "Moved to Project › Settings".

| Setting | Lives today | Lives now | Also shown |
|---|---|---|---|
| Demo logins (password) | App › Setup › Settings, "Show" on a generated value (`components/v2/templates/setup-sheet.tsx`); build preview hint "Demo login: … (Setup › Demo logins)" (`factory/legacy-build-parts.tsx`) | Settings › Demo logins, per environment, with user names; Reveal = owner/admin + step-up | Read-only on Overview › Try it and Deployments |
| Demo login user names | Nowhere (not shown today) | Settings › Demo logins, from `boss.deploy.yaml` `demo_logins:` or the app's seed | Same |
| Deploy values / app secrets | App › Setup › Settings (BOSS-managed + person-set; `/v1/apps/{key}/secrets`) and Connectors keys | Settings › Deploy values (names only; set/rotate/remove; write-only values) | Missing ones in Needs you |
| Deploy targets & hosts for this project | Infra › Deploys (`infra/deploy-sheet.tsx`, `deploy-drawer.tsx`) | Settings › Targets & hosts (this project's targets) | Deployments; Infra › server keeps the shared view |
| Captain & squad | Agent Studio (roster); no per-project captain today | Settings › Captain & squad | Overview captain card; header chip |
| Autonomy level | Govern › Autonomy (`agent-ops/autonomy-panel.tsx`, org-wide) | Settings › Autonomy & budget (this project) | Govern keeps org caps + "set per project" links |
| Ambient budget | Govern (spend caps) | Settings › Autonomy & budget (this project's week) | Overview cost tile |
| Repo bindings | Repo and models; Knowledge Graph import (`PUT /v1/projects/{id}/repos/{repo}`) | Settings › Repos | Repo & knowledge |
| Notifications | Communications (org outbox); alert contact points in Observability | Settings › Notifications (this project's routing) | Observability keeps contact points |

Factory → Code Tasks keeps everything: the same area id `code_tasks`, the Builds/Apps tabs, cockpit and links. Only the
label changes (plus the tooltip "was Factory" for one release, and edition copy in `lib/edition.ts`).

## Which existing components each screen reuses

| Screen | Reuses |
|---|---|
| 0a/0b | `setup/welcome.tsx` state (`welcomeState`), `SetupLink`, `TryRow`, `NEW_PROJECT_HREF`; `AgFace` |
| 1 | `ConsoleShell.tsx` `AREAS` (adds `projects`, relabels `code_tasks`), `project-menu.tsx` items, `edition.ts` allowlist |
| 2 | `project-strip.tsx` data (status/stage/pct), `app-grouping.ts`, `AgFaceStack`, `project-archive.tsx` (Archived filter) |
| 3 | `start-project/start-sheet.tsx` doors, `import-repo.tsx` 3 columns (Pick · BOSS reads it · Name it), `new-project-sheet.tsx` |
| 4 | `project-start.tsx` (project page), `command-center` Needs-you ordering, `agent-ops/flow-thumb.tsx`, `AgFace` |
| 5/5b | `factory/station-line.tsx`, `build-row.tsx`, `delivery-workflows-ui.tsx` run links, `workflow-readiness-ui.tsx`, `agent-ops/drawer.tsx` |
| 6 | `/v1/github/repos/{id}/facts` (import-repo facts), `knowledge-graph-ui.tsx`, `kg-scan.ts`, multirepo registry |
| 7 | `agent-ops/roadmap.tsx` (detectors, accept/reject reasons), `ambient-demo.ts` shapes |
| 8 | `deploy-ui.tsx`, `infra` targets, `host-advice.tsx`, govern deploy policy |
| 9 | Ask BOSS dock (`bx-dock`, `AgentChat`), `agents/cited-answer.tsx`, `act-card.tsx` |
| 10 | Onboarding agent (new). Checklist rows reuse `workflow-readiness-ui.tsx`'s `ReadinessPanel` model |
| 11 | The existing `bx-palette` (⌘K), extended with project records |
| 12 | `observability` area app health, app alerts, the incident squad graph (`agent-ops/flow-canvas.tsx`), Beacon's monitoring pack |
| 13 | `components/v2/templates/setup-sheet.tsx` Settings rows and step-up (`useGuardedCall`), `agent-ops/autonomy-panel.tsx`, `govern/spend-tab.tsx`, `infra/deploy-sheet.tsx` |
| 14 | `infra/host-drawer.tsx`, `infra/fleet.tsx`, `infra/plugins.tsx` |
| 15 | `observability` area app list |

## Open questions for the operator

1. **The captain.** Should each project have its own captain (Helm, Engineering Captain, as drawn) with Marshal ranking
   across projects? Or should Marshal (Chief of Staff) captain every project? Note that "Marshal" is not yet a callsign
   in the kit: `chief_of_staff` has none.
2. **Project vs app.** One project can hold several apps or repos (multi-repo is drawn). Should Code Tasks › Apps
   stay as the cross-project view, or move under each project?
3. **Light theme.** The console ships dark only. These screens propose light values in `project.css`. Should a console-wide
   light theme be built with this work?
4. **Who may reveal demo passwords.** Owners only (as drawn), or anyone with deploy access?
5. **Prod.** Is the rule "prod is always a person" for every project, or can a policy allow an agent to run it?
