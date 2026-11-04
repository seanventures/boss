# BOSSAPP-810 — Start a project: parity (old → new)

Every control on today's New project path, and where it lives after this change. Nothing was removed;
API endpoints and response fields are unchanged (two read-only routes were added).

| Old control / behaviour | Where it lives now |
|---|---|
| Project menu › `+ New project` → name/description sheet | Project menu › `+ New project` → **New project** (three doors + presets). The name/description sheet is the **New app from an idea** door (one click), unchanged: same fields, same `createProjectAction` (POST /v1/projects), same step-up handling, plus a `← Back` button. |
| Project type "Build an app" | Idea form (type radio, default) — unchanged. Also every app preset and Import a repo (default). |
| Project type "Knowledge Graph for an existing codebase" | Idea form (type radio) — unchanged, still opens Knowledge Graph › Setup. Also: Import a repo › **Map business rules too** (creates a `knowledge_graph` project with the repo and opens its Setup), and the three **banking presets** (AML alert investigation, Regulatory change, Data quality RCA) which create a knowledge-graph project and open its Documents tab where each sample loads. |
| After creating an app project: open the project (area kept) | The **New app from an idea** door is one sentence box ("Describe it in a sentence", name derived and editable, **Plan it →**): it creates the project and starts its new-app workflow, then opens the project's page. The old name/description form (both types) is one press away: **Knowledge graph or blank project**; it opens the project's page (app) or Setup (knowledge graph). |
| Command Center "Describe your first task" box (Ask BOSS composer) | On project pages that show the "What should we add?" card, the empty-state headline is hidden and the composer is a closed **Ask BOSS** disclosure (still one click). Home project and knowledge-graph projects: unchanged. |
| Ask BOSS composer template gallery (`?template=<slug>`, 16 ideas) | Unchanged. The same 16 briefs (with `buildBrief`) are also the presets grid (`See all 19 →` = 16 + 3 banking). |
| Catalogue ideas (hello, catalog-cart, banking-starter) | Unchanged. New built-in `hello-world` idea added (the sample); it also shows wherever catalogue ideas show. |
| Workflows › New build composer, `compose=new|add`, DefaultWorkflowPicker | Unchanged. Screen 3's Start uses the same start door (POST /v1/delivery-workflows/start via `delivery-api.post`) with the project's mapping: `feature.minor` if mapped, else the catalogue's Minor Feature; `project.new` default when the project has no repo. |
| Repo registry (Repo and models › Repositories), Factory › Repositories | Unchanged. Import a repo reads the same registry (GET /v1/github/repos) and adds with the same call (PUT /v1/projects/{id}/repos/{repo}). "Repo not listed? Add a GitHub account" links to Setup › GitHub. |
| KG import + mapping (repo index) | Unchanged; Import a repo triggers it by binding the repo (the DB trigger marks it due). Progress shown on Import and the project page header. |
| Project menu: switch, archive, show archived, restore | Unchanged. |
| Existing projects | Unchanged. Non-home `app` projects show the new "What should we add?" card at the top of their Command Center; the home project and knowledge-graph projects do not. |
| Readiness refusal (Start anyway), consent sheet | Same components (`ReadinessPanel`, `withConsent` / `post`), reused in the sample and Start. |

## New

- `/?new_project=sample|import|idea` and `/?new_project=1` open the flow at that choice (read by the project menu; closing removes the param).
- `GET /v1/projects/{project_id}/start` (project:read) and `GET /v1/github/repos/{repo_id}/facts?project_id=` (repository:read), read-only.
- Built-in template `templates/apps/hello-world` (node, one page + one test; labels idea, sample).

## Tests changed (behaviour change listed above)

- `apps/web/tests/new-project-in-the-org.test.tsx`, `apps/web/tests/the-step-up-is-the-top-layer.test.tsx`: two added clicks (**New app from an idea**, then **Knowledge graph or blank project**) before the form (the menu's New project now opens the three doors first).
- `apps/web/tests/every-new-build-door-opens-the-composer.test.tsx`: `app/start-project/start-sheet.tsx` added to the files allowed to say "New project".
- `engine/tests/templates/test_connectors_and_ideas.py`: `hello-world` added to the expected built-ins (idea).
- `tests/identity/matrix.yaml`: the two new GET routes added.
