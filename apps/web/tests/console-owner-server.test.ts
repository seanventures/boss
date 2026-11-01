/**
 * Build L (server side): pins resolveDeepLinkOwnerOnServer, the entry point app/page.tsx calls
 * before its fan-out (app/page.tsx:620). app/console-owner.ts's own core is pinned by
 * tests/console-owner.test.ts; this file covers the server wrapper's own two decisions: when
 * there is no API address to call, and how it drives the real `fetch`.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

const clientMock = vi.hoisted(() => ({ API_URL: "" }));
vi.mock("../lib/client", () => clientMock);

import { resolveDeepLinkOwnerOnServer } from "../lib/console-owner-server";

describe("resolveDeepLinkOwnerOnServer", () => {
  afterEach(() => {
    clientMock.API_URL = "";
    vi.unstubAllGlobals();
  });

  it("no API_URL configured: fails open without calling fetch", async () => {
    clientMock.API_URL = "";
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    const result = await resolveDeepLinkOwnerOnServer("?build=crun_1", "proj_selected");

    expect(result).toEqual({
      target: { param: "build", type: "build", id: "crun_1" },
      projectId: "proj_selected",
      switched: false,
      canView: false,
    });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("no deep-link target in the URL: keeps the current project without calling fetch", async () => {
    clientMock.API_URL = "https://api.internal";
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    const result = await resolveDeepLinkOwnerOnServer("?area=code_tasks", "proj_selected");

    expect(result).toEqual({ target: null, projectId: "proj_selected", switched: false, canView: false });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("API_URL configured and a target present: fetches the resolver with no-store and switches on success", async () => {
    clientMock.API_URL = "https://api.internal";
    const fetchSpy = vi.fn(async (_path: string, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({ project_id: "proj_owner", can_view: true }),
    }));
    vi.stubGlobal("fetch", fetchSpy);

    const result = await resolveDeepLinkOwnerOnServer("?meeting=mtg_1", "proj_selected");

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [path, init] = fetchSpy.mock.calls[0];
    expect(path).toBe("https://api.internal/v1/console/resolve-owner?type=meeting&id=mtg_1&project_id=proj_selected");
    expect(init).toEqual({ cache: "no-store", headers: { Accept: "application/json" } });
    expect(result).toEqual({
      target: { param: "meeting", type: "meeting", id: "mtg_1" },
      projectId: "proj_owner",
      switched: true,
      canView: true,
    });
  });

  it("a thrown fetch failure fails open, keeping the current project", async () => {
    clientMock.API_URL = "https://api.internal";
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("network down"); }));

    const result = await resolveDeepLinkOwnerOnServer("?build=crun_1", "proj_selected");

    expect(result).toEqual({
      target: { param: "build", type: "build", id: "crun_1" },
      projectId: "proj_selected",
      switched: false,
      canView: false,
    });
  });
});
