/**
 * CONSOLE AREA -> ACCESS (docs/product/IDENTITY.md §6.5, D6; IDENTITY_PERMISSIONS.md §8).
 *
 * Every console area declares what opening it needs: the read permission of its primary resource
 * (D6: no separate page:* keys), or `authenticated` for the viewer's own pages. The shell shows an
 * area in the rail only when `can()` allows it, and renders <AccessDenied> for a direct URL to one
 * it does not (stream S4). Actions inside an area use <Can permission=…> with the keys in
 * AREA_ACTIONS. All of this is UX only: the API re-checks every call.
 *
 * AREA_ACCESS is a TOTAL Record: a new AreaId does not type-check until it declares its access
 * (the shrinking list tests/identity/undeclared_console_areas.txt emptied and was deleted, S2).
 */
import type { AreaId } from "../area-ids";
import type { Permission } from "./permissions.gen";

export type AreaAccess =
  | { readonly permission: Permission }
  | { readonly authenticated: true };

const p = (permission: Permission): AreaAccess => ({ permission });

export const AREA_ACCESS: Readonly<Record<AreaId, AreaAccess>> = {
  overview: p("project:read"),
  code_tasks: p("code_task:read"),
  delivery_workflows: p("workflow:read"),
  workforce: p("agent:read"),
  decisions: p("decision:read"),
  infra: p("infra:read"),
  delivery: p("infra:read"), // legacy id, redirects to Infra (app/legacy-areas.ts)
  observability: p("observability:read"),
  monitoring: p("observability:read"), // legacy id, redirects to Observability
  activity: p("activity:read"),
  discovery: p("discovery:read"),
  meetings: p("meeting:read"),
  agent: p("assistant:chat"),
  dashboard: p("work_item:read"),
  work: p("work_item:read"),
  portfolio: p("work_item:read"),
  processes: p("autopilot:read"),
  shepherd: p("autopilot:read"),
  reliability: p("reliability:read"),
  "ai-sre": p("reliability:read"),
  reports: p("activity:read"),
  setup: p("setup:read"),
  skills: p("skill:read"),
  budget: p("budget:read"),
  comms: p("comms:read"),
  ops: p("system:read"),
  assurance: p("assurance:read"),
  org: p("pack:read"),
  github: p("github:read"),
  capabilities: p("skill:read"),
  sandboxes: p("sandbox:read"),
  foundations: p("system:read"),
  components: p("system:read"),
  enterprise: p("directory:read"),
};

/** The Identity sub-pages and My-account pages (not rail areas; S4 routes them). */
export const IDENTITY_PAGE_ACCESS = {
  "identity/people": p("user:read"),
  "identity/groups": p("group:read"),
  "identity/roles": p("access_role:read"),
  "identity/service-accounts": p("service_account:read"),
  "identity/agents": p("agent_passport:read"),
  "identity/sign-in": p("identity_provider:read"),
  "identity/audit": p("audit:read"),
  "me/security": { authenticated: true },
  "me/agents": { authenticated: true },
  "me/keys": p("api_key:manage_own"),
} as const satisfies Readonly<Record<string, AreaAccess>>;

export type IdentityPageId = keyof typeof IDENTITY_PAGE_ACCESS;

/**
 * The actions each page offers, and the permission each needs (IDENTITY_PERMISSIONS.md §8).
 * A visible action the viewer cannot take is shown DISABLED with "Needs <permission>", not hidden.
 */
export const AREA_ACTIONS = {
  overview: { build: "code_task:create", resumeOrClose: "workflow_run:control", cleanUp: "system:operate" },
  code_tasks: {
    newBuild: "code_task:create", steer: "code_task:steer", cancel: "code_task:cancel",
    retry: "code_task:retry", waiveGates: "code_task:override", deploy: "deployment:create",
  },
  delivery_workflows: {
    draft: "workflow:create", publish: "workflow:publish", run: "workflow_run:start",
    retryStep: "workflow_run:control", schedule: "workflow_schedule:manage", delete: "workflow:delete",
  },
  workforce: {
    newAgent: "agent:create", edit: "agent:update", publish: "agent:publish",
    giveAbility: "agent_ability:assign", passport: "agent_passport:read",
  },
  decisions: { approve: "decision:approve", reject: "decision:approve", delegate: "decision:delegate" },
  infra: {
    deploy: "deployment:create", promote: "deployment:production", rollback: "deployment:rollback",
    retireHost: "host:manage", terminate: "infra:terminate",
  },
  observability: { deployStack: "observability:manage", probe: "observability:manage" },
  activity: { export: "activity:export", prune: "activity:prune" },
  discovery: { startResearch: "discovery:create" },
  meetings: { convene: "meeting:convene", seats: "meeting:configure" },
  agent: { chat: "assistant:chat" },
  work: { addOrMoveTask: "work_item:manage" },
  portfolio: { quotasAndReleases: "work_item:manage" },
  processes: { advance: "autopilot:operate", compile: "autopilot:configure" },
  shepherd: { autopilot: "autopilot:operate", answer: "autopilot:operate" },
  reliability: { respond: "reliability:respond", policies: "reliability:manage" },
  "ai-sre": { askOrOverwatch: "reliability:respond" },
  setup: { pickRepo: "repository:manage", addModel: "model_provider:manage", repoToken: "github:connect" },
  skills: {
    importSkill: "skill:manage", registerMcp: "mcp_server:register", reviewMcp: "mcp_server:review",
    holders: "agent_ability:assign",
  },
  budget: { capsAndIntensity: "budget:manage", policies: "policy:manage" },
  comms: { deliver: "comms:send", policies: "comms:manage", connectChannel: "integration:manage" },
  ops: { doctorAct: "system:operate", deadLetters: "deadletter:manage" },
  assurance: { runScan: "assurance:run", claimsAndEvidence: "assurance:manage" },
  org: { installPack: "pack:install" },
  github: {
    connect: "github:connect", revokeOrPolicies: "github:manage", sync: "github:sync",
    authorizeMerge: "github:merge",
  },
  sandboxes: { exec: "sandbox:exec", preserveOrDelete: "sandbox:manage" },
  "identity/people": {
    invite: "user:invite", disable: "user:disable", resetMfa: "user:reset_mfa",
    signOutEverywhere: "user:revoke_sessions",
  },
  "identity/groups": { newGroup: "group:manage", members: "group:manage_members" },
  "identity/roles": { editRole: "access_role:manage", assign: "role_binding:assign", assignOwner: "owner:assign" },
  "identity/service-accounts": { create: "service_account:manage", keys: "service_account_key:manage" },
  "identity/agents": {
    ceiling: "agent_passport:manage", suspend: "agent_passport:suspend", revoke: "agent_passport:revoke",
    revokeGrant: "delegation:revoke_any",
  },
  "identity/sign-in": { providers: "identity_provider:manage", policy: "security_policy:manage" },
  "identity/audit": { exportOrVerify: "audit:export" },
  "me/agents": { grant: "delegation:grant", connectGitHub: "connection:use_own" },
  "me/keys": { manage: "api_key:manage_own" },
} as const satisfies Partial<Record<AreaId | IdentityPageId, Readonly<Record<string, Permission>>>>;
