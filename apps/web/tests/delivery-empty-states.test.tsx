// @vitest-environment happy-dom
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { fleetFixture } from "./_helpers/infra-fixtures";
import { byTestId, mountFleet, unmountInfra } from "./_helpers/infra-render";

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
  isObservabilityUnconfigured,
  MonitoringPanel,
} = await import("../app/delivery-manage");
const { MonitoringArea } = await import("../app/monitoring-ui");

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

/**
 * Infra's half, rewritten for the Fleet (infra-observability spec §4.14, §9.2): the old Infra
 * h2 sentences and the DeliveryPipeline / DeploymentHealth empties are gone; what an operator
 * sees on first paint is now the Fleet's own empty states, rendered from the view.
 */
const emptyFleet = (): Record<string, unknown> => ({
  ...fleetFixture(), counts: { ready: 0, setting_up: 0, unreachable: 0, apps: 0, previews_live: 0, needs_you: 0 },
  attention: [], racks: [], unplaced: [], retired_count: 0,
});

describe("Infra empty-state honesty (BOSS-344)", () => {
  afterEach(() => unmountInfra());

  it("no machine and no hardware place: one line and Add host, no fleet chrome", async () => {
    await mountFleet("view=fleet", { routes: [{ match: /^\/v1\/infra\/fleet$/, etag: true, reply: emptyFleet }] });
    expect(byTestId("infra-fleet-empty")?.textContent).toContain("No machines yet");
    expect(byTestId("inf-empty-add-host")?.textContent).toBe("Add host");
    expect(document.querySelector('[data-testid="inf-rack"], [data-testid="inf-rack-this"], [data-testid="inf-unplaced"]')).toBeNull();
    // P-I-08: the old h2 sentences are gone.
    expect(document.body.textContent).not.toContain("Nothing deploys yet");
    expect(document.body.textContent).not.toContain("governed deployments");
  });

  it("a target never deployed says so, and its strip is gaps, never green", async () => {
    await mountFleet("view=fleet");
    const tile = document.querySelector<HTMLElement>('[data-testid="inf-unplaced"] [data-testid="inf-tile"]')!;
    expect(tile.textContent).toContain("Never deployed");
    expect(tile.querySelector('[data-testid="inf-tile-state"]')?.textContent).toBe("Not deployed");
    expect(tile.querySelectorAll('.obs-cell[data-k="gap"]').length).toBe(32);
    expect(tile.querySelector('.obs-cell[data-k="ok"]')).toBeNull();
  });

  it("Community keeps deploy-on-merge and the registry deploy paused, with the reason", async () => {
    await mountFleet("view=fleet&target=t_cloud", { variant: "community" });
    const deploy = byTestId("inf-target-deploy") as HTMLButtonElement;
    expect(deploy.disabled).toBe(true);
    expect(deploy.getAttribute("title")).toBe("Paused in Community");
    const auto = byTestId("inf-target-auto") as HTMLInputElement;
    expect(auto.disabled).toBe(true);
    expect(auto.closest("label")?.getAttribute("title")).toBe("Paused in Community");
  });

  it("Sandbox deploys off is drawn only when the flag is known off", async () => {
    await mountFleet("view=fleet");
    expect(byTestId("inf-sandbox-off")).not.toBeNull();
    unmountInfra();
    await mountFleet("view=fleet", { routes: [{ match: /^\/v1\/infra\/fleet$/, etag: true, reply: () => {
      const f = fleetFixture();
      return { ...f, preconditions: { ...(f.preconditions as Record<string, unknown>), sandbox_deploys_enabled: true } };
    } }] });
    expect(byTestId("infra-fleet")).not.toBeNull();
    expect(byTestId("inf-sandbox-off")).toBeNull();
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
    // The area is called Infra now; "Start in the CI/CD area" named a screen that no longer
    // exists by that name (console walk, 2026-10-02).
    expect(markup).toContain("Start in Infra");
    expect(markup).not.toContain("CI/CD");
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
