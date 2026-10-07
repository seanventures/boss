/**
 * A WRITING GET CARRIES ITS CSRF (round 2, R2-WEB 4).
 *
 * The console calls a GET that writes by its POST twin (lib/writing-get.ts). When the browser's
 * request was itself a GET (a route handler like /api/kit, a page render), the twin left with the
 * person's CSRF token but NO Origin, and the API refused it 403 csrf_failed for every role. The fix
 * is one place: instrumentation.ts's identity resolver takes its origin from
 * forwardedWriteOrigin(), which speaks for the console's own origin when, and only when, the GET
 * carries the same-origin proof the API accepts for a writing GET.
 *
 * Held here three ways: the rule itself; the real resolver (instrumentation.ts, next/headers
 * mocked) driving a real proxy (app/api/kit GET) to the wire; and a scan that every route
 * handler GET reaching a twin sends it through apiWrite, the write path that carries both.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { forwardedWriteOrigin, installIdentityResolver } from "../lib/identity/forward";

const WEB = process.cwd();

describe("forwardedWriteOrigin", () => {
  afterEach(() => vi.unstubAllEnvs());
  const h = (o: Record<string, string>) => new Headers({ host: "localhost:3000", ...o });

  it("a browser write keeps its own Origin", () => {
    expect(forwardedWriteOrigin(h({ origin: "http://localhost:3000" }))).toBe("http://localhost:3000");
  });
  it("a same-origin GET (the page's own fetch) speaks for the console's origin", () => {
    expect(forwardedWriteOrigin(h({ "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors" })))
      .toBe("http://localhost:3000");
  });
  it("a typed URL or a reload (none + navigate) does too", () => {
    expect(forwardedWriteOrigin(h({ "sec-fetch-site": "none", "sec-fetch-mode": "navigate" })))
      .toBe("http://localhost:3000");
  });
  it("behind a proxy, the configured public origin wins", () => {
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://boss.example/");
    expect(forwardedWriteOrigin(h({ "sec-fetch-site": "same-origin" }))).toBe("https://boss.example");
  });
  it("a cross-site GET, a same-site one, an opaque origin or no proof at all gets none", () => {
    expect(forwardedWriteOrigin(h({ "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate" }))).toBeNull();
    expect(forwardedWriteOrigin(h({ "sec-fetch-site": "same-site", "sec-fetch-mode": "cors" }))).toBeNull();
    expect(forwardedWriteOrigin(h({ "sec-fetch-site": "none", "sec-fetch-mode": "no-cors" }))).toBeNull();
    expect(forwardedWriteOrigin(h({ origin: "null" }))).toBeNull();
    expect(forwardedWriteOrigin(h({}))).toBeNull();
  });
  it("a Host header that is not a host is never echoed as an origin", () => {
    expect(forwardedWriteOrigin(new Headers({ host: "evil.example/x?", "sec-fetch-site": "same-origin" }))).toBeNull();
  });
});

describe("the resolver and a real writing-GET proxy, to the wire", () => {
  let incoming = new Headers();
  const sent: { url: string; method: string; headers: Headers }[] = [];

  beforeEach(() => {
    sent.length = 0;
    vi.resetModules();
    vi.stubEnv("NEXT_RUNTIME", "nodejs");
    vi.stubEnv("BOSS_API_URL", "http://api.internal:8000");
    vi.doMock("next/headers", () => ({ headers: async () => incoming }));
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input instanceof Request ? input.url : input);
      sent.push({ url, method: (init?.method ?? "GET").toUpperCase(), headers: new Headers(init?.headers) });
      if (url.endsWith("/v1/identity/me")) return Response.json({ csrf_token: "csrf_tok" });
      return Response.json({ abilities: [] });
    }));
  });
  afterEach(() => {
    vi.doUnmock("next/headers");
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    delete (globalThis as { __bossIdentityFetch?: true }).__bossIdentityFetch;
    installIdentityResolver(async () => null);
  });

  async function getKit(headers: Record<string, string>) {
    incoming = new Headers({ host: "localhost:3000", cookie: "__Host-boss_session=bss_a; theme=dark", ...headers });
    await (await import("../instrumentation")).register();
    const { GET } = await import("../app/api/kit/route");
    await GET(new Request("http://localhost:3000/api/kit?project_id=p1"));
    const twin = sent.find((s) => s.url.includes("/v1/kit/load"));
    expect(twin, "the proxy called the writing GET's POST twin").toBeTruthy();
    return twin!;
  }

  it("the page's own GET /api/kit reaches POST /v1/kit/load with CSRF, Origin and the cookie", async () => {
    const twin = await getKit({ "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors" });
    expect(twin.method).toBe("POST");
    expect(twin.headers.get("x-boss-csrf")).toBe("csrf_tok");
    expect(twin.headers.get("origin")).toBe("http://localhost:3000");
    expect(twin.headers.get("sec-fetch-site")).toBe("same-origin");
    expect(twin.headers.get("cookie")).toBe("__Host-boss_session=bss_a");
  });

  it("a cross-site GET to the same proxy gets no Origin, so the API still refuses it", async () => {
    const twin = await getKit({ "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate" });
    expect(twin.headers.get("origin")).toBeNull();
  });
});

describe("every route handler GET that reaches a twin writes it through apiWrite", () => {
  function routes(dir: string): string[] {
    const out: string[] = [];
    for (const name of readdirSync(join(WEB, dir))) {
      const p = join(dir, name);
      if (statSync(join(WEB, p)).isDirectory()) out.push(...routes(p));
      else if (/^route\.tsx?$/.test(name)) out.push(p);
    }
    return out;
  }
  const twins = routes("app/api").filter((f) => /\bloadPath\(/.test(readFileSync(join(WEB, f), "utf8")));

  it("finds the proxies (kit, mcp, run-acts …)", () => {
    expect(twins).toEqual(expect.arrayContaining(["app/api/kit/route.ts", "app/api/mcp/route.ts", "app/api/run-acts/route.ts"]));
  });
  it.each(twins)("%s sends its twin with apiWrite", (file) => {
    expect(readFileSync(join(WEB, file), "utf8")).toMatch(/\bapiWrite\(/);
  });
  it("instrumentation.ts takes a write's origin from forwardedWriteOrigin, not the raw header", () => {
    const src = readFileSync(join(WEB, "instrumentation.ts"), "utf8");
    expect(src).toMatch(/const origin = forward\.forwardedWriteOrigin\(incoming\)/);
    expect(src).not.toMatch(/incoming\.get\("origin"\)/);
  });
});
