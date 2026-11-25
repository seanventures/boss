// @vitest-environment happy-dom
/**
 * The experiment says why that verdict, beside the label, not only on the
 * verdict span's title. A blank why says nothing. More than six words is a
 * paragraph. Six or fewer is a span. The label stays.
 *
 * Breaks if the why stays padded, a blank why still renders, a long why is a
 * span, the verdict label disappears, or the card stops calling
 * compareWhySentence(e.compare.why).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import * as scorecards from "../app/observability/scorecards";
import { codeOnly } from "./_helpers/source";

type WhyFn = (why: string | null | undefined) => string | null;
type WhyView = (props: { sentence: string | null }) => React.ReactNode;

const compareWhySentence = (scorecards as { compareWhySentence?: WhyFn }).compareWhySentence!;
const CompareWhy = (scorecards as { CompareWhy?: WhyView }).CompareWhy!;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "the interval of B minus A cleared zero last night";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "cleared zero";

let root: Root | null = null;
let host: HTMLElement | null = null;

function unmountShown(): void {
  act(() => root?.unmount());
  root = null;
  if (host) host.remove();
  host = null;
}

afterEach(() => {
  unmountShown();
  document.body.innerHTML = "";
});

async function show(sentence: string | null): Promise<HTMLElement> {
  unmountShown();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<CompareWhy sentence={sentence} />);
  });
  return host;
}

function why(el: HTMLElement): Element | null {
  return el.querySelector("[data-testid='sc-exp-why']");
}

describe("compareWhySentence", () => {
  it("is the trimmed why, or nothing when the why is blank", () => {
    expect(compareWhySentence(`  ${LONG}  `)).toBe(LONG);
    expect(compareWhySentence(SHORT)).toBe(SHORT);
    expect(compareWhySentence(SIX)).toBe(SIX);
    expect(compareWhySentence(SEVEN)).toBe(SEVEN);
    expect(compareWhySentence("  too   close  ")).toBe("too   close");
    expect(compareWhySentence(null)).toBeNull();
    expect(compareWhySentence(undefined)).toBeNull();
    expect(compareWhySentence("")).toBeNull();
    expect(compareWhySentence(" \n\t ")).toBeNull();
  });
});

describe("the experiment says why that verdict", () => {
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

  it("calls compareWhySentence with e.compare.why and keeps the verdict label", () => {
    const src = codeOnly(join(__dirname, "../app/observability/scorecards.tsx"));
    const start = src.indexOf('data-testid="sc-exp-verdict"');
    expect(start).toBeGreaterThan(-1);
    const nearby = src.slice(start, start + 500);
    expect(nearby).toContain("compareWhySentence(e.compare.why)");
    expect(src).toContain('title={e.compare.why} data-testid="sc-exp-verdict">{e.compare.label}');
    expect(src).toContain("promoteWhySentence(plan.why_not)");
    expect(src).toContain("Not enough evidence");
    expect(src).toContain("title={e.stop_reason ? STOP[e.stop_reason] ?? e.stop_reason : undefined}");
  });
});
