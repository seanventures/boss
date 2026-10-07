# BOSS Identity: permission catalogue (draft)

Status: DRAFT for operator review, 2026-10-06. Companion to [IDENTITY.md](IDENTITY.md).

> **Phase 0 (2026-10-06): §2–§4 now live in code.** The source of truth is `packages/identity/catalogue.yaml`; the published table is the generated [IDENTITY_CATALOGUE.md](IDENTITY_CATALOGUE.md). The YAML was converted from this draft unchanged (180 permissions; Owner 175, Admin 166, Operator 83, Viewer 46, Auditor 59). §5–§8 (route, tool and page mappings) remain the draft that stream S2 hand-checks into declarations (IDENTITY_BUILD.md §4, S2). §5 gains the email sign-in and self sign-up routes of IDENTITY.md §10, all `public:auth_endpoint`.

This is the first draft of the one catalogue every BOSS API route, MCP tool, console page and console action will declare against. When the build starts it moves into code as `packages/identity/catalogue.yaml`. The Python enum, the TypeScript union, the database seed and the built-in role table are all generated from that file (IDENTITY.md §6.2). The tables below were generated from a draft of that file. The route mapping was produced by running draft rules over every route in `apps/api/app/main.py` and `apps/api/app/routers/*` at b1befb7d.

## 1. Conventions

- **Key format:** `resource:action`, lowercase snake_case, matching `^[a-z][a-z_]*:[a-z][a-z_]*$`. One resource per BOSS object a person would name: a build is `code_task`, a workflow run is `workflow_run`.
- **No wildcards anywhere.** Neither `*` nor `resource:*` exists, in storage or in matching. Built-in roles are explicit lists generated from the `roles` column of each entry. In the role editor, "select all in a resource" expands to explicit keys when you save. So a permission added in a later BOSS release never reaches a custom role silently. A seeder that grants a wildcard, or two near-identical wildcards that drift apart, cannot happen here.
- **Actions are named for what they risk, not for the HTTP verb.** `read`, `create`, `update`, `delete` and `manage` are the common ones. Dangerous effects get their own action, such as `deployment:production`, `code_task:override`, `host:exec`, `agent_ability:assign` and `owner:assign`, so a broader permission never implies them.
- **Flags:**
  - **step-up:** the session must have passed MFA in the last 10 minutes (IDENTITY.md §4.6).
  - **delegable:** the permission may appear in an agent Passport ceiling or a delegation grant. Anything not marked delegable is human-only, whatever the agent's chain holds. Approvals, identity administration and secret management are never delegable.
  - **org-only:** the permission can only be bound at org scope, never per project.
  - **system-only:** no human role holds it. Only the built-in system service accounts do: `worker`, `collector` and `scim`.
  - **critical:** shown in red in the role editor, needs a typed confirmation to grant, and is never in Viewer or Auditor.
- **Role letters:** O = Owner, A = Admin, P = Operator, V = Viewer, U = Auditor.

**Totals:** 180 permissions in 10 categories. Owner holds 175, Admin 166, Operator 83, Viewer 46 and Auditor 59. Five are system-only.

## 2. The catalogue, with the built-in role → permission table

### Identity and access

| Permission | What it allows | O | A | P | V | U | Flags |
|---|---|:-:|:-:|:-:|:-:|:-:|---|
| `directory:read` | See the names, avatars and kinds of people, groups and agents (no security detail). | ● | ● | ● | ● | ● | delegable, org-only |
| `user:read` | List people and open a profile: roles, groups, MFA status, sessions, effective access. | ● | ● |  |  | ● | org-only |
| `user:invite` | Invite a person by email or create a local account. | ● | ● |  |  |  | step-up, org-only |
| `user:update` | Edit a person's name, email and profile fields. | ● | ● |  |  |  | step-up, org-only |
| `user:disable` | Disable or re-enable a person. Disabling revokes their sessions and suspends their grants. | ● | ● |  |  |  | step-up, org-only, critical |
| `user:delete` | Erase a disabled person. Their audit history stays, as a tombstone. | ● |  |  |  |  | step-up, org-only, critical |
| `user:reset_mfa` | Remove a person's MFA factors and issue a one-time recovery link. | ● | ● |  |  |  | step-up, org-only, critical |
| `user:revoke_sessions` | Sign a person out of every device. | ● | ● |  |  |  | step-up, org-only |
| `group:read` | List groups and their members. | ● | ● |  |  | ● | org-only |
| `group:manage` | Create, rename and delete groups. | ● | ● |  |  |  | step-up, org-only |
| `group:manage_members` | Add and remove group members (manual groups only; IdP groups are read-only). | ● | ● |  |  |  | step-up, org-only |
| `access_role:read` | See access roles and their permissions. | ● | ● | ● | ● | ● | org-only |
| `access_role:manage` | Create, edit, clone and delete custom access roles. Never more than you hold. | ● | ● |  |  |  | step-up, org-only, critical |
| `role_binding:read` | See who holds which access role, where. | ● | ● |  |  | ● | org-only |
| `role_binding:assign` | Grant or remove an access role for a person, group, service account or agent. Never more than you hold. | ● | ● |  |  |  | step-up, critical |
| `owner:assign` | Make someone an Owner, or remove an Owner. The last Owner can never be removed. | ● |  |  |  |  | step-up, org-only, critical |
| `service_account:read` | List service accounts, their owners and their keys' metadata. | ● | ● |  |  | ● | org-only |
| `service_account:manage` | Create, edit, disable and delete service accounts. | ● | ● |  |  |  | step-up, org-only, critical |
| `service_account_key:manage` | Create, rotate and revoke a service account's API keys. | ● | ● |  |  |  | step-up, org-only, critical |
| `api_key:manage_own` | Create, rotate and revoke your own personal API keys, within your own access. | ● | ● | ● | ● | ● | step-up, org-only |
| `api_key:read` | See every API key's metadata: owner, scopes, last use, expiry. Never the key. | ● | ● |  |  | ● | org-only |
| `api_key:revoke` | Revoke anyone's API key. | ● | ● |  |  |  | step-up, org-only, critical |
| `session:read` | See everyone's active sessions: device, place, sign-in method. | ● | ● |  |  | ● | org-only |
| `session:revoke` | End anyone's session. | ● | ● |  |  |  | step-up, org-only |
| `agent_passport:read` | Open an agent's Passport: sponsor, ceiling, lineage, status. | ● | ● | ● | ● | ● | delegable |
| `agent_passport:manage` | Change an agent's ceiling, sponsor, spawn rules and depth limit. | ● | ● |  |  |  | step-up, org-only, critical |
| `agent_passport:suspend` | Suspend or resume an agent. Suspending stops its runs at their next call. | ● | ● |  |  |  | step-up, org-only |
| `agent_passport:revoke` | Revoke an agent for good. Cascades to its grants, tokens and descendants. | ● | ● |  |  |  | step-up, org-only, critical |
| `delegation:grant` | Let an agent act for you, inside your own access and the agent's ceiling. | ● | ● | ● |  |  | step-up |
| `delegation:read_all` | See every delegation grant in the org, and its lineage. | ● | ● |  |  | ● | org-only |
| `delegation:revoke_any` | Revoke anyone's delegation grant (cascades to runs and sub-agents). | ● | ● |  |  |  | step-up, org-only, critical |
| `connection:use_own` | Link your own external accounts (GitHub, Google) into the vault for your agents to use. | ● | ● | ● |  |  | step-up |
| `connection:read_all` | See every vaulted external connection's metadata. | ● | ● |  |  | ● | org-only |
| `connection:revoke_any` | Disconnect anyone's external connection, and revoke it at the provider. | ● | ● |  |  |  | step-up, org-only, critical |
| `identity_provider:read` | See SSO providers (OIDC, SAML), SCIM and their status. | ● | ● |  |  | ● | org-only |
| `identity_provider:manage` | Add, change and remove SSO providers and SCIM tokens; enforce SSO. | ● |  |  |  |  | step-up, org-only, critical |
| `security_policy:read` | See the sign-in, MFA, session and delegation policy. | ● | ● |  |  | ● | org-only |
| `security_policy:manage` | Change the sign-in, MFA, session and delegation policy. | ● |  |  |  |  | step-up, org-only, critical |
| `audit:read` | Read the security audit log. | ● | ● |  |  | ● | org-only |
| `audit:export` | Export the security audit log and verify its hash chain. | ● | ● |  |  | ● | step-up, org-only |
| `scim:provision` | Provision users and groups over SCIM 2.0. |  |  |  |  |  | system-only, org-only |

### Org and projects

| Permission | What it allows | O | A | P | V | U | Flags |
|---|---|:-:|:-:|:-:|:-:|:-:|---|
| `org:read` | See the org's name and settings. | ● | ● | ● | ● | ● | org-only |
| `org:update` | Rename the org and change org-wide settings. | ● | ● |  |  |  | step-up, org-only |
| `org:delete` | Delete the org and everything in it. | ● |  |  |  |  | step-up, org-only, critical |
| `project:read` | See a project and its settings. | ● | ● | ● | ● | ● | delegable |
| `project:create` | Create a project. | ● | ● |  |  |  | step-up, org-only |
| `project:update` | Change a project's settings (builds at once, defaults, preflight). | ● | ● |  |  |  | step-up |
| `project:delete` | Delete a project. | ● |  |  |  |  | step-up, org-only, critical |
| `settings:read` | Read BOSS settings (non-secret values). | ● | ● | ● | ● | ● |  |
| `settings:update` | Change BOSS settings. | ● | ● |  |  |  | step-up |

### Factory and code

| Permission | What it allows | O | A | P | V | U | Flags |
|---|---|:-:|:-:|:-:|:-:|:-:|---|
| `code_task:read` | See builds, their activity, gates, chain, live view and retrospective. | ● | ● | ● | ● | ● | delegable |
| `code_task:create` | Start a build. | ● | ● | ● |  |  | delegable |
| `code_task:steer` | Steer, follow up, comment on and resolve review comments on a build. | ● | ● | ● |  |  | delegable |
| `code_task:cancel` | Cancel a build (throws away work in flight). | ● | ● | ● |  |  |  |
| `code_task:retry` | Retry or extend a build (spends budget). | ● | ● | ● |  |  | delegable |
| `code_task:override` | Override or waive a build's quality gates. | ● | ● |  |  |  | step-up, critical |
| `repository:read` | See registered repositories and their health. | ● | ● | ● | ● | ● | delegable |
| `repository:register` | Register a repository with BOSS. | ● | ● |  |  |  | step-up |
| `repository:manage` | Change or remove repository bindings and the default repo. | ● | ● |  |  |  | step-up |
| `github:read` | See GitHub connections, branches, leases, sync and merge state. | ● | ● | ● | ● | ● | delegable |
| `github:connect` | Connect a GitHub App or account to the org. | ● | ● |  |  |  | step-up, critical |
| `github:manage` | Enable, disable, test and revoke GitHub connections; edit branch policies. | ● | ● |  |  |  | step-up, critical |
| `github:sync` | Plan code sync, reconciliation and conflict resolution. | ● | ● | ● |  |  | delegable |
| `github:merge` | Authorize a merge into a protected branch. | ● | ● | ● |  |  | step-up |
| `preview:read` | See live previews and their logs and gates. | ● | ● | ● | ● | ● | delegable |
| `preview:manage` | Start, stop, restart and smoke-test previews. | ● | ● | ● |  |  | delegable |
| `preview:approve` | Approve a preview or leave preview feedback. | ● | ● | ● |  |  |  |
| `artifact:read` | Open artifacts, media and PDFs. | ● | ● | ● | ● | ● | delegable |
| `artifact:rebuild` | Rebuild an artifact. | ● | ● | ● |  |  | delegable |

### Workflows and work

| Permission | What it allows | O | A | P | V | U | Flags |
|---|---|:-:|:-:|:-:|:-:|:-:|---|
| `workflow:read` | See workflow definitions, specs, plans, readiness and costs. | ● | ● | ● | ● | ● | delegable |
| `workflow:create` | Draft, save, fork and wire workflows and custom steps. | ● | ● | ● |  |  | delegable |
| `workflow:publish` | Publish a workflow version so it can run. | ● | ● | ● |  |  | step-up |
| `workflow:install` | Install a workflow from the catalogue. | ● | ● | ● |  |  |  |
| `workflow:delete` | Delete a workflow definition. | ● | ● |  |  |  | step-up |
| `workflow_run:read` | See workflow runs, steps, reports and chains. | ● | ● | ● | ● | ● | delegable |
| `workflow_run:start` | Start a workflow run. | ● | ● | ● |  |  | delegable |
| `workflow_run:control` | Pause, resume, retry, skip, recheck, give input to and cancel runs. | ● | ● | ● |  |  | delegable |
| `workflow_run:compensate` | Compensate or migrate a run (undo its effects). | ● | ● |  |  |  | step-up, critical |
| `workflow_run:delete` | Delete a run. | ● | ● |  |  |  | step-up |
| `workflow_schedule:manage` | Create, enable, disable and delete workflow schedules. | ● | ● | ● |  |  |  |
| `meeting:read` | See panels, minutes, evidence and conversations. | ● | ● | ● | ● | ● | delegable |
| `meeting:convene` | Convene a panel (PRD, go/no-go, stand-up) or a conversation. | ● | ● | ● |  |  | delegable |
| `meeting:configure` | Change who sits at the table (meeting roles). | ● | ● |  |  |  |  |
| `discovery:read` | Read research dossiers. | ● | ● | ● | ● | ● | delegable |
| `discovery:create` | Start, deepen and promote research. | ● | ● | ● |  |  | delegable |
| `work_item:read` | See the task board, work plane, allocation, releases and plans. | ● | ● | ● | ● | ● | delegable |
| `work_item:manage` | Create and change tasks, initiatives, cycles, releases, quotas and plans. | ● | ● | ● |  |  | delegable |
| `job:read` | See background jobs and the queue. | ● | ● | ● | ● | ● |  |
| `job:control` | Cancel and reprioritise jobs. | ● | ● | ● |  |  |  |
| `deadletter:manage` | Retry, requeue and remediate dead letters. | ● | ● |  |  |  |  |
| `assistant:chat` | Talk to the BOSS Agent and the chat agents. | ● | ● | ● |  |  |  |
| `autopilot:read` | See captain, shepherd, process-manager and focused-agent reasoning. | ● | ● | ● | ● | ● |  |
| `autopilot:operate` | Start, wake, pause and tick the captain, shepherd and focused agents. | ● | ● | ● |  |  |  |
| `autopilot:configure` | Compile playbooks and change autopilot behaviour. | ● | ● |  |  |  | step-up |
| `memory:read` | Read what BOSS remembers. | ● | ● | ● | ● | ● | delegable |
| `memory:manage` | Review and forget memories. | ● | ● | ● |  |  | delegable |
| `feedback:submit` | Send feedback and check-ins. | ● | ● | ● | ● | ● | delegable |

### Approvals

| Permission | What it allows | O | A | P | V | U | Flags |
|---|---|:-:|:-:|:-:|:-:|:-:|---|
| `decision:read` | See the approvals inbox and decision detail. | ● | ● | ● | ● | ● | delegable |
| `decision:approve` | Approve or reject a decision, within the risk tier your seat allows. Humans only. | ● | ● | ● |  |  | step-up |
| `decision:delegate` | Hand a decision to another person. | ● | ● |  |  |  |  |
| `decision:execute` | Execute an approved decision. | ● | ● | ● |  |  |  |
| `approval_rule:manage` | Change approval rules and risk-tier seats. | ● |  |  |  |  | step-up, critical |

### Agents

| Permission | What it allows | O | A | P | V | U | Flags |
|---|---|:-:|:-:|:-:|:-:|:-:|---|
| `agent:read` | See agents, their cards, prompts, instructions, versions and activity. | ● | ● | ● | ● | ● | delegable |
| `agent:create` | Draft a new agent or a new agent version. | ● | ● | ● |  |  |  |
| `agent:update` | Edit an agent's persona, instructions and modules. | ● | ● |  |  |  | step-up |
| `agent:publish` | Publish an agent or panel version so it can take work. | ● | ● |  |  |  | step-up, critical |
| `agent:lifecycle` | Pause, retire and re-seal agents. | ● | ● |  |  |  | step-up |
| `agent:test` | Run an agent's tests or a sandboxed trial. | ● | ● | ● |  |  | delegable |
| `agent_ability:assign` | Give an agent a skill or an MCP server (grants authority). | ● | ● |  |  |  | step-up, critical |
| `autonomy:read` | See earned-autonomy scores and trust evidence. | ● | ● | ● | ● | ● |  |
| `autonomy:manage` | Raise or lower an agent's autonomy; record trust evidence. | ● | ● |  |  |  | step-up, critical |
| `skill:read` | See skills, definitions, invocations and the Kit. | ● | ● | ● | ● | ● | delegable |
| `skill:manage` | Import, enable, disable and edit skills; connect Kit abilities. | ● | ● |  |  |  | step-up |
| `skill:invoke` | Invoke a skill or try a Kit ability. | ● | ● | ● |  |  | delegable |
| `mcp_server:read` | See MCP servers and the MCP catalogue. | ● | ● | ● | ● | ● | delegable |
| `mcp_server:register` | Register an MCP server (lands switched off and unreviewed). | ● | ● | ● |  |  |  |
| `mcp_server:review` | Review, enable and set the policy of an MCP server. | ● | ● |  |  |  | step-up, critical |
| `pack:read` | See packs and agent modules. | ● | ● | ● | ● | ● |  |
| `pack:install` | Install, roll back and uninstall packs and agent modules. | ● | ● |  |  |  | step-up, critical |

### Deploy and infra

| Permission | What it allows | O | A | P | V | U | Flags |
|---|---|:-:|:-:|:-:|:-:|:-:|---|
| `deployment:read` | See deployments, runs, promotions, targets and the pipeline. | ● | ● | ● | ● | ● | delegable |
| `deployment:create` | Deploy to a non-production target. | ● | ● | ● |  |  | delegable |
| `deployment:production` | Deploy or promote to a production target. | ● | ● |  |  |  | step-up, critical |
| `deployment:rollback` | Roll back a deployment. | ● | ● | ● |  |  | step-up |
| `deployment_target:manage` | Create, change, bootstrap, purge and tear down targets and their secrets. | ● | ● |  |  |  | step-up, critical |
| `host:read` | See deployment hosts and their capacity. | ● | ● | ● | ● | ● | delegable |
| `host:manage` | Provision, attach, verify, re-key and retire hosts. | ● | ● |  |  |  | step-up, critical |
| `host:exec` | Run commands on a host or in an investigation shell. | ● | ● |  |  |  | step-up, critical |
| `sandbox:read` | See sandboxes, their files, logs and stats. | ● | ● | ● | ● |  | delegable |
| `sandbox:exec` | Execute a command in a sandbox. | ● | ● |  |  |  | step-up, critical |
| `sandbox:manage` | Preserve and delete sandboxes. | ● | ● |  |  |  |  |
| `infra:read` | See cloud, fleet, resources, topology and drift. | ● | ● | ● | ● | ● | delegable |
| `infra:provision` | Provision capacity: droplets, elastic pools, IaC apply. | ● | ● |  |  |  | step-up, critical |
| `infra:terminate` | Terminate resources and run the janitor. | ● | ● |  |  |  | step-up, critical |
| `cloud_account:manage` | Link and unlink cloud accounts. | ● |  |  |  |  | step-up, org-only, critical |
| `changeset:approve` | Approve and apply an infrastructure changeset. | ● | ● |  |  |  | step-up, critical |
| `cicd:read` | See CI/CD catalogue and status. | ● | ● | ● | ● | ● |  |
| `cicd:manage` | Generate, validate and run CI/CD pipelines. | ● | ● | ● |  |  |  |

### Models, spend and policy

| Permission | What it allows | O | A | P | V | U | Flags |
|---|---|:-:|:-:|:-:|:-:|:-:|---|
| `setup:read` | See the Repo and models checklist. | ● | ● | ● | ● | ● |  |
| `setup:manage` | Choose the repo, models and coding agents during setup. | ● | ● |  |  |  | step-up |
| `model_provider:read` | See model sources, providers, usage, health and SLOs. | ● | ● | ● | ● | ● | delegable |
| `model_provider:manage` | Add, enable, disable, test and remove model providers. | ● | ● |  |  |  | step-up, critical |
| `model_provider:rotate` | Rotate a provider's credential. | ● | ● |  |  |  | step-up, critical |
| `entitlement:read` | See entitlements and pools. | ● | ● | ● | ● | ● |  |
| `entitlement:manage` | Connect entitlement sources and their credentials. | ● | ● |  |  |  | step-up, critical |
| `budget:read` | See budget status, forecasts, spend and FinOps. | ● | ● | ● | ● | ● | delegable |
| `budget:manage` | Set caps, intensity and initiative budgets. | ● | ● |  |  |  | step-up |
| `spend:reserve` | Reserve, consume and release budget for work (runtime only). |  |  |  |  |  | delegable, system-only |
| `policy:read` | See safety, standards, concurrency, token and environment policies. | ● | ● | ● | ● | ● | delegable |
| `policy:manage` | Change safety, standards, concurrency, token and environment policies. | ● | ● |  |  |  | step-up, critical |
| `pipeline:read` | See coding-pipeline profiles and adapters. | ● | ● | ● | ● | ● |  |
| `pipeline:manage` | Change coding-pipeline profiles and propose pipeline config. | ● | ● |  |  |  | step-up |

### Secrets and integrations

| Permission | What it allows | O | A | P | V | U | Flags |
|---|---|:-:|:-:|:-:|:-:|:-:|---|
| `secret:read_metadata` | See secret references: name, backend, status. Never the value. | ● | ● | ● |  |  | delegable |
| `secret:manage` | Create and verify secrets and key pairs. | ● | ● |  |  |  | step-up, critical |
| `kms_key:manage` | Create KMS keys and grant or revoke agent access. | ● | ● |  |  |  | step-up, critical |
| `secret:lease` | Lease a secret for a running step (runtime only). |  |  |  |  |  | delegable, system-only |
| `integration:read` | See integrations, work trackers, channels, webhooks and subscriptions. | ● | ● | ● | ● | ● | delegable |
| `integration:manage` | Connect and change integrations, trackers, channels, webhooks and subscriptions. | ● | ● |  |  |  | step-up, critical |
| `comms:read` | See the comms inbox, outbox and policies. | ● | ● | ● | ● | ● | delegable |
| `comms:send` | Queue and deliver an outbound message. | ● | ● | ● |  |  | delegable |
| `comms:manage` | Change comms policies and email configuration. | ● | ● |  |  |  | step-up |

### Operations and assurance

| Permission | What it allows | O | A | P | V | U | Flags |
|---|---|:-:|:-:|:-:|:-:|:-:|---|
| `system:read` | See health, status, diagnostics, the API catalogue and the edition. | ● | ● | ● | ● | ● | delegable |
| `system:operate` | Run System Doctor actions, sweeps, ticks, backfills and refreshes. | ● | ● |  |  |  | step-up |
| `observability:read` | See apps, models, sources, LLM calls, traces and metrics. | ● | ● | ● | ● | ● | delegable |
| `observability:manage` | Deploy the observability stack and probe apps. | ● | ● |  |  |  |  |
| `reliability:read` | See incidents, pages, on-call, the knowledge graph and AI SRE. | ● | ● | ● | ● | ● | delegable |
| `reliability:respond` | Acknowledge, diagnose, remediate and post-mortem incidents; run runbooks. | ● | ● | ● |  |  | delegable |
| `reliability:manage` | Change on-call policies, sources, schedules; run chaos. | ● | ● |  |  |  | step-up |
| `assurance:read` | See assurance cases, gates and security scans. | ● | ● | ● | ● | ● | delegable |
| `assurance:manage` | Add claims, evidence, policies; evaluate and transfer surfaces. | ● | ● |  |  |  |  |
| `assurance:run` | Run security scans, evals, benchmarks and verification gates. | ● | ● | ● |  |  | delegable |
| `improvement:read` | See self-improvement proposals, programs and experiments. | ● | ● | ● | ● | ● |  |
| `improvement:manage` | Canary, roll out, promote and roll back improvements and experiments. | ● | ● |  |  |  | step-up, critical |
| `activity:read` | Read the activity log, events, receipts and change journal. | ● | ● | ● | ● | ● | delegable |
| `activity:export` | Export the activity log and replay bundles. | ● | ● |  |  | ● | step-up |
| `activity:prune` | Prune activity past retention. | ● |  |  |  |  | step-up, org-only, critical |
| `telemetry:read` | See the telemetry collector and what is shared. | ● | ● |  |  | ● |  |
| `telemetry:share` | Share telemetry bundles outside this install. | ● | ● |  |  |  | step-up, critical |
| `telemetry:manage` | Rotate, forget and delete telemetry. | ● | ● |  |  |  | step-up |
| `telemetry:ingest` | Send collector telemetry (machine only). |  |  |  |  |  | system-only |
| `worker:lease` | Poll, heartbeat and complete leased jobs (worker protocol). |  |  |  |  |  | system-only |

## 3. Built-in roles

Access roles are a different thing from BOSS agent roles (Forge's job, Vera's job). In code they are `access_role`. In the console they are "Access roles". The word "role" on its own keeps its Agent Studio meaning.

| Access role | Key | Bindable at | In one line | Cannot |
|---|---|---|---|---|
| Owner | `owner` | org | Everything a human can hold. At least one active Owner always exists. | Nothing a human can hold is withheld. Owners still step up for critical actions, and still cannot approve their own decision or delegate approvals. |
| Admin | `admin` | org, project | Runs the install: people, roles, agents, connections, infra, policy. | Assign or remove Owners, change SSO or the security policy, delete the org or a project, link cloud accounts, prune activity, change approval rules. |
| Operator | `operator` | org, project | Does the work: builds, workflows, panels, research, approvals within their seat, previews, non-production deploys. Lets agents act for them. | Change any setting, policy, agent definition, connection or secret. Deploy to production. Override gates. Manage people. |
| Viewer | `viewer` | org, project | Reads every non-sensitive page. | Write anything, read secret metadata, read the security audit or other people's sessions. |
| Auditor | `auditor` | org | Viewer plus the security audit log, identity configuration (read), sessions, keys and grants metadata. Built for compliance reviews. | Write anything. |

**System service accounts.** They are created at install, cannot sign in to the console, and their roles cannot be edited:

| Service account | Role | Holds | Replaces |
|---|---|---|---|
| `sa_worker` | `system_worker` | `worker:lease`, `spend:reserve`, `secret:lease` | `BOSS_WORKER_PRINCIPAL` and the shared key on worker protocols |
| `sa_collector` | `system_collector` | `telemetry:ingest` | collector machine POSTs |
| `sa_scim_<provider>` | `system_scim` | `scim:provision` | `BOSS_ENTERPRISE_SCIM_BEARER_TOKENS` |

A worker leasing a job never acts with its own authority on the job's effects. Each job carries the run token of the principal it runs for (IDENTITY.md §7.14). `worker:lease` only lets the worker pick the job up.

## 4. Agent Passport ceiling presets

A Passport ceiling is the most any person can delegate to that agent. The ceiling is a set of catalogue keys drawn only from the delegable permissions. It is preset per agent role, and Admins can edit it. These are the defaults for BOSS's seeded agents:

| Preset | Seeded agents | BOSS permissions in the ceiling | External ceiling |
|---|---|---|---|
| Builder | Forge, Strata, Prism, Helm | `code_task:read`, `code_task:create`, `code_task:steer`, `code_task:retry`, `repository:read`, `github:read`, `github:sync`, `preview:read`, `preview:manage`, `artifact:read`, `artifact:rebuild`, `workflow:read`, `workflow_run:read`, `skill:read`, `skill:invoke`, `memory:read`, `memory:manage`, `spend:reserve`, `secret:lease`, `sandbox:read` | GitHub: contents write and pull_requests write on the run's repo, pushing only to `boss/*` branches |
| Reviewer | Vera, Rook, Sentry, Weave, Muse | `code_task:read`, `code_task:steer`, `repository:read`, `github:read`, `preview:read`, `artifact:read`, `assurance:read`, `assurance:run`, `workflow_run:read`, `skill:read`, `skill:invoke`, `memory:read`, `spend:reserve` | GitHub: contents read, pull_requests write (comments) on the run's repo |
| Planner | Juno, Atlas, Keystone, Rally, Vector, Cadence | `code_task:read`, `code_task:create`, `meeting:read`, `meeting:convene`, `discovery:read`, `discovery:create`, `workflow:read`, `workflow:create`, `workflow_run:read`, `workflow_run:start`, `work_item:read`, `work_item:manage`, `decision:read`, `memory:read`, `memory:manage`, `spend:reserve` | GitHub: contents read; issues write |
| Operator agent | Harbor, Pilot, Bedrock, Relay, Beacon | `deployment:read`, `deployment:create`, `host:read`, `infra:read`, `observability:read`, `reliability:read`, `reliability:respond`, `preview:read`, `preview:manage`, `workflow_run:read`, `workflow_run:control`, `spend:reserve`, `secret:lease` | none by default. `deployment:production` is never delegable. |
| Analyst | Ledger, Meter, Conduit, Sweep | `budget:read`, `model_provider:read`, `infra:read`, `observability:read`, `activity:read`, `spend:reserve` | none |
| Writer and publisher | Quill, Herald, Splice | `artifact:read`, `artifact:rebuild`, `discovery:read`, `comms:read`, `comms:send`, `memory:read`, `spend:reserve` | a channel connection through a grant, such as YouTube upload |
| Guardian | Warden, Vault, Halo | `agent:read`, `agent_passport:read`, `activity:read`, `policy:read`, `secret:read_metadata` | none. These agents watch. They hold nothing that writes. |

`decision:approve`, `deployment:production`, `code_task:override`, `host:exec`, `agent_ability:assign` and every identity, secret-management and policy permission are **not delegable**. No ceiling or grant can contain them, and the token service refuses to mint a token carrying them.

## 5. New identity API → permission

These routes do not exist yet. They are specified in IDENTITY.md §10.

| Method | Path | Declaration |
|---|---|---|
| POST | `/v1/auth/login · /v1/auth/mfa/* · /v1/auth/webauthn/*` | public: rate-limited, CSRF-checked, anti-enumeration |
| POST | `/v1/auth/password/forgot · /v1/auth/password/reset` | public: rate-limited; reset token single-use |
| GET/POST | `/v1/auth/sso/{provider_id}/start · /callback` | public: state + nonce + PKCE bound to the pre-auth cookie |
| POST | `/v1/auth/setup/claim` | public: one-time setup token or the legacy password; returns 404 once an Owner exists |
| POST | `/v1/auth/invitations/{token}/accept` | public: single-use invitation token |
| POST | `/v1/auth/logout · /v1/auth/step-up` | authenticated |
| GET | `/v1/identity/me · /me/permissions` | authenticated |
| * | `/v1/identity/me/{password,mfa,passkeys,recovery-codes,sessions,identities}` | authenticated (self) + step-up for writes |
| * | `/v1/identity/me/api-keys` | api_key:manage_own |
| * | `/v1/identity/me/connections` | connection:use_own |
| POST | `/v1/identity/me/delegations` | delegation:grant |
| GET/DELETE | `/v1/identity/me/delegations/{id}` | authenticated (own grant only; 404 otherwise) |
| GET | `/v1/identity/users · /users/{id}` | user:read |
| POST | `/v1/identity/users (invite)` | user:invite |
| PATCH | `/v1/identity/users/{id}` | user:update |
| POST | `/v1/identity/users/{id}/disable · /enable` | user:disable |
| DELETE | `/v1/identity/users/{id}` | user:delete |
| POST | `/v1/identity/users/{id}/reset-mfa` | user:reset_mfa (target Owner ⇒ caller must be Owner) |
| POST | `/v1/identity/users/{id}/revoke-sessions` | user:revoke_sessions |
| GET | `/v1/identity/principals/{id}/effective` | user:read (or self) |
| GET / * | `/v1/identity/groups[...]` | group:read / group:manage / group:manage_members |
| GET / * | `/v1/identity/access-roles[...]` | access_role:read / access_role:manage |
| POST | `/v1/identity/access-roles/{id}/preview` | access_role:read |
| GET / * | `/v1/identity/role-bindings[...]` | role_binding:read / role_binding:assign (owner role ⇒ owner:assign) |
| GET / * | `/v1/identity/service-accounts[...]` | service_account:read / service_account:manage |
| * | `/v1/identity/service-accounts/{id}/keys[...]` | service_account_key:manage |
| GET / POST | `/v1/identity/api-keys · /{id}/revoke` | api_key:read / api_key:revoke |
| GET / DELETE | `/v1/identity/sessions[...]` | session:read / session:revoke |
| GET | `/v1/identity/agents/{agent_id}/passport · /lineage` | agent_passport:read |
| PATCH | `/v1/identity/agents/{agent_id}/passport` | agent_passport:manage |
| POST | `/v1/identity/agents/{agent_id}/suspend · /resume` | agent_passport:suspend |
| POST | `/v1/identity/agents/{agent_id}/revoke` | agent_passport:revoke |
| GET | `/v1/identity/delegations` | delegation:read_all |
| POST | `/v1/identity/delegations/{id}/revoke` | delegation:revoke_any (or the granting user) |
| GET / POST | `/v1/identity/connections[...]` | connection:read_all / connection:revoke_any |
| GET / * | `/v1/identity/providers[...]` | identity_provider:read / identity_provider:manage |
| GET / PUT | `/v1/identity/policy` | security_policy:read / security_policy:manage |
| GET | `/v1/identity/audit · /audit/{id}` | audit:read |
| POST | `/v1/identity/audit/export · /audit/verify` | audit:export |
| GET | `/v1/identity/posture` | user:read |
| GET | `/v1/identity/catalogue` | authenticated |
| POST | `/v1/identity/token` | run-token or actor-token holders only (RFC 8693 exchange; not a catalogue permission) |
| GET | `/.well-known/boss-jwks.json` | public: public keys only |

## 6. Route → permission mapping (sample)

Draft rules map **all 1144 method+path pairs** at b1befb7d (main.py and 49 routers) to a declaration. No route is left unmapped:

- 1061 routes need a catalogue permission held by a human role.
- 55 are system-only: the worker lease protocol, collector ingest, SCIM, runtime spend and secret leases, and host-only bootstrap.
- 16 need only an authenticated principal. These are the composite console views. Each one filters its contents per section with the section's own permission.
- 12 are public. Each public route names the mechanism that protects it instead.

The rules are a first pass. During the build every route gets a hand-checked `@permit(...)` and its scope resolver (IDENTITY.md §6.3). After that, a route without a declaration stops the API from booting.

### 6.1 Every public, authenticated-only and system route

| Method | Path | Declaration |
|---|---|---|
| GET | `/v1/auth/session` | authenticated |
| POST | `/v1/mcp-confirmations` | authenticated |
| GET | `/v1/mcp-confirmations` | authenticated |
| GET | `/v1/mcp-confirmations/{confirmation_id}` | authenticated |
| GET | `/v1/command-center/view` | authenticated:composite_view |
| GET | `/v1/console/command-center` | authenticated:composite_view |
| GET | `/v1/console/v2/decisions` | authenticated:composite_view |
| GET | `/v1/console/v2/infra` | authenticated:composite_view |
| GET | `/v1/console/v2/org` | authenticated:composite_view |
| GET | `/v1/console/v2/overview` | authenticated:composite_view |
| GET | `/v1/console/v2/panels` | authenticated:composite_view |
| GET | `/v1/console/v2/receipts` | authenticated:composite_view |
| GET | `/v1/console/v2/settings` | authenticated:composite_view |
| GET | `/v1/console/v2/workflows` | authenticated:composite_view |
| GET | `/v1/console/v2/workforce` | authenticated:composite_view |
| GET | `/v1/console/v2/workspace` | authenticated:composite_view |
| POST | `/v1/bootstrap/seed` | owner_bootstrap:host_only |
| POST | `/v1/webhooks/bitbucket` | public:hmac_signed_webhook |
| POST | `/v1/webhooks/github` | public:hmac_signed_webhook |
| POST | `/v1/webhooks/inbound/{source}` | public:hmac_signed_webhook |
| POST | `/v1/webhooks/plane` | public:hmac_signed_webhook |
| GET | `/health` | public:liveness |
| GET | `/v1/channels/{provider}/callback` | public:oauth_state_bound_callback |
| POST | `/v1/investigations/{investigation_id}/db` | public:per_investigation_callback_token |
| POST | `/v1/investigations/{investigation_id}/do` | public:per_investigation_callback_token |
| POST | `/v1/investigations/{investigation_id}/host` | public:per_investigation_callback_token |
| POST | `/v1/investigations/{investigation_id}/run` | public:per_investigation_callback_token |
| POST | `/saml/v2/{project_id}/acs` | public:saml_signed_assertion |
| GET | `/saml/v2/{project_id}/metadata` | public:saml_signed_assertion |
| GET | `/scim/v2/{project_id}/Groups` | scim:provision |
| POST | `/scim/v2/{project_id}/Groups` | scim:provision |
| GET | `/scim/v2/{project_id}/Groups/{group_id}` | scim:provision |
| PUT | `/scim/v2/{project_id}/Groups/{group_id}` | scim:provision |
| DELETE | `/scim/v2/{project_id}/Groups/{group_id}` | scim:provision |
| GET | `/scim/v2/{project_id}/Users` | scim:provision |
| POST | `/scim/v2/{project_id}/Users` | scim:provision |
| GET | `/scim/v2/{project_id}/Users/{user_id}` | scim:provision |
| PUT | `/scim/v2/{project_id}/Users/{user_id}` | scim:provision |
| DELETE | `/scim/v2/{project_id}/Users/{user_id}` | scim:provision |
| POST | `/v1/kms/keys/{key_entry_id}/lease` | secret:lease |
| POST | `/v1/kms/leases/{lease_id}/resolve` | secret:lease |
| POST | `/v1/kms/leases/{lease_id}/revoke` | secret:lease |
| POST | `/v1/budget/consume` | spend:reserve |
| POST | `/v1/budget/release` | spend:reserve |
| POST | `/v1/budget/reserve` | spend:reserve |
| POST | `/v1/telemetry/ingest` | telemetry:ingest |
| POST | `/v1/telemetry/tick` | telemetry:ingest |
| POST | `/v1/a2a/poll` | worker:lease |
| POST | `/v1/agent-groups/runs/{run_id}/finalize` | worker:lease |
| POST | `/v1/agent-groups/runs/{run_id}/members/{member_key}/complete` | worker:lease |
| POST | `/v1/agent-runtime/resolve` | worker:lease |
| POST | `/v1/comms/inbox/ingest` | worker:lease |
| POST | `/v1/delivery/deployments/{deployment_run_id}/apply` | worker:lease |
| POST | `/v1/delivery/deployments/{deployment_run_id}/execute` | worker:lease |
| POST | `/v1/deployments/stack/{deployment_id}/execute` | worker:lease |
| POST | `/v1/discoveries/{run_id}/execute` | worker:lease |
| POST | `/v1/docs/update/execute` | worker:lease |
| POST | `/v1/elastic/instances/{instance_id}/heartbeat` | worker:lease |
| POST | `/v1/github/branch-leases/{lease_id}/heartbeat` | worker:lease |
| POST | `/v1/github/code-sync-runs/{sync_run_id}/apply` | worker:lease |
| POST | `/v1/github/community/jobs/{job_id}/execute` | worker:lease |
| POST | `/v1/github/merge-authorizations/{authorization_id}/apply` | worker:lease |
| POST | `/v1/github/reconciliation-runs/{run_id}/execute` | worker:lease |
| POST | `/v1/github/resolution-plans/{plan_id}/apply` | worker:lease |
| POST | `/v1/model-gateway/leases` | worker:lease |
| POST | `/v1/model-gateway/leases/reap` | worker:lease |
| POST | `/v1/model-gateway/leases/{lease_id}/heartbeat` | worker:lease |
| POST | `/v1/model-gateway/leases/{lease_id}/invocations` | worker:lease |
| POST | `/v1/model-gateway/leases/{lease_id}/invocations/{invocation_id}/settle` | worker:lease |
| POST | `/v1/model-gateway/leases/{lease_id}/release` | worker:lease |
| POST | `/v1/preview-sandboxes/{preview_id}/heartbeat` | worker:lease |
| POST | `/v1/worker/complete` | worker:lease |
| POST | `/v1/worker/fail` | worker:lease |
| POST | `/v1/worker/heartbeat` | worker:lease |
| POST | `/v1/worker/poll` | worker:lease |
| POST | `/v1/workers/register` | worker:lease |
| POST | `/v1/workflow-compensation-runs/{compensation_id}/complete` | worker:lease |
| POST | `/v1/workflow-compensation-runs/{compensation_id}/fail` | worker:lease |
| POST | `/v1/workflow-runs/{run_id}/compensations/claim` | worker:lease |
| POST | `/v1/workflow-runs/{run_id}/steps/claim` | worker:lease |
| POST | `/v1/workflow-step-runs/{step_run_id}/complete` | worker:lease |
| POST | `/v1/workflow-step-runs/{step_run_id}/fail` | worker:lease |
| POST | `/v1/workflow-step-runs/{step_run_id}/heartbeat` | worker:lease |

### 6.2 Sample: up to three routes per permission

| Permission | Method | Path | Router |
|---|---|---|---|
| `directory:read` | GET | `/v1/members` | main.py |
| `directory:read` | GET | `/v1/members/{member_id}` | main.py |
| `directory:read` | GET | `/v1/org-members` | main.py |
| `user:invite` | POST | `/v1/members` | main.py |
| `user:invite` | POST | `/v1/org-members` | main.py |
| `role_binding:assign` | POST | `/v1/break-glass/grants` | main.py |
| `role_binding:assign` | POST | `/v1/break-glass/grants/{grant_id}/revoke` | main.py |
| `agent_passport:read` | GET | `/v1/agents/identities` | main.py |
| `agent_passport:read` | GET | `/v1/agents/{role_key}/passport` | main.py |
| `agent_passport:read` | GET | `/v1/agents/{role_key}/lineage` | main.py |
| `identity_provider:read` | GET | `/v1/enterprise/identity-rollout-policies` | main.py |
| `identity_provider:read` | GET | `/v1/enterprise/identity-providers` | main.py |
| `identity_provider:read` | GET | `/v1/enterprise/scim/groups` | main.py |
| `identity_provider:manage` | POST | `/v1/enterprise/identity-rollout-policies` | main.py |
| `identity_provider:manage` | POST | `/v1/enterprise/identity-providers` | main.py |
| `identity_provider:manage` | POST | `/v1/enterprise/scim/groups` | main.py |
| `audit:read` | GET | `/v1/break-glass/grants` | main.py |
| `scim:provision` | GET | `/scim/v2/{project_id}/Groups` | main.py |
| `scim:provision` | POST | `/scim/v2/{project_id}/Groups` | main.py |
| `scim:provision` | GET | `/scim/v2/{project_id}/Groups/{group_id}` | main.py |
| `org:read` | GET | `/v1/orgs` | main.py |
| `org:update` | POST | `/v1/orgs` | main.py |
| `project:read` | GET | `/v1/projects` | main.py |
| `project:read` | GET | `/v1/projects/{project_id}/preflight` | main.py |
| `project:read` | GET | `/v1/projects/{project_id}/host-advice` | main.py |
| `project:update` | PUT | `/v1/projects/{project_id}/settings/builds_at_once` | system_health.py |
| `settings:read` | GET | `/v1/settings` | main.py |
| `settings:update` | PUT | `/v1/settings/{key}` | main.py |
| `code_task:read` | GET | `/v1/code-tasks/{run_id}/live` | main.py |
| `code_task:read` | GET | `/v1/code-tasks/{run_id}/retrospective` | main.py |
| `code_task:read` | GET | `/v1/code-tasks/{run_id}/activity` | main.py |
| `code_task:create` | POST | `/v1/code-tasks` | main.py |
| `code_task:create` | POST | `/v1/code-tasks/step` | main.py |
| `code_task:create` | POST | `/v1/software-factory/proposals` | main.py |
| `code_task:steer` | POST | `/v1/code-tasks/{run_id}/follow-up` | main.py |
| `code_task:steer` | POST | `/v1/code-tasks/{run_id}/steer` | main.py |
| `code_task:steer` | POST | `/v1/code-tasks/{run_id}/redelivered` | main.py |
| `code_task:cancel` | POST | `/v1/code-tasks/{run_id}/cancel` | main.py |
| `code_task:retry` | POST | `/v1/code-tasks/{run_id}/retry` | main.py |
| `code_task:override` | POST | `/v1/code-tasks/{run_id}/override` | main.py |
| `repository:read` | GET | `/v1/delivery/repos` | main.py |
| `repository:read` | GET | `/v1/delivery/repos/{owner}/{repo}/refs` | main.py |
| `repository:read` | GET | `/v1/delivery/repos/{owner}/{repo}/apps` | main.py |
| `repository:manage` | POST | `/v1/setup/repository/select` | setup.py |
| `repository:manage` | POST | `/v1/setup/repository/{binding_id}/default` | setup.py |
| `repository:manage` | DELETE | `/v1/setup/repository/{binding_id}` | setup.py |
| `github:read` | GET | `/v1/github/connections` | main.py |
| `github:read` | GET | `/v1/github/connections/{connection_id}/repositories` | main.py |
| `github:read` | GET | `/v1/github/connections/{connection_id}` | main.py |
| `github:connect` | POST | `/v1/github/easy-connect` | main.py |
| `github:connect` | POST | `/v1/setup/repository/token` | setup.py |
| `github:manage` | POST | `/v1/github/connections/{connection_id}/enable` | main.py |
| `github:manage` | POST | `/v1/github/connections/{connection_id}/disable` | main.py |
| `github:manage` | POST | `/v1/github/connections/{connection_id}/revoke` | main.py |
| `github:sync` | POST | `/v1/github/connections` | main.py |
| `github:sync` | POST | `/v1/github/repository-bindings` | main.py |
| `github:sync` | PATCH | `/v1/github/repository-bindings/{binding_id}` | main.py |
| `github:merge` | POST | `/v1/github/resolution-plans/{plan_id}/approve` | main.py |
| `github:merge` | POST | `/v1/github/merge-authorizations/{authorization_id}/authorize` | main.py |
| `github:merge` | POST | `/v1/github/merge-authorizations/{authorization_id}/enqueue` | main.py |
| `preview:read` | GET | `/v1/previews` | main.py |
| `preview:read` | GET | `/v1/previews/{preview_id}/logs` | main.py |
| `preview:read` | GET | `/v1/previews/{preview_id}/gate` | main.py |
| `preview:manage` | POST | `/v1/code-tasks/{run_id}/preview` | main.py |
| `preview:manage` | POST | `/v1/previews/{preview_id}/smoke` | main.py |
| `preview:manage` | POST | `/v1/previews/{preview_id}/stop` | main.py |
| `preview:approve` | POST | `/v1/previews/{preview_id}/feedback` | main.py |
| `preview:approve` | POST | `/v1/previews/{preview_id}/approve` | main.py |
| `artifact:read` | GET | `/v1/artifacts` | main.py |
| `artifact:read` | GET | `/v1/artifacts/{artifact_id}` | main.py |
| `artifact:read` | GET | `/v1/artifacts/{artifact_id}/media` | main.py |
| `artifact:rebuild` | POST | `/v1/pdf/render` | main.py |
| `artifact:rebuild` | POST | `/v1/artifacts/{artifact_id}/rebuild` | main.py |
| `workflow:read` | GET | `/v1/delivery-workflows/schedules` | main.py |
| `workflow:read` | GET | `/v1/delivery-workflows/capabilities` | main.py |
| `workflow:read` | GET | `/v1/delivery-workflows/custom-steps` | main.py |
| `workflow:create` | POST | `/v1/delivery-workflows/custom-steps` | main.py |
| `workflow:create` | PATCH | `/v1/delivery-workflows/custom-steps/{step_key}` | main.py |
| `workflow:create` | DELETE | `/v1/delivery-workflows/custom-steps/{step_key}` | main.py |
| `workflow:publish` | POST | `/v1/delivery-workflows/apply` | main.py |
| `workflow:publish` | POST | `/v1/delivery-workflows/publish` | main.py |
| `workflow:install` | POST | `/v1/delivery-workflows/catalogue/install` | main.py |
| `workflow_run:read` | GET | `/v1/agent-runs` | main.py |
| `workflow_run:read` | GET | `/v1/workflow-runs/{run_id}/report` | main.py |
| `workflow_run:read` | GET | `/v1/workflow-runs` | main.py |
| `workflow_run:start` | POST | `/v1/delivery-workflows/start` | main.py |
| `workflow_run:start` | POST | `/v1/delivery-workflows/run-capabilities` | main.py |
| `workflow_run:start` | POST | `/v1/workflow-runs` | main.py |
| `workflow_run:control` | POST | `/v1/agent-runs/{run_id}/resume` | main.py |
| `workflow_run:control` | POST | `/v1/workflow-runs/{run_id}/report` | main.py |
| `workflow_run:control` | POST | `/v1/delivery-workflows/runs/{run_id}/retry-step` | main.py |
| `workflow_run:compensate` | POST | `/v1/workflow-runs/{run_id}/migrate` | main.py |
| `workflow_run:compensate` | POST | `/v1/workflow-runs/{run_id}/migrations/rollback` | main.py |
| `workflow_run:compensate` | POST | `/v1/workflow-runs/{run_id}/compensate` | main.py |
| `workflow_run:delete` | DELETE | `/v1/delivery-workflows/runs/{run_id}` | main.py |
| `workflow_schedule:manage` | POST | `/v1/delivery-workflows/schedules` | main.py |
| `workflow_schedule:manage` | POST | `/v1/delivery-workflows/schedules/{schedule_id}/enabled` | main.py |
| `workflow_schedule:manage` | DELETE | `/v1/delivery-workflows/schedules/{schedule_id}` | main.py |
| `meeting:read` | GET | `/v1/meeting-roles` | main.py |
| `meeting:read` | GET | `/v1/meeting-rooms` | main.py |
| `meeting:read` | GET | `/v1/meeting-rooms/{meeting_id}` | main.py |
| `meeting:convene` | POST | `/v1/meetings/prd` | main.py |
| `meeting:convene` | POST | `/v1/meetings` | main.py |
| `meeting:convene` | POST | `/v1/meetings/{meeting_id}/run` | main.py |
| `meeting:configure` | POST | `/v1/meeting-roles` | main.py |
| `meeting:configure` | PATCH | `/v1/meeting-roles/{role_key}` | main.py |
| `meeting:configure` | DELETE | `/v1/meeting-roles/{role_key}` | main.py |
| `discovery:read` | GET | `/v1/discoveries` | main.py |
| `discovery:read` | GET | `/v1/discoveries/{run_id}` | main.py |
| `discovery:create` | POST | `/v1/discoveries` | main.py |
| `discovery:create` | POST | `/v1/discoveries/{run_id}/promote` | main.py |
| `discovery:create` | POST | `/v1/discoveries/{run_id}/deepen` | main.py |
| `work_item:read` | GET | `/v1/tasks` | main.py |
| `work_item:read` | GET | `/v1/departments` | main.py |
| `work_item:read` | GET | `/v1/kpis` | main.py |
| `work_item:manage` | POST | `/v1/prd/decompose` | main.py |
| `work_item:manage` | POST | `/v1/commands/propose` | main.py |
| `work_item:manage` | DELETE | `/v1/tasks/{task_id}` | main.py |
| `job:read` | GET | `/v1/jobs` | main.py |
| `job:read` | GET | `/v1/jobs/{job_id}` | main.py |
| `job:read` | GET | `/v1/ops/queue` | main.py |
| `job:control` | POST | `/v1/jobs/{job_id}/cancel` | main.py |
| `deadletter:manage` | POST | `/v1/deadletters/classify` | main.py |
| `deadletter:manage` | POST | `/v1/deadletters/{deadletter_id}/incident` | main.py |
| `deadletter:manage` | POST | `/v1/deadletters/{deadletter_id}/requeue` | main.py |
| `assistant:chat` | POST | `/v1/engine/agent/action` | main.py |
| `assistant:chat` | POST | `/v1/engine/agent/chat` | main.py |
| `assistant:chat` | POST | `/v1/agent/chat` | main.py |
| `autopilot:read` | GET | `/v1/shepherd/readiness` | main.py |
| `autopilot:read` | GET | `/v1/shepherd/overview` | main.py |
| `autopilot:read` | GET | `/v1/coordination` | main.py |
| `autopilot:operate` | POST | `/v1/shepherd/watch` | main.py |
| `autopilot:operate` | POST | `/v1/shepherd/propose` | main.py |
| `autopilot:operate` | POST | `/v1/shepherd/sweep` | main.py |
| `autopilot:configure` | POST | `/v1/agent-processes/compile` | agent_processes.py |
| `autopilot:configure` | POST | `/v1/captain/playbooks/compile` | captain.py |
| `memory:read` | GET | `/v1/memory` | main.py |
| `memory:manage` | POST | `/v1/memory/{memory_id}/review` | main.py |
| `memory:manage` | POST | `/v1/memory/{memory_id}/forget` | main.py |
| `feedback:submit` | POST | `/v1/feedback-threads/{thread_id}/confirm` | main.py |
| `feedback:submit` | POST | `/v1/feedback` | improve.py |
| `feedback:submit` | GET | `/v1/feedback/prompts` | improve.py |
| `decision:read` | GET | `/v1/approval-rules` | main.py |
| `decision:read` | GET | `/v1/decisions/inbox` | main.py |
| `decision:read` | GET | `/v1/decisions/{decision_id}` | main.py |
| `decision:approve` | POST | `/v1/mcp-confirmations/{confirmation_id}/approve` | main.py |
| `decision:approve` | POST | `/v1/decisions/{decision_id}/approve` | main.py |
| `decision:approve` | POST | `/v1/decisions/{decision_id}/reject` | main.py |
| `decision:delegate` | POST | `/v1/decisions/{decision_id}/delegate` | main.py |
| `decision:execute` | POST | `/v1/decisions/{decision_id}/execute` | main.py |
| `agent:read` | GET | `/v1/console/v2/agents/{role_key}` | main.py |
| `agent:read` | GET | `/v1/roles` | main.py |
| `agent:read` | GET | `/v1/roles/{role_key}` | main.py |
| `agent:create` | POST | `/v1/projects/{project_id}/agents` | main.py |
| `agent:create` | POST | `/v1/roles` | main.py |
| `agent:create` | POST | `/v1/agent-studio/agents` | main.py |
| `agent:update` | PUT | `/v1/console/v2/org/{agent_id}` | main.py |
| `agent:update` | DELETE | `/v1/console/v2/org/{agent_id}` | main.py |
| `agent:update` | POST | `/v1/agent-studio/catalog/seed` | main.py |
| `agent:publish` | POST | `/v1/agent-studio/agent-versions/{version_id}/publish` | main.py |
| `agent:publish` | POST | `/v1/agent-studio/panel-versions/{version_id}/publish` | main.py |
| `agent:lifecycle` | POST | `/v1/agents/{role_key}/lifecycle` | main.py |
| `agent:test` | POST | `/v1/agent-studio/catalog-smoke` | main.py |
| `agent:test` | POST | `/v1/agent-studio/workflows/validate` | main.py |
| `agent:test` | POST | `/v1/agent-studio/agent-versions/{version_id}/validate` | main.py |
| `agent_ability:assign` | POST | `/v1/role-skills/sync` | main.py |
| `agent_ability:assign` | POST | `/v1/role-mcp-servers` | agents.py |
| `agent_ability:assign` | DELETE | `/v1/role-mcp-servers` | agents.py |
| `autonomy:read` | GET | `/v1/console/v2/autonomy` | main.py |
| `autonomy:manage` | POST | `/v1/console/v2/autonomy` | main.py |
| `autonomy:manage` | POST | `/v1/console/v2/autonomy/apply` | main.py |
| `autonomy:manage` | POST | `/v1/roles/{role_key}/autonomy/check` | main.py |
| `skill:read` | GET | `/v1/capabilities` | main.py |
| `skill:read` | GET | `/v1/toolchain/sources` | main.py |
| `skill:read` | GET | `/v1/toolchain/imports` | main.py |
| `skill:manage` | POST | `/v1/skills/import` | main.py |
| `skill:manage` | POST | `/v1/skills/{skill_id}/enable` | main.py |
| `skill:manage` | POST | `/v1/skills/{skill_id}/disable` | main.py |
| `skill:invoke` | POST | `/v1/skills/{skill_id}/invoke` | main.py |
| `skill:invoke` | POST | `/v1/kit/{ability}/test` | kit.py |
| `skill:invoke` | POST | `/v1/kit/test-all` | kit.py |
| `mcp_server:read` | GET | `/v1/mcp/servers` | main.py |
| `mcp_server:read` | GET | `/v1/mcp/servers/{server_id}/export` | main.py |
| `mcp_server:read` | GET | `/v1/mcp/catalogue` | kit.py |
| `mcp_server:register` | POST | `/v1/mcp/servers/import` | main.py |
| `mcp_server:register` | POST | `/v1/mcp/servers/test` | kit.py |
| `mcp_server:review` | POST | `/v1/mcp/servers` | main.py |
| `mcp_server:review` | PATCH | `/v1/mcp/servers/{server_id}` | main.py |
| `mcp_server:review` | DELETE | `/v1/mcp/servers/{server_id}` | main.py |
| `pack:read` | GET | `/v1/packs` | main.py |
| `pack:read` | GET | `/v1/packs/{pack_id}` | main.py |
| `pack:read` | GET | `/v1/packs/{pack_id}/export` | main.py |
| `pack:install` | POST | `/v1/packs/install` | main.py |
| `pack:install` | POST | `/v1/packs/import` | main.py |
| `pack:install` | POST | `/v1/agent-modules/install` | main.py |
| `deployment:read` | GET | `/v1/deployment-targets` | main.py |
| `deployment:read` | GET | `/v1/delivery/defaults` | main.py |
| `deployment:read` | GET | `/v1/delivery/health` | main.py |
| `deployment:create` | POST | `/v1/delivery/deploy` | main.py |
| `deployment:create` | POST | `/v1/deployments/stack/targets` | main.py |
| `deployment:create` | POST | `/v1/deployments/stack/plan` | main.py |
| `deployment:production` | POST | `/v1/deployments/{deployment_run_id}/promote` | main.py |
| `deployment:production` | POST | `/v1/deployment-promotions` | main.py |
| `deployment:production` | POST | `/v1/deployment-promotions/{promotion_id}/advance` | main.py |
| `deployment:rollback` | POST | `/v1/deployments/stack/{deployment_id}/rollback` | main.py |
| `deployment:rollback` | POST | `/v1/self-improvement/proposals/{proposal_id}/rollback` | main.py |
| `deployment:rollback` | POST | `/v1/deployment-runs/{deployment_run_id}/rollout/rollback` | main.py |
| `deployment_target:manage` | POST | `/v1/deployment-targets` | main.py |
| `deployment_target:manage` | PATCH | `/v1/deployment-targets/{target_id}` | main.py |
| `deployment_target:manage` | DELETE | `/v1/deployment-targets/{target_id}` | main.py |
| `host:read` | GET | `/v1/deployment-hosts` | main.py |
| `host:read` | GET | `/v1/infra/fleet` | infra_view.py |
| `host:read` | GET | `/v1/infra/hosts/{host_id}` | infra_view.py |
| `host:manage` | PATCH | `/v1/deployment-hosts/{host_id}` | main.py |
| `host:manage` | POST | `/v1/deployment-hosts` | main.py |
| `host:manage` | POST | `/v1/deployment-hosts/{host_id}/key` | main.py |
| `sandbox:read` | GET | `/v1/sandboxes` | main.py |
| `sandbox:read` | GET | `/v1/sandboxes/helpers` | main.py |
| `sandbox:read` | GET | `/v1/sandboxes/{sandbox_id}/tree` | main.py |
| `sandbox:exec` | POST | `/v1/sandboxes/{sandbox_id}/exec` | main.py |
| `sandbox:exec` | POST | `/v1/diag/sandbox-echo` | sandbox_echo.py |
| `sandbox:manage` | POST | `/v1/sandboxes/{sandbox_id}/preserve` | main.py |
| `sandbox:manage` | DELETE | `/v1/sandboxes/{sandbox_id}` | main.py |
| `infra:read` | GET | `/v1/resources` | main.py |
| `infra:read` | GET | `/v1/resource-topology` | main.py |
| `infra:read` | GET | `/v1/changesets` | main.py |
| `infra:provision` | POST | `/v1/resource-topology/edges` | main.py |
| `infra:provision` | DELETE | `/v1/resource-topology/edges/{edge_id}` | main.py |
| `infra:provision` | POST | `/v1/resource-topology/infer` | main.py |
| `infra:terminate` | POST | `/v1/elastic/instances/{instance_id}/terminate` | main.py |
| `infra:terminate` | POST | `/v1/resource-janitor/run` | main.py |
| `infra:terminate` | POST | `/v1/infra/resources/{resource_id}/terminate` | main.py |
| `cloud_account:manage` | POST | `/v1/cloud/easy-connect` | cloud.py |
| `cloud_account:manage` | POST | `/v1/cloud/easy-connect/{session_id}/complete` | cloud.py |
| `cloud_account:manage` | POST | `/v1/cloud/easy-connect/complete` | cloud.py |
| `changeset:approve` | POST | `/v1/changesets/{changeset_id}/approve` | main.py |
| `changeset:approve` | POST | `/v1/changesets/{changeset_id}/apply` | main.py |
| `changeset:approve` | POST | `/v1/changesets/{changeset_id}/terraform-export` | main.py |
| `cicd:read` | GET | `/v1/pipeline-adapters/catalog` | main.py |
| `cicd:read` | GET | `/v1/cicd/catalog` | cicd_pipelines.py |
| `cicd:read` | GET | `/v1/cicd/runs` | cicd_pipelines.py |
| `cicd:manage` | POST | `/v1/ci-config/generate` | main.py |
| `cicd:manage` | POST | `/v1/cicd/generate` | cicd_pipelines.py |
| `cicd:manage` | POST | `/v1/cicd/validate` | cicd_pipelines.py |
| `setup:read` | GET | `/v1/setup/status` | main.py |
| `setup:read` | GET | `/v1/setup/model-providers` | main.py |
| `setup:read` | GET | `/v1/setup/coding-agents` | main.py |
| `setup:manage` | POST | `/v1/setup/checks` | setup.py |
| `setup:manage` | PUT | `/v1/setup/jobs/{job}` | setup.py |
| `setup:manage` | PUT | `/v1/setup/budget` | setup.py |
| `model_provider:read` | GET | `/v1/model-adapters` | main.py |
| `model_provider:read` | GET | `/v1/model-route-decisions` | main.py |
| `model_provider:read` | GET | `/v1/model-gateway/leases` | main.py |
| `model_provider:manage` | POST | `/v1/model-route-decisions/simulate` | main.py |
| `model_provider:manage` | POST | `/v1/setup/coding-agents/credential` | main.py |
| `model_provider:manage` | POST | `/v1/setup/decision-model/key` | main.py |
| `model_provider:rotate` | POST | `/v1/providers/{provider_account_id}/rotate` | main.py |
| `entitlement:read` | GET | `/v1/entitlements` | main.py |
| `entitlement:read` | GET | `/v1/entitlement-pools` | main.py |
| `entitlement:read` | GET | `/v1/entitlements/sources` | main.py |
| `entitlement:manage` | POST | `/v1/entitlement-pools/{pool_id}/probe` | main.py |
| `entitlement:manage` | POST | `/v1/entitlements/sources/{source_id}/credential-ref` | main.py |
| `entitlement:manage` | POST | `/v1/entitlements/sources/{source_id}/credential` | main.py |
| `budget:read` | GET | `/v1/finops/commitment-reconciliations` | main.py |
| `budget:read` | GET | `/v1/finops/summary` | main.py |
| `budget:read` | GET | `/v1/costs` | main.py |
| `budget:manage` | POST | `/v1/finops/reconcile-commitments` | main.py |
| `budget:manage` | POST | `/v1/finops/initiative-budget` | main.py |
| `budget:manage` | POST | `/v1/budget/allocations` | main.py |
| `spend:reserve` | POST | `/v1/budget/reserve` | main.py |
| `spend:reserve` | POST | `/v1/budget/release` | main.py |
| `spend:reserve` | POST | `/v1/budget/consume` | main.py |
| `policy:read` | GET | `/v1/policies` | main.py |
| `policy:read` | GET | `/v1/policy-evals` | main.py |
| `policy:read` | GET | `/v1/platform-standards` | main.py |
| `policy:manage` | POST | `/v1/repair-policy` | main.py |
| `policy:manage` | POST | `/v1/concurrency-load` | main.py |
| `policy:manage` | POST | `/v1/policies` | main.py |
| `pipeline:read` | GET | `/v1/pipeline-profiles` | main.py |
| `pipeline:manage` | POST | `/v1/pipeline-profiles` | main.py |
| `pipeline:manage` | POST | `/v1/pipeline-profiles/default` | main.py |
| `pipeline:manage` | POST | `/v1/pipeline-config/proposals` | main.py |
| `secret:read_metadata` | GET | `/v1/secrets` | main.py |
| `secret:read_metadata` | GET | `/v1/secrets/{secret_ref_id}` | main.py |
| `secret:read_metadata` | GET | `/v1/secret-refs` | main.py |
| `secret:manage` | POST | `/v1/secrets` | main.py |
| `secret:manage` | POST | `/v1/secrets/keypair` | main.py |
| `secret:manage` | POST | `/v1/secrets/{secret_ref_id}/verify` | main.py |
| `kms_key:manage` | POST | `/v1/kms/keys` | agent_kms.py |
| `kms_key:manage` | POST | `/v1/kms/keys/{key_entry_id}/grant` | agent_kms.py |
| `kms_key:manage` | DELETE | `/v1/kms/keys/{key_entry_id}/grants/{grantee_agent}` | agent_kms.py |
| `secret:lease` | POST | `/v1/kms/keys/{key_entry_id}/lease` | agent_kms.py |
| `secret:lease` | POST | `/v1/kms/leases/{lease_id}/resolve` | agent_kms.py |
| `secret:lease` | POST | `/v1/kms/leases/{lease_id}/revoke` | agent_kms.py |
| `integration:read` | GET | `/v1/event-subscriptions` | main.py |
| `integration:read` | GET | `/v1/webhook-endpoints` | main.py |
| `integration:read` | GET | `/v1/webhook-deliveries` | main.py |
| `integration:manage` | POST | `/v1/event-subscriptions` | main.py |
| `integration:manage` | PATCH | `/v1/event-subscriptions/{subscription_id}` | main.py |
| `integration:manage` | DELETE | `/v1/event-subscriptions/{subscription_id}` | main.py |
| `comms:read` | GET | `/v1/comms/policies` | main.py |
| `comms:read` | GET | `/v1/comms/outbox` | main.py |
| `comms:read` | GET | `/v1/comms/inbox` | main.py |
| `comms:send` | POST | `/v1/comms/outbox/{message_id}/queue` | main.py |
| `comms:send` | POST | `/v1/comms/outbox/{message_id}/deliver` | main.py |
| `comms:send` | POST | `/v1/comms/outbox/{message_id}/dry-run` | comms_dry_run.py |
| `comms:manage` | POST | `/v1/oncall/notify` | main.py |
| `comms:manage` | POST | `/v1/comms/policies` | main.py |
| `comms:manage` | POST | `/v1/comms/outbox` | main.py |
| `system:read` | GET | `/v1/edition` | main.py |
| `system:read` | GET | `/v1/meta/api-catalog` | main.py |
| `system:read` | GET | `/v1/meta/api-catalog/{domain}` | main.py |
| `system:operate` | POST | `/v1/meetings/step` | main.py |
| `system:operate` | POST | `/v1/self/host-stall` | main.py |
| `system:operate` | POST | `/v1/delivery/health/sweep` | main.py |
| `observability:read` | GET | `/v1/reports/summary` | main.py |
| `observability:read` | GET | `/v1/qa/benchmark-apps` | main.py |
| `observability:read` | GET | `/v1/qa/benchmark-apps/runs/{run_id}/report` | main.py |
| `observability:manage` | POST | `/v1/observability/stack/deploy` | main.py |
| `observability:manage` | POST | `/v1/observability-suite/recommend` | observability_suite.py |
| `observability:manage` | POST | `/v1/observability-suite/health-check` | observability_suite.py |
| `reliability:read` | GET | `/v1/reliability/incidents` | main.py |
| `reliability:read` | GET | `/v1/reliability/incidents/{incident_id}` | main.py |
| `reliability:read` | GET | `/v1/reliability/graph` | main.py |
| `reliability:respond` | POST | `/v1/reliability/scan` | main.py |
| `reliability:respond` | POST | `/v1/reliability/incidents/{incident_id}/diagnose` | main.py |
| `reliability:respond` | POST | `/v1/reliability/incidents/{incident_id}/remediate` | main.py |
| `reliability:manage` | POST | `/v1/reliability/sources` | main.py |
| `reliability:manage` | POST | `/v1/reliability/policy` | main.py |
| `reliability:manage` | POST | `/v1/reliability/meta-eval` | main.py |
| `assurance:read` | GET | `/v1/assurance/overview` | governance.py |
| `assurance:read` | GET | `/v1/assurance/metrics` | governance.py |
| `assurance:read` | GET | `/v1/assurance/conclusions` | governance.py |
| `assurance:manage` | POST | `/v1/assurance/metrics` | governance.py |
| `assurance:manage` | POST | `/v1/assurance/metrics/schedule` | governance.py |
| `assurance:manage` | POST | `/v1/assurance/metrics/{metric_id}/observations` | governance.py |
| `assurance:run` | POST | `/v1/web-app` | main.py |
| `assurance:run` | POST | `/v1/qa/benchmark-apps/{benchmark_id}/runs` | main.py |
| `assurance:run` | POST | `/v1/qa/benchmark-apps/tuning-sweep` | main.py |
| `improvement:read` | GET | `/v1/improvement-programs` | main.py |
| `improvement:read` | GET | `/v1/improvement-programs/{program_id}/trials` | main.py |
| `improvement:manage` | POST | `/v1/improvement-programs` | main.py |
| `improvement:manage` | PATCH | `/v1/improvement-programs/{program_id}` | main.py |
| `improvement:manage` | POST | `/v1/improvement-programs/{program_id}/trials` | main.py |
| `activity:read` | GET | `/v1/outputs` | main.py |
| `activity:read` | GET | `/v1/resource-receipts` | main.py |
| `activity:read` | GET | `/v1/receipts` | main.py |
| `activity:export` | POST | `/v1/audit/replay-bundles` | main.py |
| `activity:export` | POST | `/v1/compliance/evidence-bundles` | main.py |
| `activity:prune` | POST | `/v1/activity/retention/prune` | activity.py |
| `telemetry:read` | GET | `/v1/telemetry/connectors/catalog` | main.py |
| `telemetry:read` | GET | `/v1/telemetry/sharing` | improve.py |
| `telemetry:read` | GET | `/v1/telemetry/preview` | improve.py |
| `telemetry:share` | POST | `/v1/telemetry/share-run` | improve.py |
| `telemetry:share` | POST | `/v1/telemetry/share` | improve.py |
| `telemetry:manage` | POST | `/v1/telemetry/query` | main.py |
| `telemetry:manage` | POST | `/v1/telemetry/mine` | main.py |
| `telemetry:manage` | PUT | `/v1/telemetry/sharing` | improve.py |
| `telemetry:ingest` | POST | `/v1/telemetry/tick` | improve.py |
| `telemetry:ingest` | POST | `/v1/telemetry/ingest` | improve.py |
| `worker:lease` | POST | `/v1/github/community/jobs/{job_id}/execute` | main.py |
| `worker:lease` | POST | `/v1/delivery/deployments/{deployment_run_id}/execute` | main.py |
| `worker:lease` | POST | `/v1/deployments/stack/{deployment_id}/execute` | main.py |

### 6.3 Route count per permission

| Permission | Routes |  | Permission | Routes |
|---|--:|---|---|--:|
| `directory:read` | 3 |  | `autonomy:read` | 1 |
| `user:invite` | 2 |  | `autonomy:manage` | 6 |
| `role_binding:assign` | 2 |  | `skill:read` | 11 |
| `agent_passport:read` | 3 |  | `skill:manage` | 12 |
| `identity_provider:read` | 7 |  | `skill:invoke` | 5 |
| `identity_provider:manage` | 5 |  | `mcp_server:read` | 3 |
| `audit:read` | 1 |  | `mcp_server:register` | 2 |
| `scim:provision` | 10 |  | `mcp_server:review` | 6 |
| `org:read` | 1 |  | `pack:read` | 3 |
| `org:update` | 1 |  | `pack:install` | 5 |
| `project:read` | 5 |  | `deployment:read` | 21 |
| `project:update` | 1 |  | `deployment:create` | 15 |
| `settings:read` | 1 |  | `deployment:production` | 3 |
| `settings:update` | 1 |  | `deployment:rollback` | 4 |
| `code_task:read` | 18 |  | `deployment_target:manage` | 8 |
| `code_task:create` | 8 |  | `host:read` | 3 |
| `code_task:steer` | 9 |  | `host:manage` | 11 |
| `code_task:cancel` | 1 |  | `sandbox:read` | 6 |
| `code_task:retry` | 1 |  | `sandbox:exec` | 2 |
| `code_task:override` | 1 |  | `sandbox:manage` | 2 |
| `repository:read` | 4 |  | `infra:read` | 29 |
| `repository:manage` | 3 |  | `infra:provision` | 21 |
| `github:read` | 24 |  | `infra:terminate` | 5 |
| `github:connect` | 2 |  | `cloud_account:manage` | 4 |
| `github:manage` | 7 |  | `changeset:approve` | 3 |
| `github:sync` | 12 |  | `cicd:read` | 4 |
| `github:merge` | 3 |  | `cicd:manage` | 4 |
| `preview:read` | 6 |  | `setup:read` | 6 |
| `preview:manage` | 8 |  | `setup:manage` | 4 |
| `preview:approve` | 2 |  | `model_provider:read` | 21 |
| `artifact:read` | 4 |  | `model_provider:manage` | 22 |
| `artifact:rebuild` | 2 |  | `model_provider:rotate` | 1 |
| `workflow:read` | 14 |  | `entitlement:read` | 4 |
| `workflow:create` | 16 |  | `entitlement:manage` | 9 |
| `workflow:publish` | 2 |  | `budget:read` | 15 |
| `workflow:install` | 1 |  | `budget:manage` | 4 |
| `workflow_run:read` | 6 |  | `spend:reserve` | 3 |
| `workflow_run:start` | 4 |  | `policy:read` | 8 |
| `workflow_run:control` | 10 |  | `policy:manage` | 7 |
| `workflow_run:compensate` | 3 |  | `pipeline:read` | 1 |
| `workflow_run:delete` | 1 |  | `pipeline:manage` | 3 |
| `workflow_schedule:manage` | 3 |  | `secret:read_metadata` | 9 |
| `meeting:read` | 8 |  | `secret:manage` | 6 |
| `meeting:convene` | 17 |  | `kms_key:manage` | 3 |
| `meeting:configure` | 3 |  | `secret:lease` | 3 |
| `discovery:read` | 2 |  | `integration:read` | 14 |
| `discovery:create` | 3 |  | `integration:manage` | 26 |
| `work_item:read` | 20 |  | `comms:read` | 5 |
| `work_item:manage` | 36 |  | `comms:send` | 3 |
| `job:read` | 5 |  | `comms:manage` | 7 |
| `job:control` | 1 |  | `system:read` | 17 |
| `deadletter:manage` | 5 |  | `system:operate` | 31 |
| `assistant:chat` | 12 |  | `observability:read` | 21 |
| `autopilot:read` | 13 |  | `observability:manage` | 5 |
| `autopilot:operate` | 27 |  | `reliability:read` | 30 |
| `autopilot:configure` | 2 |  | `reliability:respond` | 24 |
| `memory:read` | 1 |  | `reliability:manage` | 6 |
| `memory:manage` | 2 |  | `assurance:read` | 10 |
| `feedback:submit` | 5 |  | `assurance:manage` | 20 |
| `decision:read` | 3 |  | `assurance:run` | 25 |
| `decision:approve` | 4 |  | `improvement:read` | 2 |
| `decision:delegate` | 1 |  | `improvement:manage` | 8 |
| `decision:execute` | 1 |  | `activity:read` | 15 |
| `agent:read` | 38 |  | `activity:export` | 2 |
| `agent:create` | 10 |  | `activity:prune` | 1 |
| `agent:update` | 10 |  | `telemetry:read` | 8 |
| `agent:publish` | 2 |  | `telemetry:share` | 2 |
| `agent:lifecycle` | 1 |  | `telemetry:manage` | 6 |
| `agent:test` | 7 |  | `telemetry:ingest` | 2 |
| `agent_ability:assign` | 4 |  | `worker:lease` | 36 |

Catalogue permissions with no route today are used by the new identity API (§5) or by MCP tools only (§7): `user:read`, `user:update`, `user:disable`, `user:delete`, `user:reset_mfa`, `user:revoke_sessions`, `group:read`, `group:manage`, `group:manage_members`, `access_role:read`, `access_role:manage`, `role_binding:read`, `owner:assign`, `service_account:read`, `service_account:manage`, `service_account_key:manage`, `api_key:manage_own`, `api_key:read`, `api_key:revoke`, `session:read`, `session:revoke`, `agent_passport:manage`, `agent_passport:suspend`, `agent_passport:revoke`, `delegation:grant`, `delegation:read_all`, `delegation:revoke_any`, `connection:use_own`, `connection:read_all`, `connection:revoke_any`, `security_policy:read`, `security_policy:manage`, `audit:export`, `org:delete`, `project:create`, `project:delete`, `repository:register`, `workflow:delete`, `approval_rule:manage`, `host:exec`.

## 7. MCP tools → permission (all of them)

`ToolSpec` gains a required `permission` field, so a tool without one fails at import. The API-surface tools that `register_api_tools` generates from OpenAPI inherit the permission of the route they call. The hand-written tools below declare theirs. An agent calling a tool is checked against its run token's effective set. A person calling through an MCP client is checked against their own access. The `reversibility` rule (`destroys_work`, `irreversible` and `grants_authority` are never self-executed) still applies on top.

| Tool | Permission | | Tool | Permission |
|---|---|---|---|---|
| `add_review_comment` | `code_task:steer` |  | `list_previews` | `preview:read` |
| `assign_mcp_to_agent` | `agent_ability:assign` |  | `list_repositories` | `repository:read` |
| `assign_skill_to_agent` | `agent_ability:assign` |  | `list_review_comments` | `code_task:read` |
| `cancel_code_task` | `code_task:cancel` |  | `list_running_work` | `job:read` |
| `cancel_job` | `job:control` |  | `list_skills` | `skill:read` |
| `check_port_exposure` | `host:read` |  | `list_source` | `repository:read` |
| `close_orphaned_runs` | `workflow_run:control` |  | `list_standards` | `policy:read` |
| `connect_coding_agent` | `model_provider:manage` |  | `list_watches` | `workflow_run:read` |
| `consistency_report` | `system:read` |  | `mandate_standard` | `policy:manage` |
| `create_deployment_target` | `deployment_target:manage` |  | `merge_apps` | `code_task:steer` |
| `create_discovery` | `discovery:create` |  | `microvm_status` | `sandbox:read` |
| `decide` | `decision:approve` |  | `my_recent_work` | `activity:read` |
| `delete_workflow` | `workflow:delete` |  | `preflight_deployment_target` | `deployment:read` |
| `deploy_app` | `deployment:create` |  | `project_status` | `project:read` |
| `describe_deployment_host` | `host:read` |  | `propose_action` | `decision:read` |
| `describe_deployment_target` | `deployment:read` |  | `provider_audit` | `model_provider:read` |
| `describe_role` | `agent:read` |  | `query_db` | `host:exec` |
| `describe_schema` | `system:read` |  | `read_code` | `repository:read` |
| `describe_workflow` | `workflow:read` |  | `read_logs` | `observability:read` |
| `describe_workflow_run` | `workflow_run:read` |  | `read_preview_log` | `preview:read` |
| `detect_coding_agents` | `setup:read` |  | `read_pull_request` | `github:read` |
| `diagnose_run` | `workflow_run:read` |  | `read_run_turns` | `workflow_run:read` |
| `diagnose_setup` | `setup:read` |  | `read_spec` | `workflow:read` |
| `draft_custom_step` | `workflow:create` |  | `recall` | `memory:read` |
| `explain_run` | `workflow_run:read` |  | `recall_role_experience` | `memory:read` |
| `extend_code_task` | `code_task:retry` |  | `recheck_workflow_run` | `workflow_run:control` |
| `factory_briefing` | `code_task:read` |  | `redeploy` | `deployment:create` |
| `find_free_port` | `host:read` |  | `register_mcp_server` | `mcp_server:register` |
| `find_split_apps` | `code_task:read` |  | `register_repository` | `repository:register` |
| `find_starved_work` | `job:read` |  | `remember` | `memory:manage` |
| `forget` | `memory:manage` |  | `repo_activity` | `github:read` |
| `generate_image` | `artifact:rebuild` |  | `request_meeting` | `meeting:convene` |
| `get_code_task` | `code_task:read` |  | `resolve_review_comment` | `code_task:steer` |
| `get_config` | `settings:read` |  | `retarget_app` | `deployment:create` |
| `get_costs` | `budget:read` |  | `retry_code_task` | `code_task:retry` |
| `get_investigation` | `reliability:read` |  | `retry_workflow_step` | `workflow_run:control` |
| `get_meeting` | `meeting:read` |  | `run_on_host` | `host:exec` |
| `get_run_activity` | `workflow_run:read` |  | `schedule_work` | `workflow_schedule:manage` |
| `get_task_preview` | `preview:read` |  | `search_code` | `repository:read` |
| `git_history` | `repository:read` |  | `send_feedback` | `feedback:submit` |
| `http_probe` | `observability:read` |  | `set_job_priority` | `job:control` |
| `inspect_host` | `host:read` |  | `set_project_setting` | `project:update` |
| `inspect_self` | `system:read` |  | `skip_workflow_step` | `workflow_run:control` |
| `investigate` | `reliability:respond` |  | `start_preview` | `preview:manage` |
| `link_run_origin` | `code_task:steer` |  | `steer_code_task` | `code_task:steer` |
| `list_capabilities` | `skill:read` |  | `stop_preview` | `preview:manage` |
| `list_code_tasks` | `code_task:read` |  | `stop_watching` | `workflow_run:read` |
| `list_decisions` | `decision:read` |  | `system_doctor_act` | `system:operate` |
| `list_deployment_hosts` | `host:read` |  | `system_health` | `system:read` |
| `list_deployments` | `deployment:read` |  | `test_github_access` | `github:read` |
| `list_discovery` | `discovery:read` |  | `update_ledger` | `work_item:manage` |
| `list_github_connections` | `github:read` |  | `waive_gates` | `code_task:override` |
| `list_harnesses` | `setup:read` |  | `watch` | `workflow_run:read` |
| `list_mcp_servers` | `mcp_server:read` |  | `watch_code_task` | `code_task:read` |
| `list_meetings` | `meeting:read` |  | `write_db` | `host:exec` |

`query_db` and `write_db` map to `host:exec` because they run arbitrary SQL. They are not delegable, so an agent cannot hold them through a grant. Today these two tools are already kept off the unscoped MCP surface (`_UNSCOPED_SQL_TOOLS`), and that stays.

## 8. Console pages and actions → permission

Each entry in `AREAS` (apps/web/app/ConsoleShell.tsx) gains a typed `permission`. If the viewer lacks it, the rail hides the area and a direct URL renders the Access page, which names the missing permission and who can grant it. Actions are wrapped in `<Can permission=…>`. A visible action the viewer cannot perform is shown disabled, with a tooltip such as "Needs workflow:publish · ask an Admin". It is not hidden. Server actions (`*-actions.ts`) are wrapped in `guardedAction(permission, fn)`. That wrapper is only for UX. The API stays the authority.

| Area id | Label | Page permission | Actions → permission |
|---|---|---|---|
| `overview` | Command Center | `project:read` | Build -> code_task:create; Resume or close -> workflow_run:control; Clean up -> system:operate |
| `code_tasks` | Factory | `code_task:read` | New build -> code_task:create; Steer -> code_task:steer; Cancel -> code_task:cancel; Waive gates -> code_task:override; Deploy -> deployment:create |
| `delivery_workflows` | Workflows | `workflow:read` | Draft/Save -> workflow:create; Publish -> workflow:publish; Run -> workflow_run:start; Retry step -> workflow_run:control; Schedule -> workflow_schedule:manage |
| `workforce` | Agent Studio | `agent:read` | New agent -> agent:create; Edit -> agent:update; Publish -> agent:publish; Give skill/MCP -> agent_ability:assign; Passport -> agent_passport:read |
| `decisions` | Approvals | `decision:read` | Approve/Reject -> decision:approve (+ seat tier); Delegate -> decision:delegate |
| `infra` | Infra | `infra:read` | Deploy -> deployment:create; Promote -> deployment:production; Rollback -> deployment:rollback; Retire host -> host:manage; Terminate -> infra:terminate |
| `observability` | Observability | `observability:read` | Deploy stack -> observability:manage; Probe -> observability:manage |
| `activity` | Activity log | `activity:read` | Export -> activity:export; Prune -> activity:prune |
| `discovery` | Research | `discovery:read` | Start research -> discovery:create |
| `meetings` | Panels | `meeting:read` | Convene -> meeting:convene; Seats -> meeting:configure |
| `agent` | BOSS Agent | `assistant:chat` | Chat -> assistant:chat; each proposed action -> that action's permission |
| `dashboard` | Mission Dashboard | `work_item:read` | - |
| `work` | Task Board | `work_item:read` | Add/move task -> work_item:manage |
| `portfolio` | Work Allocation | `work_item:read` | Quotas, releases -> work_item:manage |
| `processes` | Agent processes | `autopilot:read` | Advance -> autopilot:operate; Compile -> autopilot:configure |
| `shepherd` | Task Shepherd | `autopilot:read` | Autopilot/answer -> autopilot:operate |
| `reliability` | Reliability | `reliability:read` | Ack/resolve/remediate -> reliability:respond; Policies -> reliability:manage |
| `ai-sre` | AI SRE | `reliability:read` | Ask/overwatch -> reliability:respond |
| `reports` | Evidence | `activity:read` | - |
| `setup` | Repo and models | `setup:read` | Pick repo -> repository:manage; Add model -> model_provider:manage; Repo token -> github:connect |
| `skills` | Skills & MCP | `skill:read` | Import skill -> skill:manage; Register MCP -> mcp_server:register; Review/enable -> mcp_server:review; Holders -> agent_ability:assign |
| `budget` | Govern | `budget:read` | Caps/intensity -> budget:manage; Policies -> policy:manage |
| `pipeline` | Coding Pipeline | `pipeline:read` | Profiles -> pipeline:manage |
| `comms` | Communications | `comms:read` | Deliver -> comms:send; Policies/email -> comms:manage; Connect channel -> integration:manage |
| `ops` | Operations | `system:read` | Doctor act/sweeps -> system:operate; Dead letters -> deadletter:manage |
| `assurance` | Assurance | `assurance:read` | Run scan -> assurance:run; Claims/evidence -> assurance:manage |
| `org` | Access and packs (retired into Identity) | `pack:read` | Install pack -> pack:install |
| `github` | GitHub | `github:read` | Connect -> github:connect; Revoke/policies -> github:manage; Sync -> github:sync; Authorize merge -> github:merge |
| `capabilities` | Capabilities | `skill:read` | - |
| `sandboxes` | Sandboxes | `sandbox:read` | Exec -> sandbox:exec; Preserve/delete -> sandbox:manage |
| `foundations` | Foundations | `system:read` | - |
| `components` | Components | `system:read` | - |
| `enterprise` | Identity | `directory:read` | see the Identity sub-pages below |
| `identity/people` | Identity › People | `user:read` | Invite -> user:invite; Disable -> user:disable; Reset MFA -> user:reset_mfa; Sign out everywhere -> user:revoke_sessions |
| `identity/groups` | Identity › Groups | `group:read` | New group -> group:manage; Members -> group:manage_members |
| `identity/roles` | Identity › Access roles | `access_role:read` | New/clone/edit -> access_role:manage; Assign -> role_binding:assign |
| `identity/service-accounts` | Identity › Service accounts | `service_account:read` | New -> service_account:manage; Keys -> service_account_key:manage |
| `identity/agents` | Identity › Agents and delegations | `agent_passport:read` | Ceiling -> agent_passport:manage; Suspend -> agent_passport:suspend; Revoke -> agent_passport:revoke; Revoke grant -> delegation:revoke_any |
| `identity/sign-in` | Identity › Sign-in and SSO | `identity_provider:read` | Providers -> identity_provider:manage; Policy -> security_policy:manage |
| `identity/audit` | Identity › Security audit | `audit:read` | Export/verify chain -> audit:export |
| `me/security` | My account › Security | `authenticated` | Password, MFA, passkeys, sessions -> self (authenticated, step-up) |
| `me/agents` | My account › Agents acting for me | `authenticated` | Grant -> delegation:grant; Revoke own grant -> authenticated (own); Connect GitHub -> connection:use_own |
| `me/keys` | My account › API keys | `api_key:manage_own` | Create/rotate/revoke -> api_key:manage_own |

## 9. Notes from the mapping pass

These were found while mapping. Each needs a decision during the build:

1. **Composite console views** (`/v1/console/*`, `/v1/command-center/view`) return data from many areas in one response. Each must filter its sections by the caller's permissions. A test asserts that a Viewer-only principal receives no section they cannot read on its own.
2. **Worker-protocol routes** (`…/execute`, `…/apply` under github, deployments, discoveries, docs and orchestration) are already excluded from MCP. They become `worker:lease` and also require the job's lease token. They also require the run token of the principal the job runs for, so the worker can never act with authority the job's owner lacks.
3. **Investigation callbacks** (`/v1/investigations/{id}/db|do|host|run`) keep their per-investigation token. Starting an investigation that can touch a host or the database requires `host:exec`, checked when the investigation is created.
4. **POSTs that only read** (`/v1/budget/quote`, `/v1/budget/cap/preview`, `/v1/safety/branch-protection/simulate`, `/v1/roles/{role_key}/scope/check`) are mapped to read permissions on purpose. The catalogue entry records `readonly_post: true`, so the "every GET is read-only" test can tell them apart.
5. **`/v1/members` and `/v1/org-members`** (POST) become thin compatibility shims over `/v1/identity/users` and role bindings for one release, then are removed.
6. **Seeded GETs that mutate.** The research found routes that seed on read, such as default members. The read-only test (IDENTITY.md §15.4) will catch them. Each one becomes an explicit POST.
7. `/v1/a2a/*` writes are mapped to `integration:manage` for people. Agent-to-agent delivery goes through run tokens and needs `comms:send` on the run's effective set.
