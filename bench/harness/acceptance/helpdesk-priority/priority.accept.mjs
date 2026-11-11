// Held by the benchmark, never shown to the agent: run on the pull request's head.
import { after, before, beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { resetStore } from "../src/store.js";
import { startService } from "./_http.mjs";

let svc;
before(async () => { svc = await startService(); });
after(async () => svc.close());
beforeEach(() => resetStore());

test("priority defaults to normal and is kept when given", async () => {
  const plain = await svc.call("POST", "/tickets", { title: "plain" });
  assert.equal(plain.status, 201);
  assert.equal(plain.body.priority, "normal");
  for (const priority of ["low", "normal", "high"]) {
    const res = await svc.call("POST", "/tickets", { title: `p ${priority}`, priority });
    assert.equal(res.status, 201);
    assert.equal(res.body.priority, priority);
    assert.equal((await svc.call("GET", `/tickets/${res.body.id}`)).body.priority, priority);
  }
});

test("an unknown priority is a 400 and creates nothing", async () => {
  for (const priority of ["urgent", "HIGH", 3, ""]) {
    const res = await svc.call("POST", "/tickets", { title: "bad", priority });
    assert.deepEqual(res, { status: 400, body: { error: "invalid_priority" } }, String(priority));
  }
  assert.deepEqual((await svc.call("GET", "/tickets")).body.tickets, []);
});

test("the list filters by priority, newest first, and unfiltered lists everything", async () => {
  await svc.call("POST", "/tickets", { title: "h1", priority: "high" });
  await svc.call("POST", "/tickets", { title: "l1", priority: "low" });
  await svc.call("POST", "/tickets", { title: "h2", priority: "high" });
  const high = await svc.call("GET", "/tickets?priority=high");
  assert.equal(high.status, 200);
  assert.deepEqual(high.body.tickets.map((t) => t.title), ["h2", "h1"]);
  const low = await svc.call("GET", "/tickets?priority=low");
  assert.deepEqual(low.body.tickets.map((t) => t.title), ["l1"]);
  const all = await svc.call("GET", "/tickets");
  assert.equal(all.body.tickets.length, 3);
});
