/**
 * Recent failures draws five rows. When more dead-letters exist, a sentence says how many
 * are not on the page. Five or fewer say nothing. The banner that already states the total stays.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { deadletterRestSentence } from "../app/workflow-ui";

const SRC = readFileSync(join(__dirname, "../app/workflow-ui.tsx"), "utf8");

const BANNER = "`${failed} failed, ${deadletters.length} dead-letter${deadletters.length === 1 ? \"\" : \"s\"}, ${staleLeaseJobIds.length} stale lease${staleLeaseJobIds.length === 1 ? \"\" : \"s\"}, ${blockerCount} blocker${blockerCount === 1 ? \"\" : \"s\"} need attention.`";

function afterTheSlice(src: string): string {
  const sliceAt = src.indexOf("deadletters.slice(0, 5)");
  const recAt = src.indexOf("recommendedActions.length", sliceAt + 1);
  if (sliceAt < 0 || recAt < 0) return "";
  return src.slice(sliceAt, recAt);
}

describe("the dead-letter list says what is not on the page", () => {
  it("says how many failures are not among the five on the page", () => {
    expect(deadletterRestSentence(8)).toBe("5 failures are on this page. 3 more are not.");
    expect(deadletterRestSentence(6)).toBe("5 failures are on this page. 1 more are not.");
  });

  it("says nothing when five or fewer failures exist", () => {
    expect(deadletterRestSentence(5)).toBeNull();
    expect(deadletterRestSentence(4)).toBeNull();
    expect(deadletterRestSentence(1)).toBeNull();
    expect(deadletterRestSentence(0)).toBeNull();
  });

  it("calls the sentence after the five-row slice and renders it as a paragraph only when set", () => {
    const tail = afterTheSlice(SRC);
    expect(tail).not.toBe("");
    expect(tail.indexOf("deadletterRestSentence(")).toBeGreaterThan(0);
    expect(tail).toMatch(
      /deadletterRestSentence\(deadletters\.length\)\s*\?\s*\(\s*<p(?:\s[^>]*)?>\{deadletterRestSentence\(deadletters\.length\)\}<\/p>/,
    );
  });

  it("leaves the banner that already states the total", () => {
    const bannerAt = SRC.indexOf("wf-banner");
    const statsAt = SRC.indexOf("wf-stats", bannerAt);
    expect(bannerAt).toBeGreaterThan(-1);
    expect(statsAt).toBeGreaterThan(bannerAt);
    const banner = SRC.slice(bannerAt, statsAt);
    expect(banner).toContain(BANNER);
    expect(banner).not.toContain("deadletterRestSentence");
    expect(banner).not.toContain("more are not");
  });
});
