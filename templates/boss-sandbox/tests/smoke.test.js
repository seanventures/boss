import test from "node:test";
import assert from "node:assert/strict";

import { greet } from "../src/index.js";

test("greet returns the sandbox banner", () => {
  assert.equal(greet(), "hello BOSS sandbox");
});

