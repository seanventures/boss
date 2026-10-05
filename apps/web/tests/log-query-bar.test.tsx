// @vitest-environment happy-dom
/**
 * The query bar is where every filter lives (spec §4.2.2): a rail click writes a token, the
 * bar's text writes a token, a pasted id becomes its scope, Backspace removes, and an exclusion
 * the server cannot do is refused in the bar's own words.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { fakeLogApi, newLog, rows, type FakeLog } from "./_helpers/log-fixtures";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { LogExplorer } = await import("../app/activity/log-explorer");

let log: FakeLog;
const params = () => new URLSearchParams(window.location.search);
const lastList = () => log.asked.filter((u) => u.startsWith("/api/activity?")).at(-1) ?? "";

async function type(value: string, key?: string) {
  const input = $("[data-testid=lx-query-input]") as HTMLInputElement;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  if (key) await act(async () => { input.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true })); });
  await settle();
}

beforeEach(async () => {
  log = newLog({ page: { items: rows(3) } });
  vi.stubGlobal("fetch", fakeLogApi(log));
  setConsoleLive(true);
  window.history.replaceState({}, "", "/?area=activity");
  await mount(<LogExplorer projectId="p1" actor="ceo" />);
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the bar is the truth; the rail and the URL follow it", () => {
  it("a rail click writes a token, the URL and the query; pressing it again removes it", async () => {
    await click($("[data-testid=lx-level-error]"));
    expect($("[data-testid=lx-token-level]")!.textContent).toBe("level: Error");
    expect(params().get("level")).toBe("error");
    expect(lastList()).toContain("level=error");
    expect($("[data-testid=lx-level-error]")!.getAttribute("aria-pressed")).toBe("true");
    // The rail's counts are re-asked (debounced) with the new filter; the SERVER leaves each
    // facet's own selection out, so the level counts still show every level.
    await act(async () => { await new Promise((r) => setTimeout(r, 300)); });
    await settle();
    const facets = log.asked.filter((u) => u.startsWith("/api/activity/facets?")).at(-1)!;
    expect(facets).toContain("level=error");
    await click($("[data-testid=lx-level-error]"));
    expect($("[data-testid=lx-token-level]")).toBeNull();
    expect(params().has("level")).toBe(false);
  });

  it("typed key:value is a token; Backspace on an empty bar removes the last one", async () => {
    await type("agent:software_engineer", "Enter");
    expect(params().get("agent")).toBe("software_engineer");
    await type("-level:verbose", "Enter");
    expect(params().get("level")).toBe("-verbose");
    expect(lastList()).toContain("exclude_level=verbose");
    await type("", "Backspace");
    expect(params().has("level")).toBe(false);
    expect(params().get("agent")).toBe("software_engineer");
  });

  it("an exclusion the server cannot do is refused in the bar", async () => {
    await type("-actor:ceo", "Enter");
    expect($("[data-testid=lx-query-error]")!.textContent).toBe("Can't exclude this one");
    expect(params().has("actor")).toBe(false);
  });

  it("a pasted run id becomes the run's scope token", async () => {
    const input = $("[data-testid=lx-query-input]")!;
    await act(async () => {
      const event = new Event("paste", { bubbles: true }) as Event & { clipboardData: { getData: () => string } };
      event.clipboardData = { getData: () => "crun_8f2a" };
      input.dispatchEvent(event);
    });
    await settle();
    expect(params().get("run")).toBe("crun_8f2a");
    expect(lastList()).toContain("run_id=crun_8f2a");
  });

  it("plain text searches after a pause; Esc clears it", async () => {
    await type("npm ERR");
    await act(async () => { await new Promise((r) => setTimeout(r, 300)); });
    await settle();
    expect(params().get("q")).toBe("npm ERR");
    expect(lastList()).toContain("q=npm+ERR");
    await type("npm ERR", "Escape");
    expect(params().has("q")).toBe(false);
  });

  it("a view is just its tokens, shown as chosen while they match", async () => {
    await click($("[data-testid=lx-view-refusals]"));
    expect(params().get("kind")).toBe("budget.refused,budget.reservation_denied");
    expect($("[data-testid=lx-view-refusals]")!.getAttribute("aria-pressed")).toBe("true");
    await click($("[data-testid=lx-view-errors]"));
    expect(params().get("level")).toBe("error");
    expect(params().has("kind")).toBe(false);
    expect($("[data-testid=lx-view-errors]")!.getAttribute("aria-pressed")).toBe("true");
  });
});
