import { beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { createTicket, getTicket, listTickets, resetStore, setStatus } from "../src/store.js";

beforeEach(() => resetStore());

test("a new ticket is open and filed under its category", () => {
  const t = createTicket({ title: "App crash on save", description: "it shows an error" });
  assert.equal(t.status, "open");
  assert.equal(t.category, "Technical issue");
  assert.deepEqual(getTicket(t.id), t);
});

test("a ticket needs a title", () => {
  assert.throws(() => createTicket({ description: "no title" }), /title_required/);
});

test("tickets are listed newest first", () => {
  createTicket({ title: "first" });
  createTicket({ title: "second" });
  assert.deepEqual(listTickets().map((t) => t.title), ["second", "first"]);
});

test("a ticket can be closed, and only to a known status", () => {
  const t = createTicket({ title: "close me" });
  assert.equal(setStatus(t.id, "closed").status, "closed");
  assert.throws(() => setStatus(t.id, "lost"), /invalid_status/);
  assert.equal(setStatus(999, "open"), null);
});
