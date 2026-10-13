# Test tiers

BOSS has about 20,000 tests. Running all of them took more than six hours (each API test spends
about 6 s resetting its database) and did not catch the problems people actually hit: slow
builds, latency, stability and screens. So tests are tiered, and a release runs the tiers that
matter for it.

| Tier | What | Target time | When it runs |
|---|---|---|---|
| **P0** | What a user hits first and must never break: install and first run, sign-in, step-up/CSRF, the authz matrix, secrets never leaving, the sandbox and network fence, the build pipeline happy path, governed deploy and health, the workflow runner core, the API smoke | ≤ 10 min (measured 8 min) | Every release |
| **P1** | The main features of each area (Workflows, Approvals, Infra/Deploys, Activity log, Factory, agents, templates, previews) and a regression test for every bug an operator reported | ≤ 30 min (estimated 5–7 min) | Major releases |
| **P2** | Everything else: source-text contract checks, copy budgets, rare edge cases | — | On demand, or nightly |

**Release gates**

- **Major release:** `scripts/test-tier.sh major` (P0 then P1).
- **Minor release (rc10.x):** `scripts/test-tier.sh changed --since <last release>`. This is P0 plus every test that touches a file changed since that ref.
- **P2:** `scripts/test-tier.sh P2` when a change works in its area, or overnight.
- **Before a release:** `scripts/test-tier.sh major` + `scripts/e2e-gate.sh plumbing` (tier A) +
  `scripts/e2e-gate.sh agent` (tier B, below).

New effort goes into checks a person would notice: performance and latency budgets, build time,
and browser walks of the main screens. It does not go into more unit tests.

## Running

```bash
scripts/test-tier.sh P0                      # ~8 min on a 12-core laptop
scripts/test-tier.sh major                   # P0 + P1
scripts/test-tier.sh changed --since v1.0-rc10
scripts/test-tier.sh P2
```

How it runs:

- **Engine tests** run against `boss_test`.
- **API tests** run sharded across `boss_test_t1..tN` on the `boss-community-db-1` container. N is half the cores, at most 6. The databases are created when missing.
- **Console tests** run in parallel with the API shards.
- **Output:** each suite's wall-clock time, and the logs in a temporary directory. Set `TEST_TIER_OUT` to keep them elsewhere.

**Only new failures fail the gate.** `tests/known-failures.txt` lists failures that also happen on
`origin/main`. They are reported but not counted. Delete a line when its test is fixed; never add a line to make a gate pass.

## Adding a test to a tier

Add its path to `tests/tiers.yaml` under `P0` or `P1` (`engine`, `api` or `web`). Anything not
listed is P2.

- **P0** stays small. Before adding a test there, ask: would a user hit this in their first ten minutes, or would it lose data or leak a secret? If neither, it belongs in P1.
- **Every bug an operator reports** gets its regression test in P1.

## The end-to-end gate, tier B: one real build (`scripts/e2e-gate.sh agent`)

Tier A proves the plumbing with scripted models. Tier B proves that BOSS still builds: ONE real,
model-driven build of a known-good brief, from start to an opened PR, on the laptop's install,
driven through BOSS's API as a person with no clicks.

```bash
scripts/e2e-gate.sh agent                    # the plain brief, budget 25 min (target ≤ 20)
scripts/e2e-gate.sh agent --with-connector   # the same brief through the login-oauth connector, 30 min
scripts/e2e-gate.sh agent --brief <name>     # another brief in tests/e2e/briefs/
```

- **The brief.** `tests/e2e/briefs/onboarding-path-builder.json`: the smallest of the reference
  builds that passed on shared-server in September (Onboarding Path Builder, 13.7 min). Instructions
  and acceptance criteria only; no repository, owner or token. It starts on the direct code-task
  path (`POST /v1/code-tasks`), on the full-stack web scaffold that reference build had
  (`BOSS_E2E_BASE_BRANCH`, default the pinned `boss/code/crun_4b60c6ef0d7848f393fa`).
  `onboarding-path-builder-login-oauth.json` applies the login-oauth connector as a new app
  (`POST /v1/templates/{id}/apply`, the console's Apply sheet), which starts the fast_fix
  workflow; the gate follows its build step.
- **Who starts it.** The test owner, signed in with email, password and the authenticator code
  from the 0600 file `seed-test-accounts` writes (`BOSS_E2E_ACCOUNTS_FILE`, default
  `~/.boss/e2e/seed-accounts.json`). Localhost only; nothing it prints carries a credential.
  It answers the consent sheet as the console does (a `task` grant naming the build's GitHub
  repository) and steps up on `401 step_up_required`. Repository `paramg/boss-sandbox`
  (`BOSS_E2E_REPO`), project `boss-dogfood` (`BOSS_E2E_PROJECT`).
- **Before it starts.** Refuses when another build is running (the timing would lie), when
  less than 20 GB is free, or when a container it reads is down.
- **While it waits** (every 15 s) it fails at once, with the reason, when the build is blocked,
  failed or cancelled; when a decision for this build is pending that it does not answer (the
  merge question a finished build leaves is expected); after 8 min with no agent turn; or over
  the budget.
- **At the end** it requires: completed with a PR; no advisory-failing gates
  (`code_task.gate_advisory`); the app started (`code_task.app_serve_checked` ok); no
  `code_task.install_failed` left unfixed; `code_task.test_budget` not over; and
  `code_task.build_profile` recorded. It prints the stage table (`build_profile`) and one
  `PASS`/`FAIL` line with the minutes.
- **Afterwards.** A PASS rejects the build's merge question, closes the PR and deletes its
  branch (`--keep` keeps them). A FAIL keeps the run, the branch and the PR, and says where
  they are. A FAIL on a real BOSS bug is the gate working: fix BOSS, not the gate.
