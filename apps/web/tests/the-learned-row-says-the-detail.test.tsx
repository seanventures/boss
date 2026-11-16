/**
 * The learned row says the detail, beside the row, not only on its title.
 * A blank detail says nothing. The same words as the label or the sub say
 * nothing again. More than six words is a paragraph. Six or fewer is a span.
 *
 * Breaks if the detail stays title-only, a blank or repeated detail still
 * renders, a long detail is a span, the sentence sits inside the row, or the
 * row title changes.
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

type WhyFn = (
  detail: string | null | undefined,
  label: string | null | undefined,
  sub: string | null | undefined,
) => string | null;
type WhyView = (props: { id: string; sentence: string | null }) => React.ReactNode;

const mod = await import("../app/projects/tab-knowledge") as {
  learnedDetailSentence?: WhyFn;
  View?: WhyView;
};
const learnedDetailSentence = mod.learnedDetailSentence;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "the suite only passes when the timezone is pinned to utc";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SPACED = "one  two  three  four  five  six";
const SHORT = "pinned timezone";
const LABEL = "Tests need TZ=UTC";
const SUB = "Run #38 · reused 2×";

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
  return el.querySelector(`[data-testid='px-kn-learned-why-${id}']`);
}

describe("learnedDetailSentence", () => {
  it("is the trimmed detail, or nothing when it is blank or already the label or the sub", () => {
    expect(typeof learnedDetailSentence).toBe("function");
    expect(learnedDetailSentence!(`  ${LONG}  `, LABEL, SUB)).toBe(LONG);
    expect(learnedDetailSentence!(SHORT, LABEL, SUB)).toBe(SHORT);
    expect(learnedDetailSentence!(SIX, LABEL, SUB)).toBe(SIX);
    expect(learnedDetailSentence!(SEVEN, LABEL, SUB)).toBe(SEVEN);
    expect(learnedDetailSentence!("Tests need TZ=UTC before the suite", LABEL, SUB)).toBe("Tests need TZ=UTC before the suite");
    expect(learnedDetailSentence!("tests need tz=utc", LABEL, SUB)).toBe("tests need tz=utc");
    expect(learnedDetailSentence!(null, LABEL, SUB)).toBeNull();
    expect(learnedDetailSentence!(undefined, LABEL, SUB)).toBeNull();
    expect(learnedDetailSentence!("", LABEL, SUB)).toBeNull();
    expect(learnedDetailSentence!(" \n\t ", LABEL, SUB)).toBeNull();
    expect(learnedDetailSentence!(`  ${LABEL}  `, LABEL, SUB)).toBeNull();
    expect(learnedDetailSentence!(LABEL, `  ${LABEL}  `, SUB)).toBeNull();
    expect(learnedDetailSentence!(`  ${SUB}  `, LABEL, SUB)).toBeNull();
    expect(learnedDetailSentence!(SUB, LABEL, `  ${SUB}  `)).toBeNull();
  });
});

describe("the learned row says the detail", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    const long = why(await show("l1", LONG), "l1");
    expect(long?.tagName).toBe("P");
    expect(long?.textContent).toBe(LONG);
    expect(long?.hasAttribute("data-user-text")).toBe(true);

    const seven = why(await show("l7", SEVEN), "l7");
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);

    const spaced = why(await show("ls", SPACED), "ls");
    expect(spaced?.tagName).toBe("SPAN");
    expect(spaced?.textContent).toBe(SPACED);
    expect(spaced?.hasAttribute("data-user-text")).toBe(true);

    const six = why(await show("l6", SIX), "l6");
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six?.hasAttribute("data-user-text")).toBe(true);

    const short = why(await show("l0", SHORT), "l0");
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when there is no sentence", async () => {
    const none = await show("l0", null);
    expect(why(none, "l0")).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls learnedDetailSentence beside the learned row and keeps the title", () => {
    const src = codeOnly(join(__dirname, "../app/projects/tab-knowledge.tsx"));
    const at = src.indexOf("px-kn-learned-row");
    expect(at).toBeGreaterThan(-1);
    const row = src.slice(at, src.indexOf("Nothing learned yet", at));
    expect(row).toContain("title={l.detail}");
    expect(row).toContain("{l.label}");
    expect(row).toContain("{l.sub}");
    expect(row).toContain('{l.state === "used" ? "used" : "new"}');
    const close = row.indexOf("</a>");
    const call = row.indexOf("learnedDetailSentence(l.detail, l.label, l.sub)");
    expect(close).toBeGreaterThan(-1);
    expect(call).toBeGreaterThan(close);
    expect(row).toContain("<View id={l.id || l.label} sentence={learnedDetailSentence(l.detail, l.label, l.sub)}");
  });
});
