/**
 * The permission matrix's shape: the generated catalogue grouped by category, then by resource
 * (the part before the colon), in catalogue order. System-only keys never appear: no access role
 * can hold them. Resource titles are the console's words for each resource.
 */
import { PERMISSIONS, PERMISSION_META, type Permission } from "../../../lib/identity/permissions.gen";

export type MatrixResource = { key: string; title: string; perms: Permission[] };
export type MatrixCategory = { name: string; resources: MatrixResource[]; perms: Permission[] };

const TITLES: Record<string, string> = {
  directory: "Directory", user: "People", group: "Groups", access_role: "Access roles", role_binding: "Role bindings",
  owner: "Owners", service_account: "Service accounts", service_account_key: "Service keys", api_key: "API keys",
  session: "Sessions", agent_passport: "Passports", delegation: "Delegations", connection: "Connections",
  identity_provider: "Identity providers", security_policy: "Security policy", audit: "Security audit", scim: "SCIM",
  org: "Org", project: "Projects", settings: "Settings", code_task: "Builds", repository: "Repositories",
  github: "GitHub", preview: "Previews", artifact: "Artifacts", workflow: "Workflows", workflow_run: "Runs",
  workflow_schedule: "Schedules", meeting: "Panels", discovery: "Research", work_item: "Tasks", job: "Jobs",
  deadletter: "Dead letters", assistant: "BOSS Agent", autopilot: "Autopilot", memory: "Memory", feedback: "Feedback",
  decision: "Decisions", approval_rule: "Approval rules", agent: "Agents", agent_ability: "Abilities",
  autonomy: "Autonomy", skill: "Skills", mcp_server: "MCP servers", pack: "Packs", deployment: "Deployments",
  deployment_target: "Targets", host: "Hosts", sandbox: "Sandboxes", infra: "Infra", cloud_account: "Cloud accounts",
  changeset: "Changesets", cicd: "CI/CD", setup: "Setup", model_provider: "Model providers", entitlement: "Entitlements",
  budget: "Budget", spend: "Spend", policy: "Policies", pipeline: "Pipeline", secret: "Secrets", kms_key: "KMS keys",
  integration: "Integrations", comms: "Comms", system: "System", observability: "Observability",
  reliability: "Reliability", assurance: "Assurance", improvement: "Improvement", activity: "Activity log",
  telemetry: "Telemetry", worker: "Worker",
};

export function resourceTitle(resource: string): string {
  return TITLES[resource] ?? resource.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

export function actionOf(p: Permission): string {
  return p.slice(p.indexOf(":") + 1).replace(/_/g, " ");
}

/** Every permission a role can hold. */
export const ROLE_PERMS: readonly Permission[] = PERMISSIONS.filter((p) => !PERMISSION_META[p].systemOnly);

export const MATRIX: readonly MatrixCategory[] = (() => {
  const cats: MatrixCategory[] = [];
  for (const p of ROLE_PERMS) {
    const meta = PERMISSION_META[p];
    let cat = cats.find((c) => c.name === meta.category);
    if (!cat) { cat = { name: meta.category, resources: [], perms: [] }; cats.push(cat); }
    const key = p.slice(0, p.indexOf(":"));
    let res = cat.resources.find((r) => r.key === key);
    if (!res) { res = { key, title: resourceTitle(key), perms: [] }; cat.resources.push(res); }
    res.perms.push(p);
    cat.perms.push(p);
  }
  return cats;
})();
