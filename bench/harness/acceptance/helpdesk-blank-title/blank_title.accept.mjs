// Held by the benchmark, never shown to the agent: run on the pull request's head.
import { after, before, beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { createTicket, resetStore } from "../src/store.js";
import { startService } from "./_http.mjs";

let svc;
before(async () => { svc = await startService(); });
after(async () => svc.close());
beforeEach(() => resetStore());

test("blank and whitespace titles are refused in the store", () => {
  for (const title of ["", "   ", "\t\n ", undefined, null]) {
    assert.throws(() => createTicket({ title }), /title_required/, JSON.stringify(title));
  }
});

test("a whitespace title is a 400 over HTTP and creates nothing", async () => {
  const res = await svc.call("POST", "/tickets", { title: "    ", description: "x" });
  assert.deepEqual(res, { status: 400, body: { error: "title_required" } });
  assert.deepEqual((await svc.call("GET", "/tickets")).body.tickets, []);
});

test("a padded title is kept, trimmed", async () => {
  const res = await svc.call("POST", "/tickets", { title: "  Refund please  " });
  assert.equal(res.status, 201);
  assert.equal(res.body.title, "Refund please");
});
