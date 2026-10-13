import { describe, expect, it } from "vitest";
import { classifyByKeyword } from "../../src/server/classify";

describe("keyword classifier", () => {
  it("P1 a refund request is Billing", () => {
    expect(classifyByKeyword("I want a refund for this invoice").category).toBe("Billing");
  });

  it("P1 text with no keyword is Other at low confidence", () => {
    const got = classifyByKeyword("hello there");
    expect(got.category).toBe("Other");
    expect(got.confidence).toBeLessThan(0.5);
  });
});
