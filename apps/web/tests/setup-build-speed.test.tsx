// @vitest-environment happy-dom
/**
 * Build speed: Balanced (3 parallel sub-agents, the default) or Fastest (6). The row says how
 * many a build gets right now and, when the machine caps it, that the machine is why. A press
 * sets the project setting build_speed through PUT /v1/settings/build_speed.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { click, fakeApi, mountBoard, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.restoreAllMocks(); });

const BALANCED = { key: "build_speed", mode: "balanced" as const, parallelism: 3, machine: 6,
                   asks: { balanced: 3, fastest: 6 }, source: "default" as const, permission: "settings:update" };

describe("Build speed", () => {
  it("defaults to Balanced, says how many at once, and Fastest is one press", async () => {
    const view = { ...board(), build_speed: BALANCED };
    const api = fakeApi(view);
    mounted = await mountBoard(view);
    expect(document.querySelector('[data-testid="su-build-speed-balanced"]')!.getAttribute("aria-checked")).toBe("true");
    expect(document.querySelector('[data-testid="su-build-speed-now"]')!.textContent).toContain("3 sub-agents at once");
    await click(document.querySelector('[data-testid="su-build-speed-fastest"]'));
    expect(api.calls.find((c) => c.op === "build_speed")?.body).toMatchObject({ key: "build_speed", value: "fastest" });
  });

  it("says when the machine allows fewer than the setting asks for", async () => {
    const view = { ...board(), build_speed: { ...BALANCED, mode: "fastest" as const, parallelism: 4, machine: 4 } };
    fakeApi(view);
    mounted = await mountBoard(view);
    expect(document.querySelector('[data-testid="su-build-speed-now"]')!.textContent)
      .toContain("this machine allows 4");
  });

  it("an older API without the setting shows no row", async () => {
    fakeApi(board());
    mounted = await mountBoard(board());
    expect(document.querySelector('[data-testid="su-row-build-speed"]')).toBeNull();
  });
});
