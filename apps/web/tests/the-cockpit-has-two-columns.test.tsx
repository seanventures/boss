// @vitest-environment happy-dom
/**
 * THE COCKPIT HAS TWO COLUMNS (factory spec §4.5): at desktop width the Story is on the left and
 * the Outputs rail on the right, sticky under the Line. On 2026-10-04 a walk fix dropped the base
 * `.fx-body { display: grid }`; the width rules kept setting columns on a block box, so at 1440
 * Outputs rendered under the Story and nothing noticed. This mounts the real cockpit with the real
 * stylesheet and reads the computed layout at each width.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, mount, unmount } from "./_helpers/live-render";
import { stubFetch } from "./_helpers/cockpit-render";
import { cockpit } from "./_helpers/factory-fixtures";

vi.mock("../app/factory/legacy-build-parts", async (orig) => ({ ...(await orig<object>()), DeploymentLine: () => <span data-testid="deploy-line" /> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Cockpit } = await import("../app/factory/cockpit");
const CSS = readFileSync(resolve(__dirname, "../app/factory/cockpit.css"), "utf8");

afterEach(() => { unmount(); vi.unstubAllGlobals(); document.head.querySelectorAll("style[data-cockpit]").forEach((s) => s.remove()); });

/** Mount at a viewport width (fresh elements, so nothing computed at another width is reused). */
async function at(width: number) {
  (window as unknown as { happyDOM: { setViewport: (v: { width: number; height: number }) => void } })
    .happyDOM.setViewport({ width, height: 900 });
  const style = document.createElement("style");
  style.dataset.cockpit = "";
  style.textContent = CSS;
  document.head.appendChild(style);
  stubFetch(cockpit());
  await mount(<Cockpit projectId="p1" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />);
  const body = $(".fx-body")!;
  const outputs = $('[data-testid="fx-outputs"]')!;
  return { body, outputs, b: getComputedStyle(body), o: getComputedStyle(outputs) };
}

const tracks = (t: string) => t.replace(/\([^)]*\)/g, "()").trim().split(/\s+/).filter(Boolean);

describe("the cockpit is Story | Outputs at desktop width", () => {
  for (const [width, columns] of [[1440, "minmax(0, 3fr) minmax(0, 2fr)"], [1280, "minmax(0, 3fr) minmax(0, 2fr)"], [1200, "minmax(0, 1fr) 380px"]] as const) {
    it(`${width}px: two columns, Story first, Outputs sticky`, async () => {
      const { body, b, o } = await at(width);
      expect(b.display).toBe("grid");
      expect(b.gridTemplateColumns).toBe(columns);
      expect(tracks(b.gridTemplateColumns)).toHaveLength(2);
      expect([...body.children].map((c) => (c as HTMLElement).dataset.testid)).toEqual(["fx-story", "fx-outputs"]);
      expect(o.position).toBe("sticky");
    });
  }

  for (const width of [1199, 1024, 390]) {
    it(`${width}px: one column, Story then Outputs, nothing sticky beside it`, async () => {
      const { body, b, o } = await at(width);
      expect(b.display).not.toBe("grid");
      expect([...body.children].map((c) => (c as HTMLElement).dataset.testid)).toEqual(["fx-story", "fx-outputs"]);
      expect(o.position).toBe("static");
    });
  }

  it("the rail keeps every output, Brief first", async () => {
    await at(1440);
    const keys = $$('[data-testid="fx-outputs"] .fx-out').map((li) => li.dataset.key);
    expect(keys[0]).toBe("brief");
    expect(keys).toHaveLength(9);
  });
});
