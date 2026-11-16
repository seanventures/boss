/**
 * The backlog row says the detail, beside the label, not only on its title.
 * A blank detail says nothing. A detail that repeats the label or the why
 * says nothing. More than six words is a paragraph. Six or fewer is a span.
 * The checklist already prints its detail and does not use this sentence.
 *
 * Breaks if the detail stays title-only, a duplicate of the label or the why
 * still renders, a long detail is a span, the title or the why span changes,
 * or the checklist row starts calling backlogDetailSentence.
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

type DetailFn = (
  detail: string | null | undefined,
  label: string | null | undefined,
  why: string | null | undefined,
) => string | null;
type DetailView = (props: { i: number; sentence: string | null }) => React.ReactNode;

const mod = await import("../app/projects/onboarding") as {
  backlogDetailSentence?: DetailFn;
  View?: DetailView;
};
const backlogDetailSentence = mod.backlogDetailSentence;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "the build waits on a missing workflow file";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SPACED = "one  two  three  four  five  six";
const SHORT = "missing workflow";
const LABEL = "Add CI";
const WHY = "71/190 pass";

let root: Root | null = null;
let host: HTMLElement | null = null;

function unmountShown(): void {
  act(() => { root?.unmount(); });
  if (host) host.remove();
  root = null;
  host = null;
}

afterEach(() => {
  unmountShown();
  document.body.innerHTML = "";
});

async function show(i: number, sentence: string | null): Promise<HTMLElement> {
  expect(typeof mod.backlogDetailSentence).toBe("function");
  expect(typeof mod.View).toBe("function");
  unmountShown();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const View = mod.View!;
  await act(async () => {
    root!.render(<View i={i} sentence={sentence} />);
  });
  return host;
}

function detail(el: HTMLElement, i: number): Element | null {
  return el.querySelector(`[data-testid='px-ob-backlog-detail-${i}']`);
}

describe("backlogDetailSentence", () => {
  it("is a different trimmed detail, and nothing when blank or the same as the label or the why", () => {
    expect(typeof backlogDetailSentence).toBe("function");
    expect(backlogDetailSentence!(`  ${LONG}  `, LABEL, WHY)).toBe(LONG);
    expect(backlogDetailSentence!(SHORT, LABEL, WHY)).toBe(SHORT);
    expect(backlogDetailSentence!(SIX, LABEL, WHY)).toBe(SIX);
    expect(backlogDetailSentence!(SEVEN, LABEL, WHY)).toBe(SEVEN);
    expect(backlogDetailSentence!(SPACED, LABEL, WHY)).toBe(SPACED);
    expect(backlogDetailSentence!("  Add CI now  ", LABEL, WHY)).toBe("Add CI now");
    expect(backlogDetailSentence!(null, LABEL, WHY)).toBeNull();
    expect(backlogDetailSentence!(undefined, LABEL, WHY)).toBeNull();
    expect(backlogDetailSentence!("", LABEL, WHY)).toBeNull();
    expect(backlogDetailSentence!(" \n\t ", LABEL, WHY)).toBeNull();
    expect(backlogDetailSentence!(`  ${LABEL}  `, LABEL, WHY)).toBeNull();
    expect(backlogDetailSentence!(LABEL, `  ${LABEL}  `, WHY)).toBeNull();
    expect(backlogDetailSentence!(`  ${WHY}  `, LABEL, WHY)).toBeNull();
    expect(backlogDetailSentence!(WHY, LABEL, `  ${WHY}  `)).toBeNull();
    expect(backlogDetailSentence!(LABEL, LABEL, LABEL)).toBeNull();
  });
});

describe("the backlog row says the detail", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    const long = detail(await show(0, LONG), 0);
    expect(long?.tagName).toBe("P");
    expect(long?.textContent).toBe(LONG);
    expect(long?.hasAttribute("data-user-text")).toBe(true);
    expect(LONG.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);

    const seven = detail(await show(7, SEVEN), 7);
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);
    expect(SEVEN.split(/\s+/).filter(Boolean).length).toBe(7);

    const spaced = detail(await show(2, SPACED), 2);
    expect(spaced?.tagName).toBe("SPAN");
    expect(spaced?.textContent).toBe(SPACED);
    expect(spaced?.hasAttribute("data-user-text")).toBe(true);
    expect(SPACED.split(/\s+/).filter(Boolean).length).toBe(6);

    const six = detail(await show(6, SIX), 6);
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six?.hasAttribute("data-user-text")).toBe(true);
    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);

    const short = detail(await show(1, SHORT), 1);
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when there is no sentence", async () => {
    const none = await show(3, null);
    expect(detail(none, 3)).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls backlogDetailSentence beside the label and leaves the title, the why span, and the checklist detail", () => {
    const src = codeOnly(join(__dirname, "../app/projects/onboarding.tsx"));
    const rowStart = src.indexOf("data-testid={`px-ob-backlog-${i}`}");
    const whyAt = src.indexOf("data-testid={`px-ob-backlog-why-${i}`}");
    expect(rowStart).toBeGreaterThan(-1);
    expect(whyAt).toBeGreaterThan(rowStart);
    const row = src.slice(rowStart, whyAt);
    expect(row).toContain("title={b.detail}");
    expect(row).toContain("{b.label}");
    expect(row).toContain("backlogDetailSentence(b.detail, b.label, b.why)");
    expect(row).toContain("<View i={i} sentence={backlogDetailSentence(b.detail, b.label, b.why)}");
    expect(src).toContain("{b.why ? <span className=\"px-faint\" data-testid={`px-ob-backlog-why-${i}`}>· {b.why}</span> : null}");

    const checkStart = src.indexOf('data-testid="px-ob-checklist"');
    const checkEnd = src.indexOf("<BacklogCard", checkStart);
    const checklist = src.slice(checkStart, checkEnd);
    expect(checklist).toContain('title={c.detail}');
    expect(checklist).toContain('{c.label}<small>{c.state === "skipped" ? <><b className="px-ck-when">{skipWord(c.key)}</b>{c.sub || c.detail ? ` · ${c.sub || c.detail}` : ""}</> : c.sub || c.detail}</small><span className="px-sr">{you ? "needs you" : c.state === "skipped" ? skipWord(c.key) : STATE_WORD[c.state]}. {c.detail}</span>');
    expect(checklist).not.toContain("backlogDetailSentence");
    expect(src.match(/<View\b/g)).toEqual(["<View"]);
    expect(src.match(/backlogDetailSentence\(/g)).toEqual([
      "backlogDetailSentence(",
      "backlogDetailSentence(",
    ]);
  });
});
