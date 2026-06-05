# BOSS Gap Backlog

Last rebuilt: June 5, 2026

This backlog is rebuilt from `PRD-v2.0`, `TECHNICAL_SPEC-v2.0`, `TRACEABILITY.md`, user-story coverage, and a code scan for `TODO`, `FIXME`, `stub`, `placeholder`, `mock`, `fake`, `not implemented`, skipped tests, broad exception handling, and fake-success patterns.

Status labels:

- `Closed`: implemented, tested, and traced.
- `Blocked`: requires external credentials, hosted service deployment, production infrastructure, or explicit rollout approval.
- `Monitoring`: currently acceptable, but should be rechecked as adjacent features change.

| Gap ID | Priority | Linked requirement | Affected journey/files | Finding | Implementation needed | Tests/validation | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GAP-2026-06-05-001 | P0 | No silent drops; AUD-003 policy denial visibility | `apps/api/app/main.py`, `apps/api/tests/test_api.py`, `docs/user-stories.md`, `docs/product/TRACEABILITY.md` | Policy-denial helper fallbacks recorded user-facing denial but silently swallowed failures when the denial audit event could not be written. | Added structured `boss.api` exception logging with project, actor, permission/event, entity, and reason context while preserving fail-closed `403` behavior. | `DATABASE_URL=postgresql+psycopg://boss:boss@localhost:56344/boss .venv/bin/python -m pytest apps/api/tests/test_api.py::test_policy_denial_audit_write_failure_is_logged`; `make lint` | Closed |
| GAP-2026-06-05-002 | P0 | Commercial/Account Connect Cloud readiness | Cloud Account Connect, hosted marketplace, hosted team/billing, hosted support routing | Local BOSS surfaces are implemented and fail closed, but the hosted Account Connect service URL/token are not configured in this environment. | Deploy/configure hosted Account Connect and set `BOSS_ACCOUNT_CONNECT_URL` plus any required token material. | `make release-blockers`; live hosted service verification after configuration | Blocked |
| GAP-2026-06-05-003 | P0 | Enterprise identity and SCIM rollout | Enterprise SSO/SAML/OIDC/SCIM runtime | Runtime surfaces and fail-closed gates are implemented, but live IdP credentials, JWKS/SAML config, SCIM bearer tokens, and rollout policies are not configured. | Configure approved enterprise identity provider(s), SCIM bearer tokens, and explicit rollout policies. | `make release-blockers`; IdP-facing SSO/SCIM integration tests in approved environment | Blocked |
| GAP-2026-06-05-004 | P0 | ProviderOps writes and live infra rollout | ProviderOps ChangeSet apply, deployment runs, Kubernetes/Helm packages | Provider write and cluster package code is implemented and validated locally, but live credentials, target cluster/context, production secrets, rollout approval, and live validation evidence are absent. | Configure approved provider credentials, `BOSS_INFRA_REAL_PROVISION_ALLOWED=true`, target cluster secrets/storage/backups, and recorded approval/rollback evidence. | `make k8s-validate`; `make infra-validate`; `make release-blockers`; live rollout verification | Blocked |
| GAP-2026-06-05-005 | P1 | No placeholders/fake providers | Specpack skills, provider adapters, web UI placeholders | Fresh scan found `mock_mode: false` in SkillSpecs, UI form placeholders, and test doubles only. Unsupported future provider billing/inventory paths fail closed with visible errors/deadletters. | No code change currently required; recheck when adding new providers or UI sections. | `rg` scan plus existing provider/skill validator tests | Monitoring |

