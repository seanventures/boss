import { afterEach, describe, expect, it, vi } from "vitest";

import {
  IDEMPOTENCY_HEADER, TOAST_EVENT, WriteInFlightError, apiWrite, deriveKey,
  installServerIntentResolver, intentFromActionBody, pressInFlight, writeAwareFetch,
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

  it("refuses a different press in the same scope, and SAYS so naming what is running", async () => {
    const shown: string[] = [];
    vi.stubGlobal("window", {
      dispatchEvent: (e: CustomEvent<{ message: string }>) => {
        if (e.type === TOAST_EVENT) shown.push(e.detail.message);
        return true;
      },
    });
    const { fetchMock, release } = deferredFetch();
    const stop = apiWrite("/api/x", { method: "POST", body: "stop" },
                          { press: { scope: "panel", label: "Stop" } });
    expect(pressInFlight("panel")).toBe("Stop");
    const retry = apiWrite("/api/x", { method: "POST", body: "retry" },
                           { press: { scope: "panel", label: "Retry" } });
    await expect(retry).rejects.toBeInstanceOf(WriteInFlightError);
    await expect(retry).rejects.toThrow(
      "Retry was not sent — Stop is still in progress. Try again when it finishes.");
    // Said by the helper itself, so no button can swallow it.
    expect(shown).toEqual([
      "Retry was not sent — Stop is still in progress. Try again when it finishes."]);
    release({});
    await stop;
    expect(pressInFlight("panel")).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("a write that is not a press takes no scope: several at once all reach the API", async () => {
    // Review of 45e7693: the panel's automatic recheck shared the press scope, so of three
    // stopped runs only the first recheck was sent.
    browser();
    const fetchMock = vi.fn(async (_url: unknown, _init?: RequestInit) => Response.json({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    const results = await Promise.all(["a", "b", "c"].map((run) =>
      apiWrite("/api/delivery-workflows", { method: "POST", body: JSON.stringify({ action: "recheck", run }) })));
    expect(results.every((r) => r.ok)).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("a background write never blocks a press, and a press never blocks it", async () => {
    browser();
    const { fetchMock, release } = deferredFetch();
    const background = apiWrite("/api/x", { method: "POST", body: "recheck" });
    const press = apiWrite("/api/x", { method: "POST", body: "retry" },
                           { press: { scope: "panel", label: "Retry" } });
    const another = apiWrite("/api/x", { method: "POST", body: "recheck-2" });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    release({});
    void background; void press; void another;
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
    await expect(apiWrite("/api/x", start, { press: { scope: "p", label: "Start" } }))
      .rejects.toThrow("network down");
    expect(pressInFlight("p")).toBeNull();
  });
});

describe("a duplicate waits for the original's answer instead of showing an error", () => {
  /**
   * Review of 45e7693: a duplicate of a running press (a second tab, a resubmitted form) got
   * `409 idempotency_key_in_progress` and the raw detail reached the form as an error, though the
   * first press was working. A non-technical operator reads that as a failure and presses again.
   */
  it("asks again with the SAME key until the first request's answer is replayed", async () => {
    vi.useFakeTimers();
    try {
      const replies = [
        Response.json({ detail: "idempotency_key_in_progress: the first request with this key is still running" }, { status: 409 }),
        Response.json({ detail: "idempotency_key_in_progress: the first request with this key is still running" }, { status: 409 }),
        Response.json({ run: { id: "wfr_1" } }, { status: 200, headers: { "Idempotency-Replayed": "true" } }),
      ];
      const fetchMock = vi.fn(async (_url: unknown, _init?: RequestInit) => replies.shift()!);
      vi.stubGlobal("fetch", fetchMock);
      const sent = apiWrite("http://api/v1/delivery-workflows/runs", {
        method: "POST", headers: { [IDEMPOTENCY_HEADER]: "press-1" }, body: "{}" });
      await vi.runAllTimersAsync();
      const response = await sent;
      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({ run: { id: "wfr_1" } });
      expect(fetchMock).toHaveBeenCalledTimes(3);
      expect(fetchMock.mock.calls.map(keyOf)).toEqual(["press-1", "press-1", "press-1"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("passes any other 409 straight back — a conflict about the request is the caller's to show", async () => {
    const fetchMock = vi.fn(async (_url: unknown, _init?: RequestInit) =>
      Response.json({ detail: "a run is already live for this project" }, { status: 409 }));
    vi.stubGlobal("fetch", fetchMock);
    const response = await apiWrite("http://api/v1/x", {
      method: "POST", headers: { [IDEMPOTENCY_HEADER]: "k" }, body: "{}" });
    expect(response.status).toBe(409);
    expect(fetchMock).toHaveBeenCalledTimes(1);
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
