// @vitest-environment happy-dom
/**
 * EVERY FIRST-RUN CONTROL GOES WHERE IT SAYS (stranger test 2026-11-17):
 *   - the first screen's step bar: "Helm onboards" and "First PR" were buttons that did nothing;
 *     a node is a button only when pressing it goes somewhere (First PR once the sample's PR exists)
 *   - "Add model" landed on Setup without opening the model drawer (a fresh install shows the
 *     stepper, which ignored `open=add`)
 *   - "Add a repo" landed on Settings with the Code card under the sticky project head
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Journey } from "../app/projects/kit";
import { firstPrHref, stripOpens } from "../app/projects/first-run";
import { SECTION_SCROLL_MARGIN } from "../app/projects/tab-settings";
import { useStickyHead } from "../app/projects/project-page";
import type { SetupView } from "../lib/setup-types";
import { AreaShownContext } from "../lib/area-shown";
import { fakeApi, flush, q } from "./_helpers/setup-board";
import { firstRun } from "./_helpers/setup-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let cleanup: (() => void) | null = null;
afterEach(() => { cleanup?.(); cleanup = null; vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });

async function render(node: React.ReactElement): Promise<HTMLElement> {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => { root.render(node); });
  cleanup = () => { act(() => root.unmount()); host.remove(); };
  return host;
}

const STRIP = (prHref: string) => [
  { key: "github", label: "Connect GitHub", state: "now" as const, sub: "", href: "/?area=setup&focus=repository" },
  { key: "model", label: "Add a model", state: "todo" as const, sub: "", href: "/?area=setup&focus=models" },
  { key: "project", label: "Add project", state: "todo" as const, sub: "", href: "" },
  { key: "onboard", label: "Helm onboards", state: "todo" as const, sub: "", href: "" },
  { key: "pr", label: "First PR", state: "todo" as const, sub: "", href: prHref },
];

describe("the first screen's step bar", () => {
  it("Helm onboards is drawn, not a button that does nothing", async () => {
    const onOpen = vi.fn();
    const host = await render(<Journey nodes={STRIP("")} testid="s" canOpen={stripOpens} onOpen={onOpen} />);
    const node = host.querySelector<HTMLButtonElement>('[data-testid="s-onboard"]')!;
    expect(node.disabled).toBe(true);
    // The ones that go somewhere stay live.
    expect(host.querySelector<HTMLButtonElement>('[data-testid="s-github"]')!.disabled).toBe(false);
    expect(host.querySelector<HTMLButtonElement>('[data-testid="s-project"]')!.disabled).toBe(false);
  });

  it("First PR is inert before there is a PR, and opens the sample's PR once there is one", async () => {
    expect(firstPrHref(null)).toBe("");
    const view = { try: { last_run: { pr_url: "https://github.com/acme/hello/pull/3" } } } as unknown as SetupView;
    const href = firstPrHref(view);
    expect(href).not.toBe("");
    const host = await render(<Journey nodes={STRIP("")} testid="s" canOpen={stripOpens} onOpen={() => {}} />);
    expect(host.querySelector<HTMLButtonElement>('[data-testid="s-pr"]')!.disabled).toBe(true);
    cleanup?.(); cleanup = null;
    const onOpen = vi.fn();
    const live = await render(<Journey nodes={STRIP(href)} testid="s" canOpen={stripOpens} onOpen={onOpen} />);
    const pr = live.querySelector<HTMLButtonElement>('[data-testid="s-pr"]')!;
    expect(pr.disabled).toBe(false);
    await act(async () => { pr.click(); });
    expect(onOpen.mock.calls[0][0].href).toBe(href);
  });

  it("a Journey without canOpen keeps its old rule (the project page's stages open by key)", async () => {
    const host = await render(<Journey nodes={STRIP("")} testid="s" onOpen={() => {}} />);
    expect(host.querySelector<HTMLButtonElement>('[data-testid="s-onboard"]')!.disabled).toBe(false);
  });
});

describe("Add model", () => {
  it("opens the model drawer on a fresh install (the stepper layout), not just Setup", async () => {
    const view = firstRun();
    fakeApi(view);
    window.history.replaceState(null, "", "/?area=setup&focus=models&open=add");
    const { SetupBoard } = await import("../app/setup/setup-board");
    await render(<AreaShownContext.Provider value={true}><SetupBoard projectId="p" initialView={view} layout="setup" /></AreaShownContext.Provider>);
    await flush();
    expect(q(document.body, "su-model-drawer")).not.toBeNull();
  });

  it("a plain link to Models on a fresh install opens nothing", async () => {
    const view = firstRun();
    fakeApi(view);
    window.history.replaceState(null, "", "/?area=setup&focus=models");
    const { SetupBoard } = await import("../app/setup/setup-board");
    await render(<AreaShownContext.Provider value={true}><SetupBoard projectId="p" initialView={view} layout="setup" /></AreaShownContext.Provider>);
    await flush();
    expect(q(document.body, "su-model-drawer")).toBeNull();
  });
});

describe("Add a repo", () => {
  it("lands the Code section below the top bar and the sticky project head, never under it", async () => {
    expect(SECTION_SCROLL_MARGIN).toContain("var(--px-head-h");
    const bar = document.createElement("div");
    bar.className = "bx-topbar";
    Object.defineProperty(bar, "getBoundingClientRect", { value: () => ({ height: 62 }) });
    document.body.appendChild(bar);
    function Head() {
      const ref = useStickyHead();
      return <div ref={(el) => { if (el) Object.defineProperty(el, "offsetHeight", { value: 170, configurable: true }); ref(el); }}>head</div>;
    }
    await render(<Head />);
    expect(document.documentElement.style.getPropertyValue("--px-head-h")).toBe("232px");
    bar.remove();
  });
});
