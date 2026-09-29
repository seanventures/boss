import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/client", () => ({
  API_URL: "http://boss-api",
  authHeaders: () => ({ Authorization: "Bearer test-key" }),
}));

import { GET } from "../app/api/run-deployments/route";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function request(ids: string[], names: string[] = []): Request {
  const url = new URL("http://console/api/run-deployments");
  url.searchParams.set("run_ids", ids.join(","));
  names.forEach((name) => url.searchParams.append("app_name", name));
  return new Request(url);
}

describe("deployment lookup batch route", () => {
  it("limits in-flight API reads and keeps each app name paired with its run", async () => {
    const pending: Array<() => void> = [];
    const seen: string[] = [];
    let active = 0;
    let peak = 0;
    vi.stubGlobal("fetch", vi.fn((input: string, options: RequestInit) => {
      expect(options.headers).toEqual({ Authorization: "Bearer test-key" });
      expect(options.cache).toBe("no-store");
      active += 1;
      peak = Math.max(peak, active);
      seen.push(input);
      return new Promise<Response>((resolve) => {
        pending.push(() => {
          active -= 1;
          resolve(Response.json({ deployments: [], linked_host: input }));
        });
      });
    }));

    const ids = Array.from({ length: 8 }, (_, i) => `run-${i}`);
    const names = ids.map((_, i) => `App ${i}`);
    const reply = GET(request(ids, names));
    expect(seen).toHaveLength(4);
    pending.splice(0, 4).forEach((resolve) => resolve());
    await vi.waitFor(() => expect(seen).toHaveLength(8));
    pending.splice(0).forEach((resolve) => resolve());
    const response = await reply;
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(peak).toBe(4);
    expect(Object.keys(body.runs)).toEqual(ids);
    for (let i = 0; i < ids.length; i += 1) {
      expect(seen[i]).toBe(`http://boss-api/v1/code-tasks/${ids[i]}/deployments?app_name=App%20${i}`);
      expect(body.runs[ids[i]].linked_host).toBe(seen[i]);
    }
  });

  it("returns a healthy run alongside a timed-out run and aborts that fetch", async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    vi.stubGlobal("fetch", vi.fn((input: string, options: RequestInit) => {
      if (input.includes("/slow/")) {
        signals.push(options.signal as AbortSignal);
        return new Promise<Response>(() => {});
      }
      return Promise.resolve(Response.json({ deployments: [{ id: "deployed" }] }));
    }));

    const reply = GET(request(["slow", "fast"]));
    await vi.advanceTimersByTimeAsync(3_000);
    const response = await reply;
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.runs.fast.deployments).toEqual([{ id: "deployed" }]);
    expect(body.runs.slow).toEqual({ error: "deployment lookup timed out" });
    expect(signals).toHaveLength(1);
    expect(signals[0].aborted).toBe(true);
  });

  it("returns explicit errors for queued runs at the outer deadline", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(() => new Promise<Response>(() => {}));
    vi.stubGlobal("fetch", fetchMock);
    const ids = Array.from({ length: 60 }, (_, i) => `run-${i}`);

    const reply = GET(request(ids));
    await vi.advanceTimersByTimeAsync(12_000);
    const response = await reply;
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(Object.keys(body.runs)).toHaveLength(60);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(4);
    expect(fetchMock.mock.calls.length).toBeLessThan(60);
    expect(body.runs["run-59"]).toEqual({ error: "deployment lookup budget exhausted" });
    expect(body.runs["run-0"]).toEqual({ error: "deployment lookup timed out" });
  });

  it("keeps per-run upstream failures visible without failing other cards", async () => {
    vi.stubGlobal("fetch", vi.fn((input: string) => Promise.resolve(
      input.includes("/denied/")
        ? new Response("denied", { status: 403 })
        : Response.json({ deployments: [] }),
    )));
    const response = await GET(request(["denied", "ok"]));
    const body = await response.json();
    expect(body.runs.denied).toEqual({ error: "HTTP 403" });
    expect(body.runs.ok).toEqual({ deployments: [] });
  });

  it("keeps a comma inside one app name paired with its run", async () => {
    const seen: string[] = [];
    vi.stubGlobal("fetch", vi.fn((input: string) => {
      seen.push(input);
      return Promise.resolve(Response.json({ deployments: [] }));
    }));
    const response = await GET(request(["first", "second"], ["Costs, Q3", "Roadmap"]));
    expect(response.status).toBe(200);
    expect(seen).toEqual([
      "http://boss-api/v1/code-tasks/first/deployments?app_name=Costs%2C%20Q3",
      "http://boss-api/v1/code-tasks/second/deployments?app_name=Roadmap",
    ]);
  });

  it("rejects incomplete positional app names", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await GET(request(["first", "second"], ["Only one"]));
    expect(response.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("preserves a comma in a legacy single-run name and refuses ambiguous legacy batches", async () => {
    const seen: string[] = [];
    vi.stubGlobal("fetch", vi.fn((input: string) => {
      seen.push(input);
      return Promise.resolve(Response.json({ deployments: [] }));
    }));
    const single = await GET(new Request(
      "http://console/api/run-deployments?run_ids=first&app_names=Costs%2C%20Q3",
    ));
    expect(single.status).toBe(200);
    expect(seen).toEqual([
      "http://boss-api/v1/code-tasks/first/deployments?app_name=Costs%2C%20Q3",
    ]);

    const ambiguous = await GET(new Request(
      "http://console/api/run-deployments?run_ids=first,second&app_names=Costs%2C%20Q3",
    ));
    expect(ambiguous.status).toBe(400);
    expect(seen).toHaveLength(1);
  });

  it("rejects an oversized batch before issuing any API reads", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await GET(request(Array.from({ length: 61 }, (_, i) => `run-${i}`)));
    expect(response.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
