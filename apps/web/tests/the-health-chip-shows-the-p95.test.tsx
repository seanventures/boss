import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { healthChipText } from "../app/projects/project-page";

describe("the health chip shows the p95", () => {
  it("puts a non-empty p95 after the health word", () => {
    expect(healthChipText("Degraded", "p95 207ms · 24 h")).toBe("Degraded · p95 207ms · 24 h");
    expect(healthChipText("Healthy", "p95 180ms · 24 h")).toBe("Healthy · p95 180ms · 24 h");
  });

  it("leaves the visible words alone when there is no p95", () => {
    expect(healthChipText("No health yet", "")).toBe("No health yet");
    expect(healthChipText("Healthy", "")).toBe("Healthy");
  });

  it("the chip's visible children call that helper and add no p95 source sentence", () => {
    const src = readFileSync(join(__dirname, "../app/projects/project-page.tsx"), "utf8");
    const at = src.indexOf('data-testid="px-head-health"');
    expect(at).toBeGreaterThan(-1);
    const chip = src.slice(at, src.indexOf("</a>", at));
    expect(chip).toContain("healthChipText(");
    expect(chip).toContain("p95Words(staging?.p95_ms, staging?.window)");
    expect(chip).not.toContain("P95_SOURCE");
  });
});
