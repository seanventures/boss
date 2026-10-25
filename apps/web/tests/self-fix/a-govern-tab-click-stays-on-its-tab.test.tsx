// @vitest-environment happy-dom
/**
 * CLICKING A GOVERN TAB STAYS ON IT (brec_4cafbcc6). Clicking Self-fix showed it for a moment and
 * then went back to Limits; only a typed `?tab=selffix` stuck. The cause was not Govern: the
 * console keeps every area it has loaded mounted, and a mounted Workflows heard the next
 * popstate and "corrected" `tab=selffix` to its own `tab=runs`; Govern read the URL and fell
 * back to Limits. So this mounts the two together, as the console does, and fires the popstate
 * the shell fires on in-console navigation.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../../lib/console-live";
import { fakeGovernApi, newGovern } from "../_helpers/govern-fixtures";
import { click, mountAs, settle, tid, unmount } from "../_helpers/multirepo-fixtures";
import { fakeSelfFixApi, newState } from "./_fixtures";

vi.mock("../../app/workflows/studio-parts", () => import("../_helpers/studio-parts-stub"));
vi.mock("../../app/workflows/new-project-composer", () => ({ NewProjectComposer: () => <div /> }));

const { GovernHome } = await import("../../app/govern/govern-home");
const { DeliveryWorkflows } = await import("../../app/delivery-workflows-ui");
const stub = await import("../_helpers/studio-parts-stub");

afterEach(() => { unmount(); vi.unstubAllGlobals(); window.history.replaceState({}, "", "/"); });

async function openConsoleOnGovern() {
  fakeSelfFixApi(newState());
  const selfFix = globalThis.fetch;
  const govern = fakeGovernApi(newGovern());
  stub.fetchLibrary.mockResolvedValue([]);
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/self-fix")) return selfFix(input, init);
    if (url.includes("delivery-workflows")) return Promise.resolve(Response.json({}));
    return govern(input, init);
  });
  setConsoleLive(true);
  window.history.replaceState({}, "", "/?area=budget&org=o1");
  // Workflows was opened earlier in the session, so it is still mounted (hidden) beside Govern.
  await mountAs(<>
    <section hidden data-area="delivery_workflows"><DeliveryWorkflows projectId="p1" actor="ceo" /></section>
    <section data-area="budget"><GovernHome projectId="p1" actor="ceo" /></section>
  </>, "owner");
}

const tab = () => new URLSearchParams(window.location.search).get("tab");
const popstate = async () => {
  await act(async () => { window.dispatchEvent(new PopStateEvent("popstate")); });
  await settle();
};

describe("a Govern tab, clicked", () => {
  for (const key of ["selffix", "sharing"]) {
    it(`${key}: stays on it, and the URL says so, after the console's next popstate`, async () => {
      await openConsoleOnGovern();
      await click(tid(`gv-tab-${key}`));
      expect(tab()).toBe(key);
      await popstate();
      expect(tab()).toBe(key);
      expect(new URLSearchParams(window.location.search).get("area")).toBe("budget");
      expect(tid(`gv-tab-${key}`)?.getAttribute("aria-selected")).toBe("true");
      expect(tid("gv-tab-limits")?.getAttribute("aria-selected")).toBe("false");
    });
  }

  it("Self-fix shows its own content after the click, matching a direct ?tab=selffix", async () => {
    await openConsoleOnGovern();
    await click(tid("gv-tab-selffix"));
    await popstate();
    await settle();
    expect(tid("gv-selffix")).not.toBeNull();
  });
});
