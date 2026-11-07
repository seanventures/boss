# Add a project v2: one screen (wireframes, for approval)

This merges the old **Add a project** sheet (`start-project/start-sheet.tsx`, `import-repo.tsx`, `new-project-sheet.tsx`)
and **Workflows › New build** (`workflows/new-project-composer.tsx`) into one screen, so New build can be deprecated.
These are wireframes only. No product code changes.

Open `a-add-project.html` and walk the screens with → / ← (Esc returns to the wireframe index).
Each page also takes `?theme=light` and `?notes=0`.

| Screen | What it shows |
|---|---|
| `a-add-project.html?mode=idea` | One input takes a sentence, a repo or a folder and detects which. Below it, a six-box diagram of what BOSS will do (captain → stack → connectors → workflow → code → staging) and the step strip with approvals in amber. One primary: **Create and start**. |
| `a-add-project.html?mode=repo` | The same screen while you type a repo. Inline search lists GitHub repos and host folders together, and Helm reads the highlighted repo live. |
| `b-adjust.html` | The popover behind each box: Connectors, Workflow, Stack, Who to tell, More detail, Captain & crew, Where the code lives, Staging. Below them, the parity table (38 of 38 kept). |
| `c-after-create.html` | Where Create and start lands: the new project in Planning, with the PRD as the first gate. It also shows **Add a feature** moving to Project › New work, and the click comparison. |

## Click counts (from the Projects page; typing not counted; approve-PRD / merge-PR gates identical in both)

| Path | Old | New |
|---|---|---|
| New app from an idea | 3 on the sheet (Add · New tab · Plan it), with no stack, connectors or workflow choice. **9** to also pick connectors: blank project (4) → Workflows → New build → 2 connectors → Start | **2** (Add · Create and start). Connectors, stack and workflow are picked for you; each change costs +2. |
| Existing repo | 4 (Add · Existing tab · repo row · Start onboarding) | **2** (paste a URL or press ↵ on the top match) · 3 if you click a result |
| A sample | 3 (Add · Try a sample · Start the sample) | **2** (Add · Try a sample ▶, which starts at once) |

## Parity: where every capability lives now

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
- One action per screen, with **Create and start** as the only primary. Labels are 4 words or fewer, and detail sits in popovers.
- Models and GitHub accounts stay global and are never picked here. They are shown read-only and link to Repo and models.
- Suggestions (stack, connectors, workflow, staging) are the captain's reasoning from the sentence or repo, never a keyword list. Each shows the words that triggered it.
