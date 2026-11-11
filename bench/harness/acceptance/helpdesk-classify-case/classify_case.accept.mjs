// Held by the benchmark, never shown to the agent: run on the pull request's head.
import { test } from "node:test";
import assert from "node:assert/strict";
import { classify } from "../src/classify.js";

test("a keyword matches whatever its case", () => {
  assert.equal(classify("Please REFUND the Invoice").category, "Billing");
  assert.equal(classify("Locked Out of my account").category, "Account access");
  assert.equal(classify("The App CRASHES with an Error").category, "Technical issue");
  assert.equal(classify("Would Love dark mode").category, "Feature request");
});

test("case does not change confidence", () => {
  assert.deepEqual(classify("REFUND INVOICE"), classify("refund invoice"));
});

test("unmatched words are still Other", () => {
  assert.deepEqual(classify("Good Morning"), { category: "Other", confidence: 0.3 });
});
