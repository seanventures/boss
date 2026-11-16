import { describe, expect, it } from "vitest";

import { stackButtonPost } from "../lib/stack-decision";

describe("a stack card posts its three answers", () => {
  it("accepts the other stack as an approval and the other two as refusals", () => {
    expect(stackButtonPost("accept_alt", "")).toEqual({
      action: "approve",
      note: "Stack choice: accept_alt.",
    });
    expect(stackButtonPost("enable_toolchain", "add Go")).toEqual({
      action: "reject",
      note: "Stack choice: enable_toolchain. add Go",
    });
    expect(stackButtonPost("stop", "")).toEqual({
      action: "reject",
      note: "Stack choice: stop.",
    });
    expect(stackButtonPost("approve", "")).toBeNull();
  });
});
