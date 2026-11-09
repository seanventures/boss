import { existsSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * Memory says what failed and asks before retiring; the leftover diagnostic route is gone.
 *
 * Console walk on boss-1, 2026-10-02 (the critic's pass over screens no map covered): the Memory
 * screen's errors read "failed" with no subject and no next step, and Retire — which changes what
 * every later agent session is told — took effect on one click. /api/debug-api, a route its own
 * comment called a temporary diagnostic, still shipped: it called /v1/status with the server's
 * key and returned the API URL and a body preview to anyone signed in.
 */
const MEMORY = codeOnly("app/memory-ui.tsx");

describe("the Memory screen", () => {
  it("names what failed and what to do", () => {
    expect(MEMORY).toContain("Could not load what BOSS remembers:");
    expect(MEMORY).toContain("Nothing changed, press it again.");
    expect(MEMORY).not.toMatch(/setError\(body\?\.error \?\? "failed"\)/);
  });

  it("asks before retiring a memory", () => {
    expect(MEMORY).toContain('action === "forget"');
    expect(MEMORY).toContain("window.confirm(`Retire this memory?");
  });
});

describe("the console ships no leftover diagnostics", () => {
  it("has no /api/debug-api route", () => {
    expect(existsSync(resolve(__dirname, "../app/api/debug-api/route.ts"))).toBe(false);
  });
});
