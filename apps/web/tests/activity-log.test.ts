import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it, vi } from "vitest";

import { emptyActivityLog, mapActivityLog } from "../lib/activity-log";

vi.mock("../lib/client", () => ({
  API_URL: "https://boss-api.test",
  PROJECT_ID: "boss-dogfood",
  authHeaders: (extra?: Record<string, string>) => ({ Authorization: "Bearer k", ...(extra ?? {}) }),
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
}));

const { GET } = await import("../app/api/activity/route");

const pageSource = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
const platformOpsSource = readFileSync(resolve(__dirname, "./platform-ops-ui.test.ts"), "utf8");

describe("the Activity log area", () => {
  it("mounts the Log explorer on data-area=activity", () => {
    expect(pageSource).toContain('import { LogExplorer } from "./activity/log-explorer"');
    expect(pageSource).toContain('data-area="activity"');
    expect(pageSource).toContain("<LogExplorer projectId={activeProjectId} actor={activeActorId} />");
  });

  it("is in the platform-ops area registry", () => {
    const match = platformOpsSource.match(/const EXISTING_AREA_IDS = new Set\(\[([\s\S]*?)\]\)/);
    expect(match?.[1] ?? "").toMatch(/"activity"/);
  });
});

describe("Activity log mappers", () => {
  it("does not invent rows or a success status", () => {
    expect(mapActivityLog({ items: [] })).toMatchObject({ apiReachable: true, items: [] });
    expect(mapActivityLog({ items: [{ title: "missing id" }] }).items).toEqual([]);
    expect(mapActivityLog({ items: [{ id: "evt_1", kind: "event", status: "mystery" }] }).items[0])
      .toMatchObject({ id: "evt_1", kind: "event", status: "info" });
    const empty = emptyActivityLog("activity api down");
    expect([empty.apiReachable, empty.items, empty.blockedReason]).toEqual([false, [], "activity api down"]);
  });

  it("keeps the names the server sends for the ids on the page", () => {
    const log = mapActivityLog({ items: [], labels: { crun_1: "Ship it #182", n: 5 }, window: { from: "a", to: null, range: "24h" } });
    expect(log.labels).toEqual({ crun_1: "Ship it #182" });
    expect(log.window).toEqual({ from: "a", to: null, range: "24h" });
  });
});

describe("the route handler", () => {
  it("forwards known filters only, with the ETag both ways, and a 304 stays a 304", async () => {
    const seen: { url: string; inm: string | null }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
      seen.push({ url: String(url), inm: new Headers(init?.headers as HeadersInit).get("If-None-Match") });
      return new Response(null, { status: 304, headers: { ETag: "\"e1\"" } });
    }));
    const r = await GET(new Request("http://console.test/api/activity?project_id=boss-dogfood&level=error&evil=1&after=c",
                                    { headers: { "If-None-Match": "\"e1\"" } }));
    expect(r.status).toBe(304);
    expect(r.headers.get("etag")).toBe("\"e1\"");
    expect(seen[0].url).toBe("https://boss-api.test/v1/activity?project_id=boss-dogfood&level=error&after=c");
    expect(seen[0].inm).toBe("\"e1\"");
    vi.unstubAllGlobals();
  });

  it("forwards a refusal as the API's sentence, never an object", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "Sort by time; use filters for level or status" }, { status: 422 })));
    const r = await GET(new Request("http://console.test/api/activity?project_id=p&sort=kind"));
    expect(r.status).toBe(422);
    expect(await r.json()).toEqual({ error: "Sort by time; use filters for level or status" });
    vi.unstubAllGlobals();
  });
});
