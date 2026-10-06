/**
 * Re-check item 3: the console's /v1 proxy buffered a collector POST whole (`req.arrayBuffer()`).
 * The collector routes are open to anyone, so a 150 MB chunked body cost this process 150 MB. Now
 * it is read chunk by chunk and refused (413) the moment it passes the cap — memory stays flat
 * and the rest of the stream is never pulled.
 */
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { COLLECTOR_BODY_CAP, readCapped } from "../lib/read-capped";

const CHUNK = 64 * 1024;

function hugeChunked(totalMb: number) {
  let pulled = 0;
  const chunk = new Uint8Array(CHUNK).fill(120);
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (pulled >= totalMb * 1024 * 1024) { controller.close(); return; }
      pulled += CHUNK;
      controller.enqueue(chunk);
    },
  });
  return { stream, pulled: () => pulled };
}

describe("a collector POST through the console", () => {
  beforeAll(() => { vi.stubEnv("BOSS_API_URL", "http://api.example.test"); });
  afterAll(() => vi.unstubAllEnvs());

  it("stops a 150 MB chunked body at the cap, with flat memory", async () => {
    const big = hugeChunked(150);
    const req = new Request("http://boss.example.test/v1/telemetry/ingest", {
      method: "POST", body: big.stream, duplex: "half",
    } as RequestInit);
    const rss = process.memoryUsage().rss;
    expect(await readCapped(req, COLLECTOR_BODY_CAP)).toBeNull();
    expect(big.pulled()).toBeLessThanOrEqual(COLLECTOR_BODY_CAP + 4 * CHUNK);
    expect(process.memoryUsage().rss - rss).toBeLessThan(40 * 1024 * 1024);
  });

  // Re-check 4, item 1: every spelling of the three routes is read under the cap, not only the
  // exact one — `/ingest/`, `delete/` and `share/?x` were buffered whole.
  for (const path of ["/v1/telemetry/ingest", "/v1/telemetry/ingest/", "/v1/telemetry/delete/",
                      "/v1/telemetry/share/?x", "/v1/telemetry/share?x"]) {
    it(`the route answers 413 at ${path}, closes the connection and never calls the API`, async () => {
      const calls: string[] = [];
      vi.stubGlobal("fetch", vi.fn(async (u: unknown) => { calls.push(String(u)); return new Response("{}"); }));
      const { POST } = await import("../app/v1/[...path]/route");
      const big = hugeChunked(150);
      const req = new Request(`http://boss.example.test${path}`, {
        method: "POST", body: big.stream, duplex: "half",
      } as RequestInit);
      const rss = process.memoryUsage().rss;
      const res = await POST(req, { params: Promise.resolve({ path: ["telemetry", "ingest"] }) });
      expect(res.status).toBe(413);
      expect(res.headers.get("connection")).toBe("close");
      expect(calls).toEqual([]);
      expect(big.pulled()).toBeLessThanOrEqual(COLLECTOR_BODY_CAP + 4 * CHUNK);
      expect(process.memoryUsage().rss - rss).toBeLessThan(40 * 1024 * 1024);
      vi.unstubAllGlobals();
    });
  }

  it("a refused browser POST stops the body: not one chunk past the first is pulled", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}")));
    const { POST } = await import("../app/v1/[...path]/route");
    const big = hugeChunked(150);
    const req = new Request("http://boss.example.test/v1/telemetry/share/?x", {
      method: "POST", body: big.stream, duplex: "half", headers: { origin: "https://evil.example.test" },
    } as RequestInit);
    const res = await POST(req, { params: Promise.resolve({ path: ["telemetry", "share"] }) });
    expect(res.status).toBe(403);
    expect(res.headers.get("connection")).toBe("close");
    await new Promise((r) => setTimeout(r, 20));
    expect(big.pulled()).toBeLessThanOrEqual(2 * CHUNK);
    vi.unstubAllGlobals();
  });

  it("a declared Content-Length over the cap is refused unread, a small body passes whole", async () => {
    const declared = new Request("http://x/v1/telemetry/ingest", {
      method: "POST", body: "x", headers: { "content-length": String(10 * 1024 * 1024) },
    });
    expect(await readCapped(declared, COLLECTOR_BODY_CAP)).toBeNull();
    const small = new Request("http://x/v1/telemetry/ingest", { method: "POST", body: "hello" });
    expect(new TextDecoder().decode((await readCapped(small, COLLECTOR_BODY_CAP))!)).toBe("hello");
  });
});
