// @vitest-environment happy-dom
/**
 * Pressing Enter on an organization opens the console with a full page load.
 *
 * It used router.push, which keeps the picker on screen — URL unchanged, no loading screen —
 * until the whole console has rendered client-side. On a fresh install that was a long silent
 * wait after Enter ("something is loading for long time and after long time it logs in"); a
 * full load of /?org=… shows the console's loading screen at once and finishes in ~1.5s.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush, replace: routerPush, refresh: vi.fn() }),
  useSearchParams: () => new URLSearchParams(""),
}));
vi.mock("../app/actions", () => ({ onboardOrgAction: vi.fn() }));
vi.mock("../app/submit-button", () => ({ SubmitButton: (p: { children: React.ReactNode }) => <button>{p.children}</button> }));

// The page modules use the automatic JSX runtime under Next; vitest here uses the classic one.
(globalThis as unknown as { React: typeof React }).React = React;

import { OrgGate } from "../app/org-gate";

const ORGS = [{ org_id: "org_boss", org_name: "BOSS", task_count: 0, meeting_count: 0,
                project_slug: "boss-dogfood" },
              { org_id: "org_two", org_name: "Two", task_count: 0, meeting_count: 0,
                project_slug: "two" }] as never;

let root: Root;
let host: HTMLDivElement;
const assign = vi.fn();
const replace = vi.fn();

beforeEach(() => {
  window.localStorage.clear();
  Object.defineProperty(window, "location", {
    configurable: true, value: { ...window.location, assign, replace, search: "" },
  });
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks(); });

describe("entering an organization", () => {
  it("is a full page load, not a client transition, and says it is opening", async () => {
    await act(async () => { root.render(<OrgGate orgs={ORGS} />); });
    const card = [...host.querySelectorAll("button")].find((b) => b.textContent?.includes("Enter"))!;
    await act(async () => { card.click(); });
    expect(assign).toHaveBeenCalledWith("/?org=org_boss");
    expect(routerPush).not.toHaveBeenCalled();
    expect(card.textContent).toContain("Opening…");
    expect(card.disabled).toBe(true);
  });

  it("a returning user is taken straight in with a full load too", async () => {
    window.localStorage.setItem("boss.org", "org_boss");
    await act(async () => { root.render(<OrgGate orgs={ORGS} />); });
    expect(replace).toHaveBeenCalledWith("/?org=org_boss");
    expect(routerPush).not.toHaveBeenCalled();
  });
});

describe("one organization is not a choice", () => {
  it("a first sign-in on a fresh install goes straight in", async () => {
    await act(async () => { root.render(<OrgGate orgs={[(ORGS as unknown as unknown[])[0]] as never} />); });
    expect(replace).toHaveBeenCalledWith("/?org=org_boss");
  });
});

describe("a deep link survives the org picker", () => {
  it("keeps every parameter but the org", async () => {
    const { orgHref } = await import("../app/org-gate");
    expect(orgHref("org_boss", "?area=setup&focus=models&source=setup-openrouter"))
      .toBe("/?org=org_boss&area=setup&focus=models&source=setup-openrouter");
    expect(orgHref("org_boss", "?pick=1&org_id=old&area=setup")).toBe("/?org=org_boss&area=setup");
    expect(orgHref("org_boss", "")).toBe("/?org=org_boss");
  });
});
