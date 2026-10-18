// @vitest-environment happy-dom
/**
 * Factory app row ⋯ › Alerts (BOSSAPP-598): alerts live in Observability; the Factory only links
 * there — to the deployed app's drawer, on its Alerts tab. An app that is not deployed anywhere
 * has no alerts to show, so the menu offers none.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import realShape from "./_helpers/apps-real-shape.json";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { AppsTab } = await import("../app/factory/apps-tab");

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("Factory ⋯ › Alerts", () => {
  it("links a deployed app to its Alerts tab in Observability, and only a deployed one", async () => {
    const apps = (realShape as { apps: Record<string, unknown>[] }).apps.map((a, i) => (
      i === 0 ? { ...a, deploys: [{ env: "staging", ok: true, word: "staging ✓", target_id: "deptgt_order" }] } : a));
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ...realShape, apps })));
    window.history.replaceState(null, "", "/?area=factory&org=org_1");
    await mount(<AppsTab projectId="p1" runs={[]} previews={[]} query="" onOpen={() => {}} onNew={() => {}} />);
    await settle();
    const rows = $$(".fx-app");
    const keyOf = (el: HTMLElement) => el.getAttribute("data-testid")!.replace("fx-app-", "");
    const deployed = rows.find((r) => r.textContent?.includes("staging ✓"))!;
    await click($(`[data-testid='fx-app-more-${keyOf(deployed)}']`));
    const link = $(`[data-testid='fx-app-menu-alerts-${keyOf(deployed)}']`)!;
    const href = new URLSearchParams(link.getAttribute("href")!.replace(/^\?/, ""));
    expect(href.get("area")).toBe("observability");
    expect(href.get("app")).toBe("deptgt_order");
    expect(href.get("atab")).toBe("alerts");
    expect(href.get("org")).toBe("org_1");
    const other = rows.find((r) => r !== deployed)!;
    await click($(`[data-testid='fx-app-more-${keyOf(other)}']`));
    expect($(`[data-testid='fx-app-menu-alerts-${keyOf(other)}']`)).toBeNull();
  });
});
