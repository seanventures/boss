// @vitest-environment happy-dom
/**
 * D12: Setup's Budget row links to the SAME cap in Govern — the Monthly cap row (focus=), and
 * keeping who you are (org=). It was a bare `?area=budget`: org dropped, landed at the top.
 */
import { afterEach, describe, expect, it } from "vitest";

import { fakeApi, mountBoard, type Mounted } from "./_helpers/setup-board";
import { everyRed } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; window.history.replaceState({}, "", "/"); });

describe("Setup › Budget › Govern ↗", () => {
  it("lands on the Monthly cap row and keeps the org", async () => {
    const view = everyRed();
    fakeApi(view);
    mounted = await mountBoard(view, "?org=org_acme&area=setup");
    const link = document.body.querySelector('[data-testid="su-govern"]');
    expect(link, "the Budget row shows its Govern link").toBeTruthy();
    const q = new URLSearchParams(link!.getAttribute("href")!.split("?")[1]);
    expect([q.get("org"), q.get("area"), q.get("focus")]).toEqual(["org_acme", "budget", "monthly_cap"]);
  });
});
