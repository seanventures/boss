# Northgate tenant configs (demo)

Each partner's gateway configuration is one file under `config/tenants/`. A pull request that adds
or changes one is checked by `tools/tenantlint.py` against [the tenant rules](rules/TENANT_RULES.md).

- `.boss/validators.yaml`: the checker BOSS runs on a tenant file.
- `.boss/rulesets/northgate-tenants.yaml`: each rule's priority and who may fix it.
- `.boss/contract_tests.yaml`: the checks BOSS runs against staging after a deploy.

Fictional: Northgate, Lumen Pay and Orbit Maps are made-up names.
