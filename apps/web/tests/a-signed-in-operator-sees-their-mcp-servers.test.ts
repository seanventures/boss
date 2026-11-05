/**
 * A SIGNED-IN OPERATOR SEES THEIR MCP SERVERS (2026-10-21 audit).
 *
 * The console's MCP list answered 401 — lib/mcp.ts, GET /v1/mcp/servers?project_id=, and the
 * Library's list — while the same list answered for a real API key. The console holds no key.
 * A signed-in call carries the person's session, the same way every other console call does,
 * and the servers come back. An anonymous call stays 401. /v1/mcp/servers is not public.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { installIdentityResolver } from "../lib/identity/forward";
import { middleware } from "../middleware";

const API = "http://api.internal:8000";
const CONSOLE = "http://localhost:3000";
const SESSION = "__Host-boss_session=bss_owner";
const SERVERS = [{ id: "mcp_web", name: "web-intel", url: "http://mcp-tools:8770/mcp", tools: [{ name: "search_web" }] }];

type Sent = { url: string; method: string; headers: Headers };

let incoming = new Headers();
const sent: Sent[] = [];

function api(url: string, method: string, headers: Headers): Response {
  if (url.endsWith("/v1/identity/me")) {
    if (!headers.get("cookie")?.includes("__Host-boss_session=")) {
      return Response.json({ error: "unauthenticated" }, { status: 401 });
    }
    return Response.json({ csrf_token: "csrf_owner" });
  }
  const listing = url.includes("/v1/mcp/servers");
  if (!listing) return Response.json({});
  const cookie = headers.get("cookie") ?? "";
  if (!cookie.includes("__Host-boss_session=bss_owner")) {
    return Response.json({ error: "unauthenticated" }, { status: 401 });
  }
  if (headers.get("authorization")) {
    return Response.json({ error: "ambiguous_credentials" }, { status: 400 });
  }
  if (method !== "GET") {
    const site = headers.get("sec-fetch-site");
    const csrfOk = headers.get("origin") === CONSOLE
      && (site === null || site === "same-origin")
      && headers.get("x-boss-csrf") === "csrf_owner";
    if (!csrfOk) return Response.json({ error: "csrf_failed" }, { status: 403 });
  }
  return Response.json({ project_id: "p1", items: SERVERS });
}

beforeEach(() => {
  sent.length = 0;
  const g = globalThis as { __bossIdentityFetch?: true; __bossFeedCache?: unknown };
  delete g.__bossIdentityFetch;
  delete g.__bossFeedCache;
  vi.resetModules();
  vi.stubEnv("NEXT_RUNTIME", "nodejs");
  vi.stubEnv("BOSS_API_URL", API);
  vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", CONSOLE);
  vi.doMock("next/headers", () => ({ headers: async () => incoming }));
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
    sent.push({ url, method, headers });
    return api(url, method, headers);
  }));
});

afterEach(() => {
  vi.doUnmock("next/headers");
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  const g = globalThis as { __bossIdentityFetch?: true; __bossFeedCache?: unknown };
  delete g.__bossIdentityFetch;
  delete g.__bossFeedCache;
  installIdentityResolver(async () => null);
});

async function signedIn(extra: Record<string, string>) {
  incoming = new Headers({ host: "localhost:3000", cookie: `${SESSION}; theme=dark`, ...extra });
  await (await import("../instrumentation")).register();
}

describe("the Library's MCP list, for a signed-in operator", () => {
  it("GET /api/mcp?check=stale returns the project's servers and posts the twin with the session", async () => {
    await signedIn({ "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors" });
    const { GET } = await import("../app/api/mcp/route");
    const res = await GET(new Request(
      `${CONSOLE}/api/mcp?project_id=p1&check=stale`,
      { headers: { cookie: SESSION, "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors" } },
    ));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ project_id: "p1", items: SERVERS });
    const twin = sent.find((s) => s.url.includes("/v1/mcp/servers/load"));
    expect(twin?.method).toBe("POST");
    expect(twin?.url).toContain("project_id=p1");
    expect(twin?.url).toContain("check=stale");
    expect(twin?.headers.get("cookie")).toBe(SESSION);
    expect(twin?.headers.get("x-boss-csrf")).toBe("csrf_owner");
    expect(twin?.headers.get("origin")).toBe(CONSOLE);
    expect(twin?.headers.get("authorization")).toBeNull();
  });

  it("the same list with no session is 401 and no key is added to make it succeed", async () => {
    await signedIn({});
    incoming = new Headers({ host: "localhost:3000" });
    const { GET } = await import("../app/api/mcp/route");
    const res = await GET(new Request(`${CONSOLE}/api/mcp?project_id=p1&check=none`));
    expect(res.status).toBe(401);
    expect(sent.some((s) => s.headers.get("authorization"))).toBe(false);
  });
});

describe("GET /v1/mcp/servers on the console's origin", () => {
  it("forwards the signed-in session and returns that project's servers", async () => {
    await signedIn({ "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", origin: CONSOLE });
    const { GET } = await import("../app/v1/[...path]/route");
    const res = await GET(new Request(`${CONSOLE}/v1/mcp/servers?project_id=p1`, {
      headers: { cookie: `${SESSION}; theme=dark`, host: "localhost:3000", origin: CONSOLE,
                 "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors" },
    }), { params: Promise.resolve({ path: ["mcp", "servers"] }) });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ project_id: "p1", items: SERVERS });
    const upstream = sent.find((s) => s.url.startsWith(`${API}/v1/mcp/servers`));
    expect(upstream?.method).toBe("GET");
    expect(upstream?.headers.get("cookie")).toBe(SESSION);
    expect(upstream?.headers.get("authorization")).toBeNull();
  });
});

describe("fetchMcpServers", () => {
  it("returns the signed-in project's servers and does not send a key", async () => {
    await signedIn({ "x-boss-path": "/", "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate",
                     "sec-fetch-dest": "document", accept: "text/html" });
    const { fetchMcpServers } = await import("../lib/mcp");
    const feed = await fetchMcpServers("p1");
    expect(feed.apiReachable).toBe(true);
    expect(feed.blockedReason).toBeUndefined();
    expect(feed.items).toEqual(SERVERS);
    const twin = sent.find((s) => s.url.includes("/v1/mcp/servers/load"));
    expect(twin?.headers.get("cookie")).toBe(SESSION);
    expect(twin?.headers.get("authorization")).toBeNull();
    expect(twin?.headers.get("x-boss-csrf")).toBe("csrf_owner");
    expect(twin?.headers.get("origin")).toBe(CONSOLE);
  });

  it("an anonymous render is told 401 and is not shown an empty project", async () => {
    await signedIn({});
    incoming = new Headers({ host: "localhost:3000", "x-boss-path": "/" });
    const { fetchMcpServers } = await import("../lib/mcp");
    const feed = await fetchMcpServers("p1");
    expect(feed.apiReachable).toBe(false);
    expect(feed.blockedReason).toBe("API returned 401");
    expect(feed.items).toEqual([]);
  });
});

describe("an anonymous request still gets 401", () => {
  it.each(["/api/mcp?project_id=p1", "/v1/mcp/servers?project_id=p1"])("%s", async (path) => {
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://boss.example");
    const res = await middleware(new NextRequest(`https://boss.example${path}`));
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ error: "unauthenticated" });
  });

  it("a session cookie is allowed through to the API, which still decides", async () => {
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://boss.example");
    const res = await middleware(new NextRequest("https://boss.example/v1/mcp/servers?project_id=p1", {
      headers: { cookie: SESSION },
    }));
    expect(res.status).toBe(200);
  });
});
