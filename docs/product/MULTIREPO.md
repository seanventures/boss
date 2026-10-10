# Multi-repo apps: spec for approval

Status: design only, no product code. Screens: `index.html` (9 mockups + PNGs). Sample: org Acme,
project Acme Shop, app Storefront = contracts (`acme/contracts`) → backend (`acme/api`) → frontend
(`acme/web`), deploy config `acme/infra` (read only).

## User stories
1. As an owner I register every repository I want BOSS to use once, from the list GitHub gives me, and
   see per repository whether BOSS can really read, write and open PRs (verify, don't trust).
2. As a project lead I choose which of those repositories my project uses, or keep "one repo".
3. As an app owner I map my app's components to repositories (and a folder, for monorepos) and their
   dependency order. New project suggests the mapping; "One repo" stays the simple default.
4. As a requester I ask for one change; BOSS plans it across components, builds each repository in its
   own sandbox in order, and opens linked PRs that are reviewed as one set.
5. As an approver I make ONE decision for the whole change: all PRs merge and deploy, or none do.
6. As an operator I deploy every component together in order, with one rollback that undoes all.
7. As security I know a run's agents can touch exactly its app's repositories, enforced by GitHub.

## Screens (one surface per job)
| # | Screen | Where | Job |
|---|--------|-------|-----|
| 1 | Repo and models › Repositories | Settings | Registry: all repos the App installation reaches; tick = allowed; Read/Write/PRs checks; filters All/Allowed/Needs a fix/Not allowed. Untick removes from every project (running work keeps its grant until it ends). Archived repos cannot be ticked. |
| 2 | Project › Repositories | Factory › Projects › project | Pick from allowed repos, give each a role hint (Contracts/Backend/Frontend/Deploy config). One repo / Several repos switch. Remove is blocked while an app uses it. |
| 3 | New project › Where the code lives | Workflows composer (replaces "Lives in") | One repo (default, today's behaviour) or Several repos: component → repository rows with suggestions and build order. Shown only when the workflow needs a repository (existing readiness rule). |
| 4 | App › Components | Factory › Apps › app | Components left-to-right in dependency order: repo, path, live version, ships to, needs. Deploy config row. Recent changes as one row per change set. |
| 5 | Multi-repo build | Factory cockpit (run) | Stage strip Plan → Build → Review set → You approve → Deploy together; one lane per repo (Sandbox, Code, Tests, Review, PR); waiting lanes say what they wait for. |
| 6 | Linked PRs | Run › Pull requests | The set side by side: per-PR CI, gates, adversarial review comments; Cross-repo check below; set status blocks "Ask for approval". |
| 7 | One approval | Approvals | One card for the change set: PRs, review, cross-repo, cost, versions, rollback summary. Approve and ship all / Send back / Reject. No per-PR approval. |
| 8 | Deploy plan | Infra › Deploys | Ordered waves per component (old → new, steps, canary, flag), per-wave rollback, automatic rollback rules, Roll back all. |
| 9 | Repo consent | Sheet before first build of an app | Exact repos and permissions for this run, the repos it cannot reach, which agents use it, token lifetime. "Remember for <app>". |

## Data model
- `github_repos` (registry, per GitHub connection): `id, connection_id, github_repo_id, full_name,
  default_branch, language, archived, allowed bool, checks jsonb {read,write,pull_requests,at}`.
  Rows come from the installation's repository list; `allowed` is the operator's tick.
- `project_repos`: `project_id, repo_id, role_hint, base_branch` (PK project_id+repo_id). Replaces the
  project's single "default/primary repository"; migration seeds one row from today's binding.
- `apps.repo_mode`: `single | multi` (single = today; one implicit component `main`).
- `app_components`: `id, app_id, name, repo_id → project_repos, path default '/', access write|read,
  kind code|deploy_config, depends_on uuid[], ship_target jsonb`. Order = topological sort of
  `depends_on`; cycles refused on save.
- `change_sets`: `id, run_id, app_id, state planned|building|in_review|approved|merging|merged|
  deploying|live|rolled_back|failed, plan jsonb`.
- `change_set_prs`: `change_set_id, component_id, repo_id, branch, pr_number, head_sha,
  ci_state, review_state, merged_sha`.
- `cross_repo_checks`: `change_set_id, head_shas jsonb, results jsonb, at` (re-run on any push).
- `run_grants`: `run_id, repo_ids[], permissions jsonb, consented_by, remember bool, expires_at`.
- Approvals and deploys reference `change_set_id` instead of one PR; single-repo runs get a one-PR set,
  so one code path serves both.

## API outline (v1, all behind identity permissions)
- `GET /v1/github/repos?connection=` · `POST /v1/github/repos/refresh` · `PATCH /v1/github/repos/:id {allowed}` → runs checks, returns status.
- `GET|PUT|DELETE /v1/projects/:id/repos[/:repo_id]` · `PATCH /v1/projects/:id {repo_mode}`.
- `GET|PUT /v1/apps/:id/components` (whole list, validated: allowed + in project + acyclic).
- `POST /v1/projects/suggest-components {repo_ids}` → mapping + reasons (name, language, manifests).
- `GET /v1/runs/:id/change-set` · `GET /v1/change-sets/:id/prs` · `POST /v1/change-sets/:id/cross-check`.
- `POST /v1/runs/:id/grant {repo_ids, remember}` (consent) · decisions keep `POST /v1/decisions/:id`.
- `GET /v1/change-sets/:id/deploy-plan` · `POST .../deploy` · `POST .../rollback`.
- MCP: `list_repositories`, `register_repository` gain `allowed`; new `describe_app_components`.

## How the pipeline changes
- **Plan**: the planner receives the component map and writes a per-component plan + interface
  changes. Components with no change are skipped (their PR is not opened); the set may be 1..N.
- **Build**: one sandbox per touched component, run in topological waves. A lane starts when its
  dependencies pass tests; it consumes the dependency's build at that branch (pre-release version,
  e.g. `1.4.0-r1482`, or a path/branch pin). Independent components build in parallel.
- **Gates**: existing gates run per PR. New set gate `cross_repo` (contract vs implementation vs
  callers, version pins, migration/deploy order, every PR links the others). A set is green only when
  every PR and the set gate are green.
- **PRs**: same branch name in every repo (`boss/r<run>-<slug>`); each description lists the set;
  adversarial review per PR; fixes push to the PR in question and re-trigger the set check.
- **Approval**: one decision per change set. Approve covers the exact head SHAs reviewed; any later
  push invalidates the approval and asks again.
- **Merge**: in dependency order, each with a required-SHA merge. If any merge fails, BOSS reverts the
  merges that landed (revert PRs auto-merged under the same approval) and marks the set failed.
- **Deploy**: waves in dependency order; per-component health checks; automatic rollback of every
  deployed component in reverse order on failure. One release record, one Roll back all.

## Security
- Grant scope = the app's components: GitHub App installation token minted with `repository_ids`
  of exactly those repos and per-repo permissions (`contents:write` only for `access=write`, else
  `read`; `pull_requests:write` only for write repos). GitHub enforces it, not prompts.
- Tokens: 1 h, minted per step, revoked when the run ends; never stored in sandboxes beyond the step.
- Each sandbox receives only its own repo's credential; the cross-repo reviewer gets read-only.
- Branch protection respected; BOSS never pushes to a base branch; branch names constrained to `boss/*`.
- A repo unticked in the registry or removed from the project is excluded from the next token mint.
- Consent is recorded (who, repos, permissions) and shown in Identity › Audit.

## Edge cases
- **One repo fails** (build, gate, review, cross-check): the whole set is blocked; nothing asks for
  approval; other lanes pause after their current step.
- **Partial merge prevented**: no merge before set approval; merges re-check every head SHA first;
  failure mid-way reverts what landed. Human merges of a single set PR on GitHub are detected and
  the set is flagged "merged outside BOSS" for the approver.
- Repo loses App access mid-run → lane fails with "Fix in GitHub"; set blocked; recheck resumes.
- Component touched by no change → no PR, shown as "unchanged" in the set.
- Monorepo: two components in one repo with different paths → one sandbox, one PR, both components.
- Cycles in `depends_on` refused; a deploy-config repo with read access cannot be a build target.
- Approval stale after a push; deploy target missing for a component → deploy plan refuses to start.
- Switching a project from Several → One repo is allowed only when every app has ≤1 component.

## Phased build plan
1. **Registry and project repos**: `github_repos`, `project_repos`, screens 1–2, migration from the
   single binding, checks per repo. Single-repo runs unchanged.
2. **Components**: `app_components`, `repo_mode`, screens 3–4, suggestions, scoped token minting
   (grant = app repos) + consent sheet (9). Still one PR per run.
3. **Change sets**: planner per component, lanes in waves (5), linked PRs + cross-repo gate (6).
   Single-repo runs become one-PR sets.
4. **One approval and set merge**: decision on change set (7), SHA-pinned merge, revert on failure.
5. **Deploy together**: release with waves, auto rollback, Roll back all (8).
Each phase: independent agent walks every touched screen in a browser; targeted tests per phase,
full suites once at the end.

## As built: where the code differs from this spec

The approved design above is unchanged. These names and tables were adapted to the codebase:

| Spec | As built | Why |
|---|---|---|
| `github_repos` per connection | `github_repos` per **org** (`org_id`, `source` app/token) | GitHub connections are per project; the App installation is the org's (`identity_github_app`). |
| `project_repos` table | a project's active `github_repository_bindings` + `role_hint`, `registry_repo_id` | Every build already resolves its repository through the bindings. Nothing needed migrating: today's binding is already the project's one repository. |
| `apps.repo_mode` | `repo_layouts(project_id, app_key, repo_mode)`; app_key `''` is the project's switch | An app has no table; it is the runs filed under one `app_key`. |
| `app_components.repo_id → project_repos`, `depends_on uuid[]` | `binding_id`, `depends_on text[]` of component **names** | A whole-list PUT can name dependencies it creates in the same call. |
| `change_sets` | `repo_change_sets` | `changesets` is the resource plane's deploy change set. |
| `PATCH /v1/projects/:id {repo_mode}` | `PUT /v1/projects/{id}/repo-mode` | The project's own PATCH stays its own. |
| `POST /v1/runs/:id/grant` | the identity consent flow: a 409 `consent_required` carrying `multirepo`, then a grant | Consent already existed for builds; the sheet shows the multi-repo layout when the body carries it. The set's grant is recorded in `repo_run_grants`. |
| Branch `boss/r<run>-<slug>` in every repo | each lane's own build branch (`boss/code/<build>`); every PR description lists the set | Each lane is an ordinary build with its own run token and push pattern. |
| MCP `describe_app_components` | not added | Components are read through `GET /v1/apps/{key}/components`. |

How it runs:
- engine: `engine/boss_engine/multirepo/` (registry, project_repos, components, grants, change_sets,
  cross_check, merge, release, github);
- API: `apps/api/app/routes_multirepo.py`;
- the tick: `POST /v1/delivery-workflows/run-capabilities` moves sets and releases;
- the merge: the worker job `change_set.merge`.

Single-repo apps are unchanged. A single-repo build's PR is also recorded as a one-PR change, so an app's Recent changes reads one table. Its approval and merge are the ones it always had.
