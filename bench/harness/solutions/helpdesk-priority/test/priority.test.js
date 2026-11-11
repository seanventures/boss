import { beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { createTicket, listTickets, resetStore } from "../src/store.js";

beforeEach(() => resetStore());

test("a ticket is normal priority unless it says otherwise", () => {
  assert.equal(createTicket({ title: "a" }).priority, "normal");
  assert.equal(createTicket({ title: "b", priority: "high" }).priority, "high");
});

test("an unknown priority is refused", () => {
  assert.throws(() => createTicket({ title: "c", priority: "urgent" }), /invalid_priority/);
});

test("tickets can be listed by priority", () => {
  createTicket({ title: "low one", priority: "low" });
  createTicket({ title: "high one", priority: "high" });
  assert.deepEqual(listTickets({ priority: "high" }).map((t) => t.title), ["high one"]);
});
