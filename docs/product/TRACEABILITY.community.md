# Community feature traceability

Last updated: September 24, 2026

This ledger covers only source and test files present in the current Community export candidate. `Implemented` means the complete cited source and automated test set is present. `Partial` means the candidate contains relevant source and tests, while other implementation paths or release checks remain pending. A listed feature is not a claim that the Community build has passed release qualification.

| Requirement label | Source | Implementation files | Test coverage | Status |
|---|---|---|---|---|
| MET-001 CEO command intake | Executive console | `apps/web/app/page.tsx` | `apps/web/tests/commands.test.ts` | Partial |
| MET-002 What changed feed | Executive console | `apps/web/lib/events.ts`, `apps/web/app/page.tsx` | `apps/web/tests/activity.test.ts` | Partial |
| MET-004 Scoped Metatron query | Executive console | `apps/web/lib/metatron.ts` | `apps/web/tests/metatron.test.ts` | Partial |
| DEC-001 Decision card fields | Decision inbox | `apps/api/app/schemas.py`, `apps/web/app/page.tsx` | `apps/web/tests/decisions.test.ts` | Partial |
| EVT-002 Event subscription registry | Event and webhook operations | `apps/api/app/schemas.py` | `apps/api/tests/test_oss_reactive_docs_foundation.py` | Partial |
| EVT-005 Event/webhook operability | Event and webhook operations | `apps/api/app/schemas.py` | `apps/api/tests/test_event_operability.py`, `apps/web/tests/deadletters.test.ts` | Partial |
| GHX-004 Fenced branch and worktree lease | GitHub project sync | `engine/boss_engine/services/github_branch_leases.py` | `apps/api/tests/test_github_branch_leases.py` | Partial |
| GHX-005 Safe code synchronization | GitHub project sync | `engine/boss_engine/services/github_code_sync.py`, `engine/boss_engine/services/github_adapter.py` | `apps/api/tests/test_github_code_sync.py`, `apps/api/tests/test_github_community_adapter.py` | Partial |
| GHX-006 Conflict branch and reviewed resolution | GitHub project sync | `engine/boss_engine/services/github_code_sync.py`, `engine/boss_engine/services/github_adapter.py` | `apps/api/tests/test_github_code_sync.py`, `apps/api/tests/test_github_community_adapter.py` | Partial |
| GHX-007 Governed merge authorization and queue | GitHub project sync | `engine/boss_engine/services/github_merges.py`, `engine/boss_engine/services/github_adapter.py` | `apps/api/tests/test_github_community_adapter.py` | Partial |
| GHX-008 Webhook and polling reconciler | GitHub project sync | `engine/boss_engine/services/github_reconciliation.py`, `engine/boss_engine/services/webhooks.py`, `engine/boss_engine/services/github_adapter.py` | `apps/api/tests/test_github_community_adapter.py` | Partial |
| GHX-009 GitHub Mission Control | GitHub project sync | `apps/web/lib/github-operations.ts`, `apps/web/app/github-operations-ui.tsx`, `apps/web/app/actions.ts` | `apps/web/tests/accessibility.test.ts` | Partial |
| ORC-001 Conversational thread store/API | Agent orchestration | `apps/api/app/schemas.py` | `apps/api/tests/test_orchestration_agent.py` | Partial |
| DOC-001 Feature documentation catalog | Documentation | `engine/boss_engine/services/feature_catalog.py`, `apps/api/app/bootstrap.py` | `apps/api/tests/test_oss_reactive_docs_foundation.py` | Partial |
| DOC-002 Reactive documentation jobs | Documentation | `engine/boss_engine/services/docs_agent.py`, `apps/worker/boss_worker/runner.py`, `apps/worker/boss_worker/control_plane.py` | `apps/api/tests/test_docs_agent.py`, `apps/api/tests/test_oss_reactive_docs_foundation.py` | Partial |
| DOC-003 Evidence-grounded docs agent | Documentation | `engine/boss_engine/services/docs_agent.py`, `packages/specpack/roles/docs_writer.yaml` | `apps/api/tests/test_docs_agent.py` | Partial |
| BUD-004 Budget dashboard | Budget governor | `apps/web/lib/budget-status.ts`, `apps/web/app/page.tsx` | `apps/web/tests/budget-status.test.ts` | Partial |
| BUD-006 Infra commitments and TTL janitor | Budget governor | `apps/web/app/page.tsx` | `apps/web/tests/infrastructure.test.ts` | Partial |
| AUD-005 Change journal and diff viewer | Audit trail | `apps/web/lib/change-journal.ts`, `apps/web/app/page.tsx` | `apps/web/tests/change-journal.test.ts` | Partial |
| SEC-002 Multi-user auth/RBAC | Access and data boundaries | `apps/api/app/config.py`, `apps/web/lib/auth-session.ts`, `apps/web/lib/members.ts` | `apps/web/tests/members.test.ts`, `apps/web/tests/auth-session.test.ts` | Partial |
| SEC-003 Break-glass access | Access and data boundaries | `apps/web/app/page.tsx` | `apps/web/tests/break-glass.test.ts` | Partial |
| NFR-002 Accessibility baseline | Local console | `apps/web/app/page.tsx`, `apps/web/app/globals.css` | `apps/web/tests/accessibility.test.ts` | Implemented |
| AGP-001 Versioned Agent Groups and collaboration protocols | Agent groups | `engine/boss_engine/services/governed_collaboration.py`, `apps/api/app/routers/governance.py`, `packages/specpack/objects/agent_group_spec.yaml` | `apps/api/tests/test_governance_runtime.py`, `apps/api/tests/test_assurance_specpack_contracts.py` | Partial |
| INT-001 Integration registry and typed skill contracts | Integrations | `engine/boss_engine/services/governed_collaboration.py`, `apps/api/app/routers/governance.py`, `packages/specpack/objects/integration_definition.yaml` | `apps/api/tests/test_governance_runtime.py`, `apps/api/tests/test_assurance_specpack_contracts.py` | Partial |
| WFL-001 Reusable mapped workflow registry and runtime | Workflow registry | `apps/api/app/schemas.py`, `apps/web/lib/workflows.ts`, `apps/web/app/workflow-registry-ui.tsx` | `apps/api/tests/test_workflow_runtime.py`, `apps/web/tests/workflows.test.tsx` | Partial |
| AST-001 Ground-truth and Assurance runtime | Assurance | `engine/boss_engine/services/assurance.py`, `apps/api/app/routers/governance.py`, `apps/web/lib/assurance.ts` | `apps/api/tests/test_governance_runtime.py`, `apps/web/tests/assurance.test.tsx` | Partial |
