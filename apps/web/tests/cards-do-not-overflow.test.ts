import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * A card must be able to get smaller than its content.
 *
 * The operator's app card was clipped at the right edge, the failure reason cut mid-word
 * ("health check returned 000; rolle…"), and the Retry button pushed to the boundary.
 *
 * Two compounding causes, both the same idea:
 *   1. `minmax(560px, 1fr)` has a HARD floor. When the panel is narrower than one track the
 *      track still cannot shrink, so the GRID overflows its container.
 *   2. `.mr-meeting-card` is a grid item with no `min-width: 0`, so it defaults to
 *      `min-width: auto` and cannot shrink below its content's intrinsic width.
 *
 * The fix for (2) was already written three lines into `.ct-deploy-line`, for the row INSIDE
 * this card — applied there, missed on the card. Fix one path, miss its twin (S3).
 *
 * CSS is the artifact here, so these assertions read the stylesheet directly. A jsdom test
 * cannot see layout, and a screenshot test is not worth its weight for four declarations.
 */

const CSS = readFileSync(resolve(__dirname, "../app/globals.css"), "utf8");

function block(selector: string): string {
  const i = CSS.indexOf(selector + " {");
  if (i === -1) throw new Error(`no rule for ${selector}`);
  return CSS.slice(i, CSS.indexOf("}", i));
}

describe("the app card grid", () => {
  it("lets a track shrink below its preferred width", () => {
    // repeat(auto-fit, minmax(560px, 1fr)) overflows; min(560px, 100%) does not.
    expect(CSS).toContain("minmax(min(29.47368rem, 100%), 1fr)");   // 560px
  });

  it("has no remaining hard-floor minmax in the tile grid", () => {
    expect(CSS).not.toContain("minmax(29.47368rem, 1fr)");   // a hard 560px floor
  });
});

describe("the card itself", () => {
  it("can shrink below its content", () => {
    expect(block(".mr-meeting-card")).toContain("min-width: 0");
  });

  it("lets its children shrink too, or its own min-width is undone", () => {
    expect(CSS).toContain(".mr-meeting-card > * { min-width: 0; }");
  });
});

describe("the deploy failure reason", () => {
  it("keeps the outcome instead of ellipsising it away", () => {
    // "health check returned 000; rolled back" is 38 chars; 26ch on one line cut "rolled back",
    // which is the half that says whether a running app still exists.
    const rule = block(".ct-deploy-reason");
    expect(rule).not.toContain("max-width: 26ch");
    expect(rule).toContain("line-clamp: 2");
  });

  it("is still bounded, so a pathological message cannot take over the card", () => {
    const rule = block(".ct-deploy-reason");
    expect(rule).toMatch(/max-width: \d+ch/);
    expect(rule).toContain("overflow: hidden");
  });

  it("still keeps the full text reachable", () => {
    const ui = readFileSync(resolve(__dirname, "../app/factory/legacy-build-parts.tsx"), "utf8");
    expect(ui).toContain("title={latest.failure_reason}");
  });
});
