# BOSS-429 MCP operator boundary audit

Source: mounted Community operations at integration commit `c218f77e` before this review. Every one of the 77 operations previously marked `operator_only_unavailable` is listed below. Two project-pinnable internal actions are now callable through the API policy path; 75 remain unavailable without a trusted human confirmation channel or a separate API fix.

| Operation | Disposition and evidence |
|---|---|
| `DELETE /v1/channels/{provider}` | Hold: removes configuration, delivery, or graph state |
| `DELETE /v1/console/v2/org/{agent_id}` | Hold: removes configuration, delivery, or graph state |
| `DELETE /v1/delivery-workflows/custom-steps/{step_key}` | Hold: removes configuration, delivery, or graph state |
| `DELETE /v1/delivery-workflows/runs/{run_id}` | Hold: removes configuration, delivery, or graph state |
| `DELETE /v1/delivery-workflows/schedules/{schedule_id}` | Hold: removes configuration, delivery, or graph state |
| `DELETE /v1/delivery-workflows/{definition_id}` | Hold: removes configuration, delivery, or graph state |
| `DELETE /v1/deployment-targets/{target_id}` | Hold: direct deploy, host, or provider effect |
| `DELETE /v1/event-subscriptions/{subscription_id}` | Hold: removes configuration, delivery, or graph state |
| `DELETE /v1/kms/keys/{key_entry_id}/grants/{grantee_agent}` | Hold: credentials, key grants, or integration revocation |
| `DELETE /v1/mcp/servers/{server_id}` | Hold: removes configuration, delivery, or graph state |
| `DELETE /v1/meeting-roles/{role_key}` | Hold: removes configuration, delivery, or graph state |
| `DELETE /v1/preview-sandboxes/{preview_id}` | Hold: direct deploy, host, or provider effect |
| `DELETE /v1/resource-topology/edges/{edge_id}` | Hold: removes configuration, delivery, or graph state |
| `DELETE /v1/role-skills` | Hold: changes agent or policy capability authority |
| `DELETE /v1/sandboxes/{sandbox_id}` | Hold: direct deploy, host, or provider effect |
| `DELETE /v1/secret-refs/{secret_ref_id}` | Hold: credentials, key grants, or integration revocation |
| `DELETE /v1/skill-definitions/{skill_key}` | Hold: changes agent or policy capability authority |
| `DELETE /v1/tasks/{task_id}` | Allow: project-scoped soft archive with task_create and task.archived event |
| `DELETE /v1/webhook-endpoints/{endpoint_id}` | Hold: removes configuration, delivery, or graph state |
| `POST /v1/activity/retention/prune` | Hold: worker-only retention deletion of diagnostic rows |
| `POST /v1/agent-modules/{module_id}/rollback` | Hold: module rollback changes active role/capability authority |
| `POST /v1/agent/threads/{thread_id}/clear` | Hold: thread archives messages without an audit event |
| `POST /v1/break-glass/grants` | Hold: break-glass or member authority |
| `POST /v1/break-glass/grants/{grant_id}/revoke` | Hold: break-glass or member authority |
| `POST /v1/budget/allocations` | Hold: budget or autonomy authority |
| `POST /v1/budget/set_intensity` | Hold: budget or autonomy authority |
| `POST /v1/changesets/{changeset_id}/approve` | Hold: human approval or decision authority |
| `POST /v1/code-tasks/{run_id}/deploy` | Hold: direct deploy, host, or provider effect |
| `POST /v1/console/v2/autonomy/apply` | Hold: budget or autonomy authority |
| `POST /v1/decisions/{decision_id}/approve` | Hold: human approval or decision authority |
| `POST /v1/decisions/{decision_id}/delegate` | Hold: human approval or decision authority |
| `POST /v1/decisions/{decision_id}/execute` | Hold: human approval or decision authority |
| `POST /v1/decisions/{decision_id}/reject` | Hold: human approval or decision authority |
| `POST /v1/decisions/{decision_id}/revise` | Hold: human approval or decision authority |
| `POST /v1/delivery/deploy` | Hold: direct deploy, host, or provider effect |
| `POST /v1/delivery/deployments/{deployment_run_id}/execute` | Hold: direct deploy, host, or provider effect |
| `POST /v1/delivery/deployments/{deployment_run_id}/redeploy` | Hold: direct deploy, host, or provider effect |
| `POST /v1/deployment-hosts/{host_id}/adopt-key` | Hold: credentials, key grants, or integration revocation |
| `POST /v1/deployment-hosts/{host_id}/install-key` | Hold: credentials, key grants, or integration revocation |
| `POST /v1/deployment-hosts/{host_id}/key` | Hold: credentials, key grants, or integration revocation |
| `POST /v1/deployment-hosts/{host_id}/provision` | Hold: direct deploy, host, or provider effect |
| `POST /v1/deployment-hosts/{host_id}/retire` | Hold: direct deploy, host, or provider effect |
| `POST /v1/deployment-runs/{deployment_run_id}/rollout/rollback` | Hold: direct deploy, host, or provider effect |
| `POST /v1/deployment-targets/{target_id}/auto-deploy` | Hold: direct deploy, host, or provider effect |
| `POST /v1/deployment-targets/{target_id}/bootstrap` | Hold: direct deploy, host, or provider effect |
| `POST /v1/deployment-targets/{target_id}/purge` | Hold: direct deploy, host, or provider effect |
| `POST /v1/deployment-targets/{target_id}/teardown` | Hold: direct deploy, host, or provider effect |
| `POST /v1/deployments/{deployment_run_id}/promote` | Hold: direct deploy, host, or provider effect |
| `POST /v1/discoveries/{run_id}/promote` | Hold: ID-only route lacks MCP session project binding |
| `POST /v1/docs/update/execute` | Hold: GitHub mutation or privileged job action |
| `POST /v1/enterprise/identity-providers` | Hold: changes agent or policy capability authority |
| `POST /v1/github/code-sync-runs/{sync_run_id}/apply` | Hold: GitHub mutation or privileged job action |
| `POST /v1/github/community/jobs/{job_id}/execute` | Hold: GitHub mutation or privileged job action |
| `POST /v1/github/connections/{connection_id}/revoke` | Hold: credentials, key grants, or integration revocation |
| `POST /v1/github/merge-authorizations/{authorization_id}/apply` | Hold: GitHub mutation or privileged job action |
| `POST /v1/github/merge-authorizations/{authorization_id}/authorize` | Hold: human approval or decision authority |
| `POST /v1/github/merge-authorizations/{authorization_id}/enqueue` | Hold: GitHub mutation or privileged job action |
| `POST /v1/github/reconciliation-runs/{run_id}/execute` | Hold: leased worker protocol, not operator invocation |
| `POST /v1/github/resolution-plans/{plan_id}/apply` | Hold: GitHub mutation or privileged job action |
| `POST /v1/github/resolution-plans/{plan_id}/approve` | Hold: human approval or decision authority |
| `POST /v1/goldeneye/apps/deploy` | Hold: direct deploy, host, or provider effect |
| `POST /v1/goldeneye/apps/{name}/rollback` | Hold: direct deploy, host, or provider effect |
| `POST /v1/improvement-programs/trials/{trial_id}/promote` | Hold: ID-only route lacks MCP session project binding |
| `POST /v1/kms/keys/{key_entry_id}/grant` | Hold: credentials, key grants, or integration revocation |
| `POST /v1/kms/leases/{lease_id}/revoke` | Hold: credentials, key grants, or integration revocation |
| `POST /v1/members` | Hold: break-glass or member authority |
| `POST /v1/observability/stack/deploy` | Hold: direct deploy, host, or provider effect |
| `POST /v1/orchestration/next-move/execute` | Hold: leased worker protocol, not operator invocation |
| `POST /v1/orgs` | Hold: break-glass or member authority |
| `POST /v1/platform-standards/apply` | Hold: changes agent or policy capability authority |
| `POST /v1/previews/{preview_id}/approve` | Hold: human approval or decision authority |
| `POST /v1/providers/{provider_account_id}/revoke` | Hold: credentials, key grants, or integration revocation |
| `POST /v1/providers/{provider_account_id}/rotate` | Hold: credentials, key grants, or integration revocation |
| `POST /v1/roles` | Hold: changes agent or policy capability authority |
| `POST /v1/work-orchestration/tracker-sync/execute` | Hold: leased worker protocol, not operator invocation |
| `POST /v1/worker-autoscaling/apply` | Hold: budget or autonomy authority |
| `POST /v1/workflow-runs/{run_id}/migrations/rollback` | Allow: paused-run rollback refuses executed steps; workflow_manage and event |

## Gate evidence for the two callable actions

- `DELETE /v1/tasks/{task_id}` is a soft archive. The API requires `project_id` and `actor`, checks `task_create`, updates only the matching project task, retains child rows, and emits `task.archived`. Existing `test_archive_task_requires_authorized_actor` checks a denied stranger and the persisted event.
- `POST /v1/workflow-runs/{run_id}/migrations/rollback` requires `project_id` and `actor`, checks `workflow_manage`, locks and verifies the project run, requires a paused run and active completed migration, refuses rollback if any new-version step executed, and emits `workflow.migration_rolled_back`. Existing workflow runtime tests exercise the rollback state transition.
- `ApiSurface.connect` accepts a bound principal only if `GET /v1/projects` returns exactly the selected project. Every invocation rechecks that project set and the API-key principal, so a long-lived MCP process fails closed after a membership or key-binding change.

## Remaining work

Discovery and improvement-trial promotion are otherwise governed, but their ID-only routes do not accept a project selector that MCP can pin; they remain unavailable. Agent-thread clearing retains history but lacks an audit event. Worker-only protocol endpoints should eventually be classified as excluded rather than operator actions. The other holds need their existing human approval, credential, provider, or deletion controls.
