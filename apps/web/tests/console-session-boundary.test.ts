import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { middleware, config as middlewareConfig } from "../middleware";
import {
  consoleAuthConfig, issueSession, passwordMatches, safeNext, SESSION_SECONDS,
  sessionCookieName, validSession,
} from "../lib/console-session";
import { POST as login } from "../app/api/auth/login/route";

const PASSWORD = "a".repeat(48);
const SECRET = "b".repeat(64);

function configured() {
  vi.stubEnv("BOSS_CONSOLE_PASSWORD", PASSWORD);
  vi.stubEnv("BOSS_CONSOLE_SESSION_SECRET", SECRET);
  vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://boss.example");
}

afterEach(() => vi.unstubAllEnvs());

describe("single-operator console boundary", () => {
  it("fails closed without separate login configuration", async () => {
    vi.stubEnv("BOSS_CONSOLE_PASSWORD", "");
    vi.stubEnv("BOSS_CONSOLE_SESSION_SECRET", "");
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://boss.example");
    expect(consoleAuthConfig()).toBeNull();
    expect((await middleware(new NextRequest("https://boss.example/v1/projects"))).status).toBe(503);
  });

  it.each(["/", "/v1/projects", "/api/projects-strip?project_id=other-org", "/api/skills"])(
    "denies an unauthenticated browser before privileged %s reaches its handler",
    async (path) => {
      configured();
      const response = await middleware(new NextRequest(`https://boss.example${path}`));
      expect(response.status).toBe(path === "/" ? 307 : 401);
      if (path === "/") expect(response.headers.get("location"))
        .toBe("https://boss.example/login?next=%2F");
      expect(response.headers.get("cache-control")).toMatch(/no-store|private/);
    },
  );

  it("covers every path, including the rendered page and hand-written API routes", () => {
    expect(middlewareConfig.matcher).toEqual(["/:path*"]);
  });

  it("issues a signed, expiring owner session that can navigate multiple projects", async () => {
    configured();
    const auth = consoleAuthConfig()!;
    expect(await passwordMatches(PASSWORD, auth)).toBe(true);
    expect(await passwordMatches("wrong", auth)).toBe(false);
    const now = 1_700_000_000_000;
    const cookie = await issueSession(auth, now, "12345678-1234-1234-1234-123456789abc");
    expect(await validSession(cookie, auth, now + 1)).toBe(true);
    expect(await validSession(cookie, auth, now + SESSION_SECONDS * 1000)).toBe(false);
    expect(await validSession(cookie.slice(0, -1) + (cookie.endsWith("0") ? "1" : "0"),
      auth, now + 1)).toBe(false);
    expect(await validSession(cookie, { ...auth, password: "c".repeat(48) }, now + 1)).toBe(false);
    const liveCookie = await issueSession(auth);
    for (const projectId of ["alpha", "another-org-project"]) {
      const request = new NextRequest(
        `https://boss.example/v1/tasks?project_id=${projectId}`,
        { headers: { cookie: `${sessionCookieName(new Request("https://boss.example"))}=${liveCookie}` } },
      );
      expect((await middleware(request)).status).toBe(200);
    }
  });

  it("rejects cross-origin and originless mutations even with a valid cookie", async () => {
    configured();
    const cookie = await issueSession(consoleAuthConfig()!);
    const headers = { cookie: `__Host-boss_console_session=${cookie}` };
    const foreign = new NextRequest("https://boss.example/v1/commands", {
      method: "POST", headers: { ...headers, origin: "https://attacker.example" },
    });
    const missing = new NextRequest("https://boss.example/api/builds", {
      method: "POST", headers,
    });
    const owner = new NextRequest("https://boss.example/v1/commands", {
      method: "POST", headers: { ...headers, origin: "https://boss.example" },
    });
    expect((await middleware(foreign)).status).toBe(403);
    expect((await middleware(missing)).status).toBe(403);
    expect((await middleware(owner)).status).toBe(200);
  });

  it("keeps login reachable and sets a private browser cookie without an API key", async () => {
    configured();
    expect((await middleware(new NextRequest("https://boss.example/login"))).status).toBe(200);
    expect((await middleware(new NextRequest("https://boss.example/api/auth/login"))).status).toBe(200);
    const form = new URLSearchParams({ password: PASSWORD, next: "/?project_id=alpha" });
    const request = new NextRequest("https://boss.example/api/auth/login", {
      method: "POST", headers: {
        origin: "https://boss.example", "content-type": "application/x-www-form-urlencoded",
      }, body: form.toString(),
    });
    const response = await login(request);
    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("/?project_id=alpha");
    const setCookie = response.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain("__Host-boss_console_session=");
    expect(setCookie).toContain("HttpOnly");
    expect(setCookie).toContain("Secure");
    expect(setCookie).toContain("SameSite=strict");
    expect(setCookie).not.toContain(PASSWORD);
    expect(setCookie).not.toContain("BOSS_API_KEY");
  });

  it("does not allow a login redirect to leave the console", () => {
    expect(safeNext("//attacker.example")).toBe("/");
    expect(safeNext("/\\attacker.example")).toBe("/");
    expect(safeNext("/\t/attacker.example")).toBe("/");
    expect(safeNext("https://attacker.example")).toBe("/");
    expect(safeNext("/?project_id=alpha")).toBe("/?project_id=alpha");
  });

  it("keeps a submitted control-character return path inside the console", async () => {
    configured();
    const request = new NextRequest("https://boss.example/api/auth/login", {
      method: "POST", headers: {
        origin: "https://boss.example", "content-type": "application/x-www-form-urlencoded",
      },
      body: new URLSearchParams({ password: PASSWORD, next: "/\t/attacker.example" }).toString(),
    });
    expect((await login(request)).headers.get("location")).toBe("/");
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

  it("trusts only an explicitly configured loopback TLS proxy and sets a Secure cookie", async () => {
    configured();
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://boss.example");
    const headers = {
      host: "boss.example", "x-forwarded-proto": "https",
      "x-forwarded-host": "boss.example", origin: "https://boss.example",
      "content-type": "application/x-www-form-urlencoded",
    };
    const page = await middleware(new NextRequest("http://localhost:3000/v2?area=work", {
      headers,
    }));
    expect(page.status).toBe(307);
    expect(page.headers.get("location"))
      .toBe("https://boss.example/login?next=%2Fv2%3Farea%3Dwork");
    const request = new NextRequest("http://localhost:3000/api/auth/login", {
      method: "POST", headers, body: new URLSearchParams({ password: PASSWORD }).toString(),
    });
    expect((await middleware(request)).status).toBe(200);
    const response = await login(request);
    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("/");
    expect(response.headers.get("set-cookie")).toContain("__Host-boss_console_session=");
    expect(response.headers.get("set-cookie")).toContain("Secure");
  });

  it("accepts local tunnel mutations even when a public HTTPS origin is configured", async () => {
    configured();
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://boss.example");
    const cookie = await issueSession(consoleAuthConfig()!);
    const request = new NextRequest("http://127.0.0.1:3000/api/builds", {
      method: "POST", headers: {
        host: "127.0.0.1:3000",
        cookie: `boss_console_session_local=${cookie}`,
        origin: "http://127.0.0.1:3000",
      },
    });
    expect((await middleware(request)).status).toBe(200);
    // The tunnel's browser-facing port can differ from Next's internal listener port.
    const tunneled = new NextRequest("http://127.0.0.1:3000/api/builds", {
      method: "POST", headers: {
        host: "127.0.0.1:3108", cookie: `boss_console_session_local=${cookie}`,
        origin: "http://127.0.0.1:3108",
      },
    });
    expect((await middleware(tunneled)).status).toBe(200);
    const otherLoopbackOrigin = new NextRequest("http://127.0.0.1:3000/api/builds", {
      method: "POST", headers: {
        host: "127.0.0.1:3108", cookie: `boss_console_session_local=${cookie}`,
        origin: "http://localhost:3108",
      },
    });
    expect((await middleware(otherLoopbackOrigin)).status).toBe(403);
    const forged = new NextRequest("http://127.0.0.1:3000/api/auth/login", {
      method: "POST", headers: {
        host: "127.0.0.1:3000", "x-forwarded-proto": "https",
        "x-forwarded-host": "boss.example", origin: "https://boss.example",
        "content-type": "application/x-www-form-urlencoded",
      }, body: new URLSearchParams({ password: PASSWORD }).toString(),
    });
    expect((await login(forged)).status).toBe(403);
  });

  it("keeps login redirects on the caller's loopback host", async () => {
    configured();
    const request = new NextRequest("http://127.0.0.1:3000/api/auth/login", {
      method: "POST", headers: {
        host: "127.0.0.1:3000", origin: "http://127.0.0.1:3000",
        "content-type": "application/x-www-form-urlencoded",
      }, body: new URLSearchParams({ password: PASSWORD }).toString(),
    });
    const response = await login(request);
    expect(response.headers.get("location")).toBe("/");
    expect(response.headers.get("set-cookie")).toContain("boss_console_session_local=");
  });
});
