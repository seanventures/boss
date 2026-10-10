import { describe, expect, it, vi } from "vitest";
import { forwardProtocol, PROTOCOL_BODY_CAP } from "../lib/identity/protocol-proxy";

const CHUNK = 64 * 1024;

function incoming(path: string, method: string, size: number, declared?: string, cancellationError = false) {
  let pulled = 0, cancelled = false;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (pulled === size) { controller.close(); return; }
      const bytes = Math.min(CHUNK, size - pulled);
      pulled += bytes;
      controller.enqueue(new Uint8Array(bytes).fill(120));
    },
    cancel() { cancelled = true; if (cancellationError) throw new Error("cancel refused"); },
  });
  const req = new Request(`http://console.test${path}`, { method, body: stream, duplex: "half",
    headers: { "content-type": path.startsWith("/saml") ? "application/x-www-form-urlencoded" : "application/scim+json",
      ...(declared === undefined ? {} : { "content-length": declared }) },
  } as RequestInit);
  const wholeRead = vi.spyOn(req, "arrayBuffer");
  return { req, wholeRead, pulled: () => pulled, cancelled: () => cancelled };
}

describe("public protocol bodies are read under their actual byte cap", () => {
  for (const [path, method] of [["/saml/v2/acs", "POST"], ["/scim/v2/Users", "POST"],
    ["/scim/v2/Users/user_1", "PUT"], ["/scim/v2/Groups/group_1", "PATCH"]]) {
    for (const declared of [undefined, "1"]) {
      it(`${method} ${path}, ${declared === undefined ? "undeclared" : "understated"} length: cancels early and never calls upstream`, async () => {
        const body = incoming(path, method, 3 * PROTOCOL_BODY_CAP, declared);
        const upstream = vi.fn(async () => new Response("{}"));
        const response = await forwardProtocol(body.req, { apiUrl: "http://api.test", fetchImpl: upstream });
        expect(response.status).toBe(413);
        expect(await response.json()).toEqual({ detail: "too_large" });
        expect(response.headers.get("cache-control")).toBe("no-store");
        expect(body.cancelled()).toBe(true);
        expect(body.pulled()).toBeLessThanOrEqual(PROTOCOL_BODY_CAP + 2 * CHUNK);
        expect(body.wholeRead).not.toHaveBeenCalled();
        expect(upstream).not.toHaveBeenCalled();
      });
    }
  }

  it("a declared oversize body is refused without reading any of its body", async () => {
    const body = incoming("/saml/v2/acs", "POST", 3 * PROTOCOL_BODY_CAP, String(PROTOCOL_BODY_CAP + 1));
    const read = vi.spyOn(body.req.body!, "getReader");
    const upstream = vi.fn(async () => new Response("{}"));
    expect((await forwardProtocol(body.req, { apiUrl: "http://api.test", fetchImpl: upstream })).status).toBe(413);
    expect(read).not.toHaveBeenCalled();
    expect(body.wholeRead).not.toHaveBeenCalled();
    expect(upstream).not.toHaveBeenCalled();
  });

  it("an exact-limit chunked body reaches upstream byte-for-byte without a whole-body reader", async () => {
    const body = incoming("/scim/v2/Users/user_1", "PATCH", PROTOCOL_BODY_CAP);
    body.req.headers.set("authorization", "Bearer scim-test-fixture");
    body.req.headers.set("cookie", "__Host-boss_session=never_forward");
    const upstream = vi.fn(async (_url: string, init: RequestInit) => {
      const sent = init.body as Uint8Array;
      expect(sent.byteLength).toBe(PROTOCOL_BODY_CAP);
      expect(sent.every((byte) => byte === 120)).toBe(true);
      expect(new Headers(init.headers).get("authorization")).toBe("Bearer scim-test-fixture");
      expect(new Headers(init.headers).has("cookie")).toBe(false);
      return new Response("{}", { status: 200 });
    });
    expect((await forwardProtocol(body.req, { apiUrl: "http://api.test", fetchImpl: upstream })).status).toBe(200);
    expect(upstream).toHaveBeenCalledOnce();
    expect(body.cancelled()).toBe(false);
    expect(body.wholeRead).not.toHaveBeenCalled();
  });

  it("a rejected cancellation still refuses the oversize request without an upstream call", async () => {
    const body = incoming("/saml/v2/acs", "POST", 3 * PROTOCOL_BODY_CAP, undefined, true);
    const upstream = vi.fn(async () => new Response("{}"));
    expect((await forwardProtocol(body.req, { apiUrl: "http://api.test", fetchImpl: upstream })).status).toBe(413);
    expect(body.cancelled()).toBe(true);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("an upstream response stays streamed and is not buffered by this request cap", async () => {
    let responseFinished = false;
    const responseStream = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new TextEncoder().encode("first")); },
      cancel() { responseFinished = true; },
    });
    const source = new Response(responseStream, { status: 200, headers: { "content-type": "application/xml" } });
    const wholeRead = vi.spyOn(source, "arrayBuffer");
    const forwarded = await forwardProtocol(new Request("http://console.test/saml/v2/metadata"), {
      apiUrl: "http://api.test", fetchImpl: async () => source,
    });
    expect(forwarded.headers.get("content-type")).toBe("application/xml");
    const reader = forwarded.body!.getReader();
    expect(new TextDecoder().decode((await reader.read()).value)).toBe("first");
    expect(wholeRead).not.toHaveBeenCalled();
    expect(responseFinished).toBe(false);
    await reader.cancel();
  });
});
