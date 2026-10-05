// @vitest-environment happy-dom
/**
 * Infra › Fleet draws the racks the view sends (infra-observability spec §4.3, §5.4): This
 * computer first, one tile per resident, dashed free slots up to max_apps (at most 8, then
 * `+n free`), `+ Place app` when the machine sets no limit, the Stage line with its one inline
 * action while a machine is set up, NOT PLACED for targets on no machine of ours — and no write
 * control where it cannot work (outward SSH off, a read-only viewer).
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { fleetFixture, type Route } from "./_helpers/infra-fixtures";
import { $, $$, byTestId, click, mountFleet, unmountInfra } from "./_helpers/infra-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));

afterEach(() => unmountInfra());

const rack = (id: string) => $(`[data-id="${id}"][data-testid^="inf-rack"]`)!;
const slotsIn = (el: HTMLElement) => el.querySelectorAll('[data-testid="inf-slot-free"]').length;
const tilesIn = (el: HTMLElement) => [...el.querySelectorAll<HTMLElement>('[data-testid="inf-tile"]')];

describe("Infra › Fleet", () => {
  it("draws This computer first, then the machines in the view's order, retired hidden", async () => {
    await mountFleet("view=fleet");
    const ids = $$('[data-testid="inf-rack-this"], [data-testid="inf-rack"]').map((r) => r.dataset.id);
    expect(ids).toEqual(["this", "h_down", "h_setup", "h_ready"]);
    expect(byTestId("inf-rack-this")?.textContent).toContain("This computer");
    expect(rack("h_down").querySelector('[data-testid="inf-rack-state"]')?.textContent).toBe("Unreachable");
    // Show retired reveals the retired machine.
    await click("inf-show-retired");
    expect($$('[data-testid="inf-rack"]').map((r) => r.dataset.id)).toContain("h_old");
  });

  it("one tile per resident, then dashed free slots up to max_apps", async () => {
    await mountFleet("view=fleet");
    const ready = rack("h_ready");
    expect(tilesIn(ready).map((t) => t.dataset.id)).toEqual(["t_live", "pv_live"]);
    expect(slotsIn(ready)).toBe(3);                    // max 4, 1 app placed
    expect(ready.querySelector('[data-testid="inf-rack-meta"]')?.textContent).toContain("apps 1 of 4");
    expect(ready.querySelector('[data-testid="inf-rack-meta"]')?.textContent).toContain("ports 2 of 50");
    expect(slotsIn(rack("this"))).toBe(8);             // max 8, nothing placed: all 8 drawn
    const tile = tilesIn(ready)[0];
    expect(tile.textContent).toContain("invoice · prod");
    expect(tile.querySelector('[data-testid="inf-tile-state"]')?.textContent).toBe("Up");
    expect(tile.textContent).toContain("9f8e7d6");
    expect(tile.querySelector('svg[role="img"]')?.getAttribute("aria-label")).toMatch(/^invoice · prod, 24 hours: up/);
  });

  it("draws at most 8 empty slots, then +n free; a machine with no limit offers + Place app", async () => {
    const routes: Route[] = [{ match: /^\/v1\/infra\/fleet$/, etag: true, reply: () => {
      const f = fleetFixture();
      const racks = f.racks as Record<string, unknown>[];
      racks[3] = { ...racks[3], max_apps: 21 };                      // h_ready: 20 free
      racks[0] = { ...racks[0], max_apps: null };                    // this: no limit
      return f;
    } }];
    await mountFleet("view=fleet", { routes });
    expect(slotsIn(rack("h_ready"))).toBe(8);
    expect(rack("h_ready").querySelector('[data-testid="inf-slot-more"]')?.textContent).toBe("+12 free");
    expect(rack("this").querySelector('[data-testid="inf-place-app"]')?.textContent).toBe("+ Place app");
    expect(rack("this").querySelector('[data-testid="inf-rack-meta"]')?.textContent).toContain("0 apps");
  });

  it("a machine being set up shows the Stage line and its one inline action", async () => {
    const mock = await mountFleet("view=fleet");
    const setup = rack("h_setup");
    expect(slotsIn(setup)).toBe(0);
    const line = setup.querySelector('[data-testid="inf-stage-line"]')!;
    expect([...line.querySelectorAll("li")].map((li) => li.getAttribute("data-state")))
      .toEqual(["done", "done", "current", "todo", "todo"]);
    expect(line.querySelector('[aria-current="step"]')?.textContent).toContain("Verify");
    await click(setup.querySelector('[data-testid="inf-rack-stage-verify"]'));
    expect(mock.writes().map((w) => w.path)).toEqual(["/v1/deployment-hosts/h_setup/verify"]);
  });

  it("NOT PLACED holds the targets no machine of ours runs", async () => {
    await mountFleet("view=fleet");
    const unplaced = byTestId("inf-unplaced")!;
    expect(unplaced.textContent).toContain("NOT PLACED");
    const [tile] = tilesIn(unplaced);
    expect(tile.dataset.id).toBe("t_cloud");
    expect(tile.textContent).toContain("Never deployed");
    expect(tile.querySelector('[data-testid="inf-tile-state"]')?.textContent).toBe("Not deployed");
  });

  it("with outward SSH off: says so once and hides every action that reaches a machine", async () => {
    await mountFleet("view=fleet", { variant: "blocked" });
    expect(byTestId("inf-ssh-off")?.textContent).toBe("SSH out is off");
    expect(byTestId("inf-ssh-off")?.getAttribute("title")).toContain("BOSS_SSH_OUTWARD_ALLOWED");
    expect(rack("h_setup").querySelector('[data-testid^="inf-rack-stage-"]')).toBeNull();
    await click("infra-add");
    expect(byTestId("infra-add-host")).toBeNull();
    expect(byTestId("infra-add-target")).not.toBeNull();
  });

  it("a read-only viewer sees no write control at all", async () => {
    await mountFleet("view=fleet", { variant: "readonly" });
    expect(byTestId("infra-add")).toBeNull();
    expect(byTestId("infra-read-only")?.getAttribute("title")).toBe("You lack providerops_manage");
    expect($('[data-testid^="inf-rack-stage-"]')).toBeNull();
    expect($('button[data-testid="inf-slot-free"]')).toBeNull();
  });

  it("Find narrows the racks to what matches", async () => {
    await mountFleet("view=fleet&q=invoice");
    const ids = $$('[data-testid="inf-rack-this"], [data-testid="inf-rack"]').map((r) => r.dataset.id);
    expect(ids).toEqual(["h_ready"]);
    expect(tilesIn(rack("h_ready")).map((t) => t.dataset.id)).toEqual(["t_live"]);
  });

  it("Sandbox deploys off is one line, its reason in the tooltip", async () => {
    await mountFleet("view=fleet");
    expect(byTestId("inf-sandbox-off")?.textContent).toBe("Sandbox deploys off");
    expect(byTestId("inf-sandbox-off")?.getAttribute("title")).toContain("BOSS_ENABLE_SANDBOX_DEPLOY");
  });
});
