/**
 * Another install's heartbeat reaches this collector through the console (shared-server's API host
 * may be fronted by it): a server-to-server POST with no Origin and no session passes the
 * middleware — on a network-trusted console AND on a password console — but only on the three
 * collector paths, only as POST, and never when it carries an Origin or a cookie (a browser). The
 * console's rule for browser writes is unchanged. docs/product/RUN_SCORECARDS.md, "Collector".
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { middleware } from "../middleware";
import { collectorMachinePost } from "../lib/console-session";

afterEach(() => vi.unstubAllEnvs());

function req(path: string, init: { method?: string; origin?: string; cookie?: string } = {}) {
  const headers: Record<string, string> = {
    host: "boss-api.example.test", "x-forwarded-host": "boss-api.example.test",
    "x-forwarded-proto": "https", "content-encoding": "gzip",
  };
  if (init.origin) headers.origin = init.origin;
  if (init.cookie) headers.cookie = init.cookie;
  return new NextRequest(`http://10.0.0.5:3000${path}`, { method: init.method ?? "POST", headers });
}

describe("a collector POST from another install", () => {
  for (const mode of ["network", ""]) {
    it(`passes on a ${mode || "password"} console, on exactly the three paths`, async () => {
      vi.stubEnv("BOSS_CONSOLE_AUTH", mode);
      vi.stubEnv("BOSS_CONSOLE_PASSWORD", mode ? "" : "a".repeat(40));
      vi.stubEnv("BOSS_CONSOLE_SESSION_SECRET", mode ? "" : "b".repeat(40));
      vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "");
      for (const p of ["/v1/telemetry/ingest", "/v1/telemetry/delete", "/v1/telemetry/share"]) {
        const r = await middleware(req(p));
        expect(r.status, p).toBe(200);
        expect(r.headers.get("x-middleware-next"), p).toBe("1");
      }
    });
  }

  it("is not a way around the browser rule", async () => {
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "");
    // A browser sends Origin: a cross-site page posting to the collector path is still refused.
    expect((await middleware(req("/v1/telemetry/ingest", { origin: "https://evil.example" }))).status).toBe(403);
    // Any other write without Origin is still refused.
    expect((await middleware(req("/v1/telemetry/sharing", { method: "PUT" }))).status).toBe(403);
    expect((await middleware(req("/v1/decisions/d1/approve"))).status).toBe(403);
    // Only POST, and never with a session cookie.
    expect(collectorMachinePost(req("/v1/telemetry/ingest", { method: "GET" }))).toBe(false);
    expect(collectorMachinePost(req("/v1/telemetry/ingest", { cookie: "__Host-boss_console_session=x" }))).toBe(false);
    expect(collectorMachinePost(req("/v1/telemetry/ingest/x"))).toBe(false);
  });

  it("the /v1 proxy attaches no credential to them: no key exists, and no cookie is forwarded", async () => {
    const { readFileSync } = await import("node:fs");
    const src = readFileSync(new URL("../app/v1/[...path]/route.ts", import.meta.url), "utf8");
    expect(src).toMatch(/forwardHeaders\(req, collector !== null\)/);
    expect(src).toMatch(/const cookie = machine \? "" : identityCookieHeader\(/);
    expect(src).not.toMatch(/set\("Authorization"/);
  });

  it("a collector route spelled any other way is refused in middleware, connection closed", async () => {
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "");
    for (const p of ["/v1/telemetry/%69ngest", "/v1//telemetry/share", "/v1/telemetry/delete%2F"]) {
      const r = await middleware(req(p));
      expect(r.status, p).toBe(404);
      expect(r.headers.get("connection"), p).toBe("close");
    }
  });
});
