/**
 * The journey says the detail after the node button, not only on its title.
 * A blank detail says nothing. The same words as the label or the sub are not
 * repeated. More than six words is a paragraph. Six or fewer is a span.
 * The timeline keeps the detail on its tip and does not say the sentence.
 *
 * Breaks if the detail stays title-only, the sentence sits inside the button,
 * a blank or repeated detail still renders, a long detail is a span, a short
 * detail is a paragraph, the button title changes, or the timeline calls
 * journeyDetailSentence.
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

type SentenceFn = (
  detail: string | null | undefined,
  label: string | null | undefined,
  sub: string | null | undefined,
) => string | null;
type DetailView = (props: { id: string; sentence: string | null }) => React.ReactNode;

const mod = await import("../app/projects/kit") as {
  journeyDetailSentence?: SentenceFn;
  View?: DetailView;
};
const journeyDetailSentence = mod.journeyDetailSentence;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "this stage is waiting because the review has not started yet";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SPACED = "one  two  three  four  five  six";
const SHORT = "review has not started";

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
  return el.querySelector(`[data-testid='px-journey-why-${id}']`);
}

describe("journeyDetailSentence", () => {
  it("is the trimmed detail, or nothing when it is blank or already the label or the sub", () => {
    expect(typeof journeyDetailSentence).toBe("function");
    expect(journeyDetailSentence!(`  ${LONG}  `, "Ready", "6/6")).toBe(LONG);
    expect(journeyDetailSentence!(`  ${SHORT}  `, "Ready", "6/6")).toBe(SHORT);
    expect(journeyDetailSentence!(SIX, "Ready", "6/6")).toBe(SIX);
    expect(journeyDetailSentence!(SEVEN, "Ready", "6/6")).toBe(SEVEN);
    expect(journeyDetailSentence!(null, "Ready", "6/6")).toBeNull();
    expect(journeyDetailSentence!(undefined, "Ready", "6/6")).toBeNull();
    expect(journeyDetailSentence!("", "Ready", "6/6")).toBeNull();
    expect(journeyDetailSentence!(" \n\t ", "Ready", "6/6")).toBeNull();
    expect(journeyDetailSentence!("  Ready  ", "Ready", "6/6")).toBeNull();
    expect(journeyDetailSentence!("Ready", "  Ready  ", "later")).toBeNull();
    expect(journeyDetailSentence!("  6/6  ", "Ready", "6/6")).toBeNull();
    expect(journeyDetailSentence!("6/6", "Ready", "  6/6  ")).toBeNull();
    expect(journeyDetailSentence!("Ready", "Working", "6/6")).toBe("Ready");
  });
});

describe("the journey says the detail", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    const long = why(await show("working", LONG), "working");
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

    const short = why(await show("ready", SHORT), "ready");
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when there is no sentence", async () => {
    const none = await show("ready", null);
    expect(why(none, "ready")).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls journeyDetailSentence after the button and leaves the timeline title-only", () => {
    const src = codeOnly(join(__dirname, "../app/projects/kit.tsx"));
    const journeyAt = src.indexOf("export function Journey");
    const timelineAt = src.indexOf("export function Timeline");
    expect(journeyAt).toBeGreaterThan(-1);
    expect(timelineAt).toBeGreaterThan(journeyAt);
    const journey = src.slice(journeyAt, timelineAt);
    const timelineRest = src.slice(timelineAt + "export function Timeline".length);
    const nextExport = timelineRest.search(/\nexport\s/);
    const timeline = nextExport === -1 ? src.slice(timelineAt) : src.slice(timelineAt, timelineAt + "export function Timeline".length + nextExport);
    const open = journey.indexOf("<button");
    const close = journey.indexOf("</button>");
    expect(open).toBeGreaterThan(-1);
    expect(close).toBeGreaterThan(open);
    const inside = journey.slice(open, close);
    const after = journey.slice(close);
    const call = "<View id={n.key || i} sentence={journeyDetailSentence(n.detail, n.label, n.sub)} />";
    expect(inside).toContain("title={n.detail || undefined}");
    expect(inside).toContain("{n.label}");
    expect(inside).toContain("{n.sub}");
    expect(inside).not.toContain("journeyDetailSentence");
    expect(inside).not.toContain("<View");
    expect(after).toContain(call);
    expect(journey.indexOf(call)).toBeGreaterThan(close);
    expect(timeline).not.toContain("journeyDetailSentence");
    expect(timeline).toContain("title={p.detail || undefined}");
  });
});
