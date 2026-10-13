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

Before a release: **test-tier major + e2e-gate plumbing + e2e-gate agent.**

- **Major release:** `scripts/test-tier.sh major` (P0 then P1), then the end-to-end gate below.
- **Minor release (rc10.x):** `scripts/test-tier.sh changed --since <last release>`. This is P0 plus every test that touches a file changed since that ref. Then the end-to-end gate below.
- **Every release:** `scripts/e2e-gate.sh plumbing` (tier A, about 1–2 min) and `scripts/e2e-gate.sh agent` (tier B, a real build with a model). Both are listed under `P0: release:` in `tests/tiers.yaml`.
- **P2:** `scripts/test-tier.sh P2` when a change works in its area, or overnight.

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

## The end-to-end gate, tier A: `scripts/e2e-gate.sh plumbing`

Unit and API tests passed while real HelpDesk builds broke on 2026-10-13, each time on the plumbing
between "the agent finished" and "the app is deployed". Tier A runs that plumbing for real on a
known-good app, with no model calls, in about a minute and a half (target: 5 minutes or less).

```bash
scripts/e2e-gate.sh plumbing                         # this checkout
scripts/e2e-gate.sh plumbing --boss-root ../old      # an older checkout, to prove a check bites
scripts/e2e-gate.sh plumbing --keep                  # keep the logs (gate.log, approval.log, ...)
```

**What it runs on.** `tests/e2e/fixtures/helpdesk-mini` is a trimmed copy of what the agent built
for HelpDesk AI: Node 22 + TypeScript run by `tsx`, eslint 9, Express 5 + Postgres, the login-oauth
connector applied as a build applies it, a `SESSION_SECRET` the app refuses to start without,
`boss.deploy.yaml`, `boss.preview.command`, and a few P0/P1 vitest tests. Its `package-lock.json`
pins what is installed. Every failure on it is BOSS's, never the app's.

**Where it runs.** A throwaway container of the installed worker image (it has `boss-sandbox-exec`
and the run uid pool), next to a throwaway Postgres on a private Docker network. All three are
removed at the end. Every app command runs as a build's own uid under umask 007, through BOSS's
sandbox runner. The app's database is a per-run database from `build_services`. The install's own
containers are never restarted, upgraded or written to. The BOSS under test is mounted read-only,
and every check calls the functions the pipeline calls, never a copy of them.

| Check | What it runs | Catches |
|---|---|---|
| clone through a GitHub 502 | `EphemeralSandbox.provision` from a local git remote whose first answer is a 502 | a GitHub blip failing a whole build (crun_2f76585e) |
| hardening keeps the app's eslint 9 | `verification_target.prepare_web_verify_env` | eslint-8 plugins added to an eslint-9 app (crun_6eaa478d) |
| BOSS's clean install | `NPM_INSTALL_CMD` through `install_rounds` and the sandbox | ERESOLVE from BOSS's own changes |
| verify | build, typecheck, lint (pass 1: no browser tests), then the app's own `npm test` against its real database | a verification target that breaks a known-good app |
| the app's programs stay runnable | `workspace_perms.normalize` and `preview_service._proof_tree` | `.bin` targets losing execute for the run's uid (crun_247435e1) |
| the app starts | `prove_app_serves`, called with the same arguments as `_check_app_serves`, plus the connector's route probes | `tsx: Permission denied`; an app-start proof without BOSS's generated secrets (SESSION_SECRET) |
| connector gate | `templates.gate.evaluate` for the build (wiring smoke, secret scan) and as its own suite (`npm ci` and JUnit count) | a connector gate that cannot pass on a good app |
| a blocked build names the gate | `templates.gate.hold_outcome` on the two HelpDesk failures and on this run's own gates | "the template's tests did not pass" when the install or the app start failed |
| pull request through a 502 | `GitHubAdapter` (`ensure_branch`, `upsert_file`, `create_pull_request`) against a local GitHub whose `POST /pulls` answers 502 once | GitHub API blips not retried in place |
| deploy to a local target | `load_manifest` and `render`, `preflight_host`, `GitHubSource` (the PR's tarball), and `StackApplier` with the container as the host, up to the health check | a deploy that cannot install or start the app, or that logs a generated secret |
| retries | the retry suites on a full-schema database: delivery-only retry, BOSS's retries under the person's grant, a retry of nothing being a fresh build | without this database, every test tier skips these suites |
| approval that needs step-up | `tests/e2e/gate/approval_step_up.py`: a real sign-in with password and TOTP; a stale second factor; Approve returns 401 `step_up_required`; step-up; the same approval sent again; the workflow step resumes | the API half of "an approval isn't re-sent after step-up" |

**The result.** One PASS/FAIL table with each check's seconds. The command exits non-zero when any
check fails or could not run. When the pipeline starts passing something new to the app-start
proof (for example generated secrets), update the gate in the same change:
`tests/e2e/selftest/test_the_plumbing_gate_is_a_release_step.py` (in `scripts/e2e.sh selftest`)
fails until you do.

## Adding a test to a tier

Add its path to `tests/tiers.yaml` under `P0` or `P1` (`engine`, `api` or `web`). Anything not
listed is P2.

- **P0** stays small. Before adding a test there, ask: would a user hit this in their first ten minutes, or would it lose data or leak a secret? If neither, it belongs in P1.
- **Every bug an operator reports** gets its regression test in P1.
