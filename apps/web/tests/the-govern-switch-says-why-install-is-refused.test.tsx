/**
 * The govern switch says why install is refused, beside "Can't install here ⓘ",
 * not only on that span's title. A blank reason says nothing. More than six
 * words is a paragraph. Six or fewer is a span.
 *
 * Breaks if the reason stays padded, a blank reason still renders, a long
 * reason is a span, the Can't install sentence disappears, the Off ⓘ switch
 * why changes, or the tab stops calling installWhySentence(v.why_not).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

type WhyFn = (why: string | null | undefined) => string | null;
type WhyView = (props: { sentence: string | null }) => React.ReactNode;

const tab = await import("../app/govern/self-fix-tab") as {
  installWhySentence?: WhyFn;
  InstallWhy?: WhyView;
};
const installWhySentence = tab.installWhySentence;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "this install was not built from a git checkout, so it cannot change itself";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SPACED = "one  two  three  four  five  six";
const SHORT = "not a checkout";

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

async function show(sentence: string | null): Promise<HTMLElement> {
  unmountShown();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const InstallWhy = tab.InstallWhy!;
  await act(async () => {
    root!.render(<InstallWhy sentence={sentence} />);
  });
  return host;
}

function why(el: HTMLElement): Element | null {
  return el.querySelector("[data-testid='gv-selffix-cannot-why']");
}

describe("installWhySentence", () => {
  it("is the trimmed reason, or nothing when the reason is blank", () => {
    expect(installWhySentence!(`  ${LONG}  `)).toBe(LONG);
    expect(installWhySentence!(SHORT)).toBe(SHORT);
    expect(installWhySentence!(SIX)).toBe(SIX);
    expect(installWhySentence!(SEVEN)).toBe(SEVEN);
    expect(installWhySentence!(null)).toBeNull();
    expect(installWhySentence!(undefined)).toBeNull();
    expect(installWhySentence!("")).toBeNull();
    expect(installWhySentence!(" \n\t ")).toBeNull();
  });
});

describe("the govern switch says why install is refused", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    const long = why(await show(LONG));
    expect(long?.tagName).toBe("P");
    expect(long?.textContent).toBe(LONG);
    expect(long?.hasAttribute("data-user-text")).toBe(true);

    const seven = why(await show(SEVEN));
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);

    const spaced = why(await show(SPACED));
    expect(spaced?.tagName).toBe("SPAN");
    expect(spaced?.textContent).toBe(SPACED);
    expect(spaced?.hasAttribute("data-user-text")).toBe(true);

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

  it("calls installWhySentence with v.why_not and keeps the Can't install sentence", () => {
    const src = codeOnly(join(__dirname, "../app/govern/self-fix-tab.tsx"));
    expect(src).toContain("installWhySentence(v.why_not)");
    expect(src).toContain("<InstallWhy sentence={installWhySentence(v.why_not)}");
    expect(src).toContain('data-testid="gv-selffix-cannot" title={v.why_not}>Can&apos;t install here ⓘ');
    expect(src).toContain('data-testid="gv-selffix-why" title={v.switch.why}>Off ⓘ');
  });
});
