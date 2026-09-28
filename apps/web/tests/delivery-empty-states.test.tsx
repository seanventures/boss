import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

/**
 * BOSS-344 — CI/CD + Monitoring empty-state honesty.
 *
 * When live has 0 deployment targets and the observability stack is not
 * deployed, Mission Control must not paint "healthy deploy" / "stack not
 * running" chrome that reads like a live fleet. These assert what an operator
 * can SEE on first paint.
 */

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

vi.mock("../app/actions", () => ({
  getDeliveryPipelineAction: vi.fn(async () => null),
  getDeliveryDefaultsAction: vi.fn(async () => ({ owner: "example-org", repo: "boss-sandbox" })),
  listReposAction: vi.fn(async () => ({ repos: [] })),
  listRepoAppsAction: vi.fn(async () => ({ apps: [] })),
  listRefsAction: vi.fn(async () => ({ branches: [] })),
  deployTargetAction: vi.fn(async () => ({})),
  deployObservabilityAction: vi.fn(async () => ({})),
  getDeploymentAction: vi.fn(async () => ({})),
  redeployAction: vi.fn(async () => ({})),
  putSecretAction: vi.fn(async () => ({})),
  generateKeypairAction: vi.fn(async () => ({})),
  verifySecretAction: vi.fn(async () => ({})),
  bindSecretAction: vi.fn(async () => ({})),
  createTargetAction: vi.fn(async () => ({})),
  bootstrapTargetAction: vi.fn(async () => ({})),
  preflightTargetAction: vi.fn(async () => ({})),
  setAutoDeployAction: vi.fn(async () => ({})),
  updateTargetAction: vi.fn(async () => ({})),
  purgeTargetAction: vi.fn(async () => ({})),
  reactivateTargetAction: vi.fn(async () => ({})),
  teardownTargetAction: vi.fn(async () => ({})),
}));

const {
  DeliveryPipeline,
  DeploymentHealth,
  isHealthUnconfigured,
  SandboxDeployFlagNotice,
} = await import("../app/delivery-ui");
const {
  isObservabilityUnconfigured,
  MonitoringPanel,
} = await import("../app/delivery-manage");
const { MonitoringArea } = await import("../app/monitoring-ui");

const emptyPipeline = {
  project_id: "boss-dogfood",
  stages: [],
  targets: [],
  configured: false,
  sandbox_deploy_enabled: false,
  sandbox_merge_enabled: false,
};

const emptyHealth = {
  project_id: "boss-dogfood",
  targets: [] as never[],
  summary: {
    up: 0,
    degraded: 0,
    down: 0,
    unknown: 0,
    not_deployed: 0,
    overall: "not_deployed",
    total: 0,
  },
};

const emptyObservability = {
  deployed: false,
  grafana_url: "",
  apps: [] as never[],
  status: { running: null as boolean | null },
};

function html(node: React.ReactElement) {
  return renderToStaticMarkup(node);
}

describe("CI/CD empty-state honesty (BOSS-344)", () => {
  it("isHealthUnconfigured is true for zero targets / total 0", () => {
    expect(isHealthUnconfigured(emptyHealth)).toBe(true);
    expect(isHealthUnconfigured({
      ...emptyHealth,
      targets: [{
        target_id: "dt_1",
        app_name: "demo",
        environment: "staging",
        url: null,
        status: "not_deployed",
        status_since: null,
        uptime_24h: null,
        uptime_7d: null,
        avg_ms: null,
        p95_ms: null,
        last_checked: null,
        last_latency_ms: null,
        last_error: null,
        sample_count: 0,
        series: [],
        incidents: [],
        deployed_commit: null,
        deployed_at: null,
        deployment_run_id: null,
      }],
      summary: { ...emptyHealth.summary, total: 1, not_deployed: 1 },
    })).toBe(false);
  });

  it("renders a setup empty state when no targets — no Deploy wizard chrome", () => {
    const markup = html(
      <DeliveryPipeline
        initial={emptyPipeline}
        secrets={{ project_id: "boss-dogfood", secrets: [] }}
        deployments={{ deployments: [] }}
      />,
    );
    expect(markup).toContain('data-empty-state="no-targets"');
    // Renamed from "No deployment targets": the empty state now leads with the FLEET, because
    // a target names the host it deploys to — so the honest first step is registering a
    // machine, not registering an app.
    expect(markup).toContain("Nothing deploys yet");
    expect(markup).toContain("Onboard an app");
    // THE DEADLOCK THIS GUARDS: the Hosts wizard was added only to the configured branch, so
    // it appeared once a deployment target already existed — unreachable on a fresh install,
    // which is exactly when it is needed. An empty state must contain the thing that ends it.
    expect(markup).toContain('data-testid="fleet-panel"');
    expect(markup).toContain("Register a host");
    // Deploy wizard is a live-fleet control; must not appear when nothing is registered.
    expect(markup).not.toContain("Deploy an app");
    expect(markup).not.toContain("Recent deployments");
    // Fail-closed sandbox flag is surfaced honestly.
    expect(markup).toContain("BOSS_ENABLE_SANDBOX_DEPLOY=false");
    expect(markup).toContain('data-testid="sandbox-deploy-disabled"');
  });

  it("shows deploy machinery only after a target is configured", () => {
    const markup = html(
      <DeliveryPipeline
        initial={{
          project_id: "boss-dogfood",
          stages: [],
          targets: [{
            id: "dt_1",
            app_name: "demo",
            environment: "staging",
            repo: "example-org/boss-sandbox",
            target_kind: "ssh",
            target_ref: "ubuntu@10.0.0.1",
            status: "active",
          }],
          configured: true,
          sandbox_deploy_enabled: true,
        }}
        secrets={{ project_id: "boss-dogfood", secrets: [] }}
        deployments={{ deployments: [] }}
      />,
    );
    expect(markup).toContain('data-empty-state="configured"');
    expect(markup).toContain("Deploy an app");
    // Renamed from "Deployment targets": the section answers "which app runs where", and a
    // host is now a first-class thing an app is pointed AT. The assertion still guards the
    // same property — the deploy machinery appears once a target exists.
    expect(markup).toContain("Apps and where they deploy");
    expect(markup).toContain("demo");
    expect(markup).not.toContain('data-testid="sandbox-deploy-disabled"');
  });

  it("SandboxDeployFlagNotice only renders when the flag is known false", () => {
    expect(html(<SandboxDeployFlagNotice enabled={false} />)).toContain(
      "BOSS_ENABLE_SANDBOX_DEPLOY=false",
    );
    expect(html(<SandboxDeployFlagNotice enabled={true} />)).toBe("");
    expect(html(<SandboxDeployFlagNotice />)).toBe("");
  });

  it("DeploymentHealth empty state has no fake fleet status chrome", () => {
    const markup = html(<DeploymentHealth health={emptyHealth} />);
    expect(markup).toContain('data-empty-state="no-health-targets"');
    expect(markup).toContain("Nothing is registered to monitor");
    // Must not paint "● not_deployed" / "0 up · 0 degraded · 0 down" as if a fleet exists.
    expect(markup).not.toContain("● not_deployed");
    expect(markup).not.toContain("0 up");
    expect(markup).not.toContain("uptime 24h");
  });
});

describe("Monitoring empty-state honesty (BOSS-344)", () => {
  it("isObservabilityUnconfigured is true when stack is not deployed and apps empty", () => {
    expect(isObservabilityUnconfigured(emptyObservability)).toBe(true);
    expect(isObservabilityUnconfigured({
      ...emptyObservability,
      deployed: true,
      grafana_url: "http://10.0.0.1:3000",
    })).toBe(false);
    expect(isObservabilityUnconfigured({
      ...emptyObservability,
      apps: [{
        app: "demo",
        environment: "staging",
        port: 8080,
        dashboard_url: null,
        scrape_url: "http://10.0.0.1:8080",
      }],
    })).toBe(false);
  });

  it("MonitoringPanel empty state has no stack-running chrome or deploy machinery", () => {
    const markup = html(
      <MonitoringPanel initial={emptyObservability} hostTargetId={undefined} />,
    );
    expect(markup).toContain('data-empty-state="stack-not-deployed"');
    expect(markup).toContain("observability stack is not deployed");
    expect(markup).toContain("No active deployment target");
    expect(markup).not.toContain("stack running");
    expect(markup).not.toContain("stack not running");
    expect(markup).not.toContain("Deploy monitoring stack");
    expect(markup).not.toContain("Open Grafana");
  });

  it("MonitoringArea collapses dual empties into one setup card", () => {
    const markup = html(
      <MonitoringArea
        health={emptyHealth}
        observability={emptyObservability}
        hostTargetId={undefined}
      />,
    );
    expect(markup).toContain('data-empty-state="monitoring-unconfigured"');
    expect(markup).toContain("Monitoring is not set up yet");
    expect(markup).toContain("CI/CD");
    // Nested fleet chrome must not leak through the combined empty.
    expect(markup).not.toContain("● not_deployed");
    expect(markup).not.toContain("Deploy monitoring stack");
    expect(markup).not.toContain("Open Grafana");
  });

  it("MonitoringArea keeps separate panels when only one side is empty", () => {
    const markup = html(
      <MonitoringArea
        health={{
          ...emptyHealth,
          targets: [{
            target_id: "dt_1",
            app_name: "demo",
            environment: "staging",
            url: "demo.example",
            status: "up",
            status_since: "2026-08-11T00:00:00Z",
            uptime_24h: 100,
            uptime_7d: 100,
            avg_ms: 12,
            p95_ms: 20,
            last_checked: "2026-08-11T01:00:00Z",
            last_latency_ms: 11,
            last_error: null,
            sample_count: 4,
            series: [],
            incidents: [],
            deployed_commit: "abc123",
            deployed_at: "2026-08-11T00:30:00Z",
            deployment_run_id: "deprun_1",
          }],
          summary: {
            up: 1, degraded: 0, down: 0, unknown: 0, not_deployed: 0,
            overall: "up", total: 1,
          },
        }}
        observability={emptyObservability}
        hostTargetId="dt_1"
      />,
    );
    expect(markup).toContain('data-empty-state="monitoring-active"');
    expect(markup).toContain("demo");
    expect(markup).toContain("not deployed");
  });
});
