/**
 * A console built for a host whose network already decides who gets in (goldeneye, behind Sean's
 * network): no sign-in of its own, but still never a cross-site write. Every other build keeps
 * the password, the session and the HTTPS check — the mode is a build arg baked into the image
 * (Dockerfile.community), not something a stray runtime variable turns on by default.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { middleware } from "../middleware";

afterEach(() => vi.unstubAllEnvs());

function proxied(path: string, init: { method?: string; origin?: string } = {}) {
  const headers: Record<string, string> = {
    host: "boss.example.test", "x-forwarded-host": "boss.example.test",
    "x-forwarded-proto": "https",
  };
  if (init.origin) headers.origin = init.origin;
  return new NextRequest(`http://10.0.0.5:3000${path}`, { method: init.method ?? "GET", headers });
}

describe("a network-trusted console", () => {
  it("opens pages and API reads with no sign-in, behind a proxy with no public origin set", async () => {
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
    vi.stubEnv("BOSS_CONSOLE_PASSWORD", "");
    vi.stubEnv("BOSS_CONSOLE_SESSION_SECRET", "");
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "");
    for (const path of ["/", "/?area=workforce", "/v1/projects"]) {
      const response = await middleware(proxied(path));
      expect(response.status, path).toBe(200);
      expect(response.headers.get("x-middleware-next")).toBe("1");
    }
  });

  it("still refuses a write from another site, and allows one from its own page", async () => {
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "");
    const evil = await middleware(proxied("/v1/decisions/d1/approve",
      { method: "POST", origin: "https://evil.example" }));
    expect(evil.status).toBe(403);
    const none = await middleware(proxied("/v1/decisions/d1/approve", { method: "POST" }));
    expect(none.status).toBe(403);
    const own = await middleware(proxied("/v1/decisions/d1/approve",
      { method: "POST", origin: "https://boss.example.test" }));
    expect(own.status).toBe(200);
  });

  it("is off unless asked for: the same request without the mode is refused", async () => {
    vi.stubEnv("BOSS_CONSOLE_AUTH", "");
    vi.stubEnv("BOSS_CONSOLE_PASSWORD", "");
    vi.stubEnv("BOSS_CONSOLE_SESSION_SECRET", "");
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "");
    expect([426, 503]).toContain((await middleware(proxied("/"))).status);
  });

  it("is a build arg of the web image that defaults to the console's own sign-in", () => {
    const dockerfile = readFileSync(join(__dirname, "..", "..", "..", "Dockerfile.community"), "utf8");
    expect(dockerfile).toMatch(/ARG BOSS_CONSOLE_AUTH=\n/);
    expect(dockerfile).toMatch(/ENV BOSS_CONSOLE_AUTH=\$\{BOSS_CONSOLE_AUTH\}/);
  });
});
