// @vitest-environment happy-dom
/**
 * A CONSOLE PAGE FAILS CLOSED. When the identity API cannot answer, a /v2 page that needs a
 * permission is not drawn: consolePageAllowed says no, and <PageDenied> renders "Can't check your
 * access right now" with Retry — never the page, and never "Access denied" (nothing was denied).
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { beforeEach, describe, expect, it, vi } from "vitest";

const boot = { me: null as unknown, org: null as unknown, project: null as unknown, problem: null as string | null };
vi.mock("../lib/identity/server-cache", () => ({ loadIdentityCached: async () => boot }));
vi.mock("next/navigation", () => ({ redirect: (to: string) => { throw new Error(`redirect ${to}`); }, useRouter: () => ({ refresh: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams() }));
vi.mock("next/headers", () => ({ headers: async () => new Headers(), cookies: async () => ({ get: () => undefined }) }));

import { consolePageAllowed } from "../components/identity/route-shell";
import { PageDenied } from "../components/identity/page-denied";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

beforeEach(() => { boot.org = null; boot.problem = null; });

describe("console pages fail closed", () => {
  it("an unreachable identity API keeps a gated page closed", async () => {
    boot.problem = "identity_unavailable";
    expect(await consolePageAllowed("/v2/hive")).toBe(false);
  });

  it("says it cannot check, with Retry, instead of the page or a denial", async () => {
    boot.problem = "identity_unavailable";
    const node = await PageDenied({ path: "/v2/hive" });
    const host = document.createElement("div");
    await act(async () => createRoot(host).render(node as React.ReactElement));
    expect(host.querySelector("[data-testid='access-unchecked']")?.textContent).toContain("Can't check your access right now");
    expect(host.textContent).toContain("Retry");
    expect(host.querySelector("[data-testid='access-denied']")).toBeNull();
  });

  it("an answered check still decides as before", async () => {
    boot.org = { epoch: 1, scope: "org", permissions: [], step_up_fresh_until: null, catalogue_fingerprint: "x" };
    expect(await consolePageAllowed("/v2/hive")).toBe(false);
    boot.org = { epoch: 1, scope: "org", permissions: ["directory:read", "agent:read", "code_task:read", "workflow:read", "project:read", "system:read", "autopilot:read", "meeting:read"], step_up_fresh_until: null, catalogue_fingerprint: "x" };
    // a page with no declared permission is never blocked
    expect(await consolePageAllowed("/v2/nowhere")).toBe(true);
  });
});
