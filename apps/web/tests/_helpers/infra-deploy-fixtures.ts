/**
 * Extra routes for Infra › Deploys sheets that mockInfraFetch() serves: CI config (catalog,
 * status, generate, validate), the deploy targets the `+ Deploy` sheet lists, and the routed
 * health check. Wire shapes are the API's (/v1/cicd/*, /v1/deployment-targets).
 */
import type { Route } from "./infra-fixtures";

export const ciCatalogFixture = () => ({
  vendors: [
    { id: "github_actions", name: "GitHub Actions", config_path: ".github/workflows/ci.yml", generates_config: true,
      live_adapter: true, adapter_kind: "github", description: "", languages: ["python", "typescript"] },
    { id: "gitlab_ci", name: "GitLab CI", config_path: ".gitlab-ci.yml", generates_config: true,
      live_adapter: false, adapter_kind: null, description: "", languages: ["python", "go"] },
  ],
  count: 2, supported_generate_targets: ["github_actions", "gitlab_ci"], notes: [],
});
export const ciStatusFixture = () => ({
  project_id: "project_1", vendors: ciCatalogFixture().vendors, supported_generate_targets: [],
  recent_runs: [{ id: "cir_1", project_id: "project_1", vendor: "github_actions", status: "passed", external_id: "991",
                  repository: "acme/invoice", ref: "main", recorded_at: "2026-10-04T10:00:00Z", source: "webhook" }],
  run_counts: { total_listed: 1, by_vendor: { github_actions: 1 }, by_status: { passed: 1 } }, links: {}, notes: [],
});
export const deploymentTargetsFixture = () => ({
  project_id: "project_1", limit: 200, offset: 0,
  items: [
    { id: "t_live", app_name: "invoice", environment: "prod", target_kind: "stack", repo: "acme/invoice", status: "active", host_id: "h_ready" },
    { id: "t_down", app_name: "api", environment: "prod", target_kind: "stack", repo: "acme/api", status: "active", host_id: "h_down" },
    { id: "t_cloud", app_name: "billing", environment: "prod", target_kind: "digitalocean_app", repo: "acme/billing", status: "active", host_id: null },
    { id: "t_gone", app_name: "old", environment: "prod", target_kind: "ssh", repo: "acme/old", status: "retired", host_id: null },
  ],
});

export const DEPLOY_EXTRA_ROUTES: Route[] = [
  { match: /^\/v1\/cicd\/catalog$/, reply: () => ciCatalogFixture() },
  { match: /^\/v1\/cicd\/status$/, reply: () => ciStatusFixture() },
  { method: "POST", match: /^\/v1\/cicd\/generate$/, reply: (_u, b) => ({
    target: "ci", vendor: (b as { vendor?: string })?.vendor ?? "github_actions", path: ".github/workflows/ci.yml",
    content: "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n", checksum: "abc123def456", commands: {} }) },
  { method: "POST", match: /^\/v1\/cicd\/validate$/, reply: () => ({ valid: true, vendor: "github_actions", errors: [], warnings: [], summary: { keys: ["jobs"], kind: null } }) },
  { match: /^\/v1\/deployment-targets$/, reply: () => deploymentTargetsFixture() },
  { method: "POST", match: /^\/v1\/deployment-runs\/[^/]+\/health-check$/, reply: () => ({
    results: [{ name: "GET /", ok: true, status: 200, latency_ms: 120 }] }) },
];
