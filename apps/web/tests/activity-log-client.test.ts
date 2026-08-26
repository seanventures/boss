import { afterEach, beforeEach, expect, test } from "vitest";

import { fetchActivityLog } from "../lib/activity-log-client";

/**
 * The activity log was the single most expensive thing left on the console after the per-card
 * server-action storm was fixed: fifteen actions on load, fourteen under 300ms between them,
 * and this one at 25.3 seconds. About 14s was the query; the rest was the Server Action reply
 * dragging a full route re-render along with it.
 *
 * What these protect is the reply path and the honesty of a failure — not the data mapping,
 * which is tested where mapActivityLog lives.
 */

let calls: string[] = [];
let respond: () => { ok: boolean; status: number; body: unknown };
const realFetch = globalThis.fetch;

beforeEach(() => {
  calls = [];
  respond = () => ({ ok: true, status: 200, body: { items: [], total: 0 } });
  globalThis.fetch = (async (input: unknown) => {
    calls.push(String(input));
    const { ok, status, body } = respond();
    return { ok, status, json: async () => body } as Response;
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

test("goes to the route handler, not back through the page", async () => {
  // A POST to the page URL is a Server Action, and that is the 11 seconds this removes.
  await fetchActivityLog("boss-dogfood");
  expect(calls).toHaveLength(1);
  expect(calls[0]).toMatch(/^\/api\/activity\?/);
});

test("carries the operator's filters through", async () => {
  await fetchActivityLog("boss-dogfood", {
    q: "deploy",
    status: "failed",
    kind: "job",
    actor: "ceo",
    limit: 50,
  });
  const params = new URL(calls[0], "http://x").searchParams;
  expect(params.get("project_id")).toBe("boss-dogfood");
  expect(params.get("q")).toBe("deploy");
  expect(params.get("status")).toBe("failed");
  expect(params.get("kind")).toBe("job");
  expect(params.get("actor")).toBe("ceo");
  expect(params.get("limit")).toBe("50");
});

test("an unset filter is omitted rather than sent empty", async () => {
  // An empty `status=` would narrow a query the operator did not narrow.
  await fetchActivityLog("boss-dogfood", { q: "", status: undefined });
  const params = new URL(calls[0], "http://x").searchParams;
  expect(params.has("q")).toBe(false);
  expect(params.has("status")).toBe(false);
});

test("a failed request reports the failure instead of an empty log", async () => {
  // "Nothing happened on this project" and "we could not find out" look identical as an empty
  // list and mean opposite things.
  respond = () => ({ ok: false, status: 503, body: {} });
  const log = await fetchActivityLog("boss-dogfood");
  expect(log.items).toEqual([]);
  expect(String(log.blockedReason ?? log.error ?? "")).toMatch(/503/);
});

test("a network error is reported, not thrown", async () => {
  globalThis.fetch = (async () => {
    throw new Error("connection refused");
  }) as typeof fetch;
  const log = await fetchActivityLog("boss-dogfood");
  expect(String(log.blockedReason ?? log.error ?? "")).toMatch(/connection refused/);
});
