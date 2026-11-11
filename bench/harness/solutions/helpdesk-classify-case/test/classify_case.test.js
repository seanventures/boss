import { test } from "node:test";
import assert from "node:assert/strict";
import { classify } from "../src/classify.js";

test("capital letters do not hide a keyword", () => {
  assert.equal(classify("REFUND my order").category, "Billing");
  assert.equal(classify("Login Broken").category, "Account access");
});
