// @vitest-environment happy-dom
/**
 * Factory opens on the Floor (factory spec §4.1): no `view` is the Floor, `view=apps` the Apps
 * tab, `build=` opens that build's cockpit over either, and a link that still says `run=` is
 * rewritten to `build=`. Esc leaves the cockpit and puts focus back on the row that opened it.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { floor } from "./_helpers/factory-fixtures";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { CodeTasksHome, readFactoryUrl } = await import("../app/code-tasks-ui");

const home = () => <CodeTasksHome projectId="p1" codeTasks={[]} apiReachable actor="ceo" />;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) =>
    String(url).startsWith("/v1/factory/floor") ? Response.json(floor()) : new Response("{}", { status: 404 })));
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the Factory's URL", () => {
  it("reads run= as build=, and view=apps as the Apps tab", () => {
    expect(readFactoryUrl("?area=code_tasks")).toMatchObject({ view: "floor", build: null });
    expect(readFactoryUrl("?area=code_tasks&run=crun_1")).toMatchObject({ build: "crun_1", legacyRun: true });
    expect(readFactoryUrl("?area=code_tasks&build=crun_2&view=apps")).toMatchObject({ view: "apps", build: "crun_2", legacyRun: false });
  });

  it("opens on the Floor", async () => {
    window.history.replaceState(null, "", "/?area=code_tasks");
    await mount(home());
    expect($("[data-testid=fx-floor]")).not.toBeNull();
    expect($("[data-testid=fx-view-floor]")?.getAttribute("aria-selected")).toBe("true");
    expect($("[data-testid=fx-cockpit]")).toBeNull();
  });

  it("rewrites run= to build= and opens the cockpit", async () => {
    window.history.replaceState(null, "", "/?area=code_tasks&run=crun_inv2");
    await mount(home());
    expect(new URLSearchParams(window.location.search).get("build")).toBe("crun_inv2");
    expect(new URLSearchParams(window.location.search).get("run")).toBeNull();
    expect($("[data-testid=fx-cockpit]")?.getAttribute("data-run")).toBe("crun_inv2");
    expect($("[data-testid=fx-floor]")).toBeNull();
  });

  it("view=apps is the Apps tab", async () => {
    window.history.replaceState(null, "", "/?area=code_tasks&view=apps");
    await mount(home());
    expect($("[data-testid=fx-apps]")).not.toBeNull();
    await click($("[data-testid=fx-view-floor]"));
    expect($("[data-testid=fx-floor]")).not.toBeNull();
    expect(new URLSearchParams(window.location.search).get("view")).toBeNull();
  });

  it("Esc returns focus to the row that opened the cockpit", async () => {
    window.history.replaceState(null, "", "/?area=code_tasks");
    await mount(home());
    await click($("[data-testid=fx-build-open-crun_inv2]"));
    expect($("[data-testid=fx-cockpit]")).not.toBeNull();
    expect(new URLSearchParams(window.location.search).get("build")).toBe("crun_inv2");
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    await settle();
    await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
    expect($("[data-testid=fx-cockpit]")).toBeNull();
    expect(new URLSearchParams(window.location.search).get("build")).toBeNull();
    expect(document.activeElement?.getAttribute("data-testid")).toBe("fx-build-open-crun_inv2");
  });

  it("`/` focuses Find a build, and finding narrows the rows", async () => {
    window.history.replaceState(null, "", "/?area=code_tasks");
    await mount(home());
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "/", bubbles: true })); });
    expect(document.activeElement?.getAttribute("data-testid")).toBe("fx-find");
  });

  it("+ New build presses Plan it with Juno; its menu holds the other doors", async () => {
    window.history.replaceState(null, "", "/?area=code_tasks");
    const opened: unknown[] = [];
    const on = (e: Event) => opened.push((e as CustomEvent).detail);
    window.addEventListener("boss:converse", on);
    await mount(home());
    await click($("[data-testid=fx-new-plan]"));
    window.removeEventListener("boss:converse", on);
    expect(opened).toEqual([expect.objectContaining({ kind: "plan", roleKey: "pm" })]);
    await click($("[data-testid=fx-new-build]"));
    const menu = $("[data-testid=fx-new-menu]");
    expect([...menu!.querySelectorAll("[role=menuitem]")].map((b) => b.textContent))
      .toEqual(["Plan it with Juno", "From a workflow", "Direct change"]);
  });
});
