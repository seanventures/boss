# Workflow Library, packs and one home for each piece of work

**The core idea.** Every piece of work has one home, the Work panel, and its "who did what" flow (Spec, Build, Validate, Review & tests, Merge, Staging, Production) is how you move around it. Clicking a step opens that step's detail on the same page, so validation, the build log, review comments and the merge are tabs of one story, not four pages. Workflows are kept the same way: each one lives once in an org Library, and a project links to a pinned version instead of holding a copy.

Design for approval. Nothing here is built yet. Wireframe: `wireframe.html` in this folder (6 clickable screens).
Read against origin/main `4e1b03172` and the laptop DB, 2026-11-21.

## 1. What is wrong today

| What you see | The numbers (laptop DB) | Why |
|---|---|---|
| The same workflow, copied everywhere | **514** catalogue rows for **25** workflows across 29 projects. 15 of them are in all 29 projects. | `workflow_catalogue.seed()` writes a definition and a version into every project on every boot. |
| Copies that differ only by accident | **597** catalogue versions but only **103** distinct contents. | `_staffed_for()` bakes each project's roster into the spec, so the hash changes per project. |
| Banking work "lost" | DQ RCA and Remediation, DQ Incident Escalation, DQ Monitor Intake, AML Alert Investigation, Extract Rules from a Rulebook, Banking Regulatory Compliance exist **only in northpeak-lending** (2 also in boss-dogfood). | They are catalogue entries in code (`dq_catalogue.py`), but they install only where the `banking-dq` / `banking-aml` ontology pack is set. Nowhere in the console says they exist. |
| Custom work trapped in one project | API onboarding (13 versions, 14 runs) lives only in northgate-gateway. NorthPeak Regulatory Compliance (mine) (6 versions, 13 runs) only in northpeak-lending. HelpDesk Delivery (D1) only in boss-dogfood. | There is no org-level place to publish to. A project workflow can only be duplicated inside its own project. |
| Custom work is not reusable even if copied | API onboarding's "Validate" step is a **Build** step with a 1,600-character prompt that names `examples/lumen-pay.bad.yaml`. | There is no Validate step type. Validation lives in prose. |
| Same pattern beyond workflows | System workflows: 928 rows for 32 keys. MCP servers: 121 rows for 9 names (`service-desk` in all 29 projects). | Same "copy into every project" seeding. |

What already exists and we reuse: the specpack **pack** format (`packages/specpack/packs/*.yaml`, `installs: roles, workflows, policies, skills`), `pack_installs` and `pack_provenance` (install records and drift), the read-only catalogue rule plus `forked_from_version_id`, content-hashed immutable versions, `.boss/validators.yaml` + `validator_adapters/` + `boss.validation/v1`, and the Validation tab (`apps/web/app/validation/validation-tab.tsx`).

## 2. The design in one picture

```
                    ORG LIBRARY  (each workflow once: owner · version · tags · source)
   ┌──────────────┬───────────────────────────────┬──────────────────────────────┐
   │ Standard (6) │ Marketplace packs             │ Custom (published by you)    │
   │ BOSS ships   │ Banking data quality · API    │ API onboarding · HelpDesk    │
   │ read-only    │ gateway validation · Security │ Delivery · NorthPeak Reg…    │
   └──────┬───────┴───────────────┬───────────────┴──────────────┬───────────────┘
          │ Install (link, pinned v3)                Publish ▲   │ Customise (fork)
          ▼                                                   │  ▼
   PROJECT WORKFLOWS   [linked v3 ▸ v4 available]   [custom, published v2]   [forked from v3]
          │ run
          ▼  the run records the exact version it used (unchanged)
```

- **Library (org).** One row per workflow. Three sources: **Standard** (BOSS ships, read-only), **Marketplace** (comes in a pack, read-only), **Custom** (published from a project; owner can publish new versions).
- **Pack.** One installable bundle: workflows + skills + validators + rulesets + connectors (MCP templates) + ontology + agent roles + guides. It is the existing specpack pack file with four more lists (`validators`, `rulesets`, `connectors`, `guides`).
- **Install into a project = a link, not a copy.** The project holds a pointer: library workflow + pinned version. A new version in the Library shows **"v4 available"** on the project card; **Review & upgrade** shows the step diff and re-points mappings. Nothing upgrades a project silently except Standard patch releases (open question 1).
- **Publish to Library** (from a project workflow's menu). Agentic import flow: one button → an agent reads the workflow → "Here is what it needs" → pre-picked chips (name, tags, which skills / validator / connectors go with it, which hard-coded values become inputs, e.g. `examples/lumen-pay.bad.yaml` → input **Original file**) → **Publish v1**.
- **Customise = fork.** From a linked workflow or a Library item. Creates a Custom workflow with "forked from Standard Product Delivery v3", a diff against the source and **Rebase on v4** when the source moves.
- **Agents stay per project.** A Library version names soft default agents; the project resolves them at run time (`step_agents.resolve` already falls back to Auto). That removes the per-project hash drift.

Runtime is unchanged: a run still pins a `workflow_versions` row in its project. On install or upgrade the engine writes that row once from the Library version, same content hash, never shown as a separate workflow. Runs, mappings, schedules and webhooks keep pointing where they point today.

## 3. Screens (see wireframe)

1. **Library (org)**: tabs Standard · Marketplace · Custom, search, pack cards with icon, contents count, "in 3 projects". One action per card: **Open**.
2. **Pack detail**: what is inside as chips (4 workflows, 3 skills, 3 connectors, 1 ontology, 11 agents), what it needs, **Install to project** (pick project → one button).
3. **Project › Workflows**: Installed (linked, version badge, amber "v4 available" with **Review & upgrade**), Custom, "Unused · 14" folded.
4. **Publish to Library**: agent reads → confirm → chips → **Publish v1**.
5. **Workflow editor with a Validate step**: Validate sits in the palette under Built-in like Panel, Build, Deploy. Its config panel: Validator, Ruleset, What it checks, Who may fix.
6. **One home for one piece of work**: the Work panel with the flow as navigation. Validate selected shows the validation view; Build selected shows the build and its log. Same page, same URL with `?step=`.

## 4. Part B: one home for the work, and Validate as a step

**Your questions, answered plainly.** The Work panel is not a custom screen for one customer: every project has it, for every piece of work. Validation sitting inside the change page was not a deliberate choice; it is a symptom. Today one piece of work is told across six surfaces, and Validate would have made a seventh. It gets no new screen: it becomes one step in the Work panel's flow.

**One home.** The Work panel's flow becomes the navigation. Clicking a step opens its detail below the flow, on the same page (the Work panel URL plus `step=validate`). One primary action at a time sits in the header (for example **Remove route**, then **Merge**).

| Step | What opens below the flow |
|---|---|
| Spec | The brief, must-haves with verdicts, the panel's decisions |
| Build | What changed (files, +/-), the build's live log, attempts |
| **Validate** | Input → Findings → Fix → Re-check (the existing validation view) |
| Review & tests | Review comments P0/P1 first, tests, "Let BOSS fix these" |
| Merge | Verdict, P-level counts, overrides, the Merge button |
| Staging / Production | Where it runs, health, contract tests, deploy log |

**Today's surfaces, mapped into the home.** None is deleted at first; each becomes a deep link that opens the home with the right step selected.

| Surface today | Where it goes |
|---|---|
| Work panel (header, flow, asked / discussed / built, comments) | Becomes the home. "Built" moves into Build; comments move into Review & tests. |
| Change page tabs: Overview, Conversation, Files, Code, Commits, Validation | Files / Code / Commits → **Build**; Validation → **Validate**; Conversation → **Review & tests**; Overview → header. The full code browser stays one click away from Build. |
| Merge card (decision inbox) | Keeps only the verdict line and **Open** → Merge step. |
| Workflow run page (step panels) | A run of a piece of work links to its home; the run page stays for workflow authors and runs that are not work items (schedules, reports). |
| Build page (tool-call log, outputs, gates) | Its log is the Build step's detail. The build page stays as "Full log" for debugging. |
| Decision inbox | Every ask links to the step that asked (`?step=validate`). |

**Validate as a step type (editor).** Validate sits in the palette under Built-in, next to Panel, Build and Deploy. Its side panel:
- **Validator**: from the repo's `.boss/validators.yaml` or an installed pack (e.g. `gwlint`).
- **Ruleset**: e.g. `northgate v3`; severities and guides come from it, never from a prompt.
- **What it checks**: **One given file** (the run's input, written to an output path) or **Everything the change touched** (files of the previous Build step matching `applies_to`).
- **Who may fix**: per P0 / P1 / P2: Tool · Agent · Person · Nobody (defaults from the ruleset's `autofix`). Merge blocks on open P0 and P1.

**Engine.** New capability `validate`. Its runner is `input_fidelity.run_validation` (one path, already merged) and its output is `boss.validation/v1`. Agent fixes use the existing remediation contract. A finding that needs a person parks the step as **Waiting · 1 needs you**, and that shows in the flow.

**Wording, customer-neutral.** "Partner file" → **Input file**; "Revert all to partner's original" → **Revert to original**; `partner_line` → `input_line` (the old key is read as an alias). There are 28 occurrences in `validation-tab.tsx`.

**Many files.** The Input column becomes a file list with a count per file ("3 files · 2 with findings"). Selecting a file shows the same columns.

**New tools without code.** A `json-fields` adapter maps any JSON report by paths in YAML:
`output: {adapter: json-fields, findings: "$.violations[*]", rule: rule, line: line, severity: level, message: message, fixable: fixable}`. SARIF, ESLint, ruff and gwlint keep their adapters.

## 5. Migration: collapse copies into links, lose nothing

1. **Add** (no change to existing rows): `library_workflows` (org, key, name, source, owner, tags, pack), `library_versions` (spec, content hash, changelog), and on `workflow_definitions` three nullable columns: `library_workflow_id`, `library_version_id`, `link` (`linked` | `forked` | `local`).
2. **Backfill Standard + Marketplace**: one Library row per catalogue key (25). Versions are the distinct contents with the roster removed (≈ one or two per key). Each of the 514 project rows becomes `linked` to the version matching its active spec.
3. **Backfill Custom**: API onboarding, NorthPeak Regulatory Compliance (mine) and HelpDesk Delivery (D1) become Custom Library items, owner = project owner; their project rows become the published source (`link = local`, published).
4. **Banking becomes visible**: the six banking entries move into the Banking data quality and Banking compliance packs. Every project can now see them in the Library. northpeak-lending is recorded as having both packs installed.
5. **Stop copying**: `seed()` installs links for the 6 Standard workflows in new projects only. Existing projects keep every link they have. The project list folds links that are unused (no run, no mapping, no schedule) under "Unused · n".

**Delete nothing.** No row in `workflow_definitions`, `workflow_versions`, `workflow_runs`, `workflow_mappings`, `workflow_schedules` or `workflow_webhook_triggers` is deleted or re-pointed by the migration. A migration test snapshots, per project, the count of runs, the version id each enabled mapping resolves to, and each schedule's workflow key, and must find them identical afterwards. Rollback = ignore the new tables and columns; old code reads the old rows.

## 6. Packs that ship first

**Standard (6):** Easy Mode MVP · Standard Product Delivery · Major Feature · Minor Feature · Fast Fix / Hotfix · Incident Response.

| Marketplace pack | Workflows | Also installs |
|---|---|---|
| **Banking data quality** (from northpeak-lending) | DQ Monitor Intake · DQ RCA and Remediation · DQ Incident Escalation · Extract Rules from a Rulebook | skills `dq_incident_rca`, `rulebook_extraction`, `findings_triage`; connectors `dq-monitor`, `profile-service`, `service-desk`; ontology `banking-dq`; 10 DQ agents + Rulebook Analyst |
| **API gateway validation** (from Northgate) | API onboarding (input: Original file) | validator `gwlint` (gwlint-json), ruleset `northgate v3` (from `.boss/rulesets/`), a guide per rule, contract-test template, Validate step |
| Banking compliance | AML Alert Investigation · Banking Regulatory Compliance | ontology `banking-aml`, `banking-regulatory`; AML + compliance agents |
| Delivery extras | Research-Led Discovery · Incremental Build · Backend Service Setup · Frontend / UX Certification · Third-Party Integration · Data / Migration Change · Deprecation / Sunset | — |
| Security & compliance | Security Review · Security Hardening · Compliance-Ready Release | security reviewer |
| Code knowledge | Scan for Business Rules · Trace Regulation to Code · KG Investigation Pipeline | KG |

## 7. Build plan (small PRs)

| PR | What | Done when |
|---|---|---|
| 1 | Library tables + backfill migration + `GET /v1/library` | Migration test: per-project runs, mapping targets and schedules identical before/after; 25 Library rows; 0 rows deleted. |
| 2 | Library screen (org) + pack detail, read-only | Playwright: Standard shows 6, search "bank" finds 2 packs, a pack lists its contents. 1440 + 400 px. |
| 3 | Install as link + project list badges + Review & upgrade | Installing Banking data quality into a new project runs DQ Monitor Intake with no copy row visible; publishing v4 shows "v4 available"; upgrade moves only the operator-owned mapping after confirm. |
| 4 | Publish to Library + Customise (fork) + Rebase | API onboarding publishes as Custom v1 with `examples/lumen-pay.bad.yaml` turned into the input Original file; a fork shows its diff to the source. |
| 5 | Pack schema (`validators`, `rulesets`, `connectors`, `guides`) + the first 2 Marketplace packs; `seed()` stops copying | Fresh install: a new project has 6 workflows, not 25; Banking data quality installs its 3 connectors as one MCP row each in that project only. |
| 6 | `validate` step type: palette, config panel, runner on `run_validation`, `json-fields` adapter | API onboarding rebuilt with a Validate step produces `boss.validation/v1` with 9 findings on the sample input; a ruff report maps via `json-fields` with no code. |
| 7 | One home: the Work panel flow becomes step tabs (`?step=`); Validate, Build, Review, Merge details move in; neutral wording; multi-file list | Clicking each flow node swaps the detail with no navigation; `/changes/<p>/<n>?tab=validation`, the merge card and the build link land on the home with the right step selected (Playwright, 1440 + 400); no "partner" string in the web app. |
| 8 | Retire the duplicates: change-page tabs, the run page for work items and the merge card become deep links | Every old URL redirects to the home with the right step; no screen shows the same validation, log or comments twice. |

Each PR runs its own tests; the full suite runs once before merge to main.

## 8. Open questions (each with a recommendation)

1. **Do Standard workflows auto-upgrade?** Recommend: yes for wording-only patches, prompt when steps change. Marketplace and Custom always prompt.
2. **The 19 non-Standard links every existing project now has.** Recommend: keep them linked, fold the unused ones under "Unused · n" with Remove. Delete nothing.
3. **Library scope.** Recommend: org-only for now; "Marketplace" means packs shipped in the BOSS repo. A public cross-org marketplace comes later.
4. **Who may publish to the Library?** Recommend: anyone with `workflow:publish` on the project; the owner is shown; an org admin can unlist. No approval queue.
5. **Banking: one pack or two?** Recommend two: Banking data quality now (includes Extract Rules from a Rulebook, which DQ needs), Banking compliance next.
6. **Same fix for system workflows (928 rows) and MCP servers (121 rows)?** Recommend yes, with the same link mechanism, after PR 3, and no new screen.
7. **16 test-generated "build"/"check" workflows in 10 throwaway projects.** Recommend: leave them (Custom, local, never published); a separate cleanup.
8. **Validate with nothing to merge** (validation failed before a change opened). Recommend: the home still shows the Validate step, since it is keyed by the work, not by the change; Merge stays grey.
9. **Does the change page go away?** Recommend: not at first. It becomes a redirect into the home (PR 8), and the code browser stays reachable from Build as "Full code view".
