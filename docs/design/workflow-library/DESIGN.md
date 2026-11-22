# Workflow Library and the Validate step (v2, simpler)

Design for approval. Nothing is built yet. Wireframe: `wireframe.html` (4 screens).

## Decisions

1. **No Work panel.** Clicking a task on the Work tab opens its latest **Workflow** run screen. The row shows title, status, kicked off, finished and the links **Workflow #N · Build · Change #N**.
2. **Iterations, latest first.** ▸ on a row lists every re-run, repair build and follow-up with its own start time, finish time and links. The data is the existing `attempts` list.
3. **Details live on the Workflows run screen.** The step list is the navigation, and a step's detail opens below it, as it does today. A header line adds only Change, Preview, Staging/Prod status and "Your move". "Run" becomes "Workflow" in every user-facing word.
4. **Validation is a Validate step in any workflow.** Click it on the run screen to see found → fixed → re-check, with overrides, in every project. The change page's Validation tab becomes a link to that step.
5. **One Library list.** Each workflow appears once, labelled **BOSS** or **Your team**, with tag filters. The actions are **Add to project** (a link, not a copy, which also adds its validator, rules and connectors) and **Share to Library** (from a project's workflow).

## Screens

| # | Screen | What is new |
|---|---|---|
| 1 | Project › Work | A plain list, with ▸ for iterations. A row click opens screen 2/3. |
| 2 | Workflows › Workflow #9 (Northgate, Lumen Pay), Validate selected | The header line and the Validate detail: 9 found → 4 tool, 3 agent, 2 you; Re-check |
| 3 | Workflows › Workflow #16 (HelpDesk, Fast Fix / Hotfix), Build selected | The same screen: files, live log, "Full brief" toggle; Change #3, Staging healthy, Your move: Approve prod |
| 4 | Workflows › Library | One list (BOSS / Your team, tags). The detail says "Includes: gwlint validator, 9 rules"; **Add to project** |

Text rule: short labels, numbers and date-times, with at most one line of description. A long brief sits behind **Full brief**.

## Migration (nothing lost)

- **Work panel → run screen.** The flow becomes the step list. Asked/discussed goes to the Spec or Panel step, Built to Build, and comments to Review & tests. Old `?work=` links redirect to `wfr=<latest>&step=`. A task with no workflow (a build on its own) opens its Build page.
- **Change page › Validation → Validate step link.** The view moves as it is (`validation-tab.tsx`), and "partner" wording becomes "input".
- **Library.** The 514 per-project copies of 25 workflows become links to one Library version each. The API onboarding, NorthPeak Regulatory Compliance and HelpDesk Delivery workflows become **Your team** items. **Nothing is deleted:** runs, versions, mappings, schedules and webhooks keep their rows, and a migration test compares their counts and targets before and after.

## Build plan (small PRs)

| PR | What | Done when |
|---|---|---|
| 1 | Words: Run → Workflow (`work-links.ts`, the Workflows tab, the run header) | No user-facing "Run #" remains |
| 2 | Work tab: a row click opens the latest Workflow; ▸ iterations with start/finish and links; Work panel retired behind a redirect | Playwright at 1440 and 400 px: click → run screen; 3 iterations listed newest first |
| 3 | Run-screen header line: Change, Preview, Staging/Prod, Your move | Shown on the HelpDesk and Northgate runs |
| 4 | `validate` step type on `run_validation` (`boss.validation/v1`); its detail on the run screen; the change tab becomes a link | The Northgate run shows 9 → 4/3/2; HelpDesk Fast Fix shows 0 found |
| 5 | Library tables and link migration (delete nothing) | The before/after snapshot test passes |
| 6 | Library list UI: source label, tags, Add to project (with includes), Share to Library | Adding API onboarding to HelpDesk adds gwlint and 9 rules, with no copy row |

## Open questions

1. **Does a row click open the full page or the inline run?** Recommend the full page (`page=full`), with ← Work back.
2. **Do BOSS workflows auto-upgrade?** Recommend yes for wording-only changes. When steps change, ask first.
3. **Who may Share to Library?** Recommend anyone with `workflow:publish`. The owner is shown, and an org admin can unlist.
4. **Does the change page's Validation tab stay as a link or go?** Recommend a link for one release, then remove it.
5. **What happens to unused linked workflows in existing projects?** Recommend folding them under "Unused · n" and deleting nothing.
