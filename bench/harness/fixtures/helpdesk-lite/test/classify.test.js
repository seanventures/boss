import { test } from "node:test";
import assert from "node:assert/strict";
import { classify } from "../src/classify.js";

test("a refund request is billing", () => {
  assert.equal(classify("please refund the duplicate charge").category, "Billing");
});

test("words that match nothing are Other", () => {
  assert.deepEqual(classify("hello there"), { category: "Other", confidence: 0.3 });
});

test("more matching words mean more confidence", () => {
  const one = classify("my login fails");
  const two = classify("login fails and my password reset never arrives");
  assert.equal(two.category, "Account access");
  assert.ok(two.confidence > one.confidence);
});
