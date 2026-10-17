// @vitest-environment happy-dom
/**
 * The app-health panel was a 560px strip: the latency chart and the uptime/traffic tiles were
 * squeezed (operator, 17 Oct: "This is not good UI — a new page or expand the side panel").
 * It opens at 880px and offers Expand, which fills the page until Collapse.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { Drawer } from "../app/infra/kit";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { document.body.innerHTML = ""; });

async function mount(node: React.ReactElement) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => root.render(node));
  return root;
}
const $ = (s: string) => document.querySelector<HTMLElement>(s);

describe("an app's health panel", () => {
  it("opens wide and expands to the page, then collapses back", async () => {
    await mount(<Drawer label="HelpDesk AI health" testId="obs-app-drawer" width={880} expandable
                        onClose={() => {}} head={<span>HelpDesk AI</span>}><p>chart</p></Drawer>);
    const drawer = $('[data-testid="obs-app-drawer"]')!;
    expect(drawer.getAttribute("data-width")).toBe("880");
    expect(drawer.hasAttribute("data-expanded")).toBe(false);
    const btn = $('[data-testid="obs-app-drawer-expand"]')!;
    expect(btn.textContent).toBe("Expand");
    await act(async () => btn.click());
    expect(drawer.hasAttribute("data-expanded")).toBe(true);
    expect(btn.getAttribute("aria-pressed")).toBe("true");
    expect(btn.textContent).toBe("Collapse");
    await act(async () => btn.click());
    expect(drawer.hasAttribute("data-expanded")).toBe(false);
  });

  it("a drawer that does not ask for it has no Expand", async () => {
    await mount(<Drawer label="Host" testId="inf-host" onClose={() => {}} head={<span>h</span>}><p>x</p></Drawer>);
    expect($('[data-testid="inf-host-expand"]')).toBeNull();
  });

  it("the app-health drawer asks for it", () => {
    const fs = require("node:fs") as typeof import("node:fs");
    const src = fs.readFileSync(require("node:path").join(__dirname, "../app/observability/app-drawer.tsx"), "utf8");
    expect(src).toContain('testId="obs-app-drawer" width={880} expandable');
  });
});
