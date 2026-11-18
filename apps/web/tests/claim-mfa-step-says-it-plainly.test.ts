// @vitest-environment node
/**
 * The claim's MFA step speaks plainly (stranger test r2, 2026-11-17): it said "Posture will show
 * 'No passkey on the only Owner' until you add one", an insider word for a stranger who just
 * claimed. Breaks if "Posture" (or the internal check's title) returns to the claim page.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const src = readFileSync(join(__dirname, "../app/(auth)/claim/claim.tsx"), "utf8");

describe("the claim's no-passkey note", () => {
  it("names no internal screen and says what to do", () => {
    expect(src).not.toMatch(/Posture/);
    expect(src).not.toContain("No passkey on the only Owner");
    expect(src).toContain("You can add a passkey later in My account.");
  });
});
