import { beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { createTicket, resetStore } from "../src/store.js";

beforeEach(() => resetStore());

test("a title of only spaces is no title", () => {
  assert.throws(() => createTicket({ title: "   " }), /title_required/);
});

test("a title is stored without its surrounding spaces", () => {
  assert.equal(createTicket({ title: "  Printer jam  " }).title, "Printer jam");
});
