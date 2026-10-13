import { describe, expect, it } from "vitest";
import { fieldSentence, IdentityError } from "../lib/identity/api";

describe("a rejected form names its field", () => {
  it("turns FastAPI's validation list into the field and the reason", () => {
    expect(fieldSentence([{ loc: ["body", "note"], msg: "String should have at most 20000 characters" }]))
      .toBe("Note: at most 20000 characters");
  });
  it("a 422 error carries that sentence instead of 'Check the fields'", () => {
    const e = new IdentityError(422, "invalid" as never, { body: { detail: [{ loc: ["body", "new_app", "name"], msg: "Field required" }] } });
    expect(e.message).toBe("Name: Field required");
  });
  it("falls back when the body has no validation list", () => {
    expect(fieldSentence("nope")).toBeNull();
  });
});
