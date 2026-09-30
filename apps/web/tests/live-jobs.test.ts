import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

describe("live jobs card", () => {
  it("is mounted on the work area and requests non-terminal updated sort", () => {
    const page = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
    const lib = readFileSync(resolve(__dirname, "../lib/live-jobs.ts"), "utf8");
    expect(page).toContain("<LiveJobsCard");
    expect(lib).toContain("include_terminal: false");
    expect(lib).toContain('sort: "updated"');
  });
});
