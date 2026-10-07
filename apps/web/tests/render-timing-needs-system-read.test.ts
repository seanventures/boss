/**
 * /api/render-timing answers only a viewer who holds `system:read` (P1-18). It is the console
 * process's own diagnostics — no API call stands behind it — so the route reads the viewer's
 * permissions itself and fails CLOSED: no session 401, identity unreachable 503, no permission
 * 403 naming it; only then the timings.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MePermissionsResponse } from "../lib/identity/contract";
import type { IdentityBoot } from "../lib/identity/server";

let boot: IdentityBoot;
vi.mock("../lib/identity/server", () => ({ loadIdentity: vi.fn(async () => boot) }));
vi.mock("../lib/render-timing", () => ({ lastRenderTimings: () => [{ path: "/", ms: 12 }] }));
vi.mock("../lib/feed-cache", async (orig) => ({ ...(await orig<object>()), slowestFeeds: () => [{ key: "feed", ms: 40 }] }));

const { GET } = await import("../app/api/render-timing/route");
const { PROJECT_ID } = await import("../lib/client");

function perms(scope: string, permissions: string[]): MePermissionsResponse {
  return { epoch: 1, scope, permissions, step_up_fresh_until: null, catalogue_fingerprint: "x" };
}

beforeEach(() => {
  boot = { me: null, org: null, project: null, problem: null };
});

describe("/api/render-timing", () => {
  it("no session: 401, no timings", async () => {
    boot = { ...boot, problem: "unauthenticated" };
    const res = await GET();
    expect(res.status).toBe(401);
    expect(JSON.stringify(await res.json())).not.toContain("renders");
  });

  it("identity unreachable: 503 (fails closed), no timings", async () => {
    boot = { ...boot, problem: "identity_unavailable" };
    const res = await GET();
    expect(res.status).toBe(503);
    expect(await res.json()).toEqual({ error: "identity_unavailable" });
  });

  it("without system:read: 403 naming the permission", async () => {
    boot = { ...boot, org: perms("org", ["directory:read"]), project: perms(`project:${PROJECT_ID}`, ["workflow:read"]) };
    const res = await GET();
    expect(res.status).toBe(403);
    expect(await res.json()).toEqual({ error: "forbidden", permission: "system:read" });
  });

  it("with system:read at the console's project: the timings", async () => {
    boot = { ...boot, org: perms("org", ["directory:read"]), project: perms(`project:${PROJECT_ID}`, ["system:read"]) };
    const res = await GET();
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ renders: [{ path: "/", ms: 12 }], slowest: [{ key: "feed", ms: 40 }] });
  });
});
