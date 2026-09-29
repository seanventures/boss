# BOSS-429 MCP operator boundary audit

Source: the 77 previously held Community actions at integration commit `c218f77e`, reconciled against the current 986-operation mounted Community surface. The mounted matrix now has 853 API-policy actions, 64 exact-request human-confirmation actions, 2 held actions, and 67 excluded protocols. Product routes that previously stayed held now require exact human confirmation plus the route's actor, project, receipt, and, where the effect spends or destroys, a budget reservation, closed lease, or recorded rollback plan. The two remaining holds are internal infrastructure and are not part of the product release. Every historical action's current MCP disposition appears below; the matrix is authoritative for operations added since that snapshot.

| Operation | Current disposition |
|---|---|
| `DELETE /v1/channels/{provider}` | Hold: removes configuration, delivery, or graph state |
| `DELETE /v1/console/v2/org/{agent_id}` | Confirmable: removes only a project-local declared chart assignment; `role_review` and `org.assignment_cleared`/`org.structure_cleared` still apply |
| `DELETE /v1/delivery-workflows/custom-steps/{step_key}` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `DELETE /v1/delivery-workflows/runs/{run_id}` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `DELETE /v1/delivery-workflows/schedules/{schedule_id}` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `DELETE /v1/delivery-workflows/{definition_id}` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `DELETE /v1/deployment-targets/{target_id}` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `DELETE /v1/event-subscriptions/{subscription_id}` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `DELETE /v1/kms/keys/{key_entry_id}/grants/{grantee_agent}` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `DELETE /v1/mcp/servers/{server_id}` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `DELETE /v1/meeting-roles/{role_key}` | Confirmable: removes only a project-local meeting template role; `workflow_manage` and `meeting_role.deleted` still apply |
| `DELETE /v1/preview-sandboxes/{preview_id}` | Hold: direct deploy, host, or provider effect |
| `DELETE /v1/resource-topology/edges/{edge_id}` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `DELETE /v1/role-skills` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `DELETE /v1/sandboxes/{sandbox_id}` | Hold: direct deploy, host, or provider effect |
| `DELETE /v1/secret-refs/{secret_ref_id}` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `DELETE /v1/skill-definitions/{skill_key}` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `DELETE /v1/tasks/{task_id}` | Allow: project-scoped soft archive with task_create and task.archived event |
| `DELETE /v1/webhook-endpoints/{endpoint_id}` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `POST /v1/activity/retention/prune` | Excluded: internal/leased worker protocol; never an operator action |
| `POST /v1/agent-modules/{module_id}/rollback` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `POST /v1/agent/threads/{thread_id}/clear` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `POST /v1/break-glass/grants` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `POST /v1/break-glass/grants/{grant_id}/revoke` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `POST /v1/budget/allocations` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `POST /v1/budget/set_intensity` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `POST /v1/changesets/{changeset_id}/approve` | Confirmable: human reviewer is the domain actor; MCP member also needs `providerops_manage` |
| `POST /v1/code-tasks/{run_id}/deploy` | Hold: direct deploy, host, or provider effect |
| `POST /v1/console/v2/autonomy/apply` | Hold: budget or autonomy authority |
| `POST /v1/decisions/{decision_id}/approve` | Hold: domain record would attribute human decision or approval to the MCP agent |
| `POST /v1/decisions/{decision_id}/delegate` | Confirmable: human reviewer is the domain actor; MCP member also needs `decision_resolve` |
| `POST /v1/decisions/{decision_id}/execute` | Confirmable: human reviewer is the domain actor; MCP member also needs `decision_resolve` |
| `POST /v1/decisions/{decision_id}/reject` | Confirmable: human reviewer is the domain actor; MCP member also needs `decision_resolve` |
| `POST /v1/decisions/{decision_id}/revise` | Confirmable: human reviewer is the domain actor; MCP member also needs `decision_resolve` |
| `POST /v1/delivery/deploy` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `POST /v1/delivery/deployments/{deployment_run_id}/execute` | Hold: direct deploy, host, or provider effect |
| `POST /v1/delivery/deployments/{deployment_run_id}/redeploy` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
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
| `POST /v1/docs/update/execute` | Excluded: internal/leased worker protocol; never an operator action |
| `POST /v1/enterprise/identity-providers` | Hold: changes agent or policy capability authority |
| `POST /v1/github/code-sync-runs/{sync_run_id}/apply` | Excluded: internal/leased worker protocol; never an operator action |
| `POST /v1/github/community/jobs/{job_id}/execute` | Excluded: internal/leased worker protocol; never an operator action |
| `POST /v1/github/connections/{connection_id}/revoke` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `POST /v1/github/merge-authorizations/{authorization_id}/apply` | Excluded: internal/leased worker protocol; never an operator action |
| `POST /v1/github/merge-authorizations/{authorization_id}/authorize` | Confirmable: independent human reviewer is the domain actor; MCP member also needs `github_manage` |
| `POST /v1/github/merge-authorizations/{authorization_id}/enqueue` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `POST /v1/github/reconciliation-runs/{run_id}/execute` | Excluded: internal/leased worker protocol; never an operator action |
| `POST /v1/github/resolution-plans/{plan_id}/apply` | Excluded: internal/leased worker protocol; never an operator action |
| `POST /v1/github/resolution-plans/{plan_id}/approve` | Confirmable: independent human reviewer is the domain actor; MCP member also needs `github_manage` |
| `POST /v1/goldeneye/apps/deploy` | Hold: direct deploy, host, or provider effect |
| `POST /v1/goldeneye/apps/{name}/rollback` | Hold: direct deploy, host, or provider effect |
| `POST /v1/improvement-programs/trials/{trial_id}/promote` | Confirmable: creates a proposal only; target project is checked against the bound actor's `self_improvement_manage` grant, and rollout retains separate gates |
| `POST /v1/kms/keys/{key_entry_id}/grant` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `POST /v1/kms/leases/{lease_id}/revoke` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `POST /v1/members` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `POST /v1/observability/stack/deploy` | Hold: direct deploy, host, or provider effect |
| `POST /v1/orchestration/next-move/execute` | Excluded: internal/leased worker protocol; never an operator action |
| `POST /v1/orgs` | Hold: break-glass or member authority |
| `POST /v1/platform-standards/apply` | API policy: renders a standards text block; no external effect |
| `POST /v1/previews/{preview_id}/approve` | Hold: domain record would attribute human decision or approval to the MCP agent |
| `POST /v1/providers/{provider_account_id}/revoke` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `POST /v1/providers/{provider_account_id}/rotate` | Confirmable: separate human full-request review and digest approval; project API policy and audit still apply |
| `POST /v1/roles` | Hold: changes agent or policy capability authority |
| `POST /v1/work-orchestration/tracker-sync/execute` | Excluded: internal/leased worker protocol; never an operator action |
| `POST /v1/worker-autoscaling/apply` | Hold: budget or autonomy authority |
| `POST /v1/workflow-runs/{run_id}/migrations/rollback` | Allow: paused-run rollback refuses executed steps; workflow_manage and event |

## Confirmation boundary

The API accepts a proposal only from an authenticated, dedicated API-key principal whose active project access is exactly the requested project. It rejects stock `ceo`, `system`, and `executive_console` principals. A separate active human CEO with `decision_resolve` must submit the full independently reviewed request and its SHA-256 digest; the API recomputes the digest and compares it with the original proposal before approval. The gate binds project, principal, alias, method, target, query, and JSON body; it expires after five minutes and is consumed once in the database before dispatch. Four audit events distinguish request, approval, consumption, and API result. The persisted preview contains only method, target, field counts, a fixed set of field names, and `effect_actor: approving_human` for the seven human-attributed routes; free-form query/body values, including `note`, are not stored. The human review submission is not persisted. An approved digest never grants the MCP principal a missing API permission. The seven human-attributed routes check the MCP member's own permission at proposal and consumption, check the human reviewer again at consumption, and leave the domain's independent-review and role policy in force.

The approval-route gate classifies the authenticated principal from its active member records before dispatch, even when the request has no MCP headers. A nonhuman or ambiguously typed API-key, OIDC, or SAML identity cannot call an approval or other MCP-confirmation-gated action directly. This includes keys bound to the seeded `system` and `executive_console` members; neither identity is a human-review exemption. The installed MCP key must remain bound to a dedicated nonhuman member, while the console's human key or human OIDC/SAML session follows the ordinary API policy and audit path.

The transport boundary uses FastAPI's route-relative path when an ASGI `root_path` proxy prefix is present. This keeps API authentication, callback exemptions, operation matching, and the exact confirmation digest aligned with the mounted `/v1/...` route. A proxied request cannot skip auth by adding the prefix, and the prefix does not create a second approval identity for the same route.

## Held route families

- Direct host, deployment, provider, and Goldeneye effects lack a consistently enforced ChangeSet, budget reservation, resource receipt, or rollback path. Some deployment endpoints currently return `409` or suppress a host teardown error; confirmation alone would not make them safe.
- Host key installation/adoption can carry raw credential material. The current review summary intentionally does not persist arbitrary body text, so these actions lack a sound human review channel.
- Global organization, role, identity-provider, autonomy, and worker-autoscaling actions do not fit a project-scoped MCP principal. Several remaining deletion endpoints lack an API-side project actor check.
- Discovery promotion still uses an ID-only target and schedules budgeted work; it needs target-project proof and an audited budget/lease path through MCP before enabling.
- Decision approval and preview approval remain held because their follow-on handlers can execute an agent tool or queue merge/deployment work; exact human attribution alone does not qualify those effects for MCP.
- The nine worker routes are now `excluded`, rather than operator actions: GitHub execution/apply and reconciliation, docs update, orchestration next move, tracker sync, and retention prune. Their lease and authorization protocol belongs to workers.

## Remaining 28 Community actions

- **Channel revocation (1):** `DELETE /v1/channels/{provider}`. Govern both the remote token revocation and local disconnect with a dedupe key, receipt, partial-failure state, and explicit review of which channel is affected.
- **Direct deployment and host effects (15):** `POST /v1/code-tasks/{run_id}/deploy`, `POST /v1/delivery/deployments/{deployment_run_id}/execute`, `POST /v1/deployment-hosts/{host_id}/provision`, `POST /v1/deployment-hosts/{host_id}/retire`, `POST /v1/deployment-runs/{deployment_run_id}/rollout/rollback`, `POST /v1/deployment-targets/{target_id}/auto-deploy`, `POST /v1/deployment-targets/{target_id}/bootstrap`, `POST /v1/deployment-targets/{target_id}/purge`, `POST /v1/deployment-targets/{target_id}/teardown`, `POST /v1/deployments/{deployment_run_id}/promote`, `POST /v1/goldeneye/apps/deploy`, `POST /v1/goldeneye/apps/{name}/rollback`, `POST /v1/observability/stack/deploy`, `DELETE /v1/preview-sandboxes/{preview_id}`, and `DELETE /v1/sandboxes/{sandbox_id}`. Require a project-bound ChangeSet, applicable budget reservation and approval, rollback plan, resource receipt, and visible failure state at the actual effect boundary.
- **Host key material (3):** `POST /v1/deployment-hosts/{host_id}/adopt-key`, `POST /v1/deployment-hosts/{host_id}/install-key`, and `POST /v1/deployment-hosts/{host_id}/key`. Move raw key material into a reviewed SecretRef flow; the MCP confirmation preview intentionally does not store the secret body.
- **Global authority and spend policy (6):** `POST /v1/console/v2/autonomy`, `POST /v1/console/v2/autonomy/apply`, `POST /v1/enterprise/identity-providers`, `POST /v1/orgs`, `POST /v1/roles`, and `POST /v1/worker-autoscaling/apply`. Define a human, scope-appropriate admin boundary; project MCP membership cannot grant org-wide, role, model-spend, or autoscaling authority.
- **Approval with follow-on execution (2):** `POST /v1/decisions/{decision_id}/approve` and `POST /v1/previews/{preview_id}/approve`. Prove that approval cannot immediately execute a tool or queue provider work without its own budget, lease, ChangeSet, receipt, and independent human attribution checks.
- **Budgeted Discovery promotion (1):** `POST /v1/discoveries/{run_id}/promote`. Bind the ID-only target to the MCP session project and qualify the downstream meeting job's reservation, lease, receipt, and retry path.

The separate [Community matrix](../community/mcp-api-capability-matrix.json) lists every mounted operation and its current status. Exact availability still depends on the live edition, member grants, and API policy.
