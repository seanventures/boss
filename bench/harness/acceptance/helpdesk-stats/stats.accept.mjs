// Held by the benchmark, never shown to the agent: run on the pull request's head.
import { after, before, beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { CATEGORIES } from "../src/classify.js";
import { resetStore } from "../src/store.js";
import { startService } from "./_http.mjs";

let svc;
before(async () => { svc = await startService(); });
after(async () => svc.close());
beforeEach(() => resetStore());

function zeros() {
  return Object.fromEntries(CATEGORIES.map((c) => [c, 0]));
}

test("an empty desk answers zeros for every status and every category", async () => {
  const res = await svc.call("GET", "/stats");
  assert.equal(res.status, 200);
  assert.deepEqual(res.body, { total: 0, by_status: { open: 0, closed: 0 }, by_category: zeros() });
});

test("the counts follow the tickets and their status", async () => {
  await svc.call("POST", "/tickets", { title: "refund the charge" });
  await svc.call("POST", "/tickets", { title: "invoice twice" });
  const crash = await svc.call("POST", "/tickets", { title: "app crash on start" });
  await svc.call("POST", "/tickets", { title: "hello" });
  await svc.call("PATCH", `/tickets/${crash.body.id}`, { status: "closed" });
  const res = await svc.call("GET", "/stats");
  const expected = zeros();
  expected.Billing = 2;
  expected["Technical issue"] = 1;
  expected.Other = 1;
  assert.deepEqual(res.body, { total: 4, by_status: { open: 3, closed: 1 }, by_category: expected });
});

test("the existing routes still answer", async () => {
  assert.deepEqual((await svc.call("GET", "/health")).body, { ok: true });
  assert.equal((await svc.call("GET", "/tickets/99")).status, 404);
});
