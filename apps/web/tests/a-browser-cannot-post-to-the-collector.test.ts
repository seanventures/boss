/**
 * Final re-check A: the collector's three paths skip middleware (so Next does not tee their
 * bodies), which also skipped the only Origin/cookie check. The /v1 route enforces it itself now:
 * a browser's POST — cross-site text/plain form, no-cors fetch, anything carrying Origin, a cookie
 * or a Sec-Fetch-* header — is refused before a byte is read; a server-to-server POST goes through
 * with no console key and only the client hop the console's own proxies vouched for (re-check C).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clientHop } from "../lib/client-hop";

type Call = { url: string; headers: Headers };
let calls: Call[] = [];

beforeEach(() => {
  vi.stubEnv("BOSS_API_URL", "http://api.example.test");
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (u: unknown, init?: RequestInit) => {
    calls.push({ url: String(u), headers: new Headers(init?.headers) });
    return Response.json({ ok: true });
  }));
});
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); vi.resetModules(); });

async function post(headers: Record<string, string>, path = "/v1/telemetry/ingest") {
  const { POST } = await import("../app/v1/[...path]/route");
  const req = new Request(`http://boss.example.test${path}`, { method: "POST", body: "x", headers });
  return POST(req, { params: Promise.resolve({ path: path.split("?")[0].split("/").slice(2) }) });
}

describe("a POST to the collector through the console", () => {
  const BROWSER: Record<string, Record<string, string>> = {
    "cross-site text/plain form": { "content-type": "text/plain", origin: "https://evil.example.test",
                                    "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document" },
    "no-cors fetch": { "content-type": "text/plain", origin: "https://evil.example.test",
                       "sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors" },
    "same-site fetch with a cookie": { cookie: "__Host-boss_console_session=x" },
    "Sec-Fetch-Site alone": { "sec-fetch-site": "same-origin" },
    "Sec-Fetch-Mode alone": { "sec-fetch-mode": "cors" },
  };
  // Re-check 4, item 1: the trailing-slash and query spellings skip middleware too, and the route
  // compared the raw path — so they skipped this check, the cap, and got the console's key.
  const SPELLINGS = [
    "/v1/telemetry/ingest", "/v1/telemetry/ingest/", "/v1/telemetry/delete/", "/v1/telemetry/share/?x",
    "/v1/telemetry/share?x=1", "/v1/telemetry/ingest//",
  ];
  for (const [name, headers] of Object.entries(BROWSER)) {
    for (const path of SPELLINGS) {
      it(`refuses a browser's: ${name}, at ${path}`, async () => {
        const res = await post(headers, path);
        expect(res.status).toBe(403);
        expect(res.headers.get("connection")).toBe("close");
        expect(calls).toEqual([]);
      });
    }
  }

  for (const path of SPELLINGS) {
    it(`a machine POST to ${path} goes keyless, to the plain path, and no Location comes back`, async () => {
      vi.stubGlobal("fetch", vi.fn(async (u: unknown, init?: RequestInit) => {
        calls.push({ url: String(u), headers: new Headers(init?.headers) });
        return new Response("{}", { status: 307, headers: { location: "http://api.internal:8000/v1/x" } });
      }));
      const res = await post({ "content-type": "application/json" }, path);
      expect(calls).toHaveLength(1);
      expect(calls[0].headers.get("authorization")).toBeNull();
      expect(calls[0].url).toMatch(/^http:\/\/api\.example\.test\/v1\/telemetry\/(ingest|delete|share)$/);
      expect(res.headers.get("location")).toBeNull();
    });
  }

  it("lets a server-to-server POST through, with no console key", async () => {
    const res = await post({ "content-type": "application/json", "content-encoding": "gzip" });
    expect(res.status).toBe(200);
    expect(calls).toHaveLength(1);
    expect(calls[0].headers.get("authorization")).toBeNull();
  });

  it("forwards only the hop the console's proxies appended (BOSS_CONSOLE_PROXY_HOPS)", async () => {
    vi.stubEnv("BOSS_CONSOLE_PROXY_HOPS", "1");
    await post({ "x-forwarded-for": "6.6.6.6, 203.0.113.5", "x-real-ip": "6.6.6.6" });
    expect(calls[0].headers.get("x-forwarded-for")).toBe("203.0.113.5");
    expect(calls[0].headers.get("x-real-ip")).toBeNull();
  });

  it("with no proxy in front, forwards no client claim at all", async () => {
    vi.stubEnv("BOSS_CONSOLE_PROXY_HOPS", "0");
    await post({ "x-forwarded-for": "6.6.6.6" });
    expect(calls[0].headers.get("x-forwarded-for")).toBeNull();
  });

  it("other /v1 writes are untouched by this rule: the person's request, never a console key", async () => {
    await post({ origin: "https://boss.example.test", "sec-fetch-site": "same-origin", "x-boss-csrf": "c1" }, "/v1/telemetry/sharing");
    expect(calls).toHaveLength(1);
    expect(calls[0].headers.get("authorization")).toBeNull();
    expect(calls[0].headers.get("x-boss-csrf")).toBe("c1");
    expect(calls[0].headers.get("origin")).toBe("https://boss.example.test");
  });
});

describe("clientHop", () => {
  it("takes the hop N from the right, and nothing malformed", () => {
    expect(clientHop("1.1.1.1, 2.2.2.2", 1)).toBe("2.2.2.2");
    expect(clientHop("1.1.1.1, 2.2.2.2, 3.3.3.3", 2)).toBe("2.2.2.2");
    expect(clientHop("unknown", 1)).toBeNull();
    expect(clientHop("1.1.1.1", 0)).toBeNull();
    expect(clientHop(null, 1)).toBeNull();
  });
});
