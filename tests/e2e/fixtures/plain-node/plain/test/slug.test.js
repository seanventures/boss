import { test } from "node:test";
import assert from "node:assert/strict";
import { slug } from "../src/slug.js";

test("slug lowercases and joins words with hyphens", () => {
  assert.equal(slug("Hello, World"), "hello-world");
});

test("slug trims separators and survives nothing", () => {
  assert.equal(slug("  --Plain Node--  "), "plain-node");
  assert.equal(slug(undefined), "");
});
