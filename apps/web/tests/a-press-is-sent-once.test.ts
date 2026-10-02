import { afterEach, describe, expect, it, vi } from "vitest";

import {
  IDEMPOTENCY_HEADER, WriteInFlightError, apiWrite, deriveKey, installServerIntentResolver,
  intentFromActionBody, writeAwareFetch, writeInFlight,
} from "../lib/api-write";

/**
 * What lib/api-write.ts promises, on both sides of the wire.
 *
 * 2026-10-02: "Stop and run again" pressed twice started two workflows, because `disabled` lands
 * only after React re-renders and nothing tied the second request to the first. Three earlier
 * duplicates (a run started twice on 08-22, "Open preview happened twice" on 08-30, duplicate
 * chat actions on 09-10) had the same cause on other buttons. These pin the helper every console
 * write now goes through: one key per press, a double click sends nothing new, a different action
 * mid-flight is refused, and on the server the key is derived from the press that caused it.
 */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function deferredFetch() {
  let release: (r: Response) => void = () => {};
  const fetchMock = vi.fn((_url: unknown, _init?: RequestInit) => new Promise<Response>((resolve) => { release = resolve; }));
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, release: (body: unknown) => release(Response.json(body)) };
}

function keyOf(call: unknown[]): string | null {
  return new Headers((call[1] as RequestInit).headers).get(IDEMPOTENCY_HEADER);
}

afterEach(() => {
  vi.unstubAllGlobals();
  installServerIntentResolver(async () => undefined);
});

describe("in the browser, a press", () => {
  const browser = () => vi.stubGlobal("window", {});
  const start = { method: "POST" as const, headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ action: "start", workflow_key: "w" }) };

  it("carries a fresh Idempotency-Key, in the caller's own header shape", async () => {
    browser();
    const { fetchMock, release } = deferredFetch();
    const sent = apiWrite("/api/delivery-workflows", start);
    release({ ok: true });
    await sent;
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.headers).toMatchObject({ "Content-Type": "application/json" });
    expect(keyOf(fetchMock.mock.calls[0])).toMatch(UUID);
  });

  it("pressed twice before the first returns, sends ONE request and both get its answer", async () => {
    browser();
    const { fetchMock, release } = deferredFetch();
    const first = apiWrite("/api/delivery-workflows", start);
    const second = apiWrite("/api/delivery-workflows", start);
    release({ run: { id: "wfr_1" } });
    const [a, b] = await Promise.all([first, second]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(await a.json()).toEqual({ run: { id: "wfr_1" } });
    expect(await b.json()).toEqual({ run: { id: "wfr_1" } });
  });

  it("refuses a different action in the same scope while one is in flight", async () => {
    browser();
    const { fetchMock, release } = deferredFetch();
    const stop = apiWrite("/api/x", { method: "POST", body: "stop" }, { action: "panel" });
    expect(writeInFlight("panel")).toBe(true);
    await expect(apiWrite("/api/x", { method: "POST", body: "start" }, { action: "panel" }))
      .rejects.toBeInstanceOf(WriteInFlightError);
    release({});
    await stop;
    expect(writeInFlight("panel")).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("is a new press once the last one finished: a new key, a new request", async () => {
    browser();
    const fetchMock = vi.fn(async (_url: unknown, _init?: RequestInit) => Response.json({}));
    vi.stubGlobal("fetch", fetchMock);
    await apiWrite("/api/x", start);
    await apiWrite("/api/x", start);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(keyOf(fetchMock.mock.calls[0])).not.toEqual(keyOf(fetchMock.mock.calls[1]));
  });

  it("frees the scope when the request fails, so the press can be tried again", async () => {
    browser();
    vi.stubGlobal("fetch", vi.fn(async (_url: unknown, _init?: RequestInit) => { throw new TypeError("network down"); }));
    await expect(apiWrite("/api/x", start, { action: "p" })).rejects.toThrow("network down");
    expect(writeInFlight("p")).toBe(false);
  });
});

describe("on the server, the key comes from the press that caused the call", () => {
  it("adds nothing when there is no press to derive from — exactly the old behaviour", async () => {
    const fetchMock = vi.fn(async (_url: unknown, _init?: RequestInit) => Response.json({}));
    vi.stubGlobal("fetch", fetchMock);
    await apiWrite("http://api/v1/x", { method: "POST", body: "{}" });
    expect(keyOf(fetchMock.mock.calls[0])).toBeNull();
  });

  it("derives one stable key per distinct call, so a repeated press repeats the same keys", async () => {
    const fetchMock = vi.fn(async (_url: unknown, _init?: RequestInit) => Response.json({}));
    vi.stubGlobal("fetch", fetchMock);
    installServerIntentResolver(async () => "8f1c2d4e-1111-4222-8333-944445555666");
    await apiWrite("http://api/v1/stop", { method: "POST", body: '{"run":"r"}' });
    await apiWrite("http://api/v1/start", { method: "POST", body: '{"run":"r"}' });
    await apiWrite("http://api/v1/stop", { method: "POST", body: '{"run":"r"}' });
    const [stop, start, stopAgain] = fetchMock.mock.calls.map(keyOf);
    expect(stop).toMatch(/^8f1c2d4e-1111-4222-8333-944445555666\.[0-9a-f]{32}$/);
    expect(stopAgain).toEqual(stop);
    expect(start).not.toEqual(stop);
  });

  it("does not depend on which host the API is reached at", async () => {
    expect(await deriveKey("k", "POST", "http://a:9000/v1/x?p=1", "b"))
      .toEqual(await deriveKey("k", "post", "http://b/v1/x?p=1", "b"));
  });

  it("keeps a key the caller already set", async () => {
    const fetchMock = vi.fn(async (_url: unknown, _init?: RequestInit) => Response.json({}));
    vi.stubGlobal("fetch", fetchMock);
    installServerIntentResolver(async () => "8f1c2d4e-1111-4222-8333-944445555666");
    await apiWrite("http://api/v1/x", { method: "POST", headers: { [IDEMPOTENCY_HEADER]: "mine" } });
    expect(keyOf(fetchMock.mock.calls[0])).toBe("mine");
  });

  it("the typed client's transport keys writes and leaves reads alone", async () => {
    const fetchMock = vi.fn(async (_url: unknown, _init?: RequestInit) => Response.json({}));
    vi.stubGlobal("fetch", fetchMock);
    installServerIntentResolver(async () => "8f1c2d4e-1111-4222-8333-944445555666");
    await writeAwareFetch("http://api/v1/x", { method: "GET" });
    await writeAwareFetch("http://api/v1/x", { method: "DELETE" });
    expect(keyOf(fetchMock.mock.calls[0])).toBeNull();
    expect(keyOf(fetchMock.mock.calls[1])).toMatch(/^8f1c2d4e/);
  });
});

describe("a server action's press is found in its form body", () => {
  const key = "0b8f6a52-3c1d-4e2f-9a7b-112233445566";

  it("reads the SubmitButton field, even under React's field-name prefix", () => {
    const multipart = `------x\r\nContent-Disposition: form-data; name="1___boss_intent"\r\n\r\n${key}\r\n------x--`;
    expect(intentFromActionBody(multipart)).toBe(key);
    expect(intentFromActionBody(`[{"__boss_intent":"${key}"}]`)).toBe(key);
  });

  it("finds nothing in a form without one", () => {
    expect(intentFromActionBody('------x\r\nname="run_id"\r\n\r\nwfr_1')).toBeNull();
  });
});
