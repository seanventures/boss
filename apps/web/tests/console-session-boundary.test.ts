import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { middleware, config as middlewareConfig } from "../middleware";
import { collectorPath, collectorPathOf, safeNext } from "../lib/console-session";

/**
 * THE CONSOLE'S BOUNDARY, AFTER IDENTITY S4: the API owns the session (`__Host-boss_session`,
 * IDENTITY.md §4.1) and middleware only routes on its presence. There is no console password,
 * no HMAC cookie and no shared key; the transport rules (HTTPS when remote, same-origin writes,
 * loopback tunnels, the collector's exception) are unchanged.
 */
const SESSION = "__Host-boss_session=bss_test";

function configured() {
  vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://boss.example");
}

afterEach(() => vi.unstubAllEnvs());

describe("the console boundary", () => {
  it("needs no console secret: with nothing configured it still routes, and still refuses the unsigned", async () => {
    vi.stubEnv("BOSS_CONSOLE_PASSWORD", "");
    vi.stubEnv("BOSS_CONSOLE_SESSION_SECRET", "");
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://boss.example");
    expect((await middleware(new NextRequest("https://boss.example/v1/projects"))).status).toBe(401);
  });

  it.each(["/", "/v1/projects", "/api/projects-strip?project_id=other-org", "/api/skills"])(
    "denies an unauthenticated browser before privileged %s reaches its handler",
    async (path) => {
      configured();
      const response = await middleware(new NextRequest(`https://boss.example${path}`));
      expect(response.status).toBe(path === "/" ? 307 : 401);
      if (path === "/") expect(response.headers.get("location"))
        .toBe("https://boss.example/login?next=%2F#");   // empty fragment: no link token rides along
      expect(response.headers.get("cache-control")).toMatch(/no-store|private/);
    },
  );

  it("covers every path, including the rendered page and hand-written API routes", () => {
    // ONE named exception: the Improve BOSS collector's three machine POSTs, kept out so Next does
    // not buffer (tee) their bodies. They carry no credential (the /v1 proxy attaches no key to
    // them), and the /v1 route refuses any browser request to them itself
    // (tests/a-browser-cannot-post-to-the-collector.test.ts).
    expect(middlewareConfig.matcher).toEqual(["/((?!v1/telemetry/(?:ingest|delete|share)/*$).*)"]);
    const re = new RegExp(`^${middlewareConfig.matcher[0]}$`);
    for (const p of ["/", "/api/govern", "/v1/projects", "/v1/telemetry/sharing", "/login", "/v1/telemetry/ingest/x",
                     "/v1/Telemetry/ingest", "/v1/telemetry/shared"]) {
      expect(re.test(p), p).toBe(true);
    }
    const route = readFileSync(join(process.cwd(), "app", "v1", "[...path]", "route.ts"), "utf8");
    expect(route).toMatch(/const collector = collectorPathOf\(req\.url\)/);
    expect(route).toMatch(/if \(collector && !collectorMachinePost\(req\)\)/);
    expect(route).not.toMatch(/COLLECTOR_PATHS/);
  });

  it("every spelling that skips middleware is one the route treats as the collector (re-check 4)", () => {
    // The HIGH: `/ingest/`, `delete/` and `share/?x` skipped middleware AND the route's exact-path
    // check, so they got no browser check, no cap — and the console's key. One decision now.
    const re = new RegExp(`^${middlewareConfig.matcher[0]}$`);
    const forms = [
      "/v1/telemetry/ingest", "/v1/telemetry/ingest/", "/v1/telemetry/delete/", "/v1/telemetry/share/",
      "/v1/telemetry/share//", "/v1/telemetry/ingest///",
    ];
    for (const p of forms) {
      expect(re.test(p), `${p} skips middleware`).toBe(false);
      expect(collectorPath(p), p).toBe(p.replace(/\/+$/, ""));
    }
    // The query never changes the decision (the matcher sees the pathname only).
    expect(collectorPathOf("http://x/v1/telemetry/share/?x=1")).toBe("/v1/telemetry/share");
    expect(collectorPathOf("http://x/v1/telemetry/ingest?y")).toBe("/v1/telemetry/ingest");
    // Spellings middleware does see are still the collector — and middleware refuses them.
    for (const p of ["/v1/telemetry/%69ngest", "/v1//telemetry/ingest", "/v1/telemetry/ingest%2F"]) {
      expect(re.test(p), p).toBe(true);
      expect(collectorPath(p), p).not.toBeNull();
    }
    // Case-sensitive, like the API's routes; neighbours are not the collector.
    for (const p of ["/v1/Telemetry/ingest", "/v1/telemetry/INGEST", "/v1/telemetry/sharing", "/v1/telemetry/ingest/x"]) {
      expect(collectorPath(p), p).toBeNull();
    }
  });

  it("passes a request carrying the API's session cookie, for any project — the API decides", async () => {
    configured();
    for (const projectId of ["alpha", "another-org-project"]) {
      const request = new NextRequest(`https://boss.example/v1/tasks?project_id=${projectId}`, { headers: { cookie: SESSION } });
      expect((await middleware(request)).status).toBe(200);
    }
    // The retired console cookie is not a session any more.
    const old = new NextRequest("https://boss.example/v1/tasks", { headers: { cookie: "__Host-boss_console_session=v1.x.y.z" } });
    expect((await middleware(old)).status).toBe(401);
  });

  it("rejects cross-origin and originless mutations even with a session", async () => {
    configured();
    const headers = { cookie: SESSION };
    const foreign = new NextRequest("https://boss.example/v1/commands", {
      method: "POST", headers: { ...headers, origin: "https://attacker.example" },
    });
    const missing = new NextRequest("https://boss.example/api/builds", { method: "POST", headers });
    const owner = new NextRequest("https://boss.example/v1/commands", {
      method: "POST", headers: { ...headers, origin: "https://boss.example" },
    });
    expect((await middleware(foreign)).status).toBe(403);
    expect((await middleware(missing)).status).toBe(403);
    expect((await middleware(owner)).status).toBe(200);
  });

  it("keeps sign-in reachable: the page and the public sign-in API, still same-origin for writes", async () => {
    configured();
    expect((await middleware(new NextRequest("https://boss.example/login"))).status).toBe(200);
    const signIn = new NextRequest("https://boss.example/v1/auth/login", {
      method: "POST", headers: { origin: "https://boss.example", "content-type": "application/json" }, body: "{}",
    });
    expect((await middleware(signIn)).status).toBe(200);
    const forged = new NextRequest("https://boss.example/v1/auth/login", {
      method: "POST", headers: { origin: "https://attacker.example", "content-type": "application/json" }, body: "{}",
    });
    expect((await middleware(forged)).status).toBe(403);
    // The old shared-password routes are gone.
    const { existsSync } = await import("node:fs");
    expect(existsSync(join(process.cwd(), "app", "api", "auth", "login"))).toBe(false);
    expect(existsSync(join(process.cwd(), "app", "api", "auth", "logout"))).toBe(false);
  });

  it("does not allow a sign-in redirect to leave the console", () => {
    expect(safeNext("//attacker.example")).toBe("/");
    expect(safeNext("/\\attacker.example")).toBe("/");
    expect(safeNext("/\t/attacker.example")).toBe("/");
    expect(safeNext("https://attacker.example")).toBe("/");
    expect(safeNext("/?project_id=alpha")).toBe("/?project_id=alpha");
  });

  it("requires HTTPS on remote origins, including direct HTTP with forged API requests", async () => {
    configured();
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "");
    const request = new NextRequest("http://localhost:3000/v1/projects", { headers: {
      host: "boss.example", "x-forwarded-proto": "https", "x-forwarded-host": "boss.example",
    } });
    expect((await middleware(request)).status).toBe(426);
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://boss.example");
    expect((await middleware(new NextRequest("http://localhost:3000/login", { headers: {
      host: "boss.example", "x-forwarded-proto": "http", "x-forwarded-host": "boss.example",
    } }))).status).toBe(426);
    expect((await middleware(new NextRequest("http://localhost:3000/login", { headers: {
      host: "boss.example", "x-forwarded-proto": "https", "x-forwarded-host": "other.example",
    } }))).status).toBe(426);
    expect((await middleware(new NextRequest("http://127.0.0.1:3000/v1/projects"))).status)
      .toBe(401);
  });

  it("shows local sign-in without switching the browser's loopback host", async () => {
    configured();
    const response = await middleware(new NextRequest("http://127.0.0.1:3000/v2", {
      headers: { host: "127.0.0.1:3000" },
    }));
    expect(response.status).toBe(200);
    expect(response.headers.get("location")).toBeNull();
    expect(response.headers.get("x-middleware-rewrite"))
      .toBe("http://localhost:3000/login?next=%2Fv2");
  });

  it("behind a configured TLS proxy, sends a signed-out page to sign in on the public origin", async () => {
    configured();
    const headers = { host: "boss.example", "x-forwarded-proto": "https", "x-forwarded-host": "boss.example" };
    const page = await middleware(new NextRequest("http://localhost:3000/v2?area=work", { headers }));
    expect(page.status).toBe(307);
    expect(page.headers.get("location")).toBe("https://boss.example/login?next=%2Fv2%3Farea%3Dwork#");
  });

  it("accepts local tunnel mutations even when a public HTTPS origin is configured", async () => {
    configured();
    const request = new NextRequest("http://127.0.0.1:3000/api/builds", {
      method: "POST", headers: { host: "127.0.0.1:3000", cookie: SESSION, origin: "http://127.0.0.1:3000" },
    });
    expect((await middleware(request)).status).toBe(200);
    // The tunnel's browser-facing port can differ from Next's internal listener port.
    const tunneled = new NextRequest("http://127.0.0.1:3000/api/builds", {
      method: "POST", headers: { host: "127.0.0.1:3108", cookie: SESSION, origin: "http://127.0.0.1:3108" },
    });
    expect((await middleware(tunneled)).status).toBe(200);
    const otherLoopbackOrigin = new NextRequest("http://127.0.0.1:3000/api/builds", {
      method: "POST", headers: { host: "127.0.0.1:3108", cookie: SESSION, origin: "http://localhost:3108" },
    });
    expect((await middleware(otherLoopbackOrigin)).status).toBe(403);
  });
});
