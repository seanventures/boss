# Project experience: acceptance checklist (BOSSAPP-913)

This is the gate the build must pass. The operator's words: "I want every single piece in wireframes to be delivered with
utmost quality and clearly.. no ifs and buts". Every visible element of the approved wireframes is listed below, in
reading order, with a stable ID and a one-line test. An item passes only when the test holds in a browser, on real data
(a real project with a repo, real runs and PRs), in both themes the console ships. Nothing here is optional.

**The one approved change.** The operator's models/GitHub feedback (`PROJECT_EXPERIENCE_CONTRACT.md` §15) replaces the
wireframes' model and GitHub-account steps and pickers on project pages. Those items are written to §15 and marked
**(approved change)**. Every other item must look and behave exactly as the wireframe shows it.

Sources: `screens/*.html`, `shell.js`, `project.css`, `README.md` (click map, parity tables), and
`docs/product/PROJECT_EXPERIENCE_CONTRACT.md`.

## How to use this

- Walk each screen at 1440 px, then 1024, 768 and 390 (phone). Tick each item, or write the failure in the table at the end.
- "Opens" means the click lands on the named place with no extra step. "Drawer" means a side drawer on the same page with
  a deep link (`&drawer=<kind>:<id>`); Esc and Back close it.
- Sample text in the wireframes (HelpDesk AI Demo, PR #3, Run #43, 203.0.113.20) is illustrative. On real data the
  element must exist and show the real value of the same kind. A number shown in the wireframe must be a real count, never a constant.
- Colour meanings are the console's. They are the same on every screen:
  - **green**: done or healthy
  - **teal**, with a pulse: running or live now
  - **amber**: needs a person ("you")
  - **red**: failed, a bug or CI
  - **violet**: ideas, roadmap or cost charts
  - **blue**: info, captain or "new"
  - **dashed or faint**: not started (todo)
  - **grey "idle"**: quiet
- State is always shown as a badge, a colour and a word together. Colour alone never carries the meaning.

---

## 00a — First run: empty (`00a-first-run-empty.html`)

Route: `?area=projects&first_run=1` when no project has a repo. Rail: Projects active, its sub-list shows only "Add project". Breadcrumb: `Projects`.

- [ ] **00a-01** A fresh install, with no project that has a repo, lands here. Pass when a new install opens on this screen and no separate "Get started" rail button exists.
- [ ] **00a-02** Rail: Projects is active (`aria-current`). Its sub-list holds only `+ Add project`. Pass when no fake project rows are shown and "Add project" opens screen 03.
- [ ] **00a-03** Breadcrumb reads `Projects`. Pass when the top bar shows exactly that.
- [ ] **00a-04** Hero card: full width, with a radial accent glow from the top centre. Pass when the card has the soft accent gradient in dark and light themes.
- [ ] **00a-05** Illustration: a project folder (accent fill and outline, two text lines) in the centre of a dashed elliptical orbit. Pass when the SVG folder and the dashed orbit render.
- [ ] **00a-06** Illustration crew: the real kit faces of Helm (left, largest), Juno, Forge, Vera and Beacon sit on the orbit. Pass when all 5 faces are the `lib/agent-avatar.ts` avatars, not placeholders.
- [ ] **00a-07** Illustration accents: three small dots (teal, green, violet) sit on the orbit. Pass when they are visible and the illustration is `aria-hidden`.
- [ ] **00a-08** Heading `Add your first project` (h2). Pass when the text is exact.
- [ ] **00a-09** Lead text: "Point the crew at a repo or an idea. Helm, your captain, reads it, gets it ready, and suggests the first piece of work." Pass when it shows in full, not truncated, and names the real default captain.
- [ ] **00a-10** First-run strip: 5 journey nodes joined by wires, at most 720 px wide, centred. Pass when the 5 nodes render in order with wires between them.
- [ ] **00a-11** Strip node 1 `Connect GitHub` (GitHub logo icon). **(approved change)** Pass when the node ticks green (✓) if a GitHub account is connected, and is `now` (teal pulse) if it is the first step not yet done. The state is read from the real status.
- [ ] **00a-12** Strip node 2 `Add a model` (brain icon). **(approved change)** Pass when the node ticks if a model is configured, from the real status, and is `now` only if it is the first step not yet done.
- [ ] **00a-13** Strip node 3 `Add project` (folder-plus icon). Pass when it is `now` once GitHub and a model are both configured, and `todo` (faint) before that.
- [ ] **00a-14** Strip node 4 `Helm onboards` (compass icon), todo. Pass when the caption names the real captain.
- [ ] **00a-15** Strip node 5 `First PR` (pull-request icon), todo. Pass when it renders faint.
- [ ] **00a-16** Strip wires: done→done wires are green, done→now wires go from green to teal, the rest are grey. Pass when the wire colours follow the node states.
- [ ] **00a-17** Caption `About 10 minutes · change anything later in Repo and models`. Pass when "Repo and models" is present and links to the Repo and models page.
- [ ] **00a-18** Three step cards in one row (3 columns at desktop). Pass when the order is Step 1, Step 2, Step 3.
- [ ] **00a-19** Step 1 card: teal icon tile (GitHub logo, large), blue tag `Step 1`, title `Connect GitHub`, sub "Pick the repos the crew may work on.". Pass when all four render.
- [ ] **00a-20** Step 1 action. **(approved change)** Pass when:
  - if GitHub is not connected, a primary `Connect` button (plug icon) opens Repo and models › GitHub, never an inline picker;
  - if it is connected, the card shows ✓, the account name and `Manage ›` linking to Repo and models;
  - if the status cannot be read, the card shows a neutral `Check in Repo and models ›` and never a false ✓.
- [ ] **00a-21** Step 2 card: violet icon tile (brain), tag `Step 2`, title `Add a model`, sub "A subscription, an API key, or a local model.". Pass when all four render.
- [ ] **00a-22** Step 2 action. **(approved change)** Pass when:
  - if no model is configured, the `Add model` button (plus icon) opens Repo and models (models), never an inline picker;
  - if one is configured, the card shows ✓, the model's name and `Manage ›`;
  - if the status cannot be read, the card shows the neutral `Check in Repo and models ›`.
- [ ] **00a-23** Step 3 card: green icon tile (folder-plus), tag `Step 3`, title `Add a project`. Pass when they render.
- [ ] **00a-24** Step 3 button `Existing repo` (git-branch icon). Pass when it opens screen 03 with "Existing · repo or folder" selected.
- [ ] **00a-25** Step 3 button `From an idea` (lightbulb icon). Pass when it opens screen 03 with "New · from an idea" selected.
- [ ] **00a-26** Step 3 link `Try a sample` (flask icon). Pass when it starts the sample project (README parity: "Start the sample"). The sample then appears as a project card on screen 02.
- [ ] **00a-27** Step 3 link `One small PR` (lightning icon). Pass when it opens the quick-PR path (README parity: the TryRow quick PR).
- [ ] **00a-28** One primary per view: only the first step not yet done has the primary (filled) button. Later steps are visible but quiet, and done steps tick and collapse. Pass when no two primary buttons are on screen at once.
- [ ] **00a-29** Card `What happens to each piece of work`: blue icon tile (path icon) and the title. Pass when it renders under the step cards.
- [ ] **00a-30** Flow chips, in order: `Idea` (lightbulb), `Plan` (Juno's face, agent pill), `You approve` (amber), `Build` (Forge's face, agent pill), `Review` (Vera's face, agent pill), `Pull request` (PR icon), `Live preview` (Beacon's face, agent pill), with arrows between. Pass when all 7 chips and 6 arrows render with those faces and tones.
- [ ] **00a-31** Design note 1. Pass when the 5-node strip becomes the guided flow (00b) as soon as step 1 is done. **(approved change)** "Step 1" means the first step not yet done per §15.
- [ ] **00a-32** Design note 2: one action per view, and done steps tick and collapse. Pass when this matches 00a-28 with real states.
- [ ] **00a-33** Design note 3: every Get started capability keeps a place (README parity table). Pass when each row of the "Get started today" parity table can be reached from 00a or 00b, including the last sample run's PR, which becomes a row on the sample project's Work tab.
- [ ] **00a-34** Command Center's first-run help rows are replaced by this screen when there are no projects. Pass when Command Center no longer shows them on a fresh install.

**Responsive**
- 1440: hero centred; 3 step cards in a row; flow on one line.
- 1024: the same layout, with no horizontal page scroll; the flow may wrap.
- 768: no horizontal page scroll; the strip fits or scrolls inside the card.
- 390: rail hidden behind ☰; step cards stack in 1 column; strip nodes 64 px wide with small captions; illustration scales to the width; 16 px gutter.

**States**
- Loading: skeleton of the hero and 3 cards, with no ✓ shown before the status is known.
- Empty: this screen is itself the empty state.
- Error (status unreadable): neutral "Check in Repo and models ›" on each card, never a false ✓.
- Needs you: the first step not yet done is the single primary action.

---

## 00b — First run: guided (`00b-first-run-guided.html`)

Breadcrumb: `Projects / First project`.

- [ ] **00b-01** Breadcrumb `Projects / First project`, with `Projects` muted. Pass when it renders and `Projects` is a link.
- [ ] **00b-02** Two-column layout: the guide on the left (sticks at the top) and the live diagram on the right. Pass when both are visible side by side at desktop.
- [ ] **00b-03** Guide card header: icon tile (signpost), title `Your first project`, and a mono counter at the right. **(approved change)** Pass when the counter counts only the project steps (from Add project on) and uses real progress.
- [ ] **00b-04** **(approved change)** A read-only `Workspace ready` row sits above the checklist. It shows `GitHub ✓` (with the login and repo count, e.g. "paramg · 14 repos") and `Model ✓` (e.g. "Claude · subscription"), each linking to its global page (Setup › GitHub, Setup › Models). Pass when the row is read-only, uses the real status, and offers no picker.
- [ ] **00b-05** **(approved change)** The wireframe's guide steps `Connect GitHub` and `Add a model` are not checklist steps on this page. Pass when the checklist starts at `Add project`.
- [ ] **00b-06** **(approved change)** If GitHub or the model is missing, the `Only you can` card shows a card linking to the global page. Pass when removing the model makes that card appear, with a link to Repo and models.
- [ ] **00b-07** Guide step `Add project`: done ✓ (green filled circle), sub = the repo name (`helpdesk-ai-demo`). Pass when the real repo name is shown.
- [ ] **00b-08** Guide step `Helm onboards`: `now` (teal tint and border, compass icon), sub `Discover · 2 of 4`. Pass when the sub shows the real onboarding stage and count.
- [ ] **00b-09** Guide step `Ready for work`: todo (list-checks icon), sub "checks the agent runs". Pass when it renders and turns `now` or done as onboarding moves.
- [ ] **00b-10** Guide step `First work`: todo (pull-request icon), sub `suggested → your PR`. Pass when it renders and turns done when the first PR opens.
- [ ] **00b-11** Guide connector: the line between steps is green after a done step. Pass when the connector colour follows the states.
- [ ] **00b-12** **(approved change)** The guide footer's `+ another account` / `+ another model` links are not on this page. Pass when they are absent; adding accounts and models happens on Repo and models.
- [ ] **00b-13** Live card: teal radial background, teal icon tile (compass), title `Helm is reading your repo`, blue `live` tag with a pulsing teal dot, and a mono elapsed timer (`1m 52s`) at the right. Pass when the timer counts real elapsed time (`elapsed_s`).
- [ ] **00b-14** Diagram, Helm node: a large blue circle with Helm's live face (the `working` state) and the caption `Helm`. Pass when the face is the kit's working avatar and it pulses.
- [ ] **00b-15** Diagram, node `Stack found` (done, green, ✓) with mono sub `FastAPI · React`. Pass when the sub is the real stack.
- [ ] **00b-16** Diagram, node `Mapping code` (now, teal, ◌) with sub `118 / 214 files`. Pass when the count updates live.
- [ ] **00b-17** Diagram, node `Tests run` (done, green, ✓) with sub `142 / 148 pass`. Pass when the numbers are real.
- [ ] **00b-18** Diagram, node `Ready check`: a dashed pill with faint text (todo). Pass when it renders dashed until reached.
- [ ] **00b-19** Diagram edges: Helm→done nodes are solid green curves; Helm→now is a dashed teal line; nodes→Ready check are dashed grey. Pass when each edge's colour follows its target's state, and a done edge turns green with a 200 ms ease.
- [ ] **00b-20** Diagram labels are at most 4 words each. Pass on inspection.
- [ ] **00b-21** Card `Only you can`: amber icon tile (hand-waving). Pass when it renders whenever there is an owner=you item.
- [ ] **00b-22** Only-you item: amber `!` icon, title `Add OPENAI_API_KEY`, faint sub "The app needs it to run", amber `Add` button. Pass when the secret's name is real and `Add` opens the write-only value entry (Settings › Deploy values).
- [ ] **00b-23** The footnote "Everything else Helm does itself." sits under the items. Pass when it renders.
- [ ] **00b-24** Card `First work, suggested`: violet border, violet icon tile (sparkle), and Marshal's small face at the right (tooltip "Ranked by Marshal"). Pass when the face and tooltip render.
- [ ] **00b-25** Suggested work title `Fix 6 failing tests`. Pass when it is the real `first_work.label`.
- [ ] **00b-26** Chips: red `CI` (x-circle icon), mono cost-style `≈ 15 min`, mono `≈ $1.50`. Pass when the source, estimate and cost are real.
- [ ] **00b-27** Primary button `Start when ready` (play icon), dimmed with the tooltip "Starts when Ready is done". Pass when it is disabled until Ready is done, then becomes enabled and starts the work (`POST …/work`).
- [ ] **00b-28** Ghost button `Other ideas`. Pass when it opens the other candidates (`others_href`: the Roadmap or the backlog list).
- [ ] **00b-29** Design note 1: there are no forms after Add project. Left is where you are, right is the live diagram. Pass when no form appears in the guided flow.
- [ ] **00b-30** Design note 2: "First work, suggested" is ready before onboarding ends, and the flow ends at a real PR. Pass when pressing Start leads to a PR on the user's repo.
- [ ] **00b-31** Design note 3 (motion): the live node pulses, done edges turn green with a 200 ms ease, nothing else moves, and `prefers-reduced-motion` removes the motion. Pass when this holds in the browser.

**Responsive**
- 1440: guide on the left, diagram and cards on the right; the two cards side by side.
- 1024: the same, with no horizontal scroll; the SVG scales.
- 768: no horizontal scroll; the cards may stack.
- 390: guide above the diagram (1 column); the two cards stack; the diagram scales to the width; notes hidden.

**States**
- Loading: guide skeleton plus diagram placeholder.
- Empty: not applicable (a project exists).
- Error: onboarding `failed` shows the failed node in red with the reason and a retry; never a blank diagram.
- Needs you: the "Only you can" items, which also show in the header bell.

---

## 01 — Left nav (`01-nav.html`)

- [ ] **01-01** Card `What changes in the rail`, with an icon tile (sidebar-simple). Pass when the build's rail matches each door below. This screen is a spec; it is not shipped as a page.
- [ ] **01-02** `Projects` rail item: teal folders icon, blue `new` tag. It is the second item in Run, under Command Center. Pass when the rail shows Projects in Run right after Command Center in the Platform group, with a `NEW` badge when it is not active.
- [ ] **01-03** The Projects sub-list lists every project with a state dot (amber = needs you, teal = running, green = done or live). Pass when real projects are listed with real state dots.
- [ ] **01-04** The sub-list ends with `+ Add project` (tooltip "Add a project — new from an idea, or an existing repo"). Pass when it opens screen 03.
- [ ] **01-05** `Code Tasks` item (code icon), tag `renamed`, flow `Factory → Code Tasks`. Pass when the rail label is `Code Tasks` and the area id `code_tasks` is unchanged.
- [ ] **01-06** Code Tasks keeps the same area and tabs (Builds, Apps, Lab). Pass when all three tabs open as before.
- [ ] **01-07** The Code Tasks tooltip says "was Factory" for one release. Pass when hovering shows "Builds, apps and gated PRs (was Factory)".
- [ ] **01-08** Old `area=` values (Factory) keep working. Pass when an old Factory link opens Code Tasks.
- [ ] **01-09** `Get started` (rocket icon, tag `absorbed`) has no separate rail button. Pass when no Get started item exists and an unfinished install opens 00a or 00b.
- [ ] **01-10** `Collapsed rail` door (violet sidebar icon). Pass when ⌘\ toggles a 60 px icon rail.
- [ ] **01-11** In the collapsed rail, labels move to tooltips. Pass when each icon's tooltip shows "Label — purpose".
- [ ] **01-12** In the collapsed rail, badges stay on the icon. Pass when the Approvals count still shows.
- [ ] **01-13** The collapsed state is remembered per browser. Pass when it survives a reload.
- [ ] **01-14** `Phone` door (green phone icon). Pass when the rail at 390 px is a slide-over opened from ☰.
- [ ] **01-15** On a phone, the project tabs scroll sideways under a sticky header. Pass when, at 390, the tabs scroll horizontally and the header stays.
- [ ] **01-16** Rail order and labels, exactly:
  - Platform: Command Center.
  - Run: Projects, Code Tasks, Agents at work, Workflows, Agent Studio, Knowledge Graph, Approvals (badge), Infra, Observability, Self-fixes.
  - Settings: Repo and models, Skills & MCP, Govern, Identity.

  Each item has its Phosphor icon (squares-four, folders, code, robot, flow-arrow, users-three, graph, seal-check, hard-drives, pulse, wrench, database, puzzle-piece, scales, identification-card). Pass when the order, labels and icons match, minus any items the edition allowlist hides.
- [ ] **01-17** Each rail item's tooltip reads "Label — purpose", as in `shell.js` (e.g. "Projects — Every project: its work, crew, repo and deploys"). Pass on hover.
- [ ] **01-18** Rail foot: the user's avatar and "Name · Role" (e.g. "Param · Owner"). Pass when the real user and role are shown.
- [ ] **01-19** The top-bar project menu becomes the rail list under Projects. Pass when no separate top-bar project menu duplicates it.

**Responsive**
- 1440: full rail with labels.
- 1024: full rail; collapsed if the user chose it.
- 768: rail or collapsed rail, with no horizontal scroll.
- 390: rail hidden; ☰ opens a 260 px slide-over with a dim scrim, and labels and group names are shown.

**States**
- Loading: the rail renders at once; the project sub-list shows a skeleton.
- Empty: the sub-list shows only "Add project".
- Error: the project list fails quietly, with "Couldn't load projects · Retry", and the rail still works.
- Needs you: the amber dot on the project and the Approvals badge.

---

## 02 — Projects (`02-projects.html`)

Route: `?area=projects`. Breadcrumb `Projects`.

- [ ] **02-01** Page header: large icon tile (folders), h1 `Projects`, muted sub "Each one has a captain, its work, repo and deploys". Pass when it renders.
- [ ] **02-02** Primary `+ Add project` button at the right of the header. Pass when it opens screen 03, and it is the only primary on the page.
- [ ] **02-03** Tile `Projects`: the count. Pass when it equals the real number of projects (excluding archived).
- [ ] **02-04** Tile `Need you`: amber border, amber count, sub `in N project(s)`. Pass when it is the real sum of needs-you items and the number of projects that hold them.
- [ ] **02-05** Tile `Running`: teal count, with the faces of the live agents (working-state avatars). Pass when the faces are the agents running now.
- [ ] **02-06** Tile `Cost · week`: dollars, sub `of $N cap`. Pass when it is the real week's spend and the org cap.
- [ ] **02-07** Filters: `All N` (selected), `Needs you` (amber dot), `Working`, `Onboarding`, `Archived`. Pass when each filter narrows the cards and the counts are real.
- [ ] **02-08** The `Archived` filter shows archived projects (`project-archive.tsx`). Pass when an archived project appears only there.
- [ ] **02-09** Project card (one per project) is a link (tooltip "Open <name>"). Pass when a click on the card opens that project's Overview (`?project_id=…&area=project`).
- [ ] **02-10** Card head: a gradient mark tile with initials (`HD`), the project name, and the mono repo (`paramg/helpdesk-ai-demo`), with a suffix such as `· KG` or `· sample` when it applies. Pass when the initials, name and repo are real.
- [ ] **02-11** Card status pill at the right: amber `N need you`, blue `Onboarding`, or green `Live`. Pass when the pill follows the project's real state.
- [ ] **02-12** Card journey strip (small): 4 nodes `Onboard`, `Ready`, `Working`, `Deployed`, with wires. Done nodes are green ✓. The now node is teal, with its icon (hammer for working, list-checks for ready) and a pulse. Todo nodes are empty and faint. Pass when it follows the real `journey`.
- [ ] **02-13** Card health ring (36 px): health percent (`99.9`), or onboarding progress (`4/6`, teal). Pass when the ring's fill and label are real.
- [ ] **02-14** Card status lines: a dot plus a short state (`staging healthy`, `mapping rules`, `preview up`), and a faint second line (`3 merged · 1 building`, `1,284 nodes so far`, `1 merged · idle`). Pass when both lines are real.
- [ ] **02-15** Card cost sparkline (violet, 70×24) for the week. Pass when it draws 7 real daily values (`series`).
- [ ] **02-16** Card foot: the captain's face and name (`Helm`), the crew's faces (the working face when live), and the mono week cost (`$14.20`) at the right. Pass when the faces and cost are real.
- [ ] **02-17** A needs-you card has an amber border and sorts first. Pass when a project with needs-you items is first and outlined in amber.
- [ ] **02-18** An onboarding card shows the captain's live face and a teal progress ring. Pass on a project in onboarding.
- [ ] **02-19** The sample project (Hello World, `· sample`) shows as an ordinary card. Pass when the sample started from 00a appears here.
- [ ] **02-20** Last card `+ Add project` (dashed, large plus tile), sub "New idea · existing repo". Pass when it opens screen 03.
- [ ] **02-21** Design note 1: one card is one project, with journey, ring, sparkline, captain and crew. Pass when every card has all five.
- [ ] **02-22** Design note 2: the rail lists the same projects (dot = state). Pass when the rail list and the cards agree.
- [ ] **02-23** Card hover lifts or outlines (link affordance). Pass when hover shows the card is clickable.

**Responsive**
- 1440: 4 tiles, 3 cards per row.
- 1024: 4 tiles; cards 3 or 2 per row with no overflow.
- 768: tiles fit; cards reflow; no horizontal scroll.
- 390: tiles 2×2; cards in 1 column; filters wrap or scroll.

**States**
- Loading: skeleton tiles and cards.
- Empty (no project with a repo): redirects to 00a.
- Error: "Couldn't load projects · Retry", never a blank grid.
- Needs you: amber cards first, plus the Need you tile.

---

## 03 — Add a project (`03-new-project.html`)

A sheet over Projects. Breadcrumb `Projects / Add project`.

- [ ] **03-01** It opens as a sheet (dialog, `aria-label` "Add a project") over a dimmed Projects page with a scrim. Pass when the page behind is dimmed and still visible.
- [ ] **03-02** Sheet header: icon tile (folder-plus) and title `Add a project`. Pass when it renders.
- [ ] **03-03** Segmented control: `New · from an idea` (lightbulb) / `Existing · repo or folder` (git-branch). Pass when both modes work and the 00a doors preselect the right one.
- [ ] **03-04** `New` mode is the `new-project-sheet.tsx` path (an idea, then Helm's plan). Pass when New starts a new app from an idea.
- [ ] **03-05** `Try a sample` link (flask icon) in the header. Pass when it starts the sample project.
- [ ] **03-06** Close ✕. Pass when ✕, Esc and Back all close the sheet.
- [ ] **03-07** Three columns, with step numbers in circles: `1 Pick it`, `2 Helm reads it`, `3 Name & captain`. Pass when the columns render in order.
- [ ] **03-08** Column 1 source switch: `GitHub` (GitHub icon, selected) / `Folder on host` (folder-open). Pass when both sources list real repos or folders.
- [ ] **03-09** Account chip `paramg` (user icon) with a `+ account` link. **(approved change)** Pass when the chip shows the connected account read-only and `+ account` opens Repo and models › GitHub, never an inline account picker.
- [ ] **03-10** Search box with a magnifier, placeholder `Search repos`. Pass when typing filters the list.
- [ ] **03-11** Repo rows: git-branch icon, mono name, and a faint mono age at the right (`3h`, `12d`, `1mo`). Pass when the names and ages of the last push are real.
- [ ] **03-12** The selected repo row is highlighted (`aria-selected`). Pass when clicking a row selects it and starts column 2.
- [ ] **03-13** A repo already used by a project shows an `in use` tag. Pass on the sample's repo.
- [ ] **03-14** Column 2 tinted teal, title `Helm reads it`, blue `live` tag with a pulsing dot. Pass when it goes live as soon as a repo is picked.
- [ ] **03-15** Helm's working face (m) with "Reading `helpdesk-ai-demo`…". Pass when it shows the picked repo.
- [ ] **03-16** Check `Access` ✓, sub `read · branch · PR`. Pass when it reflects the real access check.
- [ ] **03-17** Check `Stack` ✓, sub `Python 3.11 · FastAPI · React`. Pass when the stack is real.
- [ ] **03-18** Check `Build & test` ✓, sub `make setup · make test`. Pass when the commands are real.
- [ ] **03-19** Check `Map code` running (teal, pulse), sub `118 / 214 files`. Pass when it counts live.
- [ ] **03-20** Teal progress bar under the checks. Pass when it advances with the real progress.
- [ ] **03-21** Column 3, field `Name`, prefilled (`HelpDesk AI Demo`). Pass when it is prefilled from the repo and editable.
- [ ] **03-22** Field `Captain`: Helm's face, **Helm**, faint `Engineering Captain`, and a caret. Pass when the dropdown lists the real candidate agents and defaults to the default captain (never Marshal by default).
- [ ] **03-23** Field `Crew`: the faces of Juno, Forge, Vera and Beacon, with "default crew · change later". Pass when the real default crew's faces show.
- [ ] **03-24** Checkbox `Map business rules too (Knowledge Graph)`. Pass when it toggles and, if checked, onboarding builds the KG rules.
- [ ] **03-25** Primary button `Start onboarding` (rocket icon), full width. Pass when it creates the project, starts onboarding and lands on screen 10.
- [ ] **03-26** Footnote "Helm takes it from here · ≈ 5 min · ≈ $1". Pass when the estimate is shown (real estimate, or the default).
- [ ] **03-27** Only one primary in the sheet. Pass on inspection.

**Responsive**
- 1440: centred sheet with 3 columns.
- 1024: 3 columns, no overflow.
- 768: no horizontal scroll; columns may stack.
- 390: full-screen sheet with no radius; columns stacked with top borders.

**States**
- Loading: the repo list shows a skeleton.
- Empty (no repos): "No repos yet · Connect on Repo and models ›" **(approved change)**.
- Error: a failed access check shows red with the reason and `Re-check`.
- Needs you: missing access is an amber row with its action.

---

## 04 — Overview (`04-overview.html`)

Route: `?project_id=<id>&area=project&tab=overview`. Breadcrumb `Projects / HelpDesk AI Demo`. The project header is the shared one; see X-30 to X-48.

- [ ] **04-01** Journey card: teal icon tile (path), title `Journey`, and the `How it works` link (info icon) at the right. Pass when "How it works" explains each stage (a drawer or popover).
- [ ] **04-02** Journey node `Onboarded` done ✓, meta `Nov 2`, tooltip "Onboarded Nov 2 — repo read, stack found, tests run". Pass when it shows the real date and detail.
- [ ] **04-03** Journey node `Ready` done ✓, meta `6/6`, tooltip "Ready for work: 6 of 6 checks". Pass when the count is real.
- [ ] **04-04** Journey node `Backlog` done ✓, meta `7 of 12`, tooltip "Backlog proposed by Marshal, 12 items, you accepted 7". Pass when the numbers are real.
- [ ] **04-05** Journey node `Working` now (teal, hammer, ring pulse 1.8 s), meta `3 merged`, tooltip "Working: 3 merged, 1 building, 1 waiting on you". Pass when it is real and pulses.
- [ ] **04-06** Journey node `Staging` done ✓, meta `2h ago`, tooltip "Staging deploys automatically after merge (policy)". Pass when it shows the real last deploy.
- [ ] **04-07** Journey node `Production`: amber filled (lock icon), meta `needs you`, tooltip "Production needs a person: pick a host, then approve". Pass when it is amber whenever prod waits on a person.
- [ ] **04-08** Journey wires are green between done nodes, green to teal into `now`, and grey after. Pass when they follow the states.
- [ ] **04-09** Each journey node opens a drawer with that stage's record (`drawer=stage:<key>`). Pass when every node opens its drawer.
- [ ] **04-10** The journey scrolls sideways inside its card when it is narrow. Pass when the page never scrolls horizontally.
- [ ] **04-11** Tile `Open work` (link, kanban icon, caret): count `9` and a 4-part stacked bar (teal running, amber needs you, grey planned, green done). Pass when the bar widths come from `by_state`.
- [ ] **04-12** Open work legend: dots with counts (teal 1, amber 1, grey 4, green 3). Pass when the counts are real; click opens the Work tab.
- [ ] **04-13** Tile `Running` (teal lightning, caret): a ring `3/6` (stations done), the label `Search filters`, and the agent's working face with `Building · 12m`. Pass when it is the real current item; click opens that item's work drawer.
- [ ] **04-14** Tile `Staging` (green heartbeat, caret): ring `99.9`, green `Healthy`, mono `p95 180ms`, and a green latency sparkline. Pass when the values and series are real; click opens Deployments.
- [ ] **04-15** Tile `Cost · week` (violet coins, caret): `$14.20`, a green `−18%` delta tag, a violet area sparkline, and `28% of $50 cap`. Pass when the values are real, the delta tag is green when cost falls (red when it rises), and click opens Govern for this project.
- [ ] **04-16** Each tile is a link with a hover state and a chevron. Pass on hover.
- [ ] **04-17** Card `Needs you`: amber hand-waving icon and an amber count tag `2`. Pass when the count equals the header bar.
- [ ] **04-18** Item `Merge PR #3`: merge icon in an amber circle, plus the mini flow `✓ Tests 148 → ✓ Review → Merge (amber)`. Pass when the mini flow comes from the real checks.
- [ ] **04-19** `Review PR` amber button. Pass when it opens the work drawer (or the PR) for that item.
- [ ] **04-20** Item `Pick a prod host`: cloud icon, sub "Beacon can't choose this", and a `Pick host` button. Pass when it opens Infra's add-target flow (preselecting this project) or Settings › Targets.
- [ ] **04-21** Each Needs-you item opens a drawer with its action inside. Pass when clicking the row opens its drawer.
- [ ] **04-22** Onboarding's "only you" items (`kind: setup`) appear here too. Pass when a missing secret shows.
- [ ] **04-23** Card `Recent work` (kanban icon) with the `All work →` link. Pass when the link opens the Work tab.
- [ ] **04-24** Recent work rows (at most 5): a state dot, the title (ellipsis only in the row; the full title is in the tooltip or drawer), a sub (kind or PR, plus mono age or cost), the 6-station strip, faces, and a chevron. Pass when the rows are real and each opens its work drawer.
- [ ] **04-25** Recent row, running (`Ticket search filters`): stations P ✓, D ✓, B teal pulsing, R/PR/S todo; faces Juno and Forge (live). Pass when it matches its real stages.
- [ ] **04-26** Recent row, needs you (`Visual refresh finish`, `PR #3`, `$2.10`): amber tint and left bar, PR station amber. Pass when it matches.
- [ ] **04-27** Recent row, done (`UI refresh`, `PR #2 merged`, `$3.40`): all stations green. Pass when it matches.
- [ ] **04-28** Card `This week` (blue clock icon), a timeline: a line with dots (green merges, teal deploys, violet ideas, amber waiting), labels below and mono day names above (`Mon`, `Wed`, `Thu`, `Today`). Pass when the events are the real `week[]`, in local time.
- [ ] **04-29** The This week timeline is hidden on phones. Pass at 390.
- [ ] **04-30** Card `Captain` (blue steering-wheel icon) with the `Change` link (tooltip "Hand the project to another captain"). Pass when Change opens Settings › Captain & squad.
- [ ] **04-31** Captain body: Helm's large face, `Helm` with a blue `Engineering Captain` tag, and the now-line (teal dot) `Watching 1 build · next: intake`. Pass when the status is the captain's real status.
- [ ] **04-32** Primary `Ask Helm` (chat icon). Pass when it opens the captain drawer (screen 09).
- [ ] **04-33** Button `Plan next week` (map icon). Pass when it opens the captain drawer with that prompt, or the Roadmap plan.
- [ ] **04-34** Crew row: Juno `Plans`, Forge `Builds`, Vera `Tests`, Beacon `Deploys`, and Marshal `Ranks` (tooltip "Chief of Staff ranks this project's backlog against the others"). Pass when the squad is real and each member opens Agent Studio (`workforce&role=`).
- [ ] **04-35** Card `What's next` (violet lightbulb) with the `Roadmap →` link. Pass when the link opens the Roadmap tab.
- [ ] **04-36** Next item 1: rank badge `1`, `Email-to-ticket intake`, sub `Feature · accepted`, green `Next` tag. Pass when it is real.
- [ ] **04-37** Next items 2 and 3: rank, title, sub with a source icon (bug `Incident hotspot`, bell-slash `Alert gap`), and an `Accept` button. Pass when Accept turns the item into planned work.
- [ ] **04-38** Card `Try it` (green key) with the `Settings` link (gear icon). Pass when the link opens Settings › Demo logins.
- [ ] **04-39** Try it `URL` row (globe), with a mono link `203.0.113.20:8004`. Pass when it opens the live app in a new tab.
- [ ] **04-40** Try it demo login row: user icon, role label (`Admin`), mono user `admin@demo.test`, masked `••••••`, eye (tooltip "Reveal (audited)") and copy. Pass when reveal asks for step-up, shows for 30 s and is audited; copy copies the user or revealed password; real users are listed.
- [ ] **04-41** Design note 1: the journey shows the whole life; pulse = live, amber = only you. Pass when this holds.
- [ ] **04-42** Design note 2: Needs you is also pinned in the header on every tab, and here it expands with the mini diagram. Pass when it shows in both places.
- [ ] **04-43** Design note 3: one captain per project, Marshal ranks, and Ask Helm opens screen 09. Pass when it holds.
- [ ] **04-44** Design note 4: every tile, row and chip is a link. Rows and tiles open a drawer; ↗ opens external. Pass when no element on Overview is a dead end.
- [ ] **04-45** A new Needs-you item glows amber once (`aa-glow`) on arrival, then stays still. Pass when you watch a new decision arrive.

**Responsive**
- 1440: journey full width; 4 tiles; 2-column split (left: Needs you, Recent, This week; right: Captain, What's next, Try it).
- 1024: the same, with no overflow.
- 768: no horizontal scroll; the split may stack.
- 390: tiles 2×2; split in 1 column; This week hidden; work rows reflow (stations top-right, links on the next line, faces and chevron hidden); journey nodes 64 px, scrolling inside the card.

**States**
- Loading: skeleton journey, tiles and cards.
- Empty: no work shows "No work yet · New work"; no next shows "Helm is drafting ideas"; no deploy shows the Staging tile "Not deployed".
- Error: a per-card "Couldn't load · Retry".
- Needs you: the card plus the header bar.

---

## 05 — Work (`05-work.html`)

Route: `…&tab=work`. Breadcrumb `Projects / HelpDesk AI Demo / Work`.

- [ ] **05-01** New-work card, accent-bordered, with an icon tile (plus-circle). Pass when it is at the top of Work.
- [ ] **05-02** Kind switch: `Feature` (sparkle, selected), `Bug` (bug), `Ticket` (ticket). Pass when each is selectable and sent as `kind`.
- [ ] **05-03** Input placeholder: Describe it — e.g. "let agents tag tickets by product". Pass when the field accepts text. For Ticket, it accepts a GitHub or Plane issue link.
- [ ] **05-04** Primary `Start` (paper-plane). Pass when it calls `POST …/work`, the new row slides in (150 ms) and readiness or step-up sheets pass through.
- [ ] **05-05** `Helm's plan` label with the plan strip: `Plan` (Juno, agent) → `You approve` (amber) → `Build` (hammer) → `Review` (magnifier) → `PR` (PR icon) → `Staging` (rocket). Pass when the strip reflects the chosen workflow's real steps.
- [ ] **05-06** Estimate chips `≈ 25 min` and `≈ $3` (mono). Pass when the estimates come from the workflow.
- [ ] **05-07** Workflow name `Minor feature workflow · change`. Pass when "change" lets the user pick another workflow.
- [ ] **05-08** Filters: `All 9` (selected), `Needs you 1` (amber dot), `Running 1` (teal dot), `Planned 4`, `Done 3` (green dot). Pass when the counts are real (`counts`) and filtering works.
- [ ] **05-09** Station legend: P Plan, D Decide, B Build, R Review, PR, S Staging. Pass when it shows on desktop and is hidden on phones.
- [ ] **05-10** View switch `List` (rows, selected) / `Board` (kanban), hidden on phones. Pass when Board shows the same rows as cards in columns Needs you · Running · Planned · Done.
- [ ] **05-11** Group header `Needs you` (amber dot). Pass when items waiting on a person are grouped first.
- [ ] **05-12** Row `Visual refresh finish`: amber merge icon tile, `Feature` tag, `Waiting 3h`, mono `$2.10`, stations P D B R done and PR amber, S todo, faces Juno, Forge and Vera. Pass when the row is real, with an amber tint and left bar.
- [ ] **05-13** Row links: `Run #43` (flow-arrow, tooltip "Workflow run"), `Build` (code, tooltip "Build page in Code Tasks"), `PR #3 ↗` (tooltip "Pull request on GitHub"). Pass when each opens its target (`delivery_workflows&wfr=`, `code_tasks&…build=`, GitHub in a new tab).
- [ ] **05-14** Row action `Merge` (amber button). Pass when it opens the merge decision (a person merges).
- [ ] **05-15** Group header `Running` (teal dot). Pass when present.
- [ ] **05-16** Row `Ticket search filters`: teal lightning tile, violet `from Roadmap` tag, `Build · 12m`, mono `$0.90`, stations P D done and B pulsing teal, faces Juno and Forge (live). Pass when it is real.
- [ ] **05-17** Running row links: `Run #44`, `Build`, `Live log` (terminal icon). Pass when Live log streams the running log.
- [ ] **05-18** Group header `Planned · ranked by Marshal`. Pass when the planned rows are in Marshal's rank.
- [ ] **05-19** Row `Email-to-ticket intake`: envelope tile, `Feature`, `Next up`, all stations todo, Juno's face, `Start now` (play). Pass when Start now starts it.
- [ ] **05-20** Row `SLA timer ignores weekends`: red bug tile, red `Bug` tag, GitHub `#12` link chip, no faces, `Start now`. Pass when #12 opens the issue.
- [ ] **05-21** Row `Fix 6 failing tests`: red x-circle tile, red `CI` tag, the file `tests/test_notify.py`, `Start now`. Pass when it is real.
- [ ] **05-22** Row `Index tickets by status`: violet database tile, violet `from Roadmap`, `Start now`. Pass when it is real.
- [ ] **05-23** Planned rows can be dragged to re-rank (design note 3). Pass when dragging changes the order and the order is saved.
- [ ] **05-24** Group header `Done` (green dot). Pass when present.
- [ ] **05-25** Done row `UI refresh`: green check tile, `Feature`, `Wed`, `$3.40`, all stations green, faces Forge and Beacon. Links: `Run #41`, `PR #2 ↗` (merge icon), `Staging` (globe). Pass when the links open.
- [ ] **05-26** Done row `SLA timers` (`Mon`, `$4.80`), with links `Run #38`, `PR #1 ↗` and `Staging`. Pass when it is real.
- [ ] **05-27** Done row `Onboarding`: `Setup` tag, `Nov 2`, `$1.20`, a mini flow `Discover → Ready → Backlog` (all done; hidden on phones), faces Helm and Marshal, link `Run #36`. Pass when it is real.
- [ ] **05-28** Clicking anywhere on a row (outside the chips) opens the work drawer (05b) in place. Pass with no navigation and the URL gaining `&drawer=run:<id>` or `build:<id>`.
- [ ] **05-29** Each row's tile icon reflects its kind (feature, bug, CI, roadmap, setup). Pass on inspection.
- [ ] **05-30** Search, where `q` matches words or a PR number (`#3`). Pass when the filter (or ⌘K scoped to the project) finds by PR number. This is a contract feature reached through the filter or search.
- [ ] **05-31** Design note 1: one box, and the plan is shown with time and cost before Start. Pass when the plan updates before pressing Start.
- [ ] **05-32** Design note 2: the same station diagram appears everywhere (Overview, Projects card, Code Tasks). Pass when the same station component is used in each.
- [ ] **05-33** Design note 3: Board view columns, plus drag to re-rank Planned. Pass when it holds.

**Responsive**
- 1440: rows in a grid (icon · title · stations · faces · links).
- 1024: the same, with no overflow; links may wrap.
- 768: no horizontal scroll.
- 390: rows reflow (stations at the top right; links under, full width); faces hidden; legend and view switch hidden.

**States**
- Loading: skeleton rows.
- Empty: "No work yet. Describe the first piece above", with the box focused.
- Error: "Couldn't load work · Retry". A Start error shows the existing sheet (readiness or step-up), never a raw error.
- Needs you: the Needs you group first, amber.

---

## 05b — Work drawer (`05b-work-drawer.html`)

- [ ] **05b-01** The drawer slides in from the right (240 ms) over the dimmed Work list with a scrim. Pass when the list stays visible behind it.
- [ ] **05b-02** It is deep-linked (`&drawer=run:<id>`). Esc, ✕, the scrim and Back all close it. Pass when reloading the URL reopens it.
- [ ] **05b-03** Header: amber icon tile (merge), the full title (`Visual refresh finish`, not truncated), and meta: `Feature` tag, `Run #43`, mono `$2.10`, mono `31 min`. Pass when the values are real.
- [ ] **05b-04** Expand icon (arrows-out, tooltip "Open full page in Code Tasks"). Pass when it opens the full build page.
- [ ] **05b-05** Close ✕ (tooltip "Close"). Pass when it closes.
- [ ] **05b-06** Needs bar inside: amber `!`, `Ready to merge`, and the amber button `Merge PR #3` (merge icon). Pass when it is the one action, and it acts or opens the PR.
- [ ] **05b-07** Section `How it went`: a checklist of steps, each with the agent's face, step name, sub and link. Pass when the steps are the real run's steps.
- [ ] **05b-08** Step `Plan` ✓, Juno, sub `PRD · 4 changes`, link `PRD`. Pass when PRD opens the PRD.
- [ ] **05b-09** Step `You approved` ✓, the user's face, sub `Tue 14:02` (local time). Pass when the time is the viewer's local time.
- [ ] **05b-10** Step `Build` ✓, Forge, sub `11 files · +420 −96`, link `Diff`. Pass when Diff opens the diff.
- [ ] **05b-11** Step `Review & test` ✓, Vera, sub `148/148 · 2 findings fixed`, link `Findings`. Pass when Findings opens the review findings.
- [ ] **05b-12** Step `Merge`, amber `!`, the user's face, sub `Only a person merges`, link `PR #3 ↗`. Pass when it opens GitHub.
- [ ] **05b-13** Step `Staging` todo, Beacon, sub `Auto after merge`. Pass when it turns done after the deploy.
- [ ] **05b-14** Mini card `Preview`: globe, mono `:8104` link, green `Up` tag. Pass when it opens the preview and the state is real.
- [ ] **05b-15** Mini card `Cost`: a 3-part bar (teal / blue / green) labelled `Plan · Build · Review`. Pass when the split is real.
- [ ] **05b-16** Links section: `Workflow run`, `Build in Code Tasks`, `PR #3 ↗`, `Logs`, `What it learned` (graph icon). Pass when each opens its target. What it learned opens the KG learned items for this run.
- [ ] **05b-17** Footer composer: Helm's face, "Ask Helm about this work…" and a send icon. Pass when sending opens the captain chat with this work as the subject.
- [ ] **05b-18** It opens in place with no navigation (README §5). Pass when the tab and scroll position are kept after closing.

**Responsive**
- 1440 / 1024 / 768: right drawer at its fixed width.
- 390: the drawer is 100 vw.

**States**
- Loading: drawer skeleton.
- Empty: not applicable.
- Error: "Couldn't load this work · Retry".
- Needs you: the amber bar at the top.

---

## 06 — Repo & knowledge (`06-repo-knowledge.html`)

Route: `…&tab=knowledge`. Breadcrumb `Projects / HelpDesk AI Demo / Repo & knowledge`.

- [ ] **06-01** Card `Repositories` (GitHub icon) with a count tag `1` and the `+ Add repo` link. Pass when Add repo opens today's Import-a-repo picker.
- [ ] **06-02** Repo name in bold mono, `paramg/helpdesk-ai-demo`, with an external icon. Pass when it opens GitHub.
- [ ] **06-03** Repo meta: branch `main`, `last push 3h`, `via GitHub App · paramg`. Pass when the values are real.
- [ ] **06-04** Access strip `Access`: `Read` ✓ (eye), `Branch` ✓, `Open PR` ✓, `Merge: you` (dashed amber lock, tooltip "By design a person merges"), `Staging` ✓ (rocket). Pass when each check is the real result (tooltip "Access checks — BOSS re-checks before every run").
- [ ] **06-05** `Re-check` button (arrows-clockwise). Pass when it reruns the access checks live.
- [ ] **06-06** Multi-repo: each repo gets its own access strip. Pass on a project with 2 repos.
- [ ] **06-07** Card `Dossier` (teal identification-badge), with "by Helm · re-read 3h ago" and the `Full dossier` link. Pass when Full dossier opens the dossier drawer (`drawer=dossier`).
- [ ] **06-08** Stack chips: Python 3.11, FastAPI, React 18, Vite, Postgres 16, Docker, each with an icon. Pass when they are the real `dossier.stack`.
- [ ] **06-09** `Commands` key-value: Install `make setup`, Test `make test`, Build `npm run build`, Run `docker compose up`, as code. Pass when they are real and copyable.
- [ ] **06-10** `Tests`: ring `96%`, mono `142 / 148`, and "6 failing → on Work". Pass when it is real and "on Work" links to the Work row.
- [ ] **06-11** `Code map`: a 4-part bar (app 42% blue, web 35% teal, tests 15% green, infra 8% amber), each with a tooltip, and a caption `app · web · tests · infra · 214 files`. Pass when the split is real.
- [ ] **06-12** `Hotspots`: heat bars with a mono path, a bar, and a count (`9×`, `6×`, `4×`). Pass when they are the real hotspots (paths readable, ellipsis only for long paths, with a tooltip).
- [ ] **06-13** Card `Docs & decisions` (blue files) with the `Add doc` link (upload). Pass when Add doc uploads a document.
- [ ] **06-14** Doc rows: `PRD · SLA timers` (Juno · approved Nov 3), `PRD · Visual refresh` (Juno · approved Nov 5), `ADR-001 · Postgres queue` (violet signpost, from repo /docs), `Staging deploys on merge` (amber gavel, Policy · you, Nov 2), each with a chevron. Pass when each opens its document (`drawer=doc:<path>`).
- [ ] **06-15** Card `Knowledge graph` (violet graph) with the `Open graph →` link. Pass when it opens the Knowledge Graph scoped to this project (`area=knowledge`).
- [ ] **06-16** Graph snippet centred on the hottest entity (`Ticket`, a large blue node). Pass when the centre is the real hottest entity.
- [ ] **06-17** Graph nodes: `Customer` (teal), `SLA policy` (violet), `sla.py` (green), `Escalation` (amber), `Org` (teal small), each labelled. Pass when the nodes are real and the colours mean their kind.
- [ ] **06-18** Graph edges: the centre to each neighbour, plus Customer→Org; one rule edge (violet) `breach → escalate` with a label. Pass when the edges are real and the rule edge is visually distinct.
- [ ] **06-19** Graph chips: `412 nodes` (cube), violet `9 rules` (scales), blue `14 learned` (brain). Pass when the counts are real.
- [ ] **06-20** Card `Learned on the job` (green brain). Pass when it renders.
- [ ] **06-21** Learned rows: the agent's face, the lesson (`Tests need TZ=UTC`), the sub (`Run #38 · reused 2×`), and a tag: green `used` or grey `new`. Pass when the rows are real lessons with run links and reuse counts.
- [ ] **06-22** Design note 1: the dossier is re-read on every merge; facts are chips, commands are copyable, hotspots are heat bars, prose only in the drawer. Pass when "re-read" updates after a merge.
- [ ] **06-23** Design note 2: KG centred on the hottest entity; learned items show their run and reuse. Pass when it holds.
- [ ] **06-24** Design note 3: multi-repo "Add repo" uses today's picker. Pass when it holds.

**Responsive**
- 1440: Repositories full width; split (left: Dossier, Docs; right: KG, Learned).
- 1024: the same, with no overflow.
- 768: no horizontal scroll.
- 390: 1 column; the access strip wraps; the KG SVG scales.

**States**
- Loading: skeletons.
- Empty: no dossier shows "Helm builds this during onboarding"; no KG shows "Not indexed yet · Index".
- Error: an access check failure shows a red node with the reason.
- Needs you: missing access is an amber node with its action.

---

## 07 — Roadmap (`07-roadmap.html`)

Route: `…&tab=roadmap`. Breadcrumb `Projects / HelpDesk AI Demo / Roadmap`.

- [ ] **07-01** Card `Plan` (violet map), with "Helm proposes · Marshal ranks · you decide" and the `Plan with Helm` link (chat icon). Pass when the link opens the captain drawer with a planning prompt.
- [ ] **07-02** Column `Now` (teal dot) with count tag `2`: `Search filters` (running, Forge live) and `Visual refresh` (amber `!`, the user's face). Pass when Now holds the real running and needs-you items.
- [ ] **07-03** Column `Next` (grey dot) with count `3`: `Email intake`, `Weekend SLA fix`, `Fix 6 tests`. Pass when these are the real accepted items in rank order.
- [ ] **07-04** Column `Later` (idle dot) with count `2`: `Reply suggestions`, `Status index`. Pass when it is real.
- [ ] **07-05** Items in the columns open their drawer. Pass when clicking an item opens it.
- [ ] **07-06** `Recommendations` heading (violet lightbulb), with a violet `5 new` tag. Pass when the count is real.
- [ ] **07-07** Filters: `Open 5` (selected), `Accepted 7`, `Rejected 2`, `Snoozed 1`. Pass when the counts are real and filtering works.
- [ ] **07-08** Rec card 1: rank `1`, title `Regression test for SLA weekend pause`. Pass when it renders.
- [ ] **07-09** Rec 1 flow: `2 incidents` (signal, amber warning) → `sla.py hot` (signal, fire) → `Incident hotspot` (detector, violet detective) → `Test + fix` (agent pill, Helm). Pass when the evidence, detector and agent are real.
- [ ] **07-10** Rec 1 meta: `Hardening` tag, a `Value` bar (82%, green), `Effort S`, `≈ $2`, and a `Why? ›` link. Pass when Why opens the full reasoning (drawer `rec:<id>`).
- [ ] **07-11** Rec 1 actions: primary `Accept` (check) and `Reject` (x). Pass when Accept turns it into planned work (in Marshal's rank on Work, and in Next).
- [ ] **07-12** Rec card 2: `Alert rule for /api/tickets 5xx`, flow `New endpoint` (plug) → `Alert gap` (bell-slash) → `Add alert` (Beacon), `Hardening`, Value 74%, Effort S, `≈ $1`, Accept and Reject. Pass when it is real.
- [ ] **07-13** Rec card 3 in reject mode: red border, flow `3 asks in PRDs` (chats) → `Feature demand` (trend-up) → `Write PRD` (Juno). Pass when it renders.
- [ ] **07-14** Reject reasons: `Reject because`, one-tap chips `Not now` (selected, accent), `Wrong idea`, `Duplicate`, `Won't do`, and an input "Say more (optional)". Pass when one tap selects a reason, the note is optional and the reason category is sent.
- [ ] **07-15** Reject-mode actions: primary `Reject` and ghost `Cancel`. Pass when Reject records the reason and Cancel restores the card.
- [ ] **07-16** Rec card 4: `Index tickets(status, created_at)`, flow `List p95 1.4s` (timer) → `Slow query` (gauge) → `Migration` (Forge), `Performance`, Value 60%, Effort S, Accept and Reject. Pass when it is real.
- [ ] **07-17** Accepted card: a green border, a green check in place of the rank, `Email-to-ticket intake`, flow `✓ Accepted → On Work · Next` (kanban). Pass when it appears after Accept.
- [ ] **07-18** Accepted card `Open work →` link chip. Pass when it opens that work item.
- [ ] **07-19** Snooze lives in the card's ⋯ menu (design note 2). Pass when each open card has ⋯ → Snooze (until, reason).
- [ ] **07-20** Design note 1: each card is a diagram (evidence → detector → agent). Pass when no card is prose only.
- [ ] **07-21** Design note 2: Reject always asks a reason in one tap and teaches the detector. Pass when Reject cannot complete without a reason chip.
- [ ] **07-22** It works with the ambient preview off (`BOSS_AMBIENT_COS=off`). Pass when listing, accept and reject work with it off.

**Responsive**
- 1440: Now/Next/Later in 3 columns; rec cards with actions at the right.
- 1024: the same.
- 768: no horizontal scroll.
- 390: Now/Next/Later stack; rec actions move under the card in a row.

**States**
- Loading: skeleton.
- Empty: no recs shows "No ideas yet · Plan with Helm".
- Error: Retry.
- Needs you: the Now `!` item.

---

## 08 — Deployments (`08-deployments.html`)

Route: `…&tab=deployments`. Breadcrumb `Projects / HelpDesk AI Demo / Deployments`.

- [ ] **08-01** Card `How code ships` (teal rocket) with the `Policy in Govern` link. Pass when the link opens Govern's deploy policy.
- [ ] **08-02** Ship path: `PR merged` ✓ (merge) → `Image built` ✓ (package) → `Staging · auto` (Beacon agent pill) → `Smoke test` ✓ → `Prod · you approve` (amber, lock). Pass when the nodes and states come from `ship_path`, and the gates come from policy.
- [ ] **08-03** Ship-path edges are green up to Smoke test and grey into Prod. Pass when they follow the states.
- [ ] **08-04** Staging env card: green flask icon, `Staging`, green `Healthy` tag with a dot. Pass when the health is real.
- [ ] **08-05** `Redeploy` button (arrow-counter-clockwise). Pass when it redeploys (with the existing confirm).
- [ ] **08-06** Env URL `http://203.0.113.20:8004 ↗` (globe). Pass when it opens the live app in a new tab.
- [ ] **08-07** Uptime ring `99.9%` (56 px). Pass when it is real.
- [ ] **08-08** `24h` health bars (24 bars, amber for a degraded hour). Pass when the bars are the real hourly health.
- [ ] **08-09** Key-values `p95 180ms` and `Errors 0.1%`. Pass when they are real.
- [ ] **08-10** Last-deploy line: Beacon's face, "Last deploy **PR #2** · 2h ago", mono commit `a41c9e2`. Pass when it is real and PR #2 opens the PR.
- [ ] **08-11** `Logs` link chip (terminal). Pass when it opens the env logs.
- [ ] **08-12** `runs on 203.0.113.20 ↗` link chip (hard-drives, tooltip "Opens this server in Infra (shared by 3 projects)"). Pass when it opens Infra's host drawer for that server (`host_href`).
- [ ] **08-13** Production card in the locked style: amber buildings icon, `Production`, amber `Not set up` tag. Pass when it shows whenever prod has no host.
- [ ] **08-14** Production empty-state illustration: a dashed box → amber box, with an amber `!` badge. Pass when it renders.
- [ ] **08-15** Text `Pick a host for production` and "Beacon sets it up; you approve each prod deploy.". Pass when they render.
- [ ] **08-16** Amber `Pick host` button (cloud). Pass when it opens Infra's add-target sheet (`add=target`) for this project.
- [ ] **08-17** When prod exists (`ready`), it is a full env card like Staging. Pass on a project with prod.
- [ ] **08-18** Card `Demo logins` (green key) with a `Staging` env tag and the `Manage in Settings` link (gear). Pass when the link opens Settings › Demo logins.
- [ ] **08-19** Login row Admin: blue `Admin` tag (crown), mono `admin@demo.test`, masked password, eye (tooltip "Reveal — owners only, audited") and copy. Pass when it is real.
- [ ] **08-20** Login row Agent in the revealed state: `Agent` (headset), mono password `demo-agent-24`, an amber countdown tag `29s` (tooltip "Revealed for 30s"), eye-slash and copy. Pass when reveal needs step-up, counts down from 30 s, then hides; audited as `project.demo_logins_revealed`.
- [ ] **08-21** Login row Customer (user icon), masked, eye and copy. Pass when it is real.
- [ ] **08-22** Demo logins here are read-only (no edit or rotate). Pass when only reveal and copy are offered.
- [ ] **08-23** Card `History` (blue clock). Pass when it renders.
- [ ] **08-24** History row `Staging · PR #2 UI refresh` ✓, `Wed 16:40 · 3m 10s · smoke ✓`, Beacon. Pass when the time is local and the values are real.
- [ ] **08-25** History row `Staging · PR #1 SLA timers` ✓, `Mon 11:05 · 2m 50s · smoke ✓`. Pass when it is real.
- [ ] **08-26** History row in the warn state `↺ Staging · first deploy`, `Nov 2 · port clash → moved to 8004 by Beacon`. Pass when recoveries show amber with what the agent did.
- [ ] **08-27** History rows open the deploy (`infra&view=deploys&deploy=`). Pass when clicked.
- [ ] **08-28** Design note 1: the ship path is from Govern policy; prod is amber and locked until a person. Pass when it holds.
- [ ] **08-29** Design note 2: read-only logins, reveal rules, and the Infra link back. Pass when Infra › server lists this project and links back here.

**Responsive**
- 1440: ship path; 2 env cards side by side.
- 1024: the same.
- 768: no horizontal scroll.
- 390: env cards stack; login rows put the user on a second line; the ship path wraps or scrolls inside the card.

**States**
- Loading: skeletons.
- Empty: nothing deployed shows "Not deployed yet · first deploy after the first merge".
- Error: env health unknown shows a grey `Unknown`, never a false Healthy.
- Needs you: Prod `you approve` / `Pick host`.

---

## 09 — Ask the captain (`09-captain-drawer.html`)

- [ ] **09-01** A drawer (480 px), with the Overview dimmed behind it. Pass when it opens from Ask (header), Ask Helm (Overview) or ⌘K "Ask Helm…".
- [ ] **09-02** It is the existing Ask BOSS dock scoped to the project (subject `{kind: project}`), not a second chat. Pass when the same conversation shows in the dock.
- [ ] **09-03** Header: Helm's face (m), **Helm**, a blue `Captain` tag. Pass when it shows the real captain.
- [ ] **09-04** Scope line: folders icon, `HelpDesk AI Demo only` · green dot · `knows 412 facts`. Pass when the project and fact count are real.
- [ ] **09-05** Header icons: history (tooltip "Past conversations") and ✕ (Close). Pass when history lists past chats and ✕, Esc and Back close the drawer.
- [ ] **09-06** User message bubble (right): "What's left before I can demo this…". Pass when the user's messages render as bubbles.
- [ ] **09-07** Agent answer starts with a mini flow: `Merge PR #3` (amber) → `Search filters` (teal, running) → `Staging` (done) → `Demo ready` (todo, dashed). Pass when answers can carry a flow-chip diagram.
- [ ] **09-08** Answer text with bold key items. Pass when it renders as formatted text, with no raw markdown.
- [ ] **09-09** Citation chips: `PR #3`, `Run #44`, `Staging`, `Demo logins`. Pass when each opens its target.
- [ ] **09-10** Second user message: "Also add a 'VIP customer' flag…". Pass when it renders.
- [ ] **09-11** Agent answer with an action card: teal sparkle tile, `New work · VIP customer flag`, cost chips `≈ $3` and `≈ 30m`. Pass when the act-card renders.
- [ ] **09-12** Action card plan strip: `Plan` (Juno) → `You approve` → `Build` → `PR`. Pass when it renders.
- [ ] **09-13** Action buttons: primary `Start` (play), `Edit`, ghost `Not now`. Pass when Start creates the work, Edit opens it prefilled in New work, and Not now dismisses it.
- [ ] **09-14** Citations under it: `#12 weekend SLA` (bug) and `sla.py` (file-code). Pass when they open the issue and the file.
- [ ] **09-15** Footer suggestion chips: `Plan next week` (map), `Why is PR #3 waiting?` (question), `What did this cost?` (coins). Pass when tapping one sends it and the suggestions are relevant to the project.
- [ ] **09-16** Composer: paperclip, placeholder "Ask Helm, or describe new work…", and a primary send. Pass when sending works and attaching works.
- [ ] **09-17** Answers cite links (`[act:<id>]`, work hrefs) and offer actions. Pass on a real question.

**Responsive**
- 1440 / 1024 / 768: a 480 px drawer.
- 390: 100 vw.

**States**
- Loading: typing indicator.
- Empty: the suggestions only.
- Error: "Helm couldn't answer · Retry".
- Needs you: answers point to Needs-you items.

---

## 10 — Onboarding (`10-onboarding.html`)

Breadcrumb `Projects / HelpDesk AI Demo / Onboarding`.

- [ ] **10-01** Header: the `HD` mark, h1 `HelpDesk AI Demo`, repo link chip, blue `Onboarding` tag (compass), mono elapsed `6 min`. Pass when they are real.
- [ ] **10-02** Captain chip: Helm's live face, **Helm** / `Onboarding`, and an `Ask` button. Pass when Ask opens the captain drawer.
- [ ] **10-03** Stage strip node `Discover`, done (magnifier), meta `done · 3m`. Pass when it is real.
- [ ] **10-04** Stage `Ready for work`, now (list-checks, pulse), meta `4 of 6`. Pass when it is real.
- [ ] **10-05** Stage `Propose backlog` (list-numbers), meta `drafting`. Pass when it is real.
- [ ] **10-06** Stage `Work it` (arrows-clockwise), meta `verified loop`. Pass when it renders.
- [ ] **10-07** Stage `Deploy` (rocket), meta `staging auto`. Pass when it renders.
- [ ] **10-08** Stage connectors are green through done and now. Pass when they follow the states.
- [ ] **10-09** Now line: Beacon's live face, **Now**, "Beacon is checking staging port 8004", and a `Watch` link (terminal). Pass when it names the agent and action happening now, and Watch opens the live log.
- [ ] **10-10** Card `Discovered` (green magnifier) with a green `done` tag. Pass when it renders.
- [ ] **10-11** Discovery map: Helm at the centre, edges to 5 green ✓ nodes: `214 files`, `FastAPI+React`, `142/148 tests`, `3 hotspots`, `4 docs`. Pass when the values are real.
- [ ] **10-12** `Open dossier →` link. Pass when it opens the dossier (Repo & knowledge).
- [ ] **10-13** Card `Ready for work`: teal border, list-checks icon, blue `4/6`. Pass when the count is real.
- [ ] **10-14** Check `Repo access` ✓, `read · branch · PR`, Helm. Pass when it is real.
- [ ] **10-15** Check `Builds clean` ✓, `make setup · 1m 40s`, Forge. Pass when it is real, or `skipped` with "the first build runs the tests in its own sandbox" when there is no CI.
- [ ] **10-16** Check `Tests run` ✓, `6 failing → backlog`, Vera. Pass when it is real.
- [ ] **10-17** Check `Workflow picked` ✓, `Minor feature`, Helm. Pass when it is real.
- [ ] **10-18** Check `Staging host` running (teal pulse), `checking :8004`, Beacon live. Pass when it is real.
- [ ] **10-19** Check `App secret`, amber `!`, `OPENAI_API_KEY — only you`, amber `Add`. Pass when Add opens the write-only value entry, and the item is also in Needs you and on the bell.
- [ ] **10-20** **(approved change)** The model check is read-only: Models shows ✓ from the global setting, or links to Repo and models if missing, with no picker. Pass when no model picker appears.
- [ ] **10-21** Each check line shows the agent doing it. Pass on every line.
- [ ] **10-22** Card `Backlog proposed` (violet list-numbers), with Marshal's face (tooltip "Ranked by Marshal, Chief of Staff") and "by Marshal". Pass when it renders.
- [ ] **10-23** Backlog item 1 `Fix 6 failing tests`, sub CI (x-circle), red `bug` tag. Pass when it is real.
- [ ] **10-24** Backlog item 2 `SLA ignores weekends`, `issue #12` (GitHub), red `bug`. Pass when the issue link opens.
- [ ] **10-25** Backlog item 3 `SLA timers`, `issue #9`, `feature`. Pass when it is real.
- [ ] **10-26** Backlog item 4 `UI refresh`, `TODO ×7` (note), `feature`. Pass when it is real.
- [ ] **10-27** `… +8 more` row. Pass when the count is real and it expands or links to the full list.
- [ ] **10-28** Backlog buttons: primary `Accept backlog` (dimmed, tooltip "Unlocks when Ready is 6/6") and `Edit`. Per contract §14, these resolve to "See the roadmap": items are accepted one by one on the Roadmap and What's next. Pass when the build shows the buttons the contract settles on, they unlock at 6/6, and they lead to the Roadmap.
- [ ] **10-29** Design note 1: the agent does the checklist and asks only what it cannot; the amber item is mirrored in Needs you and on the bell. Pass when it holds.
- [ ] **10-30** Design note 2: Work it = build → review → tests → PR, never skipped; Deploy = staging by policy, prod a person. Pass when it holds.
- [ ] **10-31** Design note 3: on completion this page becomes the Overview (screen 04) at the same URL, and the journey keeps these stages as its first nodes. Pass when it holds.
- [ ] **10-32** It works with the ambient preview off; onboarding is an explicit per-project action. Pass when it holds.

**Responsive**
- 1440: stage strip; 3 cards in a row.
- 1024: the same.
- 768: no horizontal scroll.
- 390: the stage strip scrolls sideways (56 px min per stage); cards stack.

**States**
- Loading: skeleton.
- Empty: `not_started` shows a `Start onboarding` button.
- Error: `failed` shows a red stage with the error and a retry.
- Needs you: amber "only you" items.

---

## 11 — Jump to (⌘K) (`11-jump-palette.html`)

- [ ] **11-01** ⌘K (or Ctrl-K, or the top-bar `Jump to…`) opens the palette centred near the top (at most 660 px wide), over a scrim. Pass on every screen.
- [ ] **11-02** Input row: magnifier, the query (`pr 3`), a scope tag with folders icon `HelpDesk AI Demo`, and an `esc` key hint. Pass when the scope is the current project and can be removed to search everything.
- [ ] **11-03** Group `Work`. Pass when results are grouped.
- [ ] **11-04** Result `PR #3 · Visual refresh finish`: amber PR tile, amber `needs you` tag, hint `↵ drawer · ⌘↵ GitHub`, selected. Pass when ↵ opens the drawer and ⌘↵ opens GitHub.
- [ ] **11-05** Result `Run #43 · workflow run` (teal flow-arrow), hint `Workflows`. Pass when it opens the run.
- [ ] **11-06** Result `Build · Visual refresh finish` (code), hint `Code Tasks`. Pass when it opens the build.
- [ ] **11-07** Group `Deploys & agents`. Pass when it renders.
- [ ] **11-08** Result `Staging · 203.0.113.20:8004` (green globe), hint `open ↗`. Pass when it opens the live app.
- [ ] **11-09** Result `Ask Helm about "pr 3"` (Helm's face), hint `captain`. Pass when it opens the captain drawer with the query.
- [ ] **11-10** Group `Do` (top border). Pass when it renders.
- [ ] **11-11** Action `New work in this project` (plus), key `N`. Pass when N, or choosing it, opens New work.
- [ ] **11-12** Action `Switch project…` (folders), key `P`. Pass when it lists projects.
- [ ] **11-13** Keyboard: ↑/↓ move the selection (accent highlight), ↵ opens, Esc closes. Pass when it holds.
- [ ] **11-14** With no query, the newest items are shown. Pass when it holds.
- [ ] **11-15** It reaches any run, PR, deploy, agent, doc or project in one keystroke (README zero hunting). Pass when each kind is findable.

**Responsive**
- 1440 / 1024 / 768: the palette at its width.
- 390: `calc(100vw − 32px)`; the top-bar entry is hidden (⌘K hint hidden), so it opens from the search icon.

**States**
- Loading: a spinner in the input.
- Empty: "No matches · Ask Helm" row.
- Error: "Search unavailable".
- Needs you: the amber tag on matching results.

---

## 12 — Health (`12-health.html`)

Route: `…&tab=health`. Breadcrumb `Projects / HelpDesk AI Demo / Health`.

- [ ] **12-01** Staging card: green heartbeat icon, `Staging`, green `Healthy` tag, a live link chip `203.0.113.20:8004 ↗` (tooltip "Opens the live app"), and a faint `last 24h`. Pass when they are real.
- [ ] **12-02** Tile `Uptime`: ring `99.9` and `SLO 99.5%`. Pass when they are real (default SLO 99.5% until a rule sets one).
- [ ] **12-03** Tile `Error rate`: `0.4%` with an amber sparkline. Pass when the series is real.
- [ ] **12-04** Tile `p95 latency`: `180 ms` with a teal sparkline. Pass when it is real.
- [ ] **12-05** Tile `SLO burn`: a green bar (22%), a green `OK` tag, and "22% of budget used". Pass when the burn comes from the last 24 h, with `warning` amber and `breach` red.
- [ ] **12-06** The ring and sparklines open the environment drawer with full charts (`drawer=env:<target id>`). Pass when clicked.
- [ ] **12-07** Production row in the locked style: amber buildings, `Production`, amber `Not set up`, "Health appears once prod has a host", and a `Deployments →` link chip. Pass when it opens Deployments.
- [ ] **12-08** Card `Incidents` (red siren) with the `In Agents at work →` link. Pass when the link opens Agents at work filtered to this project.
- [ ] **12-09** Incident 1, resolved: green ✓, `5xx on /api/tickets`, `Today 09:12 · 14 min · resolved by the squad`. Pass when it is real, in local time.
- [ ] **12-10** Incident 1 mini flow: `Alert` (signal, bell) → `SRE squad` (Beacon agent pill) → `Pool exhausted` (decision, magnifier) → `Pool 10→20` (done, wrench) → `Recovered` (done). Edges green. Pass when the flow is the real incident path.
- [ ] **12-11** Incident `Graph ↗` link chip (robot). Pass when it opens the full squad graph in Agents at work (`agent_ops&item=`).
- [ ] **12-12** Incident 2, needs you: amber round `!`, `Slow ticket list`, `Wed · fix needs your approval`, flow `p95 1.4s` → `SRE squad` → `Missing index` → `Approve fix` (amber) → `Recovered` (todo, dashed), and `Graph ↗`. Pass when it is real, and it also shows in Needs you.
- [ ] **12-13** Card `Alerts` (amber bell-ringing) with a firing tag (`0 firing` green; amber or red when firing). Pass when the count is real.
- [ ] **12-14** Alert rows: a dot, the name, a sub (`page · resolved 09:26`, `ticket · quiet 3d`, `page · quiet 5d`), and a tag (`resolved` green, `ok` idle). Pass when they are real.
- [ ] **12-15** **Parity (§13).** Below the summary, the full per-app alert-rules component from Observability, scoped to this project's environments: query, Every/Window/For/Keep firing/Threshold/Min req/Severity/Routes to/Data, state chip, `Respond automatically`, `Tune`, `Silence`, `+ New alert rule`, sources and history. Pass when every action works here and still works in Observability.
- [ ] **12-16** Card `Monitoring` (teal shield-check). Pass when it renders.
- [ ] **12-17** Monitoring body: Beacon's large face, `Beacon's pack`, green dot `6 rules · in repo · on deploy`. Pass when the counts are real.
- [ ] **12-18** Rule chips: Uptime (pulse), 5xx (warning), Latency (timer), Disk, DB conns, TLS (certificate). Pass when they are the real rules.
- [ ] **12-19** Link `monitoring/alerts.yaml ↗` (file-code). Pass when it opens the file in the repo.
- [ ] **12-20** Card `Go deeper` (blue link icon). Pass when it renders.
- [ ] **12-21** `Grafana dashboard` (chart-line, external icon). Pass when it opens Grafana only when Grafana answers; otherwise it is disabled with the reason (`grafana_reason`).
- [ ] **12-22** `Logs · staging` (terminal, caret). Pass when it opens the logs.
- [ ] **12-23** `Observability › this app` (pulse, arrow, tooltip "Org-wide view of every app and BOSS itself"). Pass when it opens Observability on this app, which links back.
- [ ] **12-24** The header health chip on every tab opens this tab. Pass from every tab.
- [ ] **12-25** Design note 1: one card per environment with four numbers. Pass when each environment has its own card.
- [ ] **12-26** Design note 2: incident mini flows, Graph ↗, needs-you mirrored. Pass when it holds.
- [ ] **12-27** Design note 3: Beacon installs the pack on first deploy; the links are one click away. Pass when it holds.

**Responsive**
- 1440: env card with 4 tiles; split (Incidents, Alerts | Monitoring, Go deeper).
- 1024: the same.
- 768: no horizontal scroll.
- 390: tiles 2×2; split stacks; the incident flows wrap.

**States**
- Loading: skeleton.
- Empty (`empty {label, detail, href}`): "Nothing deployed yet" or "No rules yet · Beacon's pack installs on deploy".
- Error: unknown health is grey `Unknown`.
- Needs you: the amber incident.

---

## 13 — Settings (`13-settings.html`)

Route: `…&tab=settings` (`&drawer=section:<key>` jumps to a section). Breadcrumb `Projects / HelpDesk AI Demo / Settings`.

- [ ] **13-01** Section nav, sticky on the left (200 px): `Demo logins` (key, current), `Deploy values` (lock-key), `Targets & hosts` (hard-drives), `Captain & squad` (steering-wheel), `Autonomy & budget` (sliders), `Repos` (GitHub), `Notifications` (bell). Pass when each scrolls to its section and the current one is highlighted.
- [ ] **13-02** Card `Demo logins` (green key), with an env switch `Staging` (selected) / `Preview` / `Prod`. Pass when switching shows that environment's logins.
- [ ] **13-03** Row Admin: blue `Admin` tag (crown), mono `admin@demo.test`, masked password, a `Reveal` button (eye, tooltip "Owner/admin · asks you to confirm it's you") and copy. Pass when Reveal triggers step-up ("Confirm it's you") if stale, shows the password for 30 s, and is audited.
- [ ] **13-04** Row Agent (headset): `agent@demo.test`, Reveal and copy. Pass when it is real.
- [ ] **13-05** Row Customer (user): `customer@demo.test`, Reveal and copy. Pass when it is real.
- [ ] **13-06** Footnote: info icon, "Users from `boss.deploy.yaml` · one password per environment (DEMO_PASSWORD)", and a `Rotate` link (arrows-clockwise). Pass when the source is the real `users_from` (manifest, repository or none) and Rotate rotates DEMO_PASSWORD for that environment.
- [ ] **13-07** Only owner/admin can reveal. Pass when a member sees Reveal disabled with the reason.
- [ ] **13-08** Card `Deploy values` (violet lock-key), with "names only · values write-only" and `+ Add value`. Pass when Add opens the write-only entry.
- [ ] **13-09** Value `DATABASE_URL` ✓, `BOSS manages · staging`, tag `generated`. Pass when it is real.
- [ ] **13-10** Value `DEMO_PASSWORD` ✓, `BOSS manages · per env`, `generated`. Pass when it is real.
- [ ] **13-11** Value `OPENAI_API_KEY`, amber `!`, `you set · missing in prod`, amber `Set`. Pass when Set opens the write-only entry and the missing value is also in Needs you.
- [ ] **13-12** Value `SMTP_URL` ✓, `you set · rotated 12d ago`, with `Rotate` and `Remove` link chips. Pass when both work (Remove confirms).
- [ ] **13-13** No value is ever shown. Pass on inspection of the DOM and network.
- [ ] **13-14** Card `Targets & hosts` (teal hard-drives). Pass when it renders.
- [ ] **13-15** Target `Staging` ✓, `docker · port 8004`, link chip `203.0.113.20 ↗` (tooltip "Opens the host in Infra"). Pass when it opens the Infra host.
- [ ] **13-16** Target `Production`, amber `!`, `no host yet`, amber `Pick host`. Pass when it opens Infra's add-target (`add=target`).
- [ ] **13-17** Card `Captain & squad` (blue steering-wheel): Helm's face (m), **Helm**, `Engineering Captain`, and a `Change` button. Pass when Change offers the real candidates (owner/admin, step-up), and a retired agent is refused cleanly.
- [ ] **13-18** Squad faces (Juno, Forge, Vera, Beacon), `4 in squad`, and the `Edit squad` link. Pass when Edit squad edits the squad.
- [ ] **13-19** Card `Autonomy & budget` (amber sliders). Pass when it renders.
- [ ] **13-20** `Level` segment: `A0 Ask`, `A1 Propose` (selected), `A2 Act`, `A3 Ship`. Pass when changing it saves this project's level.
- [ ] **13-21** `Ambient budget · week`: a violet bar (28%) and mono `$14 / $50`. Pass when it is real and editable.
- [ ] **13-22** Footnote "Org caps still apply (Govern)". Pass when Govern shows "set per project" links.
- [ ] **13-23** Card `Repos` (GitHub) with `+ Add`. Pass when Add uses the Import-a-repo picker.
- [ ] **13-24** Repo row: git-branch, mono `paramg/helpdesk-ai-demo`, tag `main`, green `access ✓`. Pass when it is real.
- [ ] **13-25** Card `Notifications` (red bell). Pass when it renders.
- [ ] **13-26** Route `Needs you` (hand-waving) → blue `Email · Slack`. Pass when it is real and editable.
- [ ] **13-27** Route `Incidents` (siren) → `Slack`. Pass when it is real.
- [ ] **13-28** Route `PR ready` (merge) → `Email`. Pass when it is real.
- [ ] **13-29** Old-homes card (dashed, max 560 px): blue arrow-bend icon, `Old homes, after the move`, "e.g. App › Setup › Settings". Pass as a spec: each old home renders the bar from 13-30.
- [ ] **13-30** Old-home bar: gear, **Moved to Project › Settings**, primary `Open →`. Pass when every old home in the README parity table shows exactly this line and Open lands on the right section.
- [ ] **13-31** Parity: each setting row in the README table (demo logins, user names, deploy values, targets, captain, autonomy, budget, repos, notifications) works here AND its old home still works or redirects. Pass for each row.
- [ ] **13-32** Settings links only to console screens (`open`), never to raw API hrefs. Pass on inspection.
- [ ] **13-33** **(approved change)** There is no model or GitHub-account picker on Settings; models and accounts are global. Pass on inspection.
- [ ] **13-34** Design note 1: logins moved here with user names; reveal rules; read-only elsewhere. Pass when it holds.
- [ ] **13-35** Design note 2: this project's autonomy and budget; Govern keeps org caps. Pass when it holds.

**Responsive**
- 1440: 200 px section nav on the left, cards on the right.
- 1024: the same.
- 768: no horizontal scroll.
- 390: the section nav becomes a horizontal scrolling row at the top; cards stack; login rows put the user on a second line.

**States**
- Loading: skeleton.
- Empty: no demo users shows "No demo users found · add `demo_logins:` to boss.deploy.yaml" (with the hint).
- Error: a step-up failure shows "Confirm it's you" again, never a raw 401.
- Needs you: amber Set / Pick host.

---

## 14 — Infra › server (`14-infra-host.html`)

Rail: Infra active. Breadcrumb `Infra / Servers / 203.0.113.20`.

- [ ] **14-01** Page header: teal hard-drives tile, h1 = the host (`203.0.113.20`), sub `Docker host · Ubuntu 22.04 · shared by 3 projects`, and a green `Reachable` tag. Pass when they are real.
- [ ] **14-02** Tile `CPU`: teal ring `34%`, `8 vCPU`. Pass when it is real.
- [ ] **14-03** Tile `Memory`: blue ring `61%`, `16 GB`. Pass when it is real.
- [ ] **14-04** Tile `Disk`: amber ring `72%`, `200 GB`. Pass when it is real, and the ring turns amber or red with the threshold.
- [ ] **14-05** Tile `Ports used`: `7`, `8001–8104`. Pass when it is real.
- [ ] **14-06** Card `Projects on this server` (folders). Pass when it lists every project with a resident on the host.
- [ ] **14-07** Each row has a project mark (its gradient, `HD`, `HW`, `NP`), name, env and mono port, a health tag (`Healthy` green, `Degraded` amber), and Beacon's face (live when acting). Pass when they are real.
- [ ] **14-08** Each row has a `Project › Deployments` link chip (rocket). Pass when it opens that project's Deployments tab.
- [ ] **14-09** Card `Host plugins` (blue plugs): Docker 27, node-exporter, Caddy TLS. Pass when they are real.
- [ ] **14-10** Card `Recent changes` (violet clock): `HelpDesk staging deploy`, `2h ago · Beacon`. Pass when it is real.
- [ ] **14-11** Every existing Infra host capability is kept (`host-drawer.tsx`, fleet, plugins). Pass under the §13 parity rule.
- [ ] **14-12** Design note: shared stays in Infra, linked both ways. Pass when 08-12 → here → back works.

**Responsive**
- 1440: 4 tiles and a list.
- 1024: the same.
- 768: no horizontal scroll.
- 390: tiles 2×2; rows reflow.

**States**
- Loading: skeleton.
- Empty: "No projects on this server yet".
- Error: unreachable shows a red `Unreachable` tag with the reason.
- Needs you: a degraded resident shows amber.

---

## 15 — Observability › apps (`15-observability-app.html`)

Rail: Observability active. Breadcrumb `Observability / Apps`.

- [ ] **15-01** Page header: green pulse tile, h1 `Observability`, sub "Every app and BOSS itself — shared alert sources, contact points, Grafana". Pass when it renders.
- [ ] **15-02** Filters: `Apps 3` (selected), `BOSS platform`, `Alert sources`, `Contact points`. Pass when each view works as before.
- [ ] **15-03** App row: mark, name, env, and mono `uptime · p95` (`99.9% · p95 180ms`). Pass when they are real.
- [ ] **15-04** App row sparkline (140 px): green when healthy, amber when degraded. Pass when the series is real.
- [ ] **15-05** App row firing tag: `0 firing` green, `1 firing` amber. Pass when it is real.
- [ ] **15-06** App row `Project › Health` link chip (heartbeat). Pass when it opens that project's Health tab.
- [ ] **15-07** The app detail (Alerts with every rule field and action, and Overview) stays unchanged (§13). Pass under the parity rule.
- [ ] **15-08** Design note: Observability keeps the cross-app view and everything shared, and links both ways. Pass when 12-23 → here → back works.

**Responsive**
- 1440 / 1024 / 768: rows in full.
- 390: rows reflow; the sparkline may hide.

**States**
- Loading: skeleton.
- Empty: "No apps deployed yet".
- Error: Retry.
- Needs you: firing rows amber or red.

---

## Cross-screen

### Rail (every screen)
- [ ] **X-01** The brand block is the `B` logo tile and `BOSS`. Pass when it shows on every screen.
- [ ] **X-02** Groups are Platform, Run and Settings, with separators and the items of 01-16. Pass when it holds.
- [ ] **X-03** The current area is marked `aria-current` and highlighted. Pass when it holds.
- [ ] **X-04** Under Projects, each project row has a state dot. The open project is current. Pass when it holds.
- [ ] **X-05** `+ Add project` closes the sub-list. Pass when it holds.
- [ ] **X-06** The Approvals badge shows the real waiting count. Pass when it holds.
- [ ] **X-07** The NEW badge shows on Projects when Projects is not the current area. Pass when it holds.
- [ ] **X-08** The foot shows the user's face and "Name · Role". Pass when it holds.
- [ ] **X-09** Collapse and phone behaviour follow 01-10 to 01-15. Pass when it holds.

### Top bar (every screen)
- [ ] **X-10** ☰ burger (phone only) opens the rail slide-over. Pass when it holds.
- [ ] **X-11** The breadcrumb is a path with `/` separators. Earlier parts are muted links, and the last part is the current page. On a phone, only the last part shows. Pass when every crumb navigates.
- [ ] **X-12** `Jump to… ⌘K` (magnifier, tooltip "Jump to any project, run, PR, deploy or agent") opens the palette. It is hidden on a phone, where a search icon remains. Pass when it holds.
- [ ] **X-13** The `Live` indicator shows the console's live connection. It is hidden on a phone. Pass when it turns off or grey when the stream drops.
- [ ] **X-14** The bell (tooltip "Approvals waiting on you") has an amber count. Pass when the count is real and the bell opens Approvals or the needs-you list. Onboarding "only you" items appear here too.
- [ ] **X-15** The avatar shows the user's face and opens the account menu. Pass when it holds.

### Project header (every project tab: 04, 05, 05b, 06, 07, 08, 09, 11, 12, 13)
- [ ] **X-30** The header is sticky, so it stays while the tab scrolls. Pass when it holds.
- [ ] **X-31** The mark tile shows the initials (`HD`), with the name as the tooltip. Pass when it holds.
- [ ] **X-32** The h1 is the project name. Pass when it holds.
- [ ] **X-33** The repo chip (GitHub icon, tooltip "Repository — opens Repo & knowledge") opens the Repo & knowledge tab. Pass when it holds.
- [ ] **X-34** The live chip (globe, `staging · 203.0.113.20:8004 ↗`, tooltip "Staging — opens the live app") opens the live URL in a new tab. Pass when it holds.
- [ ] **X-35** The health chip (dot plus word, e.g. `Healthy`; tooltip "Healthy · 99.9% · p95 180ms — opens Health") opens the Health tab. Its colour follows the health enum. Pass when it holds on every tab.
- [ ] **X-36** The stage tag (icon plus word, e.g. green `Working` with a hammer; tooltip "Project stage on the journey") follows the real stage. Pass when it holds.
- [ ] **X-37** The captain chip shows the face, **Helm**, `Captain`, and an `Ask` button (chat icon). The tooltip is "Helm captains this project. Click to talk to Helm." Pass when the chip or Ask opens screen 09.
- [ ] **X-38** `New work` (plus, primary, tooltip "A feature, a bug or a ticket — Helm plans it") opens the new-work entry. It is the single primary in the header. Pass when it holds.
- [ ] **X-39** The Needs-you bar is always visible. It has an amber `!`, a bold `N need you`, the items as amber chips (`Merge PR #3` with merge icon, `Pick prod host` with cloud-check icon), and `≈ 2 min` at the right (hidden on a phone). Pass when each chip opens its drawer and the count is real.
- [ ] **X-40** The tabs are, in order: Overview (house), Work (kanban, count `9`), Roadmap (map-trifold, count `5`), Repo & knowledge (git-branch), Deployments (rocket-launch), Health (heartbeat), Settings (gear). Pass when the counts are real, the selected tab has `aria-selected`, and the URL `tab=` matches.
- [ ] **X-41** On a phone, the tabs scroll sideways, the mark and spacer are hidden, and the title takes the full width. Pass at 390.
- [ ] **X-42** With no Needs-you items, the bar shows a calm "Nothing needs you" or collapses. It never vanishes inconsistently between tabs. Pass when it holds.

### Drawers
- [ ] **X-50** Every drawer is deep-linked with `&drawer=<kind>:<id>`. The kinds are `run:`, `build:`, `env:`, `rec:`, `stage:`, `onboarding`, `section:`, `dossier`, `doc:` and `captain`. Pass when each reopens from its URL.
- [ ] **X-51** Esc, ✕, the scrim and browser Back each close the drawer and keep the tab and scroll. Pass when it holds.
- [ ] **X-52** A drawer slides from the right in 240 ms (the rail's curve). Pass when it holds.
- [ ] **X-53** A full page (Workflow run, Code Tasks build) is a chip inside the drawer, never an automatic navigation. Pass when it holds.

### ⌘K
- [ ] **X-60** ⌘K works from every screen, and it is scoped to the project on project pages. Pass when it holds.
- [ ] **X-61** Results carry their kind and open a drawer (↵) or external (⌘↵). Pass when it holds.
- [ ] **X-62** "Ask Helm…" and "New work" are reachable from ⌘K. Pass when it holds.

### Rules that apply everywhere
- [ ] **X-70** **Parity (§13).** No feature is killed. Every capability that moved or is duplicated works in both places:
  - Observability alerts and Overview;
  - Infra deploys;
  - Code Tasks build pages;
  - Workflow runs;
  - Setup demo logins;
  - Approvals.

  Pass when an independent tester ticks the parity list for each.
- [ ] **X-71** **Get started parity.** Every row of the README "Get started today" table has its new home. Pass when it holds. **(approved change)** Model and account rows go to Repo and models.
- [ ] **X-72** **Models and GitHub are global (§15).** **(approved change)** No project page offers a model or account picker. Their status is read-only, with links to Setup › Models, Setup › GitHub and Setup › Advanced. Pass when inspection finds no picker on any project screen.
- [ ] **X-73** **No raw ids or enums.** No raw `project_id`, `wi_…`, `dec_…`, `tgt_…`, UUID or enum (`needs_you`, `engineering_captain`, `not_started`) is visible. Pass when every label is in words.
- [ ] **X-74** **No truncated sentences.** Ellipsis is used only for names and paths in rows, and the full text is in the tooltip or drawer. Sentences, notes and answers are never cut. Pass when it holds.
- [ ] **X-75** **Local times.** Every time is in the viewer's zone (`whenLocal` / `sinceLocal`). Words such as "2h ago" show as sent. Pass when it holds.
- [ ] **X-76** **Labels are at most 4 words** on diagram nodes. Pass when it holds.
- [ ] **X-77** **State is badge plus colour plus word.** Pass when it holds.
- [ ] **X-78** **One primary button per view.** Pass when it holds.
- [ ] **X-79** **Every tile, row and chip is a link.** Internal targets open a drawer; external targets open with ↗ in a new tab. Pass when no dead ends exist.
- [ ] **X-80** **Real faces.** Avatars are the kit's real faces, with the `working` state for live agents. Pass when it holds.
- [ ] **X-81** **Icons.** Each section has an icon tile in its own colour (Phosphor). Pass when it holds.
- [ ] **X-82** **Motion.**
  - Live nodes pulse (`aa-ring`, 1.8 s).
  - A done edge turns green in 200 ms.
  - A new row slides in over 150 ms.
  - A drawer slides in over 240 ms.
  - A new Needs-you item glows once (`aa-glow`).

  Pass when it holds, and `prefers-reduced-motion` removes all movement while the meaning stays.
- [ ] **X-83** **Themes.** Dark is the default. Light is approved in `project.css`; amber fills use white text in light. Pass in each theme the console ships.
- [ ] **X-84** **Sample IPs only in docs.** Wireframes and fixtures use 203.0.113.x. Real data shows the real host. Pass when it holds.
- [ ] **X-85** **Ambient off.** Onboarding and the captain work with `BOSS_AMBIENT_COS=off`. Pass when it holds.
- [ ] **X-86** **Permissions.** Reads use the project scope. Reveal, captain change and secret writes need owner/admin plus step-up. Pass when a member is refused cleanly, with no raw error.
- [ ] **X-87** **Responsive.**
  - No horizontal page scroll at 1440, 1024, 768 or 390.
  - The phone has a 16 px gutter.
  - On a phone, design pins and notes are hidden.
  - On a phone, drawers and sheets are full width.

  Pass when it holds.

---

## Validation tally

| Screen | Items | Pass | Fail | Notes |
|---|---|---|---|---|
| 00a First run: empty | 34 | | | |
| 00b First run: guided | 31 | | | |
| 01 Left nav | 19 | | | |
| 02 Projects | 23 | | | |
| 03 Add a project | 27 | | | |
| 04 Overview | 45 | | | |
| 05 Work | 33 | | | |
| 05b Work drawer | 18 | | | |
| 06 Repo & knowledge | 24 | | | |
| 07 Roadmap | 22 | | | |
| 08 Deployments | 29 | | | |
| 09 Ask the captain | 17 | | | |
| 10 Onboarding | 32 | | | |
| 11 Jump to (⌘K) | 15 | | | |
| 12 Health | 27 | | | |
| 13 Settings | 35 | | | |
| 14 Infra › server | 12 | | | |
| 15 Observability › apps | 8 | | | |
| Cross-screen | 53 | | | |
| **Total** | **504** | | | |

Each screen also has 4 responsive widths and 4 states to check, listed under that screen.
