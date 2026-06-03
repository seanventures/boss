# BOSS PRD/Spec Traceability

Last updated: June 3, 2026

Status labels:

- `Implemented`: runtime path exists and is covered by automated tests.
- `Partial`: runtime path exists but lacks a full PRD-grade implementation or coverage.
- `Planned`: documented but not implemented.
- `Blocked`: needs credentials, external setup, or explicit approval.

| Requirement label | Source | Implementation files | Test coverage | Status |
|---|---|---|---|---|
| MET-001 CEO command intake | PRD Executive Console; Workflow `ceo_command_to_pr` intake | `apps/api/app/services/intake.py`, `apps/api/app/main.py`, `apps/web/app/page.tsx` | `apps/api/tests/test_api.py`, `apps/web/tests/commands.test.ts` | Implemented |
| MET-002 What changed feed | PRD Executive Console | `apps/api/app/main.py`, `apps/web/lib/events.ts`, `apps/web/app/page.tsx` | `apps/web/tests/activity.test.ts`, `apps/api/tests/test_api.py` | Implemented |
| MET-003 Top risks panel | PRD Executive Console | `apps/api/app/services/health.py`, `apps/api/app/main.py`, `apps/web/app/page.tsx` | `apps/api/tests/test_api.py` | Implemented |
| MET-004 Scoped Metatron query | PRD Executive Console | `apps/api/app/services/metatron.py`, `apps/api/app/main.py`, `apps/web/lib/metatron.ts` | `apps/api/tests/test_api.py`, `apps/web/tests/metatron.test.ts` | Implemented |
| DEC-001 Decision card fields | PRD Decision Inbox | `db/schema.sql`, `apps/api/app/schemas.py`, `apps/web/app/page.tsx` | `apps/api/tests/test_api.py`, `apps/web/tests/decisions.test.ts` | Implemented |
| DEC-002 Decision state transitions | PRD Decision Inbox | `apps/api/app/services/decisions.py` | `apps/api/tests/test_api.py` | Implemented |
| DEC-003 Tiered approval policy | PRD Decision Inbox; `default_safety.yaml` approvals | `apps/api/app/services/members.py`, `apps/api/app/services/decisions.py` | `apps/api/tests/test_api.py` | Implemented |
| DEC-004 Safe default expiry | PRD Decision Inbox | `apps/api/app/services/decisions.py` | `apps/api/tests/test_api.py` | Implemented |
| DEC-005 Decision digests | PRD Decision Inbox | `apps/api/app/services/decisions.py`, `apps/api/app/services/comms.py`, `apps/api/app/main.py`, `packages/api-client/src/index.ts`, `apps/web/app/page.tsx` | `apps/api/tests/test_api.py`, `apps/web/tests/api-client.test.ts` | Implemented |
| CAN-001 Canonical objects | PRD Canonical work system; technical spec data model | `db/schema.sql`, `apps/api/app/services/canonical.py`, `apps/api/app/main.py`, `packages/api-client/src/index.ts` | `apps/api/tests/test_api.py`, `apps/worker/tests/test_worker.py` | Implemented |
| CAN-002 Versioned artifacts | PRD Canonical work system | `apps/api/app/services/core.py`, artifact API/UI | `apps/api/tests/test_api.py` | Implemented |
| CAN-003 Immutable events | PRD Canonical work system | `apps/api/app/services/core.py`, event feed | `apps/api/tests/test_api.py`, `apps/web/tests/activity.test.ts` | Implemented |
| CAN-004 Task hierarchy/dependencies/owners | PRD Canonical work system | `db/schema.sql`, `apps/api/app/services/intake.py`, `apps/api/app/main.py`, `packages/api-client/src/index.ts`, `apps/web/lib/tasks.ts`, `apps/web/app/page.tsx` | `apps/api/tests/test_api.py`, `apps/web/tests/api-client.test.ts` | Implemented |
| BUD-001 Budget policy caps | PRD Budget Governor | `db/schema.sql`, `apps/api/app/services/budget.py` | `apps/api/tests/test_api.py` | Implemented |
| BUD-002 Quote/reserve/execute/receipt | PRD Budget Governor; technical spec | `apps/api/app/services/budget.py`, `apps/api/app/services/software_factory.py` | `apps/api/tests/test_api.py` | Implemented |
| BUD-003 Hierarchical budgets | PRD Budget Governor | `db/schema.sql`, `apps/api/app/services/budget.py`, `apps/api/app/main.py`, `apps/web/app/page.tsx` | `apps/api/tests/test_api.py` | Implemented |
| BUD-004 Budget dashboard | PRD Budget Governor | `apps/api/app/services/budget.py`, `apps/api/app/main.py`, `apps/web/lib/budget-status.ts`, `apps/web/app/page.tsx` | `apps/api/tests/test_api.py`, `apps/web/tests/budget-status.test.ts` | Implemented |
| BUD-005 Budget state behavior | PRD Budget Governor | `apps/api/app/services/jobs.py`, `apps/api/app/services/budget.py` | `apps/api/tests/test_api.py` | Implemented |
| BUD-006 Infra commitments and TTL janitor | PRD Budget Governor | `db/schema.sql`, `apps/api/app/services/infrastructure.py`, `apps/api/app/services/digitalocean_adapter.py`, `apps/api/app/main.py`, `packages/api-client/src/index.ts`, `apps/web/app/page.tsx` | `apps/api/tests/test_api.py`, `apps/worker/tests/test_worker.py`, `apps/web/tests/infrastructure.test.ts` | Implemented |
| RUN-001 Job state machine | PRD Durable runtime; technical spec | `apps/api/app/services/jobs.py`, `apps/worker/boss_worker/main.py` | `apps/api/tests/test_api.py`, `apps/worker/tests/test_worker.py` | Implemented |
| RUN-002 Pull-based workers | PRD Durable runtime | `apps/api/app/services/jobs.py`, `apps/worker/boss_worker/main.py` | `apps/api/tests/test_api.py`, `apps/worker/tests/test_worker.py` | Implemented |
| RUN-003 Lease reclaim/retry/deadletter | PRD Durable runtime | `apps/api/app/services/jobs.py` | `apps/api/tests/test_api.py` | Implemented |
| RUN-004 Receipt dedupe | PRD Durable runtime; technical spec | `apps/api/app/services/receipts.py`, effect services | `apps/api/tests/test_api.py` | Implemented |
| ROL-001 RoleSpec validation | PRD Role system | `packages/specpack/roles`, `packages/specpack/modules`, `apps/api/app/services/specpack.py` | `apps/api/tests/test_api.py` | Implemented |
| ROL-002 Core Pack v1 | PRD Role system | `packages/specpack/packs/core_v1.yaml`, `apps/api/app/bootstrap.py` | `apps/api/tests/test_api.py` | Implemented |
| ROL-003 Autonomy ladder | PRD Role system | `packages/specpack/policies/autonomy_ladder.yaml`, `apps/api/app/services/role_governance.py`, `apps/api/app/main.py` | `apps/api/tests/test_api.py` | Implemented |
| ROL-005 Role drift prevention | PRD Role system | `packages/specpack/roles`, `apps/api/app/services/role_governance.py`, `apps/api/app/main.py`, `packages/api-client/src/index.ts` | `apps/api/tests/test_api.py`, `apps/web/tests/api-client.test.ts` | Implemented |
| AGT-001 Agent modules | User-added marketplace requirement; technical spec | `packages/specpack/modules`, `apps/api/app/services/specpack.py`, `apps/web/app/page.tsx` | `apps/api/tests/test_api.py` | Implemented |
| SWF-001 Spec-to-PR workflow | PRD Software Factory; workflow spec | `apps/api/app/services/software_factory.py`, `apps/api/app/services/decisions.py`, worker | `apps/api/tests/test_api.py` | Implemented for sandbox PR path |
| SWF-003 CI and sandbox validation gate | PRD Software Factory | `apps/api/app/services/github_adapter.py`, `apps/api/app/services/sandbox_validation.py`, `apps/api/app/services/software_factory.py`, decisions | `apps/api/tests/test_api.py` | Implemented |
| SWF-004 Review packet | PRD Software Factory | `apps/api/app/services/software_factory.py` | `apps/api/tests/test_api.py` | Implemented |
| SWF-005 Staged rollout evidence | PRD Software Factory; PRD Self-improvement rollout discipline | `db/schema.sql`, `apps/api/app/services/software_factory.py`, `apps/api/app/main.py`, `packages/api-client/src/index.ts` | `apps/api/tests/test_api.py` | Implemented for sandbox deployments |
| SKL-001 SkillSpec registry | PRD Skills Registry | `packages/specpack/skills`, `apps/api/app/services/entitlements.py`, `api/openapi.yaml` | `apps/api/tests/test_api.py` | Implemented |
| MEB-001 Entitlement pools | Model Entitlement Broker requirement | `packages/specpack/models/providers.yaml`, `apps/api/app/services/entitlements.py` | `apps/api/tests/test_api.py` | Implemented |
| MEB-002 No scraping/rate bypass | Model Entitlement Broker guardrail; policy spec | `packages/specpack/policies/default_safety.yaml`, `apps/api/app/services/entitlements.py` | `apps/api/tests/test_api.py` | Implemented |
| RPL-001 ProviderOps as core plane | PRD v1.1 Elastic Resource Plane; Technical Spec v1.1 | `docs/product/PRD-v1.1.md`, `docs/product/TECHNICAL_SPEC-v1.1.md`, `packages/specpack/policies/resource_plane_policy.yaml` | Planned acceptance tests from `docs/user-stories.md` | Planned |
| RPL-002 Provider connection center | PRD v1.1 Provider Connection Center | `packages/specpack/objects/provider_account.yaml`, `packages/specpack/objects/provider_adapter.yaml`, `packages/specpack/objects/secret_ref.yaml`, `packages/specpack/workflows/providerops_readonly_sync.yaml` | Planned provider adapter tests | Planned |
| RPL-003 Resource inventory graph | PRD v1.1 Resource Inventory Graph | `packages/specpack/objects/external_resource.yaml`, `packages/specpack/objects/capability.yaml`, `packages/specpack/objects/reconciliation_run.yaml` | Planned sync/reconciliation tests | Planned |
| RPL-004 Cost and billing ledger | PRD v1.1 Cost and Billing Ledger; Financial Control Model | `packages/specpack/objects/billing_account.yaml`, `packages/specpack/objects/cost_event.yaml`, `packages/specpack/objects/commitment.yaml` | Planned cost confidence/reconciliation tests | Planned |
| RPL-005 ChangeSet planner and actuation | PRD v1.1 ChangeSet Planner; workflow `changeset_apply_with_receipts` | `packages/specpack/objects/changeset.yaml`, `packages/specpack/objects/resource_receipt.yaml`, `packages/specpack/workflows/changeset_apply_with_receipts.yaml` | Planned state-machine and dedupe tests | Planned |
| RPL-006 Deployment registry | PRD v1.1 Deployment Registry; workflow `deployment_run` | `packages/specpack/objects/deployment_target.yaml`, `packages/specpack/workflows/deployment_run.yaml` | Planned deployment state-machine tests | Planned |
| RPL-007 Provider skill catalog | PRD v1.1 Provider Skill Catalog | `packages/specpack/skills/cloudflare.yaml`, `packages/specpack/skills/digitalocean.yaml`, `packages/specpack/skills/supabase.yaml`, `packages/specpack/skills/model_providers.yaml` | Planned adapter-boundary tests | Planned |
| RPL-008 Janitor, drift, health, autoscaler | PRD v1.1 Resource Janitor, Health Monitor, Autoscaler, Drift Detector | `packages/specpack/objects/drift_report.yaml`, `packages/specpack/objects/provider_health.yaml`, `packages/specpack/objects/resource_policy.yaml` | Planned drift/janitor/autoscaler policy tests | Planned |
| RPL-009 ProviderOps roles | PRD v1.1 ProviderOps Roles | `packages/specpack/roles/providerops_manager.yaml`, `packages/specpack/roles/finops_sentinel.yaml`, `packages/specpack/roles/resource_janitor.yaml` | Planned RoleSpec validation tests when installed into a pack | Planned |
| ARCH-001 OpenClaw not foundation | PRD v1.1 Architecture; Conflicts and Resolutions | `docs/product/PRD-v1.1.md`, `docs/product/TECHNICAL_SPEC-v1.1.md`, `AGENTS.md` | Documentation validation | Implemented in specs |
| COM-002 Deterministic email/inbox backbone | PRD Communications | `apps/api/app/services/comms.py`, `apps/api/app/services/email_adapter.py`, `apps/api/app/config.py`, `apps/api/app/main.py`, `packages/specpack/skills/email.yaml`, `apps/web/app/page.tsx` | `apps/api/tests/test_api.py` | Implemented |
| COM-003 No silent comms drops | PRD Communications | `apps/api/app/services/comms.py`, `apps/worker/boss_worker/runner.py` | `apps/api/tests/test_api.py`, `apps/worker/tests/test_worker.py` | Implemented |
| COM-004 Quiet hours and throttling | PRD Communications | `apps/api/app/config.py`, `apps/api/app/services/comms.py`, `apps/api/app/main.py`, `apps/web/app/page.tsx`, `.env.example` | `apps/api/tests/test_api.py`, `apps/web/tests/api-client.test.ts` | Implemented |
| AUD-001 Events/receipts chain | PRD Audit/replay | `apps/api/app/services/audit.py`, `apps/api/app/main.py` | `apps/api/tests/test_api.py` | Implemented |
| AUD-003 Policy denial visibility | PRD Policy logs | `apps/api/app/main.py` | `apps/api/tests/test_api.py` | Implemented |
| AUD-004 Compliance evidence bundle | PRD 9.4 Compliance posture; PRD Audit Center | `apps/api/app/services/audit.py`, `apps/api/app/main.py`, `packages/api-client/src/index.ts`, `apps/web/app/page.tsx` | `apps/api/tests/test_api.py`, `apps/web/tests/api-client.test.ts` | Implemented |
| AUD-005 Change journal and diff viewer | PRD Audit/replay | `apps/api/app/services/audit.py`, `apps/api/app/main.py`, `packages/api-client/src/index.ts`, `apps/web/lib/change-journal.ts`, `apps/web/app/page.tsx` | `apps/api/tests/test_api.py`, `apps/web/tests/api-client.test.ts`, `apps/web/tests/change-journal.test.ts` | Implemented |
| SIM-001 Self-improvement gates | PRD Self-improvement | `apps/api/app/services/self_improvement.py`, decisions | `apps/api/tests/test_api.py` | Implemented for proposal/canary/rollout/rollback records |
| SEC-001 API transport authentication | PRD security/non-functional; API-first user request | `apps/api/app/main.py`, `apps/api/app/config.py`, `packages/api-client/src/index.ts` | `apps/api/tests/test_api.py` | Implemented |
| SEC-002 Multi-user auth/RBAC | PRD enterprise hardening | `apps/api/app/main.py`, `apps/api/app/config.py`, `packages/api-client/src/index.ts` | `apps/api/tests/test_api.py` | Partial: API-key principal binding implemented; external SSO/OIDC blocked pending provider config |
| SEC-003 Break-glass access | PRD 9.2 Identity and access | `db/schema.sql`, `apps/api/app/services/break_glass.py`, `apps/api/app/services/members.py`, `apps/api/app/main.py`, `apps/web/app/page.tsx` | `apps/api/tests/test_api.py`, `apps/web/tests/break-glass.test.ts` | Implemented |
| SEC-004 Data-boundary classification | PRD 9.1 Data boundaries | `db/schema.sql`, `apps/api/app/services/core.py`, `apps/api/app/services/intake.py`, `apps/api/app/services/jobs.py`, `apps/api/app/services/receipts.py`, `apps/api/app/services/comms.py`, `apps/api/app/services/audit.py`, `apps/api/app/main.py`, `packages/api-client/src/index.ts` | `apps/api/tests/test_api.py`, `apps/web/tests/api-client.test.ts` | Implemented |
| SEC-005 Data-boundary routing and effect enforcement | PRD 9.1 Data boundaries; Model Entitlement Broker guardrail | `db/schema.sql`, `apps/api/app/services/entitlements.py`, `apps/api/app/services/software_factory.py`, `apps/api/app/main.py`, `packages/specpack/models/providers.yaml`, `packages/specpack/policies/default_safety.yaml` | `apps/api/tests/test_api.py` | Implemented |
| NFR-001 Local portability | PRD Non-functional | `Makefile`, `infra/docker-compose.yml`, `.env.example` | `make smoke` | Implemented |
| NFR-002 Accessibility baseline | PRD Non-functional | `apps/web/app/page.tsx`, `apps/web/app/globals.css` | `apps/web/tests/accessibility.test.ts` | Implemented |

## Current Highest-Risk Gaps

1. External SSO/OIDC and delegated org-wide RBAC require provider configuration and secrets.
2. Real Slack/email sends remain disabled by default until explicit policy, provider credentials, and rollout choices are configured.
3. Broader production promotion and non-sandbox infrastructure targets remain intentionally disabled until explicit policy, credentials, and deployment targets are configured.

## Assumptions and Decisions

- API key auth is the safest immediate production-readiness step before full SSO. It should protect `/v1/*` routes when enabled, while keeping `/health` public for liveness probes.
- Project member authorization remains separate from transport authentication: API keys identify clients, while project members authorize the actor represented in mutation payloads. When `BOSS_API_KEY_PRINCIPALS` is configured, the authenticated key must match the claimed actor.
- Slack/email real delivery and real infrastructure provisioning fail closed unless policy and provider credentials explicitly permit them.
