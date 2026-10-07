/**
 * SECURITY FINDING #4 (HIGH): `/v1/previews/<id>/%77eb/` kept the console's preview matcher from
 * matching, so the console proxied it WITH the operator's cookie and dropped the sandbox CSP; the
 * previewed app then read the CSRF token from /v1/identity/me and wrote as the operator.
 *
 * Every spelling a server could route as a preview — percent-escapes (once or twice, either case),
 * an escaped slash, dot segments, doubled slashes, backslashes — is judged on the normalised path,
 * sent to the preview origin, and nothing is forwarded upstream for it. And the API's CSP is
 * relayed on EVERY proxied answer, so no spelling can lose a sandbox the API applied.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { installIdentityResolver, isPreviewCapabilityPath, normalisePath, previewOrigin } from "../lib/identity/forward";

const COOKIES = "__Host-boss_session=sess_secret; __Host-boss_device=dev";
let seen: { url: string; headers: Headers }[] = [];

function upstream(headers: Record<string, string> = {}) {
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    seen.push({ url: String(url), headers: new Headers(init?.headers) });
    return new Response("<script>steal()</script>", { status: 200, headers: { "content-type": "text/html", ...headers } });
  }));
}

async function route() {
  vi.resetModules();
  delete (globalThis as { __bossIdentityFetch?: true }).__bossIdentityFetch;
  installIdentityResolver(async (write) => ({
    cookie: "__Host-boss_session=sess_secret", csrf: write ? "csrf_secret" : null, origin: "http://boss.example.test",
  }));
  return import("../app/v1/[...path]/route");
}

const ctx = (path: string) => ({ params: Promise.resolve({ path: path.replace(/^\/+v1\/+/, "").split("/") }) });

const SPELLINGS = [
  "/v1/previews/prv_1/%77eb/",          // the reported bypass
  "/v1/previews/prv_1/%77%65%62/",
  "/v1/previews/prv_1/%57%45%42/",       // uppercase escapes (and an uppercase name)
  "/v1/previews/prv_1/%2577eb/",        // double-encoded
  "/v1/previews%2fprv_1%2fweb/",         // escaped slashes, lowercase
  "/v1/previews%2Fprv_1%2Fweb/x",        // escaped slashes, uppercase
  "/v1/identity/..%2f..%2fv1/previews/prv_1/web/",
  "/v1/identity/%2e%2e/previews/prv_1/web/",
  "//v1//previews//prv_1//web/",
  "/v1/previews/prv_1/web%5c..%5cweb/",
  "/v1/previews/prv_1/feedback%2f",
];

describe("a preview path in any spelling never runs on the console's origin", () => {
  beforeAll(() => {
    vi.stubEnv("BOSS_API_URL", "http://api.example.test");
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
  });
  afterAll(() => { vi.unstubAllEnvs(); installIdentityResolver(async () => null); });
  beforeEach(() => { seen = []; vi.unstubAllGlobals(); vi.stubEnv("BOSS_PREVIEW_ORIGIN", ""); vi.stubEnv("BOSS_PREVIEW_PORT", ""); });

  it("normalises before it matches", () => {
    expect(normalisePath("/v1/previews/prv_1/%77eb/")).toBe("/v1/previews/prv_1/web/");
    expect(normalisePath("/v1/previews/prv_1/%2577eb")).toBe("/v1/previews/prv_1/web");
    expect(normalisePath("/v1/a/..%2F..%2F..%2Fv1/previews/p/web")).toBe("/v1/previews/p/web");
    expect(normalisePath("//v1//previews/./p/web/")).toBe("/v1/previews/p/web/");
    expect(normalisePath("/v1/previews/p/web/%E0%A4%A")).toMatch(/^\/v1\/previews\/p\/web\//);   // malformed: still judged
    for (const spelling of SPELLINGS) expect(isPreviewCapabilityPath(spelling), spelling).toBe(true);
    for (const other of ["/v1/identity/me", "/v1/previews/prv_1/stop", "/v1/previews/prv_1/%73top", "/v1/previews"]) {
      expect(isPreviewCapabilityPath(other), other).toBe(false);
    }
  });

  for (const spelling of SPELLINGS) {
    it(`${spelling}: no cookie, CSRF or marker forwarded; sent to the preview origin`, async () => {
      upstream();   // an upstream that would answer WITHOUT a sandbox, as the bypass got
      const { GET, POST } = await route();
      for (const [method, handler] of [["GET", GET], ["POST", POST]] as const) {
        const res = await handler(new Request(`http://boss.example.test${spelling}`, {
          method, body: method === "POST" ? "{}" : undefined,
          headers: { cookie: COOKIES, "x-boss-csrf": "csrf_secret", host: "boss.example.test", "content-type": "application/json" },
        }), ctx(spelling));
        expect(seen, `${method} ${spelling} reached the API`).toHaveLength(0);
        expect(res.status).toBe(307);
        expect(res.headers.get("location") ?? "").toMatch(/^http:\/\/boss\.example\.test:3001\/v1\/previews\/prv_1\/(?:web|WEB|feedback)/);
        expect(res.headers.get("set-cookie")).toBeNull();
        expect(await res.text()).toBe("");
      }
    });
  }

  it("relays the API's CSP on every proxied answer, not only on paths it recognises", async () => {
    upstream({ "content-security-policy": "sandbox allow-scripts" });
    const { GET } = await route();
    const path = "/v1/factory/flow";
    const res = await GET(new Request(`http://boss.example.test${path}`, { headers: { cookie: COOKIES } }), ctx(path));
    expect(seen).toHaveLength(1);
    expect(res.headers.get("content-security-policy")).toBe("sandbox allow-scripts");
  });

  it("the preview origin is BOSS_PREVIEW_ORIGIN, else the console's host on BOSS_PREVIEW_PORT", () => {
    expect(previewOrigin("localhost:3000", "http:")).toBe("http://localhost:3001");
    expect(previewOrigin("[::1]:3000", "http")).toBe("http://[::1]:3001");
    expect(previewOrigin("evil.com/x@y", "http")).toBeNull();
    vi.stubEnv("BOSS_PREVIEW_PORT", "4100");
    expect(previewOrigin("boss.local", "https:")).toBe("https://boss.local:4100");
    vi.stubEnv("BOSS_PREVIEW_ORIGIN", "https://previews.example.com/ignored");
    expect(previewOrigin("anything", "http")).toBe("https://previews.example.com");
  });

  it("with previews switched off, a preview path is refused, never proxied", async () => {
    vi.stubEnv("BOSS_PREVIEW_PORT", "off");
    upstream();
    const { GET } = await route();
    const path = "/v1/previews/prv_1/%77eb/";
    const res = await GET(new Request(`http://boss.example.test${path}`, { headers: { cookie: COOKIES, host: "boss.example.test" } }), ctx(path));
    expect(res.status).toBe(404);
    expect(seen).toHaveLength(0);
  });
});
