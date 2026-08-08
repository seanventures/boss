import type { CodeTaskRun } from "@boss/api-client";
import { describe, expect, it } from "vitest";

import { structuredCodeTaskTests } from "../lib/code-tasks";

describe("structuredCodeTaskTests", () => {
  it("keeps structured test evidence and rejects legacy scalar payloads", () => {
    expect(
      structuredCodeTaskTests({
        tests: [{ command: "pnpm test", exit_code: 0, duration_ms: 420 }],
      }),
    ).toEqual([{ command: "pnpm test", exit_code: 0, duration_ms: 420 }]);

    expect(
      structuredCodeTaskTests({ tests: "pnpm test" } as unknown as CodeTaskRun["state"]),
    ).toEqual([]);
    expect(structuredCodeTaskTests()).toEqual([]);
  });
});
