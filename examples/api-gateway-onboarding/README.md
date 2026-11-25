# API gateway onboarding: demo tenant

Demo data for the API gateway onboarding chain ([design](../../docs/design/API_GATEWAY_ONBOARDING.md),
BOSSAPP-1134). All names are fictional: **Northgate** is the gateway team, **Lumen Pay** is the
partner being onboarded, **Orbit Maps** is a partner already on the gateway.

This is not a workflow setup script like the other examples (it has no `setup.py`). It is the
tenant repository the chain runs on, plus a reset.

## What is here

| Path | What it is |
|---|---|
| `tenant-repo/` | The gateway team's repo as it starts: rules, checker, contract tests, one passing tenant. |
| `tenant-repo/tools/tenantlint.py` | The checker. Standard-library Python; prints the JSON BOSS reads with its `gwlint-json` adapter. |
| `tenant-repo/rules/TENANT_RULES.md` | The nine rules: four security, five guideline. |
| `tenant-repo/.boss/` | What BOSS reads: `validators.yaml` (which checker), `rulesets/northgate-tenants.yaml` (priority and who may fix each rule), `contract_tests.yaml` (checks against staging). |
| `partner/lumen-pay.bad.yaml` | The partner's first file. Breaks 4 security and 3 guideline rules (8 findings). |
| `partner/lumen-pay.corrected.yaml` | The same tenant after review. Passes every rule. |
| `reset.sh` | Puts the demo back to its start. |

What the bad file breaks (`python3 tenant-repo/tools/tenantlint.py partner/lumen-pay.bad.yaml`):

| Kind | Rule | Where |
|---|---|---|
| security | `no-inlined-secrets` | a plaintext `api_key` (a dummy value) |
| security | `cors-no-wildcard-with-credentials` | `allow_origins: ["*"]` with credentials |
| security | `no-sensitive-paths-exposed` | `/v1/admin/accounts` |
| security | `upstream-https-required` | `http://` upstream |
| guideline | `rate-limit-required` | `/v1/payments` has none |
| guideline | `request-size-capped` | both routes lack `max_body_bytes` |
| guideline | `pii-fields-declared` | PII tenant without `pii_fields` |

## Reset

```bash
# A local repo, for a dry run or a BOSS project that points at a local path:
examples/api-gateway-onboarding/reset.sh --local /tmp/northgate-tenants

# A GitHub repo you own for the demo (force-resets it; needs the gh CLI signed in):
examples/api-gateway-onboarding/reset.sh --github <owner>/northgate-tenants --yes
```

Both give `main` (the gateway team's repo) and a branch `onboard/lumen-pay` that adds the bad file
at `config/tenants/lumen-pay.yaml`. With `--github`, the branch is pushed and a pull request is
opened, which is the event BOSS's GitHub intake receives (filter `data.changed_files` glob
`config/tenants/*.yaml`, `engine/boss_engine/connectors/intake/github/adapter.py`). The script
refuses a directory or repository that is not already a demo repo (no `.northgate-demo` marker).

## Running the chain on it

1. Connect the GitHub repo to a BOSS project ([CONNECT_GITHUB.md](../../docs/community/CONNECT_GITHUB.md)).
2. Start **Onboard a config file** with `target_path: config/tenants/lumen-pay.yaml` and the bad
   file as `start_from`. BOSS runs `tenantlint`, shows the 8 findings, fixes what the ruleset lets
   an agent fix, and holds the rest (`never`, `human`) for a person.
3. Staging and contract tests need a staging gateway the project can deploy config to (a
   deployment target). The cases in `.boss/contract_tests.yaml` assume the gateway answers
   `/healthz`, refuses a tokenless `/v1/payments` with 401, and does not publish `/v1/admin/...`.

Which chain steps are on `main` today, and which are not yet, is in the
[design doc](../../docs/design/API_GATEWAY_ONBOARDING.md#what-is-on-main-today).
`engine/tests/test_api_gateway_demo_tenant.py` keeps this fixture honest.
