/**
 * EVERY CLIENT-SIDE WRITE THAT DOES NOT GO THROUGH A SERVER ACTION, AND THE PERMISSION ITS API
 * ROUTE CHECKS (P1-18, IDENTITY.md §6.5).
 *
 * callAction buttons carry their permission (lib/call-action.ts, lib/identity/action-permissions.ts).
 * The console's other writes leave the browser through lib/api-write.ts (apiWrite / runPress) —
 * from a helper module (lib/agents.ts, lib/kit-api.ts …), a dispatcher that names its operation
 * (`post("start")`, `setupWrite(p, "budget")`), or inline in a component. Each is listed here with
 * the permission the control plane's route declares (apps/api @permit; FX-B's changed declarations
 * in IDENTITY_BUILD §7: kit connect → integration:manage, workflow save/fork → workflow:publish).
 *
 * The UI reads it only to MIRROR: a control that would make a refused write is disabled up front
 * with the reason (components/gated.tsx). The API decides every call.
 * tests/every-write-is-pre-greyed.test.ts fails when a write path is missing here, or when a file
 * that presses one does not gate its permission.
 *
 * "self": the caller's own account or a person's own chat — no permission beyond being signed in
 * (or the route's permission is held by every role that can reach the screen).
 */
import type { Permission } from "./permissions.gen";

export type WriteGate = Permission | "self";

/** A helper (`module#export`, or `module#object.method`) → the permission(s) a press of it needs. */
export const WRITE_HELPERS: Readonly<Record<string, WriteGate | readonly Permission[]>> = {
  // lib/agents.ts — /v1/agents, /v1/role-skills, /v1/role-mcp-servers, /v1/workflow-mappings
  "lib/agents.ts#createAgent": "agent:create",                    // POST /v1/agents
  "lib/agents.ts#updateAgent": "agent:update",                    // PATCH /v1/agents/{role}
  "lib/agents.ts#deleteAgent": "agent:lifecycle",                 // DELETE /v1/agents/{role}
  "lib/agents.ts#repointMapping": "workflow:publish",             // POST /v1/workflow-mappings/{id}/repoint
  "lib/agents.ts#grantSkill": "agent_ability:assign",             // POST /v1/role-skills
  "lib/agents.ts#revokeSkill": "agent_ability:assign",            // DELETE /v1/role-skills
  "lib/agents.ts#grantMcp": "agent_ability:assign",               // POST /v1/role-mcp-servers
  "lib/agents.ts#revokeMcp": "agent_ability:assign",              // DELETE /v1/role-mcp-servers

  // lib/agent-world.ts — an agent's persona, lifecycle, memories, persona test
  "lib/agent-world.ts#savePersona": "agent:update",               // PATCH /v1/agents/{role}
  "lib/agent-world.ts#setLifecycle": "agent:lifecycle",           // POST /v1/agents/{role}/lifecycle
  "lib/agent-world.ts#draftPersonaField": "agent:create",         // POST /v1/agents/{role}/persona/draft
  "lib/agent-world.ts#forgetMemory": "memory:manage",             // POST /v1/memory/{id}/forget
  "lib/agent-world.ts#runPersonaTest": "agent:test",              // POST /v1/agents/{role}/test

  // app/agents/outside-agents-actions.ts — connected outside (external) agents (KG P6b)
  "app/agents/outside-agents-actions.ts#registerOutsideAgent": "agent:create",   // POST /v1/external-agents
  "app/agents/outside-agents-actions.ts#testOutsideAgent": "agent:test",         // POST /v1/external-agents/{id}/test
  "app/agents/outside-agents-actions.ts#suspendOutsideAgent": "agent:lifecycle", // POST /v1/external-agents/{id}/suspend
  "app/agents/outside-agents-actions.ts#removeOutsideAgent": "agent:lifecycle",  // POST /v1/external-agents/{id}/remove
  "app/agents/outside-agents-actions.ts#putOutsideAgentSecret": "secret:manage", // POST /v1/secrets

  // lib/agent-instructions.ts — an agent's instruction documents
  "lib/agent-instructions.ts#instructionsApi.add": "agent:update",          // POST /v1/agents/{role}/instructions
  "lib/agent-instructions.ts#instructionsApi.update": "agent:update",       // PATCH …/instructions/{id}
  "lib/agent-instructions.ts#instructionsApi.remove": "agent:update",       // DELETE …/instructions/{id}
  "lib/agent-instructions.ts#instructionsApi.skillFromFile": "skill:manage", // POST /v1/skills/from-file

  // lib/agent-converse.ts — a conversation with agents (create, turns, summary, proposals, start)
  "lib/agent-converse.ts#converseApi": "meeting:convene",                       // POST /v1/conversations
  // The hook's sends, summaries and proposals; its Start (workflow_run:start) is pressed only in
  // converse-parts.tsx ProposalCard, which gates it there.
  "lib/agent-converse.ts#useConversation": "meeting:convene",               // …/turns, …/summary, …/propose

  // lib/kit-api.ts — Skills & MCP (FX-B §7: connect/disconnect need integration:manage)
  "lib/kit-api.ts#kitApi.connect": "integration:manage",          // POST /v1/kit/{ability}/connect
  "lib/kit-api.ts#kitApi.disconnect": "integration:manage",       // DELETE /v1/kit/{ability}/connection
  "lib/kit-api.ts#kitApi.test": "skill:invoke",                   // POST /v1/kit/{ability}/test
  "lib/kit-api.ts#kitApi.testAll": "skill:invoke",                // POST /v1/kit/test-all
  "lib/kit-api.ts#kitApi.tryIt": "skill:invoke",                  // POST /v1/kit/{ability}/try
  "lib/kit-api.ts#kitApi.holders": "agent_ability:assign",        // PUT /v1/kit/{ability}/holders
  "lib/kit-api.ts#kitApi.policy": "skill:manage",                 // POST /v1/kit/{ability}/policy
  "lib/kit-api.ts#kitApi.enabled": "skill:manage",                // POST /v1/kit/{ability}/enabled
  "lib/kit-api.ts#kitApi.recommended": "agent_ability:assign",    // POST /v1/kit/recommended
  "lib/kit-api.ts#kitApi.testCrew": "skill:invoke",               // POST /v1/kit/test-crew
  "lib/kit-api.ts#mcpApi.test": "mcp_server:register",            // POST /v1/mcp/servers/test
  "lib/kit-api.ts#mcpApi.register": "mcp_server:review",          // POST /v1/mcp/servers
  "lib/kit-api.ts#mcpApi.update": "mcp_server:review",            // PATCH /v1/mcp/servers/{id}
  "lib/kit-api.ts#mcpApi.review": "mcp_server:review",            // POST /v1/mcp/servers/{id}/review
  "lib/kit-api.ts#mcpApi.remove": "mcp_server:review",            // DELETE /v1/mcp/servers/{id}
  "lib/kit-api.ts#mcpApi.policy": "mcp_server:review",            // POST /v1/mcp/servers/{id}/policy
  "lib/kit-api.ts#mcpApi.grant": "agent_ability:assign",          // POST /v1/role-mcp-servers
  "lib/kit-api.ts#mcpApi.revoke": "agent_ability:assign",         // DELETE /v1/role-mcp-servers
  "lib/kit-api.ts#mcpApi.roleTools": "agent_ability:assign",      // POST /v1/role-mcp-servers
  "lib/kit-api.ts#mcpApi.addFromCatalogue": "mcp_server:review",  // POST /v1/mcp/catalogue/{key}
  "lib/kit-api.ts#mcpApi.createStdioTemplate": "host:exec",       // POST /v1/mcp/stdio-templates
  "lib/kit-api.ts#mcpApi.createGateway": "mcp_server:review",     // POST /v1/mcp/gateways
  "lib/kit-api.ts#mcpApi.mintGatewayToken": "mcp_server:review",  // POST /v1/mcp/gateways/{id}/tokens
  "lib/kit-api.ts#mcpApi.revokeGatewayToken": "mcp_server:review", // POST /v1/mcp/gateways/{id}/tokens/{t}/revoke
  "lib/kit-api.ts#mcpApi.revokeGateway": "mcp_server:review",     // POST /v1/mcp/gateways/{id}/revoke
  // BOSSAPP-846..848: sign-in, whose credential, tools granted on request.
  "lib/kit-api.ts#mcpApi.signIn": "mcp_server:review",            // POST /v1/mcp/servers/{id}/oauth/start
  "lib/kit-api.ts#mcpApi.signInMine": "connection:use_own",       // POST /v1/mcp/servers/{id}/oauth/mine
  "lib/kit-api.ts#mcpApi.credentialPolicy": "mcp_server:review",  // POST /v1/mcp/servers/{id}/credential-policy
  // DELETE …/credentials/mine/{id} (your own: connection:use_own) or …/credentials/{id}
  // (anyone's: mcp_server:review); each button gates its own.
  "lib/kit-api.ts#mcpApi.removeCredential": ["connection:use_own", "mcp_server:review"],
  "lib/kit-api.ts#mcpApi.revokeGrant": "agent_ability:assign",    // POST /v1/mcp/access-grants/{id}/revoke

  // lib/workflow-webhooks.ts — a workflow's webhook triggers (#512, through /api/delivery-workflows)
  "lib/workflow-webhooks.ts#createWebhook": "workflow_schedule:manage",      // POST /v1/delivery-workflows/webhooks
  "lib/workflow-webhooks.ts#rotateWebhook": "workflow_schedule:manage",      // POST …/webhooks/{id}/rotate
  "lib/workflow-webhooks.ts#setWebhookEnabled": "workflow_schedule:manage",  // POST …/webhooks/{id}/enabled

  // lib/workflow-api.ts — the Workflow Studio editor (through /api/delivery-workflows)
  "lib/workflow-api.ts#publishSpec": "workflow:publish",          // POST /v1/delivery-workflows/publish
  "lib/workflow-api.ts#renameWorkflow": "workflow:create",        // PATCH /v1/delivery-workflows/{id}
  "lib/workflow-api.ts#deleteWorkflow": "workflow:delete",        // DELETE /v1/delivery-workflows/{id}
  "lib/workflow-api.ts#draftFromDescription": "workflow:create",  // POST /v1/delivery-workflows/draft
  "lib/workflow-api.ts#startTestRun": "workflow_run:start",       // POST /v1/delivery-workflows/test-runs
  // POST /v1/delivery-workflows/{key}/designs/{stage}/review|revise (workflow:create), …/lock
  // (workflow:publish); each button gates its own (agent-contract-view.tsx DesignBar).
  "lib/workflow-api.ts#designAction": ["workflow:create", "workflow:publish"],

  // lib/hardware.ts — Infra › hardware places
  "lib/hardware.ts#registerLocalPlace": "host:manage",            // POST /v1/hardware/places/local
  "lib/hardware.ts#enrollHardwareAgent": "host:manage",           // POST /v1/hardware/places/agents
  "lib/hardware.ts#registerHardwareProvider": "host:manage",      // POST /v1/hardware/places/providers
  // lib/add-server.ts — Infra › Add a server
  "lib/add-server.ts#createServer": "host:manage",                // POST /v1/servers (step-up)
  // lib/work-items.ts — Project › Work › Tree (BOSSAPP-830/831)
  "lib/work-items.ts#createItem": "work_item:manage",             // POST /v1/work-items
  "lib/work-items.ts#updateItem": "work_item:manage",             // PATCH /v1/work-items/{id}
  "lib/work-items.ts#completeItem": "work_item:manage",           // POST …/{id}/complete
  "lib/work-items.ts#relateItem": "work_item:manage",             // POST …/{id}/relations
  // lib/tool-policies.ts — Skills & MCP › Rules (BOSSAPP-815/817)
  "lib/tool-policies.ts#createRule": "mcp_server:review",         // POST /v1/tool-policies
  "lib/tool-policies.ts#updateRule": "mcp_server:review",         // PATCH /v1/tool-policies/{id}
  "lib/tool-policies.ts#revokeRule": "mcp_server:review",         // DELETE /v1/tool-policies/{id}
  "lib/tool-policies.ts#simulate": "mcp_server:read",             // POST …/simulate (records nothing)
  // lib/skill-policies.ts — Skills & MCP › Rules › Skill access (BOSSAPP-890)
  "lib/skill-policies.ts#createSkillRule": "skill:manage",        // POST /v1/skill-policies
  "lib/skill-policies.ts#updateSkillRule": "skill:manage",        // PATCH /v1/skill-policies/{id}
  "lib/skill-policies.ts#revokeSkillRule": "skill:manage",        // DELETE /v1/skill-policies/{id}
  "lib/skill-policies.ts#simulateSkillRule": "skill:read",        // POST …/simulate (records nothing)
  // lib/subscription-pools.ts — Setup › Advanced › Several accounts per provider (BOSSAPP-886)
  "lib/subscription-pools.ts#createPool": "entitlement:manage",        // POST /v1/subscription-pools
  "lib/subscription-pools.ts#addPoolMember": "entitlement:manage",     // POST …/{id}/members
  "lib/subscription-pools.ts#updatePoolMember": "entitlement:manage",  // PATCH …/members/{id}
  "lib/subscription-pools.ts#removePoolMember": "entitlement:manage",  // DELETE …/members/{id}
  // lib/llm-gateways.ts — Setup › Advanced › Gateways (BOSSAPP-887)
  "lib/llm-gateways.ts#createGateway": "model_provider:manage",   // POST /v1/llm-gateways
  "lib/llm-gateways.ts#updateGateway": "model_provider:manage",   // PATCH /v1/llm-gateways/{id}
  "lib/llm-gateways.ts#deleteGateway": "model_provider:manage",   // DELETE /v1/llm-gateways/{id}
  "lib/llm-gateways.ts#testGateway": "model_provider:manage",     // POST …/{id}/test (one live call)
  // lib/reflection.ts — Activity › Feedback › Reflection (BOSSAPP-888)
  "lib/reflection.ts#setReflection": "agent:update",              // PUT /v1/reflection/settings
  "lib/reflection.ts#runReflection": "agent:update",              // POST /v1/reflection/run
  // lib/agent-feedback.ts — Activity › Feedback (BOSSAPP-889)
  "lib/agent-feedback.ts#setFeedbackStatus": "agent:update",     // PATCH /v1/agent-feedback/{id}
  // lib/watch-app.ts — Project › Health › Watch this app
  "lib/watch-app.ts#startWatching": "observability:manage",       // POST /v1/projects/{id}/watch
  "lib/watch-app.ts#verifyChannel": "observability:manage",       // POST …/watch/{target}/verify
  "lib/watch-app.ts#resendCode": "observability:manage",          // POST …/watch/{target}/resend
  "lib/watch-app.ts#changeWatch": "observability:manage",         // POST …/watch/{target}/change

  // lib/stack-deploy.ts — stack deployments
  "lib/stack-deploy.ts#createStackTarget": "deployment_target:manage", // POST /v1/deployments/stack/targets
  "lib/stack-deploy.ts#planStackDeploy": "deployment:create",          // POST /v1/deployments/stack/plan
  "lib/stack-deploy.ts#decideStackDeploy": "decision:approve",         // POST /v1/decisions/{id}/{verdict}
  "lib/stack-deploy.ts#rollbackStackDeploy": "deployment:rollback",    // POST /v1/deployments/stack/{id}/rollback

  // lib/knowledge-graph.ts, lib/security-scan.ts — the two client-side writes in those modules
  "lib/knowledge-graph.ts#seedGraphFromInfra": "observability:manage", // POST /v1/observability-suite/graph-seed
  "lib/security-scan.ts#evaluateSecurityGate": "assurance:run",        // POST /v1/security-scan/runs/{id}/gate

  // lib/kg-view.ts — the Graph area (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §3, §5.7). Rule review is
  // owners/admins (operator decision): knowledge:review, which only they hold.
  "lib/kg-view.ts#approveRule": "knowledge:review",   // POST /v1/knowledge/view/rules/{id}/approve
  "lib/kg-view.ts#editRule": "knowledge:review",      // POST /v1/knowledge/view/rules/{id}/edit
  "lib/kg-view.ts#rejectRule": "knowledge:review",    // POST /v1/knowledge/view/rules/{id}/reject
  "lib/kg-view.ts#indexNow": "knowledge:read",        // POST /v1/knowledge/repositories/index (read-safe)
  "lib/kg-view.ts#addDocument": "knowledge:manage",   // POST /v1/knowledge/documents
  "lib/kg-view.ts#setPack": "knowledge:manage",       // POST /v1/knowledge/packs
  "lib/kg-view.ts#loadAmlSample": "knowledge:manage", // POST /v1/knowledge/samples/aml
  "lib/kg-view.ts#loadDqPlatform": "knowledge:manage", // POST /v1/knowledge/samples/dq
  "lib/kg-view.ts#scheduleDqIntake": "knowledge:manage", // POST /v1/knowledge/samples/dq/schedule
  "lib/kg-view.ts#stageUpload": "knowledge:manage",   // POST /v1/knowledge/documents/uploads
  "lib/kg-view.ts#editUpload": "knowledge:manage",    // POST /v1/knowledge/documents/uploads/{id}/edit
  "lib/kg-view.ts#confirmUpload": "knowledge:manage", // POST /v1/knowledge/documents/uploads/{id}/confirm
  "lib/kg-view.ts#discardUpload": "knowledge:manage", // POST /v1/knowledge/documents/uploads/{id}/discard
  "lib/kg-view.ts#stageDqRulebook": "knowledge:manage", // POST /v1/knowledge/samples/dq-rulebook
  "lib/kg-view.ts#promoteFact": "knowledge:manage",   // POST /v1/knowledge/facts/{id}/promote
  "lib/kg-view.ts#rejectFact": "knowledge:manage",    // POST /v1/knowledge/facts/{id}/reject
  "lib/kg-view.ts#forgetFact": "knowledge:manage",    // POST /v1/knowledge/facts/{id}/forget
  // lib/kg-setup.ts — a knowledge-graph project's setup: the team step (the default-team door).
  "lib/kg-setup.ts#setProjectTeam": "work_item:manage", // POST /v1/projects/{id}/agents action=set

  // app/delivery-workflows-ui.tsx — a run's answer pressed on another screen (Command Center)
  "app/delivery-workflows-ui.tsx#sendRunAnswer": "workflow_run:control", // retry-step / skip-step / recheck / input

  // app/factory/legacy-build-parts.tsx — Factory › retire a deploy target
  "app/factory/legacy-build-parts.tsx#retireTarget": "deployment_target:manage", // DELETE /v1/deployment-targets/{id}
};

/** Dispatchers: the first string literal a call passes names the operation. */
export const WRITE_OPS: Readonly<Record<string, Readonly<Record<string, WriteGate>>>> = {
  // app/workflows/delivery-api.ts `post(action, …)` → /api/delivery-workflows → /v1/delivery-workflows/*
  "app/workflows/delivery-api.ts#post": {
    install: "workflow:install", apply: "workflow:publish", create: "workflow:create",
    fork: "workflow:publish", save: "workflow:publish", start: "workflow_run:start",
    advance: "worker:lease", define_step: "workflow:create", schedule: "workflow_schedule:manage",
    schedule_enabled: "workflow_schedule:manage", publish: "workflow:publish", draft: "workflow:create",
    retry_step: "workflow_run:control", skip_step: "workflow_run:control", recheck: "workflow_run:control",
    set_input: "workflow_run:control", prepare_target: "deployment_target:manage",
    start_preview: "preview:manage", stop: "workflow_run:control",
    // A NEW run from one step of a finished one (#512): a start, not a control of the old run.
    rerun_from: "workflow_run:start",
  },
  // lib/govern.ts `governWrite(op, …)` → /api/govern → lib/govern-ops.ts (the two previews only read)
  "lib/govern.ts#governWrite": {
    setting: "settings:update", pace: "budget:manage", pace_preview: "self", cap_preview: "self", // budget:read, which opening Govern needs
    monthly_cap: "budget:manage", reconcile: "budget:manage", initiative: "budget:manage",
    limits: "budget:manage", project_limit: "budget:manage", // Govern › Limits (BOSSAPP-807)
  },
  // lib/setup-client.ts `setupWrite(projectId, op, …)` → /api/setup → /v1/setup/*
  "lib/setup-client.ts#setupWrite": {
    checks: "setup:manage", repo_token: "github:connect", repo_select: "repository:register",
    repo_app: "github:connect", repo_signin: "github:connect", repo_default: "repository:manage", repo_remove: "repository:manage",
    models: "model_provider:manage", model_catalogue: "model_provider:manage", source_model: "model_provider:manage",
    model_remove: "model_provider:manage", job: "setup:manage", budget: "budget:manage", try: "workflow_run:start",
    decision_key: "model_provider:manage", routing: "setup:manage", routing_catalogue: "model_provider:manage",
    ci_workflows: "github:connect", build_database: "settings:update", build_speed: "settings:update",
    release_hold: "entitlement:manage", embedding_model: "model_provider:manage",
    embedding_model_check: "model_provider:manage", embedding_model_remove: "model_provider:manage",
    // Subscription Sign in (BOSSAPP-769): the same permission as POST /v1/setup/models.
    sign_in_start: "model_provider:manage", sign_in_code: "model_provider:manage",
    sign_in_cancel: "model_provider:manage",
  },
  // lib/improve.ts `improveWrite(op, …)` → /api/improve (lib/improve-ops.ts)
  "lib/improve.ts#improveWrite": {
    switch: "telemetry:manage", weights: "telemetry:manage", collector_url: "telemetry:manage",
    mask_words: "telemetry:manage", rotate: "telemetry:manage", forget: "telemetry:manage",
    share_run: "telemetry:share", backfill: "system:operate", experiment: "improvement:manage",
    experiment_stop: "improvement:manage", experiment_promote: "improvement:manage",
    feedback: "feedback:submit", prompt_shown: "feedback:submit", checkin: "feedback:submit",
  },
  // lib/self-fix.ts `selfFixWrite(op, …)` → /api/self-fix (lib/self-fix-ops.ts); every one is
  // improvement:manage at the API (apps/api/app/routers/self_fix.py). Approval is not here.
  "lib/self-fix.ts#selfFixWrite": {
    start: "improvement:manage", cancel: "improvement:manage", settings: "improvement:manage",
    preview: "improvement:manage", edit_preview: "improvement:manage", send: "improvement:manage",
    fork: "improvement:manage", remove: "improvement:manage", remove_all: "improvement:manage",
  },
  // app/observability/obs-write.ts `obsWrite(path, …)`
  "app/observability/obs-write.ts#obsWrite": {
    "/v1/observability-suite/recommend": "observability:read",
    "/v1/observability/stack/deploy": "observability:manage",
    "/v1/observability-suite/health-check": "observability:manage",
    "/v1/observability/apps/": "observability:manage",
    // The rule sheet: the PromQL dry run is a read (observability:read, which opening Observability
    // needs); PUT /v1/alert-rules/{id}/notify is a change.
    "/v1/alert-rules/preview": "self",
    "/v1/alert-rules/": "observability:manage",
    // The incident card's "Tell reporters": POST …/incidents/{id}/tell-reporters.
    "/v1/reliability/incidents/": "reliability:respond",
    // The work tree (lib/work-items.ts, BOSSAPP-830): every change to the graph.
    "/v1/work-items": "work_item:manage",
    // A typed question's Send answer / Decline (app/typed-form-ui.tsx, BOSSAPP-832); "Approve +
    // always allow" on a tool-call Ask card (app/always-allow-ui.tsx, BOSSAPP-817).
    "/v1/decisions/": "decision:approve",
    // Skills & MCP › Rules (lib/tool-policies.ts, BOSSAPP-815): the simulate box is a read.
    "/v1/tool-policies/simulate": "mcp_server:read",
    "/v1/tool-policies": "mcp_server:review",
    // Skills & MCP › Rules › Skill access (lib/skill-policies.ts, BOSSAPP-890): simulate reads.
    "/v1/skill-policies/simulate": "skill:read",
    "/v1/skill-policies": "skill:manage",
    // Setup › Advanced › Several accounts per provider (lib/subscription-pools.ts, BOSSAPP-886).
    "/v1/subscription-pools": "entitlement:manage",
    // Setup › Advanced › Gateways (lib/llm-gateways.ts, BOSSAPP-887).
    "/v1/llm-gateways": "model_provider:manage",
    // Activity › Feedback › Reflection (lib/reflection.ts, BOSSAPP-888).
    "/v1/reflection/": "agent:update",
    // Activity › Feedback (lib/agent-feedback.ts, BOSSAPP-889): a person closes a record.
    "/v1/agent-feedback/": "agent:update",
  },
};

/** Components that write inline (apiWrite / runPress in the file itself): what their presses need. */
export const INLINE_WRITES: Readonly<Record<string, readonly WriteGate[]>> = {
  "app/agent-quick-create.tsx": ["agent:publish"],                 // POST /v1/agent-studio/templates/{id}/install
  "app/agent-ui.tsx": ["assistant:chat"],                          // /api/agent-thread, /api/agent clear
  "app/agents-on-this.tsx": ["work_item:manage"],                  // POST /v1/projects/{id}/agents
  "app/default-agent-picker.tsx": ["work_item:manage"],            // POST /v1/projects/{id}/agents
  "app/ask-boss-hero.tsx": ["workflow:publish", "workflow_run:start", "discovery:create"], // apply, /api/builds, /api/discoveries
  "app/delivery-workflows-ui.tsx": ["workflow_schedule:manage", "workflow:create", "deployment:create", "deployment_target:manage"],
  "app/factory/legacy-build-parts.tsx": ["deployment:create", "deployment_target:manage"], // /api/app-deploy, retire
  "app/memory-ui.tsx": ["memory:manage"],                          // POST /v1/memory/{id}/{forget|review}
  "app/run-report-ui.tsx": ["workflow_run:control", "workflow:publish"], // POST …/report, apply
  "app/sandboxes-ui.tsx": ["sandbox:exec"],                        // POST /v1/sandboxes/{id}/exec
  "app/setup/capacity-row.tsx": ["entitlement:manage"],            // /v1/entitlements/…
  "app/skills/skill-drawer.tsx": ["skill:manage", "agent_ability:assign"], // /api/skills enable/disable/delete, assign
  "app/skills/skill-history.tsx": ["skill:manage", "skill:invoke"], // /api/skill-supply restore/publish/cases/grade, run_tests
  "app/skills/skill-source-import.tsx": ["skill:manage"],         // /api/skill-supply preview_source/import_source
  "app/skills/youtube-connection.tsx": ["integration:manage"],     // /v1/channels/youtube/*
  "app/workflows/captain-can-run-it.tsx": ["agent_ability:assign"], // /api/skills assign → POST /v1/role-skills
  "app/workflows/editor/define-step-modal.tsx": ["workflow:create"], // custom-steps POST/PATCH
  "app/workflows/editor/inspector/workflow-settings.tsx": ["workflow:publish"], // apply
  "app/workflows/new-project-composer.tsx": ["work_item:manage"],  // POST /v1/projects/{id}/agents
  "app/workforce-ui.tsx": ["agent:update"],                        // PUT/DELETE /v1/console/v2/org/{agent}, POST …/org/takeover
  "components/v2/AgentChat.tsx": ["assistant:chat"],               // POST /v1/engine/agent/chat
  "components/v2/AutonomyControl.tsx": ["approval_rule:manage"],   // POST /v1/console/v2/autonomy
  "components/v2/DeployControls.tsx": ["deployment:create", "deployment_target:manage"],
  "components/v2/LaunchModal.tsx": ["meeting:convene"],            // POST /v1/meetings/prd
};
