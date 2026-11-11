import { beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { createTicket, resetStore, setStatus, ticketStats } from "../src/store.js";

beforeEach(() => resetStore());

test("no tickets, all zero", () => {
  const s = ticketStats();
  assert.equal(s.total, 0);
  assert.deepEqual(s.by_status, { open: 0, closed: 0 });
  assert.equal(s.by_category.Billing, 0);
});

test("counts by status and by category", () => {
  createTicket({ title: "refund please" });
  const t = createTicket({ title: "app crash" });
  setStatus(t.id, "closed");
  const s = ticketStats();
  assert.equal(s.total, 2);
  assert.deepEqual(s.by_status, { open: 1, closed: 1 });
  assert.equal(s.by_category.Billing, 1);
  assert.equal(s.by_category["Technical issue"], 1);
});
