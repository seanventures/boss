# Add a project v2: one screen (wireframes, for approval)

This merges the old **Add a project** sheet (`start-project/start-sheet.tsx`, `import-repo.tsx`, `new-project-sheet.tsx`)
and **Workflows › New build** (`workflows/new-project-composer.tsx`) into one screen, so New build can be deprecated.
These are wireframes only. No product code changes.

Open `a-add-project.html` and walk the screens with → / ← (Esc returns to the wireframe index).
The walk is a (idea · fix · repo) → b → c → d (typing · chat · ready · refused) → e.
Each page also takes `?theme=light` and `?notes=0`.

| Screen | What it shows |
|---|---|
| `a-add-project.html?mode=idea` | **v3:** the input is a multi-line box (5 lines, grows to 12, ⤢ full screen) and Helm rewrites it into the **Project brief** (title · problem · who uses it · 3–6 must-haves · out of scope · done when · open questions with quick answers), with Looks right / Fix it / Rewrite with a hint. Helm's additions are dotted-underlined. Below it, as approved: one input takes a sentence takes a sentence, a repo or a folder and detects which. Below it, a six-box diagram of what BOSS will do (captain → stack → connectors → workflow → code → staging) and the step strip with approvals in amber. One primary: **Create and start**. |
| `a-add-project.html?mode=fix` | **v3:** Fix it pressed: every line of the brief is editable in place (must-haves reorder, add, delete), and a rewrite hint is typed. |
| `a-add-project.html?mode=repo` | **v3:** for a repo the card summarises what Helm found (what it is, main parts, health) and what onboarding will do; lines typed under the repo become a note to Helm. As approved: the same screen while you type a repo. Inline search lists GitHub repos and host folders together, and Helm reads the highlighted repo live. |
| `b-adjust.html` | The popover behind each box: Connectors, Workflow, Stack, Who to tell, More detail, Captain & crew, Where the code lives, Staging. Below them, the parity table (38 of 38 kept). |
| `d-new-work.html?state=typing` | **New:** the New work modal (replaces the one-line composer on Project › Work). Kind tabs, multi-line box, Helm reading the words and the code graph, defaults already picked. |
| `d-new-work.html?state=chat` | Helm's understanding (title · what changes · area from the code graph · done when · risk) and **Talk it through**: Helm asks one question with quick replies. |
| `d-new-work.html?state=ready` | Ready to start: brief confirmed and folded, the chat's answers applied (+ criterion, priority, who to tell), defaults, plan preview, **Start** or **Add to Next**. |
| `d-new-work.html?state=refused` | A readiness refusal inside the modal: the same checklist, and Start anyway locked until each missing item is ticked. |
| `e-new-work-parity.html` | Work tab before/after, defaults per kind, the Workflow chip's popover, click counts, and the 20-row parity table. |
| `c-after-create.html` | Where Create and start lands: the new project in Planning, with the PRD as the first gate. It also shows **Add a feature** moving to Project › New work, and the click comparison. |

## Click counts (from the Projects page; typing not counted; approve-PRD / merge-PR gates identical in both)

| Path | Old | New |
|---|---|---|
| New app from an idea | 3 on the sheet (Add · New tab · Plan it), with no stack, connectors or workflow choice. **9** to also pick connectors: blank project (4) → Workflows → New build → 2 connectors → Start | **2** (Add · Create and start). Connectors, stack and workflow are picked for you; each change costs +2. |
| Existing repo | 4 (Add · Existing tab · repo row · Start onboarding) | **2** (paste a URL or press ↵ on the top match) · 3 if you click a result |
| A sample | 3 (Add · Try a sample · Start the sample) | **2** (Add · Try a sample ▶, which starts at once) |

## New work: click counts (typing not counted; merge-PR gate identical)

| Path | Old (one-line composer) | New (modal) |
|---|---|---|
| From the header | 2 (New work, which jumps to Work and focuses the box · Start) | **2** (New work · Start), with the brief and defaults shown first |
| From the Work tab | 1 (type · Start) | **2** (bar · Start); **0** by keyboard (type in the bar, ⌘↵) |
| Another workflow | 4 (New work · change · pick · Start) | **4** (New work · Workflow chip · pick · Start) |
| Answer Helm's question | not possible | 3 (New work · quick reply · Start), optional |
| Plan it for later | not possible here (Roadmap only) | 2 (New work · Add to Next) |

## New work: parity with the one-line composer (`apps/web/app/projects/tab-work.tsx › Composer`), 20 of 20 kept

| Capability | Was | Now |
|---|---|---|
| Kind: Feature · Bug · Ticket | Segmented control | Kind tabs (Feature · Bug · Ticket or link); a link is read on any tab |
| Per-kind example text | Placeholder | Placeholder, plus Helm's suggestion chips |
| Describe the work | One line (2000 chars) | Multi-line box (same limit), grows, full screen, attach |
| Ticket = paste a GitHub / Plane issue | Ticket tab, URL keyboard | Ticket or link tab; Helm reads the issue into the brief |
| Link to an issue | Only by pasting a ticket | Issue chip: link or create, on any kind |
| Helm's plan strip | Strip under the box | Plan preview + the Workflow chip's step strip |
| ≈ time (history median or typical) | Tag with basis tooltip | Estimate chip ("from 7 runs here"), same tooltip |
| ≈ cost, or "cost after its first run" | Tag | Estimate chip and footer tag |
| Workflow name + why | Text with tooltip | Workflow chip, why on the line |
| "change" workflow · fits-a-kind · back to default | Menu | Workflow chip popover, same list and default link |
| Readiness refusal (409) checklist | Panel under the composer | Same panel inside the modal |
| Consent: Start anyway with ticks | Start → Start anyway, locked until ticked | Same |
| Older engine fallback (project start door) | Behind Start | Unchanged |
| Header New work (`compose=1`) | Jumps to Work, focuses the box | Opens the modal on any tab |
| Ask-Helm answer "New work · Edit" prefill | Text + kind prefilled, never sent | Modal opens prefilled, never sent |
| Permission gate `workflow_run:start` | Gated Start | Gated Start and Add to Next |
| Busy / result message, sample mode | "Starting…", "Started — Helm is planning it" | Same, as a toast; the modal closes |
| New row slides in | Yes | Yes |
| Add-a-feature sizes (Quick · Small · Spec first · Major) | Old New build | Workflow chip options |
| What changes (screen · API · data · access · + connector) | Old New build chips | "What changes" and Area in the brief; connector under More |

New in the modal: Helm's understanding (confirm or edit), the short chat (at most 4 turns), priority, who to tell, target, create an issue, Add to Next, and a draft kept on Esc.

## Add a project: parity (where every capability lives now)

| Capability | Was | Now |
|---|---|---|
| New from an idea / Existing repo tabs | Sheet tabs | Detected from the one input. Click the kind pill to force another. |
| Sentence / brief, full screen | Sheet; New build "What should it do?" | The one input, with ⤢ for full screen |
| Start from an idea (examples), Undo | New build | Ideas chip in the input |
| Build from its reference ↗ | New build | Connectors popover footer |
| Name | Both | Name chip, prefilled from the sentence or repo |
| Stack + stack warning | New build | Stack box (AUTO) and its popover, with the warning |
| Connectors (up to 6, stack-filtered, "in app"); several at once | New build | Connectors box with suggestions; the popover allows several |
| Who it's for · Done when · Read first · Talks to | New build › More detail | More detail popover |
| Who to tell (`notify_channel`) | New build (workflow input) | Tell chip and its popover |
| A workflow's own start form | New build | Workflow popover |
| Workflow name · steps · approvals; step list with owners | How it's built | Workflow box, step strip, and Workflow popover › Steps |
| Change workflow (paths + all installed) | How it's built | Workflow popover |
| Team: edit, add agent, put it back; captain; crew | How it's built; sheet col 3 | Captain box and the Captain & crew popover |
| Where the code lives; one or several repos; mapping; repository; result | How it's built | Code box and its popover |
| Readiness dots, checklist, Start anyway | Both | Readiness dots. A refusal expands the same panel inline and the primary becomes Start anyway. |
| Cost history, deploy note | How it's built footer | Cost tag; "Nothing reaches prod without you" |
| Plan it with an agent (PRD first) | New build footer | Footer link |
| Plan it → / Start the project / Start onboarding | 3 different buttons | One button: **Create and start** (⌘↵) |
| Cancel · ✕ · Esc · ⌘↵ | Both | ✕ · Esc · ⌘↵ |
| Try a sample | Sheet (2 presses) | Header chip that starts in 1 press |
| More samples & presets (banking KG) | Sheet › choose | Footer › More ways › Presets |
| Blank project, KG project (type, description) | Sheet › form | Footer › More ways |
| GitHub / Folder on host; account chip, + account | Sheet col 1 | Detected from the input, with one result list. The account is read-only, and + account opens Repo and models (global). |
| Search repos, ages, in use; folder host + path | Sheet col 1 | Inline results; a typed `/path`, with the host on the result row |
| Helm reads it (access, stack, build & test, map code) | Sheet col 2 | The summary fills live (FOUND tags, live tag, file count) |
| Map business rules (KG) | Sheet col 3 | Chip toggle (repo mode) |
| Estimate footnote | Sheet col 3 | Cost tag |
| Staging target | Later, in onboarding | Staging box (suggested) and its popover. This is new. |
| Add a feature mode (app picker, what should change, kinds, Quick/Small/Spec/Major, proposal) | New build | Project › New work (already exists) |
| Doors into New build (Command Center, Factory, Workflows) | Workflows › New build | New app → Add a project; add a feature → Project › New work. The old tab shows "Moved" for one release. |

The full 38-row table is on `b-adjust.html`.

## Rules kept
- Helm's understanding is the same pattern in both places (Add a project and New work): your words stay, Helm's rewrite sits below them with its additions marked, and the one primary (Create and start / Start) confirms the brief as shown. Looks right is optional, so the click counts above do not grow.
- One action per screen, with **Create and start** as the only primary. Labels are 4 words or fewer, and detail sits in popovers.
- Models and GitHub accounts stay global and are never picked here. They are shown read-only and link to Repo and models.
- Suggestions (stack, connectors, workflow, staging) are the captain's reasoning from the sentence or repo, never a keyword list. Each shows the words that triggered it.
