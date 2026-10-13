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
