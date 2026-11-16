/**
 * The promote sheet says why evidence is short, beside "Not enough evidence",
 * not only on that span's title. A blank reason says nothing. More than six
 * words is a paragraph. Six or fewer is a span.
 *
 * Breaks if the reason stays padded, a blank reason still renders, a long
 * reason is a span, the short words disappear, or the sheet stops calling
 * promoteWhySentence(plan.why_not).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import * as scorecards from "../app/observability/scorecards";
import { codeOnly } from "./_helpers/source";

type WhyFn = (why: string | null | undefined) => string | null;
type WhyView = (props: { sentence: string | null }) => React.ReactNode;

const promoteWhySentence = (scorecards as { promoteWhySentence?: WhyFn }).promoteWhySentence as WhyFn;
const PromoteWhy = (scorecards as { PromoteWhy?: WhyView }).PromoteWhy as WhyView;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "the staging probes have been failing since last night";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "probe down";

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
    root!.render(<PromoteWhy sentence={sentence} />);
  });
  return host;
}

function why(el: HTMLElement): Element | null {
  return el.querySelector("[data-testid='sc-promote-why']");
}

describe("promoteWhySentence", () => {
  it("is the trimmed reason, or nothing when the reason is blank", () => {
    expect(promoteWhySentence(`  ${LONG}  `)).toBe(LONG);
    expect(promoteWhySentence(SHORT)).toBe(SHORT);
    expect(promoteWhySentence(SIX)).toBe(SIX);
    expect(promoteWhySentence(SEVEN)).toBe(SEVEN);
    expect(promoteWhySentence(null)).toBeNull();
    expect(promoteWhySentence(undefined)).toBeNull();
    expect(promoteWhySentence("")).toBeNull();
    expect(promoteWhySentence(" \n\t ")).toBeNull();
  });
});

describe("the promote sheet says why evidence is short", () => {
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

  it("calls promoteWhySentence with plan.why_not and keeps the short words", () => {
    const src = codeOnly(join(__dirname, "../app/observability/scorecards.tsx"));
    expect(src).toContain("promoteWhySentence(plan.why_not)");
    expect(src).toContain("<PromoteWhy sentence={promoteWhySentence(plan.why_not)}");
    expect(src).toContain("title={plan.why_not}>Not enough evidence");
    expect(src).toContain("title={e.stop_reason ? STOP[e.stop_reason] ?? e.stop_reason : undefined}");
    expect(src).toContain('title={e.compare.why} data-testid="sc-exp-verdict">{e.compare.label}');
  });
});
