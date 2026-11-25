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

Before a release: **test-tier major + e2e-gate plumbing + e2e-gate agent + a green enterprise lane.**

**A release needs a green enterprise lane** (`scripts/enterprise-lane.sh`, [below](#the-enterprise-lane-scriptsenterprise-lanesh)).
A red lane step blocks the release until it is fixed or ticketed as a real product bug with an
owner; it is never waived to ship.

- **Major release:** `scripts/test-tier.sh major` (P0 then P1), then the end-to-end gate below.
- **Minor release (rc10.x):** `scripts/test-tier.sh changed --since <last release>`. This is P0 plus every test that touches a file changed since that ref. Then the end-to-end gate below.
- **Every release:** `scripts/e2e-gate.sh plumbing` (tier A, about 1–2 min), `scripts/e2e-gate.sh engines` (tier A, no model: the real build pipeline end to end on the scripted conformance engine, as the poorest and the richest engine, across a clean build and four injected faults; every engine must show the same stages, gates, fix rounds, PR, events, profile fields and failure words) and `scripts/e2e-gate.sh agent` (tier B, a real build with a model). All are listed under `P0: release:` in `tests/tiers.yaml`.
- **Every release:** `scripts/enterprise-lane.sh` (on a fresh 4 vCPU / 8 GB Linux VM; on a Mac, `scripts/enterprise-lane/lima.sh`) ends in `PASS`. Attach its report to the release notes' checklist.
- **Weekly (macOS Colima):** Run a fresh Colima install on a real Mac per [COLIMA.md](../community/COLIMA.md#weekly-verification-checklist) and confirm `boss status`, `boss doctor`, and a sample build/preview pass.
- **P2:** `scripts/test-tier.sh P2` when a change works in its area, or overnight.
- **Before a release:** `scripts/test-tier.sh major` + `scripts/e2e-gate.sh plumbing` and
  `scripts/e2e-gate.sh engines` (tier A) +
  `scripts/e2e-gate.sh agent` (tier B, below) +
  a green `scripts/enterprise-lane.sh` (below).

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
- **Worker tests** (`apps/worker/tests`: the lease loops, job slots, the stall clock) run after the engine suite, with `apps/worker` on the path. No gate ran them before R3 (BOSSAPP-1431).
- **Console tests** run in parallel with the API shards.
- **Output:** each suite's wall-clock time, and the logs in a temporary directory. Set `TEST_TIER_OUT` to keep them elsewhere.

**Red is not normal (BOSSAPP-1429).** Each suite has one known-failures baseline,
`tests/known-failures/<suite>.txt` (engine, api, worker, scripts, web). Every line names a test (or a
whole test file), its ticket, an owner, the date it was listed and why it fails:

```
engine/tests/test_x.py::test_y | BOSSAPP-123 | engine | 2026-11-23 | the sqlite stub has no host column
```

`scripts/known_failures.py` judges every run (`scripts/test-tier.sh`, `scripts/web-unit-ci.py`, CI and
the local merge gate). A run fails on:

- a failure no line names (a **new** failure);
- a listed test that ran and passed (**stale**: delete its line, the list only shrinks). Every file a
  baseline names runs in every tier, so every line is judged on every run;
- a red **security guard** (`tests/tiers.yaml` `security:`), listed or not. A security guard can never be
  listed: `scripts/known_failures.py check` refuses the baseline.

Never add a line to make a gate pass: a line is added only for a failure that also happens on
`origin/main`, with its ticket. The worker suite (`apps/worker/tests`) is small and runs whole in every tier.

**Without the install's database container** (CI, `.github/workflows/ci.yml`), these settings apply:

| Setting | Effect |
|---|---|
| `BOSS_TEST_PG_URL=postgresql://user:pass@host:port` | Uses any Postgres that may create databases, instead of `boss-community-db-1` |
| `TEST_TIER_DB_PREFIX` | Names the databases (default `boss_test`, so `boss_test` and `boss_test_t1..tN`). The name must start with `boss_test` |
| `TEST_TIER_SHARDS` | Sets the number of API shards |
| `TEST_TIER_SUITES=engine,api,worker,scripts` | Limits the run to those suites (default: all five) |

A fresh database needs the schema and the install's first-boot seed:
`python scripts/ci_apply_schema.py boss_test`.

### The test Postgres (BOSSAPP-1152, 1162)

Tests do not run inside the install's database server. Until November 2026 the engine and API
suites, the harness bench and developers' scratch databases all lived in the same Postgres as
the live BOSS (~9 GB of them on one laptop), and one slow test query spilled 11 GB of temp
files next to the real data. Three rules now hold, all in `engine/boss_engine/dev/test_databases.py`:

**1. A second Postgres for dev and test.** The `db-test` compose service (profile `dev`, same
image as `db`, port **55700**, volume `boss_community_pg_test_v2` labelled `owner=boss kind=test
ttl=24h`) is where test databases belong. It runs with `max_locks_per_transaction=1024`,
`temp_file_limit=1GB` and a `statement_timeout` of 60 s for every connection.

```bash
boss dev up            # or: docker compose -f scripts/install/community-compose.yml --env-file .env --profile dev up -d db-test
export BOSS_TEST_DATABASE_URL="postgresql+psycopg://boss:$BOSS_DB_PASSWORD@localhost:55700/boss_test"
export BOSS_TEST_PG_URL="postgresql://boss:$BOSS_DB_PASSWORD@localhost:55700"   # test-tier.sh creates boss_test and the shards there
```

**2. The suites refuse the live server.** Both conftests check `BOSS_TEST_DATABASE_URL` (the
API suite: `DATABASE_URL`) against the install's server — `BOSS_DATABASE_URL`, or a
`DATABASE_URL` that is not itself named like a test database. On that server only a database
called `boss_test*` or `boss_scratch*` is allowed; anything else stops with a message that says
how to start `db-test`. (The shared bench database `boss_test_bench` passes by name.)

**3. Throwaways carry their birthday and are swept.** A test that makes its own database or
schema names it `boss_scratch_<purpose>_<stamp>` / `boss_test_<purpose>_<stamp>` with a UTC
`YYYYmmddtHHMMSS` stamp (`scratch_database()` and `test_schema_name()` do this), and drops it
in `finally`/`atexit`. Postgres records no creation time for a schema or database, so the stamp
is the only age a sweeper can trust — and the reason it can promise to touch nothing else:
`boss`, `boss_lf_dev`, `boss_test`, `boss_test_bench`, `boss_test_t1` have no stamp and are
never candidates. Every test session starts by sweeping stamped names past 24 h and by a
lock-table preflight; on demand:

```bash
boss dev sweep-test-db --dry-run              # inside the install, against db-test
python -m boss_engine dev sweep-test-db --url "$BOSS_TEST_DATABASE_URL" --dry-run   # any test server
python -m boss_engine dev sweep-test-db --url "$BOSS_TEST_DATABASE_URL"             # drop them
python -m boss_engine dev test-db-preflight --url "$BOSS_TEST_DATABASE_URL"
```

`--include-unstamped` adds the API suite's legacy `boss_test_<12 hex>` schemas (the shape before
the stamp) that no other session holds a lock in. Use it once, when no suite from before the
change is running.

**Why schemas were left behind ("out of shared memory").** The API suite gives each run a
private schema of ~2100 relations (578 tables, 675 indexes, toast tables, sequences). One
`DROP SCHEMA … CASCADE` locks every one of them in a single transaction, and the server's lock
table has only `max_locks_per_transaction (64) × max_connections (100) = 6400` slots for every
session together. Three API shards ending at once asked for ~6350 — the drop failed and the
schema stayed; 146 of them (298,000 catalog rows, 4.8 GB) then slowed every catalog query the
suites make. The schema is now dropped a hundred tables at a time (`drop_schema_in_batches`),
`db-test` runs with 1024 locks per transaction, and the session preflight warns when a server's
lock table is too small for the schema it is about to hold.

**CI runs** three jobs on every pull request:

- `p0`: this tier's tests of the suites `TEST_TIER_SUITES` names in `ci.yml` (today engine and API;
  worker and scripts need a workflow change, BOSSAPP-1429), and every test a baseline names, judged against `tests/known-failures/` (no new failure, no stale line, no red security guard);
- `web`: the type check, and the whole web unit suite. Failing files that are known are listed in
  `tests/known-failures/web.txt` with their ticket. That list can only shrink;
- `bench-scripted`: the harness benchmark on the scripted engine against its committed baseline
  (`docs/architecture/HARNESS_BENCHMARK.md`).

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
pins what is installed. Every failure on it is BOSS's, never the app's. A second fixture,
`tests/e2e/fixtures/plain-node`, is a plain Node ESM app with no build, typecheck or lint script
and its own tests under `node --test`.

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
| verify | BOSS's web gate as `resolve_gate_plan` plans it in a first pass (`run_web_gate`): each of build, typecheck and lint the app defines, then its own `npm test`, against its real database | a verification target that breaks a known-good app, or never runs its tests |
| a plain Node ESM app passes | the plain-node fixture cloned, prepared and run through BOSS's whole web gate, install first | a gate that runs scripts the app does not define (`Missing script: "build"`) and not the tests it does (crun_decc93db) |
| the app's programs stay runnable | `workspace_perms.normalize` and `preview_service._proof_tree` | `.bin` targets losing execute for the run's uid (crun_247435e1) |
| the app starts | `prove_app_serves`, called with the same arguments as `_check_app_serves`, plus the connector's route probes | `tsx: Permission denied`; an app-start proof without BOSS's generated secrets (SESSION_SECRET) |
| connector gate | `templates.gate.evaluate` for the build (wiring smoke, secret scan); the app carries none of the connector's tests or test scripts (a connector is pre-tested, `templates.connector_policy`); the connector's own suite runs in its own tier from the template (`templates.trial.run_stack`, JUnit count) | a connector gate that cannot pass on a good app; a build asked to carry a pre-tested connector's suite |
| a blocked build names the gate | `templates.gate.hold_outcome` on the two HelpDesk failures, a build whose own checks failed, and this run's own gates | "the template's tests did not pass" when the install, the app start or the app's own checks failed |
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

**A P0/P1 test may mock the model, never the step that fails for users.** The model is slow,
costly and not ours to test, so a scripted answer is fine. The last mile is not: starting the
preview, launching the engine through `boss-sandbox-exec`, writing to the repository, the
app-start proof. Each of these once passed its test while failing for a customer, because the
test replaced exactly that step (a preview row inserted as "running" and never started; a sandbox
check run without the build launcher; a Setup proof that only read). Where the real step cannot
run in the tier, run BOSS's real code path against a local fake service (a local HTTP server,
a local bare git remote, a fake binary on `PATH`), never a monkeypatch of our own function.
`engine/tests/test_gate_tests_never_mock_the_last_mile.py` fails when a P0/P1 test patches one
of the named last-mile functions.

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
  builds that passed on the shared server in September (Onboarding Path Builder, 13.7 min). Instructions
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

## The enterprise lane (`scripts/enterprise-lane.sh`)

**Why it exists.** Every other gate runs on our own machines: big hosts, Docker Desktop or native
Docker, direct internet, personal model logins, the engine inner sandbox off. Companies run BOSS
on a small, locked-down box: a TLS-inspecting proxy with the company's own CA, internal package
mirrors on private addresses, AppArmor with the inner sandbox on, and a Codex login whose
organisation forbids full access. Most of the problems those users hit (the enterprise RCA of
2026-11-22: 13 of 32 gaps) only happen there, so our tests never saw them. The lane is that
box, made reproducible.

**What it is.** One script that turns a fresh Linux VM (Ubuntu 24.04, 4 vCPU, 8 GB, 30 GB) into
that box, installs BOSS on it from scratch, and runs the release gates there. It prints one
`PASS` or `FAIL` line and writes a report (Markdown) with every step, its time and its evidence.

```bash
scripts/enterprise-lane.sh                     # on the Linux VM, from a BOSS checkout (needs sudo)
scripts/enterprise-lane/lima.sh                # on a Mac: makes/reuses the Lima VM boss-ent-lane,
                                               # copies this checkout's HEAD in, runs the lane,
                                               # brings the report back, stops the VM
scripts/enterprise-lane.sh --reuse-install     # keep the last run's install (skip step 3)
scripts/enterprise-lane.sh --only codex,build  # only some steps (host and network always run)
```

**The steps, in plain words.**

| Step | What happens | What a red step means |
|---|---|---|
| host | Checks the VM is the small one and whether it has AppArmor | The VM is wrong, not BOSS |
| network | Starts the "company network": an HTTPS-inspecting proxy (mitmproxy) with its own CA, a private npm mirror (verdaccio) and PyPI mirror (proxpi) on a private address with TLS from that CA, and a company DNS. Then locks the VM down: nothing leaves except through the proxy, Docker's own pulls included. It proves each of these before going on | The lane's setup broke, not BOSS |
| install | `install.sh` from scratch, `--install-tier dev`, given the CA, proxy and mirrors exactly as [ENTERPRISE_NETWORK.md](../community/ENTERPRISE_NETWORK.md) tells a customer | A customer behind a company proxy cannot install BOSS |
| sandbox | Turns on engine inner-sandbox support the way [ENGINE_SANDBOX.md](../community/ENGINE_SANDBOX.md) says (load the AppArmor profile, `BOSS_ENGINE_INNER_SANDBOX=seccomp+apparmor`, or `seccomp` without AppArmor, then `boss start`) and requires `boss doctor`'s engine sandbox line to be green | The documented hardening does not work |
| codex | Puts a managed Codex policy (`requirements.toml` forbidding full access) in the api and worker containers, then runs Codex's own sandbox under a build's identity, through the real launcher `boss-sandbox-exec`: it must start, write, write every toolchain cache and reach the private npm mirror through BOSS's fence. No model is called | A managed Codex login cannot write on a hardened host |
| plumbing | `scripts/e2e-gate.sh plumbing --inner-sandbox <mode>`: tier A in a container with the install's seccomp and AppArmor profiles | The post-agent pipeline breaks under the hardened profiles |
| engines | `scripts/e2e-gate.sh engines --inner-sandbox <mode>`: the whole build pipeline on the scripted engine, the same way | A build breaks under the hardened profiles |
| build | One scripted build (the engines gate's clean scenario) inside the INSTALLED api container, where builds run: its real profiles, fence, CA and launcher | A build breaks on the real install, not only in the gate's container |

**Only a full run counts.** A run with `--only` or `--reuse-install` ends in `PARTIAL`, never
`PASS`: use it while fixing, then run the whole lane for the release. The script refuses to run
unless `ENTLANE_THROWAWAY_VM=1` (the Lima driver sets it), because it rewrites Docker's settings,
the firewall and apt on the machine it runs on.

**The tenth run behaves like the first.** The network step first takes back everything an
earlier run set (Docker's proxy and DNS, apt's proxy, the lane CA in the trust store, the firewall
chains, the lane's containers), then fetches every image it runs while egress is still direct, and
only then points the VM at the proxy it just started. Bringing the network up never depends on
the network it brings up (`engine/tests/test_the_enterprise_lane_reruns_from_a_known_state.py`).

**No model key is needed.** Builds use the scripted conformance engine. When `CODEX_API_KEY` or
`OPENAI_API_KEY` is exported, the installer receives it the way a person would give it; nothing
the lane prints carries a credential.

**When it runs.** Before every release (it is listed under `P0: release:` in `tests/tiers.yaml`),
and nightly when a VM is free. It took 44 minutes on the 4 vCPU VM (2026-11-24): about 9 minutes for the
from-scratch install through the proxy, 28 for the engines tier.

**A red lane.** Read the step's log (`~/boss-ent-lane/logs/<step>.log` on the VM). A red
`network` or `host` is the lane's own problem; fix the lane. Any other red is a product bug
until shown otherwise: fix it, or ticket it with the report attached. Never edit the lane to
make a step pass.

**What it does not cover yet.** A Colima install on a real Mac (do that by hand weekly with
[COLIMA.md](../community/COLIMA.md)), a proxy that needs a login, authenticated registries,
low-disk and long-queue behaviour, and a real managed Codex login doing a full build.
