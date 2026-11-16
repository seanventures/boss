/**
 * The project header says why health is what it is, beside the health chip,
 * not only on the chip title. A blank reason says nothing. More than six
 * words is a paragraph. Six or fewer is a span.
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
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    const long = why(await show(LONG));
    expect(long?.tagName).toBe("P");
    expect(long?.textContent).toBe(LONG);
    expect(long?.hasAttribute("data-user-text")).toBe(true);

    const seven = why(await show(SEVEN));
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);

    const six = why(await show(SIX));
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six?.hasAttribute("data-user-text")).toBe(true);

    const short = why(await show(SHORT));
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when there is no sentence", async () => {
    const none = await show(null);
    expect(why(none)).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls healthReasonSentence with healthWhy", () => {
    const src = codeOnly(join(__dirname, "../app/projects/project-page.tsx"));
    expect(src).toContain("healthReasonSentence(healthWhy)");
    expect(src).toContain("<HealthWhy sentence={healthReasonSentence(healthWhy)}");
  });
});
