# API gateway onboarding

**What this is:** how BOSS takes a partner's API gateway configuration from "pushed" to "live in
production", explained for people (part 1), then the low-level design for build agents (part 2),
then an 8–10 minute demo storyline (part 3).
**Status:** part of the chain is on `main`; the rest is being built on branch
`feat/config-onboarding-full-chain`. Every step below says which.
**Ticket:** BOSSAPP-1128 (epic BOSSAPP-1127). Demo data: BOSSAPP-1134,
[`examples/api-gateway-onboarding/`](../../examples/api-gateway-onboarding/).
**Names:** all fictional. **Northgate** is the gateway team, **Lumen Pay** is the partner being
onboarded.

---

## Part 1: explained

### The problem

A gateway team publishes many partners' APIs. Each partner sends a configuration file: routes,
auth, CORS, rate limits, upstreams. Today a person reads each file against a long rules document,
writes back what is wrong, waits for a new file, deploys it to staging by hand, pokes it, and asks
someone to approve production. It takes days per partner, and the rules are applied unevenly.

### Before and after

| Step | Before | With BOSS |
|---|---|---|
| A file arrives | Someone notices an email or a PR | BOSS sees the push or pull request that changes `config/tenants/*.yaml` |
| Check it | A person reads it against the rules | The repository's own checker runs; each finding names its rule, line, reason and fix |
| Wrong file | A person writes back | BOSS comments with the reasons and opens a fix PR that edits the partner's own file |
| What a machine may not decide | Mixed in with everything else | Held for a person, by rule (for example: a plaintext secret, an admin path) |
| Staging | Manual deploy | The merged file is deployed to staging under a governed change set |
| Does it work | Manual pokes | The repository's contract tests run against staging |
| Something breaks | Manual revert | BOSS restores the previous version |
| Production | An approval email | The same bytes are promoted after a person approves |

### The flow

```
 partner pushes / opens PR
          │
          ▼
 ┌────────────────┐   findings    ┌─────────────────────────────┐
 │ 1. Validate    │──────────────▶│ 2. Reject with reasons      │
 │ repo's checker │               │    + fix PR on their file   │
 └───────┬────────┘               └──────────────┬──────────────┘
         │ clean                                  │ partner / reviewer merges
         ▼                                        ▼
 ┌────────────────┐   ┌────────────────┐   fail   ┌──────────────┐
 │ 3. Staging     │──▶│ 4. Contract    │─────────▶│ 5. Rollback  │
 │ governed deploy│   │    tests       │          │ previous ver.│
 └────────────────┘   └───────┬────────┘          └──────────────┘
                              │ pass
                              ▼
                     ┌────────────────────┐
                     │ 6. Promotion       │  a person approves;
                     │ same bytes to prod │  same sha256 as staging
                     └────────────────────┘
```

### What BOSS does at each step

1. **Validate.** BOSS runs the checker the repository declares in `.boss/validators.yaml` on the
   changed file. The rules belong to the gateway team, not to BOSS. Each finding has a priority
   (P0–P3) and a fix policy from `.boss/rulesets/<name>.yaml`: `mechanical`, `agent`, `human` or
   `never`.
2. **Reject or fix.** BOSS posts the findings to the partner's PR and opens a fix PR that edits
   their file with the smallest change. Findings the rules reserve for a person stay open and are
   named in the report.
3. **Staging.** Once merged, the file is deployed to staging. The deploy is a change set: planned,
   checked, applied atomically, reloaded and health-checked.
4. **Contract tests.** BOSS runs the cases in `.boss/contract_tests.yaml` against staging: does a
   good token get 200, does no token get 401, does the admin path stay closed.
5. **Rollback.** If the reload or health check fails, the previous version is restored. If the
   contract tests fail, staging goes back to the previous version (not yet: see the table below).
6. **Promotion.** The exact bytes that passed staging go to production, only after a person
   approves.

### What is on main today

| Step | On `main`? | Where |
|---|---|---|
| Intake: a push or PR touching `config/tenants/*.yaml` | Yes, the filter | `engine/boss_engine/connectors/intake/github/adapter.py` (`changed_files`), glob in `services/intake_filters.py` |
| Intake starts the onboarding workflow | **Not yet** | started by hand today (*Onboard a config file*) |
| 1. Validate with the repo's checker | Yes | `services/validators.py`, `services/rulesets.py`, `services/input_fidelity.py` (`run_validation`, `repo_checks`) |
| 2a. Fix the file in place, original committed first | Yes | build step "Fix the file", `services/start_from_file.py`; test `engine/tests/test_onboard_a_config_file.py` |
| 2b. Comment the reasons on the partner's PR | Capability yes, **not in the workflow** | `github_comment` (`services/workflow_capabilities.py`, `services/github_actions.py::post_comment`) |
| 2c. Fix PR that edits the partner's own file | Capability yes, **not in the workflow** | `fix_pr` (`services/github_actions.py::open_fix_pr`); test `engine/tests/test_github_tools.py` |
| Open P0/P1 blocks the merge | Yes | `checks_must_pass` on the workflow (`services/workflow_catalogue.py`, `onboard_config_file`) |
| 3. Staging deploy as a change set | Yes | `services/config_deploy.py::deploy_files`; test `engine/tests/test_config_is_deployed_through_a_governed_changeset.py` |
| 4. Contract tests from the repo | Yes | `services/contract_tests.py` (`REPO_FILE`, `from_repo`); test `engine/tests/test_contract_test.py` |
| 5a. Rollback when reload or health fails | Yes | `services/config_deploy.py::apply_config` (`restore`, status `rolled_back`) |
| 5b. Rollback when contract tests fail | **Not yet** | no step; `services/contract_remedy.py` only drops a case that can never pass |
| 6. Promote the same bytes, person approves | Function yes, **not in the workflow** | `services/config_deploy.py::promote` (checks sha256; production needs a person) |
| Security vs guideline as separate classes | **Not yet** | only the P-level; the demo checker labels each finding with a `category` |

---

## Part 2: low-level design

### 2.1 Pieces

| Piece | Module | Contract |
|---|---|---|
| Intake | `connectors/intake/github/adapter.py` | Push: `changed_files` from the commits. PR: fetched lazily in `enrich`. Filter `{"field": "data.changed_files", "op": "glob", "value": "config/tenants/*.yaml"}`. |
| Start from a file | `services/start_from_file.py` | `start_from`, `start_from_content`, `target_path` → a build `remediate` contract. Secrets masked (`masked`, `mask_run_input`). |
| Validators | `services/validators.py` | `.boss/validators.yaml`: `id`, `applies_to` globs, `run` with `{file}`, optional `fix`, `output.adapter`, `rules_doc`, `ruleset`. Read from the base branch. |
| Adapters | `services/validator_adapters/` | `gwlint-json`, `sarif`, `eslint-json`, `ruff-json`; none named → generic JSON reader. |
| Ruleset | `services/rulesets.py` | `.boss/rulesets/<name>.yaml`: per rule `severity` P0–P3, `enabled`, `autofix`, `guide`; `custom` agent-checked rules. A change cannot edit the ruleset it is judged by (`unapplied`). |
| Security guard | `validators.security_changes` | An unexplained change to auth, jwks, issuer, upstream or tls keys, or a URL moved to another host, becomes a P0 finding. |
| Record | `validators.record` | `boss.validation/v1` with `boss.finding/v1` items (rule, severity, basis, location, original/fixed, evidence, override). |
| Comment | capability `github_comment` | `number`, `body`, optional `marker` (a marked comment is updated in place). |
| Fix PR | capability `fix_pr`, tool `github_open_fix_pr` | Branch `boss/<run>-fix-<slug>`; writes only changed files; risk tier 2 (a person releases it). |
| Staging | capability `config_deploy`, tool `deploy_config` | `source_file`, `environment` → `deployment_id`, `deployed_url`, `bundle_sha256`. Failed or advisory checks force a person (`staging_needs_a_person`). |
| Apply | `config_deploy.apply_config` | Keeps 10 versions in `.boss-versions`, atomic write, reload, `wait_healthy`; `restore` on failure. |
| Contract tests | capability `contract_test` | No `cases` → the repo's `.boss/contract_tests.yaml`; case fields `name, method, path, headers, json|body, expect_status, expect_json, expect_json_exists, expect_body_contains`; a case may not carry its own `url`. |
| Promotion | capability `promote_config` | `deployment_id` bound to this run's staging step; same `bundle_sha256`; production → `person_required=True` and the production quality bar. |

### 2.2 The target workflow

The workflow on `main` (`onboard_config_file`) is:
`build ("Fix the file") → review → merge → config_deploy ("Staging") → contract_test`.

The target chain adds, in order (being built on `feat/config-onboarding-full-chain`; **none of
these is on `main` yet**):

1. **Intake route.** A push or PR matching the glob starts the workflow with `target_path` set to
   the changed file and `start_from` set to the partner's branch.
2. **Report on the PR.** After validation, `github_comment` with a marker posts the findings table
   (rule, line, why, fix, who decides). Re-runs update the same comment.
3. **Fix PR.** `fix_pr` against the partner's branch, editing `target_path` only. Findings marked
   `human`/`never` are listed as open in the PR body.
4. **Rollback on contract failure.** A failed contract test restores the previous staging version
   through the same `restore` path, and the run reports `rolled_back` with the failing cases.
5. **Promotion.** `promote_config` bound to the staging deployment, waiting on a person's approval.

### 2.3 Rules for the build

- No gateway words in BOSS: the workflow, prompts and capabilities stay generic
  (`test_no_gateway_specific_prompt` in `engine/tests/test_onboard_a_config_file.py`). The rules,
  the checker and the contract tests live in the partner-facing repository.
- A secret in the partner's file is masked in everything BOSS stores and commits.
- Production is always a person's answer.
- The same bytes: promotion checks the staging bundle's sha256.

### 2.4 Acceptance checklist

- [ ] A push of `lumen-pay.bad.yaml` (demo) starts the run with no person.
- [ ] The PR comment lists all 8 findings with rule, line and reason.
- [ ] The fix PR edits only `config/tenants/lumen-pay.yaml`; `never`/`human` findings stay open.
- [ ] After the partner's corrected push, validation is clean and merge proceeds.
- [ ] Staging deploy succeeds; the three contract cases pass.
- [ ] A forced contract failure restores the previous staging version.
- [ ] Production waits for a person, then serves the same sha256.

---

## Part 3: demo storyline (8–10 minutes)

Run from a fresh state: `examples/api-gateway-onboarding/reset.sh --github <owner>/<repo> --yes`.

| # | Chapter | Time | On screen |
|---|---|---|---|
| 1 | The problem | 0:00–1:00 | A gateway team, many partners, a rules document; days per partner. |
| 2 | The partner's file | 1:00–2:00 | Lumen Pay's PR in GitHub. The file looks plausible. |
| 3 | BOSS sees it | 2:00–3:00 | The run starts from the PR. The checker runs. 8 findings: 4 security, 4 guideline. |
| 4 | Reasons, not just "no" | 3:00–4:30 | The PR comment: each rule, line, why and fix. Which ones BOSS may fix and which need a person. |
| 5 | The fix PR | 4:30–5:30 | The diff on the partner's own file: rate limits, size caps, https. The plaintext key and admin route are left for a person. |
| 6 | The partner corrects it | 5:30–6:15 | The corrected push (`lumen-pay.corrected.yaml`). Clean validation. Merge. |
| 7 | Staging and contract tests | 6:15–7:30 | Governed deploy; three contract cases pass. |
| 8 | When staging breaks | 7:30–8:15 | A forced failure; the previous version comes back. |
| 9 | Production | 8:15–9:15 | A person approves; the same bytes go live. |
| 10 | What it cost | 9:15–10:00 | Time, model spend and approvals for the whole run, from the run page. |

Chapters 4, 5, 8 and 9 need the target steps in 2.2. Record only after the whole chain runs on an
install end to end.
