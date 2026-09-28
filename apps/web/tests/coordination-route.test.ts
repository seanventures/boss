import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/client", () => ({
  API_URL: "http://boss-api",
  authHeaders: () => ({ Authorization: "Bearer test-key" }),
}));

import { GET } from "../app/api/coordination/route";

afterEach(() => vi.unstubAllGlobals());

describe("coordination route", () => {
  it("relays the project-scoped API snapshot without changing its shape", async () => {
    const snapshot = { roster: [], inFlight: [], observedAt: "2026-09-28T12:00:00.000Z" };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(snapshot), {
      headers: { "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    const response = await GET(new Request("http://console/api/coordination?project_id=alpha%20team"));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(snapshot);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://boss-api/v1/coordination?project_id=alpha%20team",
      { headers: { Authorization: "Bearer test-key" }, cache: "no-store" },
    );
  });

  it("preserves upstream authorization errors and rejects missing project scope", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"detail":"denied"}', {
      status: 403, headers: { "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const missing = await GET(new Request("http://console/api/coordination"));
    expect(missing.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
    const denied = await GET(new Request("http://console/api/coordination?project_id=alpha"));
    expect(denied.status).toBe(403);
    expect(await denied.json()).toEqual({ detail: "denied" });
  });
});
