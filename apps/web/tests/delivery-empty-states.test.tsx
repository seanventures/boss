// @vitest-environment happy-dom
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

/**
 * Monitoring's half. The MonitoringPanel / MonitoringArea empties (delivery-manage.tsx,
 * monitoring-ui.tsx) retired into Observability, and the same first-paint promises are held there:
 *   - nothing deployed is ONE line with the way to Infra, no fleet chrome —
 *     observability-apps-is-one-pulse "nothing deployed: one line and the way to Infra"
 *   - a stack that is not deployed and has no machine offers no deploy machinery —
 *     sources-tab-deploys-checks-and-recommends "no machine: Needs a machine, Add host, and
 *     nothing to deploy"
 *   - one side empty, the other not: an app scraped but not health-checked is still named —
 *     observability-apps-is-one-pulse "an app scraped but not health-checked is named, not dropped"
 */
