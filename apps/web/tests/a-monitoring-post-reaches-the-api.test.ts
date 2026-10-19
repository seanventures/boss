/**
 * External tools post to BOSS's public console URL (BOSSAPP-598): OTLP, remote write and alert
 * webhooks to /v1/ingest/**, and an email's ack link to /v1/alert-actions/<token>. They pass the
 * console with no session, the body goes upstream as the exact bytes sent, only the senders'
 * own credential headers are forwarded (never a cookie, never anything the console adds), and the
 * API's status and body come back. Any other spelling of those prefixes is refused.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { middleware, config as middlewareConfig } from "../middleware";
import { forwardMonitoring, monitoringPath, oddMonitoringSpelling, MONITORING_BODY_CAP } from "../lib/monitoring-proxy";

afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

type Seen = { url: string; method: string; headers: Headers; body: Uint8Array | null };
function upstream(status: number, body: string, type = "application/json") {
  const seen: Seen[] = [];
  const fetchImpl = vi.fn(async (url: string, init: RequestInit) => {
    const b = init.body ? new Uint8Array(init.body as Uint8Array) : null;
    seen.push({ url, method: String(init.method), headers: new Headers(init.headers), body: b });
    return new Response(body, { status, headers: { "content-type": type, "set-cookie": "x=1", location: "http://api.internal/" } });
  });
  return { seen, fetchImpl };
}

// gzip magic + snappy-ish bytes: anything a decoder would mangle.
const RAW = new Uint8Array([0x1f, 0x8b, 0x08, 0x00, 0xff, 0x00, 0x80, 0x7f, 0xc3, 0x28, 0x0a, 0x0d]);

describe("monitoring's machine routes", () => {
  it("forwards an OTLP post byte for byte, with its credential headers and nothing of the console's", async () => {
    const api = upstream(202, '{"accepted":12}');
    const req = new Request("http://boss.example.test/v1/ingest/otlp/v1/metrics?x=1", {
      method: "POST", body: RAW,
      headers: {
        authorization: "Bearer src-token", "content-type": "application/x-protobuf", "content-encoding": "gzip",
        "x-boss-timestamp": "1760000000", "x-boss-signature": "sha256=ab", "x-boss-token": "t",
        "sentry-hook-resource": "issue", "x-prometheus-remote-write-version": "0.1.0",
        cookie: "__Host-boss_session=s", "x-boss-csrf": "c", "x-forwarded-user": "mallory", origin: "https://evil.example",
      },
    });
    const res = await forwardMonitoring(req, { apiUrl: "http://api.internal", fetchImpl: api.fetchImpl });
    expect(res.status).toBe(202);
    expect(await res.text()).toBe('{"accepted":12}');
    expect(res.headers.get("set-cookie")).toBeNull();
    expect(res.headers.get("location")).toBeNull();
    const s = api.seen[0];
    expect(s.url).toBe("http://api.internal/v1/ingest/otlp/v1/metrics?x=1");
    expect(Array.from(s.body!)).toEqual(Array.from(RAW));
    for (const h of ["authorization", "content-type", "content-encoding", "x-boss-timestamp", "x-boss-signature", "x-boss-token",
                     "sentry-hook-resource", "x-prometheus-remote-write-version"]) {
      expect(s.headers.get(h), h).not.toBeNull();
    }
    for (const h of ["cookie", "x-boss-csrf", "x-forwarded-user", "origin", "x-boss-console-auth", "x-boss-speaks-for-nobody"]) {
      expect(s.headers.get(h), h).toBeNull();
    }
  });

  it("an email's ack link: a GET with no session gets the API's HTML and status", async () => {
    const api = upstream(200, "<html><body>Acknowledged</body></html>", "text/html; charset=utf-8");
    const req = new Request("http://boss.example.test/v1/alert-actions/tok_abc123", { method: "GET" });
    const res = await forwardMonitoring(req, { apiUrl: "http://api.internal", fetchImpl: api.fetchImpl });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("text/html");
    expect(await res.text()).toContain("Acknowledged");
    expect(api.seen[0].method).toBe("GET");
  });

  it("relays the API's refusal as is, and stops a body over 4 MB without calling it", async () => {
    const api = upstream(401, '{"detail":"That token is not this source\'s"}');
    const res = await forwardMonitoring(new Request("http://x/v1/ingest/src_1/alertmanager", { method: "POST", body: "{}" }),
                                        { apiUrl: "http://api.internal", fetchImpl: api.fetchImpl });
    expect(res.status).toBe(401);
    const big = await forwardMonitoring(new Request("http://x/v1/ingest/prom/remote_write", {
      method: "POST", body: new Uint8Array(MONITORING_BODY_CAP + 1),
    }), { apiUrl: "http://api.internal", fetchImpl: api.fetchImpl });
    expect(big.status).toBe(413);
    expect(api.seen).toHaveLength(1);
    // Ingest is POST only.
    expect((await forwardMonitoring(new Request("http://x/v1/ingest/otlp/v1/metrics"), { apiUrl: "http://a", fetchImpl: api.fetchImpl })).status).toBe(405);
  });

  it("only the plain spellings are machine routes; the others are refused, never proxied as a person", () => {
    for (const p of ["/v1/ingest/otlp/v1/metrics", "/v1/ingest/prom/remote_write", "/v1/ingest/src_1/sentry", "/v1/alert-actions/tok-1"]) {
      expect(monitoringPath(p), p).not.toBeNull();
      expect(oddMonitoringSpelling(p), p).toBe(false);
    }
    for (const p of ["/v1/%69ngest/otlp", "/v1/ingest//otlp", "/v1/ingest/../decisions/d1/approve", "/v1/ingest/%2e%2e/x",
                     "/v1/alert-actions/a/b", "/v1/Ingest/otlp"]) {
      expect(monitoringPath(p), p).toBeNull();
      expect(oddMonitoringSpelling(p), p).toBe(true);
    }
    expect(oddMonitoringSpelling("/v1/ingestion")).toBe(false);
  });

  it("middleware skips the plain spellings (no body tee) and refuses the others", async () => {
    const re = new RegExp(`^${middlewareConfig.matcher[0]}$`);
    for (const p of ["/v1/ingest/otlp/v1/metrics", "/v1/alert-actions/tok_1"]) expect(re.test(p), p).toBe(false);
    for (const p of ["/v1/ingest/%2e%2e/x", "/v1/ingest", "/v1/alert-actions/a/b"]) expect(re.test(p), p).toBe(true);
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "");
    const odd = await middleware(new NextRequest("http://10.0.0.5:3000/v1/%69ngest/otlp", { method: "POST" }));
    expect(odd.status).toBe(404);
  });

  it("the /v1 route hands them to the monitoring forward before anything else", async () => {
    vi.stubEnv("BOSS_API_URL", "http://api.example.test");
    const calls: { url: string; headers: Headers }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (u: unknown, init?: RequestInit) => {
      calls.push({ url: String(u), headers: new Headers(init?.headers) });
      return new Response("{}", { status: 202 });
    }));
    const { POST } = await import("../app/v1/[...path]/route");
    const res = await POST(new Request("http://boss.example.test/v1/ingest/otlp/v1/metrics", {
      method: "POST", body: RAW, headers: { authorization: "Bearer k", cookie: "__Host-boss_session=s" },
    }), { params: Promise.resolve({ path: ["ingest", "otlp", "v1", "metrics"] }) });
    expect(res.status).toBe(202);
    expect(calls[0].url).toMatch(/\/v1\/ingest\/otlp\/v1\/metrics$/);
    expect(calls[0].headers.get("authorization")).toBe("Bearer k");
    expect(calls[0].headers.get("cookie")).toBeNull();
  });
});
