/**
 * A host's facts are words. Its labels are an object, and the host drawer printed
 * "Labels: [object Object]" (independent walk, item 10).
 */
import { describe, expect, it } from "vitest";

import { fact } from "../app/infra/host-drawer";

describe("a host fact", () => {
  it("says a label set as words, never [object Object]", () => {
    expect(fact({ role: "ci", region: "fsn1" })).toBe("role ci, region fsn1");
    expect(fact({ gpu: true })).toBe("gpu");
    expect(fact({})).toBe("—");
    expect(fact({ a: null })).toBe("—");
  });

  it("keeps plain values and lists as they were", () => {
    expect(fact("x86_64")).toBe("x86_64");
    expect(fact(22)).toBe("22");
    expect(fact(["a", "b"])).toBe("a, b");
    expect(fact(null)).toBe("—");
    expect(fact("")).toBe("—");
  });
});
