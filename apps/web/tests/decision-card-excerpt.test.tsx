// @vitest-environment happy-dom
/**
 * AML video polish [7]: decision-card excerpts (output/evidence/why) must never cut mid-word or
 * mid-sentence. Long text is cut at a sentence boundary with a "Show all" control that expands in
 * place and toggles aria-expanded; short text renders whole with no control at all.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { DecisionCardExcerpt } from "../app/decision-inbox-ui";
import { $, mount, settle, unmount } from "./_helpers/live-render";

afterEach(() => { unmount(); });

const SENTENCE = "The review confirmed documented biweekly payroll cadence for 14 employees matches historical pattern. ";

function longText(totalLength: number): string {
  let text = "";
  while (text.length < totalLength) text += SENTENCE;
  return text.slice(0, totalLength);
}

describe("DecisionCardExcerpt", () => {
  it("cuts long text at a sentence boundary and Show all expands it with aria-expanded toggling", async () => {
    const full = longText(1500);
    await mount(<DecisionCardExcerpt text={full} testid="dq-excerpt" />);

    const shown = $('[data-testid="dq-excerpt"]')?.textContent ?? "";
    expect(shown.length).toBeLessThan(full.length);
    expect(/[.!?]\s*$/.test(shown.trim())).toBe(true);

    const toggle = $('[data-testid="dq-excerpt-toggle"]') as HTMLButtonElement | null;
    expect(toggle).toBeTruthy();
    expect(toggle?.getAttribute("aria-expanded")).toBe("false");

    toggle!.click();
    await settle();

    expect($('[data-testid="dq-excerpt-toggle"]')?.getAttribute("aria-expanded")).toBe("true");
    expect($('[data-testid="dq-excerpt"]')?.textContent).toBe(full);
  });
});
