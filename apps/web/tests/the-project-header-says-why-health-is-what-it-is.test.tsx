// @vitest-environment happy-dom
/**
 * The project header says why health is what it is, beside the health chip,
 * not only on the chip title: a small "Why?" toggle that opens the reason below
 * the chips (operator 2026-11-21: never a paragraph of engine prose in the
 * header). A blank reason says nothing.
 *
 * Breaks if the reason stays padded, a blank reason still renders, a long
 * reason is a span, or the header stops calling healthReasonSentence(healthWhy).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { HealthWhy, healthReasonSentence } from "../app/projects/project-page";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "the staging probes have been failing since last night";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "probe down";

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

async function show(sentence: string | null): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<HealthWhy sentence={sentence} />);
  });
  return host;
}

function why(el: HTMLElement): Element | null {
  return el.querySelector("[data-testid='px-head-health-why']");
}

describe("healthReasonSentence", () => {
  it("is the trimmed reason, or nothing when the reason is blank", () => {
    expect(healthReasonSentence(`  ${LONG}  `)).toBe(LONG);
    expect(healthReasonSentence(SHORT)).toBe(SHORT);
    expect(healthReasonSentence(SIX)).toBe(SIX);
    expect(healthReasonSentence(SEVEN)).toBe(SEVEN);
    expect(healthReasonSentence(null)).toBeNull();
    expect(healthReasonSentence(undefined)).toBeNull();
    expect(healthReasonSentence("")).toBeNull();
    expect(healthReasonSentence(" \n\t ")).toBeNull();
  });
});

describe("the project header says why health is what it is", () => {
  it("is a small Why? toggle for any reason, long or short — never the reason inline", async () => {
    for (const text of [LONG, SEVEN, SIX, SHORT]) {
      const el = why(await show(text));
      expect(el?.tagName).toBe("BUTTON");
      expect(el?.textContent).toBe("Why?");
      expect(el?.getAttribute("aria-expanded")).toBe("false");
      expect(el?.getAttribute("aria-controls")).toBe("px-head-why-block");
    }
  });

  it("says nothing when there is no sentence", async () => {
    const none = await show(null);
    expect(why(none)).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls healthReasonSentence with healthWhy", () => {
    const src = codeOnly(join(__dirname, "../app/projects/project-page.tsx"));
    expect(src).toContain("healthReasonSentence(healthWhy)");
    expect(src).toContain("<HealthWhy sentence={healthReasonSentence(healthWhy)} open={why === \"health\"}");
  });
});
