/**
 * The custom step says its summary, beside the label, not only on its title.
 * A blank summary says nothing. The same words as the label are not repeated.
 * More than six words is a paragraph. Six or fewer is a span.
 * The risk mark keeps its unusable reason on the tip.
 *
 * Breaks if the summary stays title-only, the label is replaced, a blank or
 * repeated summary still renders, a long summary is a span, a short summary
 * is a paragraph, the title changes, or the risk span's unusable reason changes.
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

type SentenceFn = (
  summary: string | null | undefined,
  label: string | null | undefined,
) => string | null;
type WhyView = (props: { id: string; sentence: string | null }) => React.ReactNode;

const mod = await import("../app/workflows/library") as {
  stepSummarySentence?: SentenceFn;
  View?: WhyView;
};
const stepSummarySentence = mod.stepSummarySentence;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "this step sends the weekly report after the books close";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SPACED = "one  two  three  four  five  six";
const SHORT = "weekly report";
const LABEL = "Ship";

let root: Root | null = null;
let host: HTMLElement | null = null;

function unmountShown(): void {
  act(() => { root?.unmount(); });
  if (host) host.remove();
  host = null;
  root = null;
}

afterEach(() => {
  unmountShown();
  document.body.innerHTML = "";
});

async function show(id: string, sentence: string | null): Promise<HTMLElement> {
  unmountShown();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const View = mod.View!;
  await act(async () => {
    root!.render(<View id={id} sentence={sentence} />);
  });
  return host;
}

function why(el: HTMLElement, id: string): Element | null {
  return el.querySelector(`[data-testid='wf-step-why-${id}']`);
}

describe("stepSummarySentence", () => {
  it("is the trimmed summary, or nothing when it is blank or already the label", () => {
    expect(typeof stepSummarySentence).toBe("function");
    expect(stepSummarySentence!(`  ${LONG}  `, LABEL)).toBe(LONG);
    expect(stepSummarySentence!(`  ${SHORT}  `, LABEL)).toBe(SHORT);
    expect(stepSummarySentence!(SIX, LABEL)).toBe(SIX);
    expect(stepSummarySentence!(`  ${SEVEN}  `, LABEL)).toBe(SEVEN);
    expect(stepSummarySentence!(null, LABEL)).toBeNull();
    expect(stepSummarySentence!(undefined, LABEL)).toBeNull();
    expect(stepSummarySentence!("", LABEL)).toBeNull();
    expect(stepSummarySentence!(" \n\t ", LABEL)).toBeNull();
    expect(stepSummarySentence!("  Ship  ", "Ship")).toBeNull();
    expect(stepSummarySentence!("Ship", "  Ship  ")).toBeNull();
    expect(stepSummarySentence!(`  ${LABEL}  `, ` ${LABEL} `)).toBeNull();
    expect(stepSummarySentence!("  Send the report  ", LABEL)).toBe("Send the report");
  });
});

describe("the custom step says its summary", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    const long = why(await show("weekly", LONG), "weekly");
    expect(long?.tagName).toBe("P");
    expect(long?.textContent).toBe(LONG);
    expect(long?.hasAttribute("data-user-text")).toBe(true);

    const seven = why(await show("r7", SEVEN), "r7");
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);

    const spaced = why(await show("rs", SPACED), "rs");
    expect(spaced?.tagName).toBe("SPAN");
    expect(spaced?.textContent).toBe(SPACED);
    expect(spaced?.hasAttribute("data-user-text")).toBe(true);

    const six = why(await show("r6", SIX), "r6");
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six?.hasAttribute("data-user-text")).toBe(true);

    const short = why(await show("ship", SHORT), "ship");
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when there is no sentence", async () => {
    const none = await show("ship", null);
    expect(why(none, "ship")).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls stepSummarySentence beside the label and leaves the risk reason alone", () => {
    const src = codeOnly(join(__dirname, "../app/workflows/library.tsx"));
    const labelAt = src.indexOf('className="wf-libsteps-label"');
    expect(labelAt).toBeGreaterThan(-1);
    const cellEnd = src.indexOf("</td>", labelAt);
    expect(cellEnd).toBeGreaterThan(labelAt);
    const cell = src.slice(labelAt, cellEnd);
    const labelText = cell.indexOf("{c.label}");
    const call = "<View id={c.step_key} sentence={stepSummarySentence(c.summary, c.label)} />";
    expect(cell).toContain("title={c.summary || undefined}");
    expect(labelText).toBeGreaterThan(-1);
    expect(cell.indexOf(call)).toBeGreaterThan(labelText);
    expect(cell).not.toContain("unusable_reason");
    expect(src).toContain("unusable_reason: c.unusable_reason ?? undefined");
    expect(src).toContain('title={!c.usable ? (c.unusable_reason ?? "Cannot run")');
    expect(src).toContain(": c.approval_forced_manual ? `Risk ${c.risk_tier}, always needs a person`");
    expect(src).toContain(": `Risk ${c.risk_tier}`}");
    expect(src).toContain('<span className="wf-risk" data-risk={risk}');
    expect(src).toContain("data-testid={`wf-step-why-${");
  });
});
