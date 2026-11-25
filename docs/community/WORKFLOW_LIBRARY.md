# The Workflow Library: reuse, change and share workflows

The Library is one list of workflows you can reuse. Each item is labelled **BOSS** (shipped with
BOSS) or **Your team** (shared by someone in your org). You can search it, filter it by tag, add
an item to any project, edit your project's copy, and share your own workflows back.

Open it from **Workflows › Library › Shared library**.

This guide uses the **API gateway onboarding** template as the example. It takes a partner's
gateway config file through these steps:

```
Fix the file → Adversarial review → Merge → Staging → Contract tests
```

- **Fix the file** checks the file with the repository's own checkers and fixes it.
- **Merge** waits for the change's merge card.
- **Staging** ships the merged file.
- **Contract tests** runs the repository's contract tests against staging.

## 1. Add the template to a project

1. In **Shared library**, search `gateway`, or press the `gateway` tag.
2. Open **API gateway onboarding**. The card shows:
   - its steps;
   - **Input**: what Start asks for, such as *Start from a file* and *Where the fixed file goes*;
   - **Used**: how many projects have it;
   - **Full brief**: what the workflow is for.
3. Pick a project and press **Add to project**.

The workflow now sits in that project's own workflows, marked *from Library · v1.0*. Until you
change it, it takes each new Library version by itself.

> Want only your own copy to change? Press **Edit a copy** instead. It adds the workflow to this
> project (if it is not there yet) and opens the copy in the editor. Nothing you change there
> reaches other projects.

## 2. Change a step

In the editor, select a step to open its inspector.

- **Rename it:** for example, rename *Staging* to *Northgate staging*.
- **Change who does it:** in **Agent**, pick another agent. The agent must hold a skill the step
  can run. Save refuses a step whose agent cannot do the work, and says why.
- **Change what it is told:** in **Task**, edit the brief. For *Fix the file*, the brief is the
  instruction the fixing agent reads.
- **Add or remove a step:** use the palette, or **Ask BOSS** to draft the change.

Press **Check**, then **Save**. Your copy is now marked **Modified**.

## 3. Change an input

Open **Start form** (the workflow's settings, above the steps).

- Make a field **Required**, or optional.
- Change a field's wording.
- Add a field. For example, add `partner_ticket`, then wire it into a later step through that
  step's **Inputs**.

A step reads an input through its **Inputs** section, where you choose where each value comes
from: the Start form, an earlier step's output, or a fixed value.

## 4. Add a gate

There are two ways to make a person decide before something happens:

- **Needs approval** on a step. For example, turn it on for *Staging* so nothing reaches staging
  until a person approves the card.
- An **Approval gate** step from the palette, with its own question. Put it in front of the step
  it guards. Its card's subject can quote the run, such as `{input.target_path}` or
  `{steps.build.output.pull_request_url}`. A subject with a value missing is not shown
  half-filled.

Production always asks a person, whatever your copy says. A gate that you add never removes a
gate that BOSS requires.

## 5. Change the rulebook

The checkers and their rules live in the **repository**, not in the workflow, so a change to
them is reviewed like any other change:

- `.boss/validators.yaml` says which checker runs on which files, and how it fixes them:

  ```yaml
  validators:
    - id: gwlint
      applies_to: ["config/tenants/*.yaml"]
      run:   "go run ./cmd/gwlint validate {file} --json"
      fix:   "go run ./cmd/gwlint --fix {file}"
      output: {adapter: gwlint-json}
      ruleset: northgate
  ```

- `.boss/rulesets/<name>.yaml` sets each rule's level and who may fix it:

  ```yaml
  ruleset: northgate
  version: 4
  validator: gwlint
  rules:
    tenant-rate-limit-required: {severity: P1, autofix: agent,
       guide: "rps 50, burst 50 unless the brief names a quota."}
    no-sensitive-paths-exposed: {severity: P0, autofix: human}
  ```

  - `severity` is P0 to P3.
  - `autofix` is `mechanical` (the checker's own fix), `agent`, `human` (left for a person) or
    `never`.
  - `guide` is the text the fixing agent receives, word for word.
  - `enabled: false` turns a rule off. Its findings still show, marked *suppressed*.

Commit the change to the repository. The next run reads the ruleset at that commit, and its
validation record names the ruleset version it used.

## 6. Run it again

- **Start**: press **Start** on the workflow and fill in the Start form. For this template, give
  the partner's file (upload it, or name a repository path) and where the fixed file goes.
- **Test run**: try your changes without spending money. Pin the steps you do not want to run,
  such as the build. See [Workflow test runs](WORKFLOW_TEST_RUNS.md).
- **Run again from a step**: on a finished run, retry from the step you changed.

Each run uses the version it started on. Editing the workflow while a run is going does not
change that run.

## 7. When the Library version changes

- **A copy you never changed** takes the new version by itself.
- **A copy you changed** is never overwritten. The new version waits beside it, and the card says
  **Update vX**. Open **Review update** and choose one:
  - **Take it with my changes** (offered only when your changes apply cleanly);
  - **Take it only**;
  - **Not now**.

## 8. Share your own workflow

On one of your project's workflows, open **⋯ › Share to Library…**. Give it:

- a name;
- one line that says what it does;
- tags, such as `gateway, onboarding`;
- a version.

When you press **Share**, every project in your org can add it from the Library, labelled
**Your team**. To publish a new version, share it again.

- **Who can share:** sharing org-wide needs **Share workflows to the org Library**
  (`workflow:share`), an org-level permission that Owners and Admins hold. A project Operator can
  build and save workflows in their project but cannot share them org-wide.
- **Checked first:** BOSS runs the same checks a Save runs before it shares. A workflow is refused,
  with the reason for each step, when it cannot run here. For example, it is refused when a
  step's agent holds no skill that can run.
- **Unshare:** this takes the workflow out of the Library. Projects that already added it keep
  their copies.

## For developers: the API

All routes are under `/v1/delivery-workflows/marketplace` and work on every edition.

| Do | Call | Permission |
|---|---|---|
| List the Library | `GET ?project_id=` returns `items` (each with `tags`, `workflows[].steps`, `inputs`, `objective`, `installed_in`) and `projects` (where you may add) | `workflow:read` |
| Add to project | `POST /install {project_id, pack_id}` | `workflow:install` on that project |
| Review an update | `POST /update {project_id, definition_id, act: take \| take_keep \| dismiss}` | `workflow:install` |
| Remove | `POST /uninstall {project_id, pack_id}` | `workflow:install` |
| Share | `POST /publish {project_id, definition_id, name?, summary?, version?, tags?}` | `workflow:publish` on the project, plus `workflow:share` at the org |
| Unshare | `POST /unpublish {project_id, pack_id}` | the same as Share |

The engine side is `engine/boss_engine/services/workflow_marketplace.py`:

- BOSS's items are `SHIPPED_PACKS`.
- A **template** (`PackTemplate`) is a standard catalogue workflow shipped under its own key.
  API gateway onboarding is the `onboard_config_file` standard under the key
  `api_gateway_onboarding`. The standard is still installed in every project as before, and the
  template's steps are always the standard's, so a change to the standard reaches unmodified
  copies.
- `engine/tests/test_the_library_installs_and_runs_a_template.py` installs the template, edits
  it, runs it through the workflow runner to the merge, and checks the share-time checks.
