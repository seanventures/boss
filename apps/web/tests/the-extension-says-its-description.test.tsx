// @vitest-environment happy-dom
/**
 * The extension says its description beside the name, not only on the title.
 * A blank description says nothing. A description that only repeats the name
 * says nothing. More than six words is a paragraph. Six or fewer is a span.
 * The tools title stays a tip. The blocked-problem sentence stays where it is.
 *
 * Breaks if the description stays title-only, padding survives, a blank or
 * name-only description still renders, a long description is a span, the
 * name cell title changes, the tools title line changes, or the row stops
 * calling extensionDescriptionSentence(e.description, e.name).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

type WhyFn = (
  description: string | null | undefined,
  name: string | null | undefined,
) => string | null;
type WhyView = (props: { id: string; sentence: string | null }) => React.ReactNode;

const mod = await import("../app/infra/extensions") as {
  extensionDescriptionSentence?: WhyFn;
  View?: WhyView;
};
const extensionDescriptionSentence = mod.extensionDescriptionSentence;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "reads the deploy notes and posts them on the project";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SPACED = "one  two  three  four  five  six";
const SHORT = "deploy notes";
const NAME = "Deploy notes";

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

function said(el: HTMLElement, id: string): Element | null {
  return el.querySelector(`[data-testid='inf-ext-desc-${id}']`);
}

describe("extensionDescriptionSentence", () => {
  it("is the trimmed description when it says more than the name", () => {
    expect(typeof extensionDescriptionSentence).toBe("function");
    expect(extensionDescriptionSentence!(`  ${LONG}  `, NAME)).toBe(LONG);
    expect(extensionDescriptionSentence!(SHORT, NAME)).toBe(SHORT);
    expect(extensionDescriptionSentence!(SIX, NAME)).toBe(SIX);
    expect(extensionDescriptionSentence!(SEVEN, NAME)).toBe(SEVEN);
    expect(extensionDescriptionSentence!(`  ${SPACED}  `, NAME)).toBe(SPACED);
    expect(extensionDescriptionSentence!("notes", "  Notes  ")).toBe("notes");
    expect(extensionDescriptionSentence!("reads notes", "")).toBe("reads notes");
    expect(extensionDescriptionSentence!("reads notes", "   ")).toBe("reads notes");
    expect(extensionDescriptionSentence!("reads notes", null)).toBe("reads notes");
    expect(extensionDescriptionSentence!("reads notes", undefined)).toBe("reads notes");
  });

  it("is nothing when the description is blank", () => {
    expect(typeof extensionDescriptionSentence).toBe("function");
    expect(extensionDescriptionSentence!(null, NAME)).toBeNull();
    expect(extensionDescriptionSentence!(undefined, NAME)).toBeNull();
    expect(extensionDescriptionSentence!("", NAME)).toBeNull();
    expect(extensionDescriptionSentence!(" \n\t ", NAME)).toBeNull();
    expect(extensionDescriptionSentence!("  \n  ", "")).toBeNull();
  });

  it("is nothing when the description only repeats the name", () => {
    expect(typeof extensionDescriptionSentence).toBe("function");
    expect(extensionDescriptionSentence!(NAME, NAME)).toBeNull();
    expect(extensionDescriptionSentence!(`  ${NAME}  `, NAME)).toBeNull();
    expect(extensionDescriptionSentence!(NAME, `  ${NAME}  `)).toBeNull();
    expect(extensionDescriptionSentence!(`\n${NAME}\t`, `  ${NAME}  `)).toBeNull();
    expect(extensionDescriptionSentence!(SIX, SIX)).toBeNull();
    expect(extensionDescriptionSentence!(`  ${SEVEN}  `, SEVEN)).toBeNull();
  });
});

describe("the extension says its description", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    expect(typeof mod.View).toBe("function");
    const long = said(await show("e1", LONG), "e1");
    expect(long?.tagName).toBe("P");
    expect(long?.textContent).toBe(LONG);
    expect(long?.hasAttribute("data-user-text")).toBe(true);
    expect(LONG.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);

    const seven = said(await show("e7", SEVEN), "e7");
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);
    expect(SEVEN.split(/\s+/).filter(Boolean).length).toBe(7);

    const spaced = said(await show("es", SPACED), "es");
    expect(spaced?.tagName).toBe("SPAN");
    expect(spaced?.textContent).toBe(SPACED);
    expect(spaced?.hasAttribute("data-user-text")).toBe(true);
    expect(SPACED.split(/\s+/).filter(Boolean).length).toBe(6);

    const six = said(await show("e6", SIX), "e6");
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six?.hasAttribute("data-user-text")).toBe(true);
    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);

    const short = said(await show("e0", SHORT), "e0");
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.hasAttribute("data-user-text")).toBe(true);
    expect(short?.querySelector("button, a")).toBeNull();
  });

  it("says nothing when there is no sentence", async () => {
    expect(typeof mod.View).toBe("function");
    const none = await show("e0", null);
    expect(said(none, "e0")).toBeNull();
    expect(none.textContent).toBe("");
    expect(none.querySelector("p, span, button, a")).toBeNull();
  });

  it("calls extensionDescriptionSentence beside the name and leaves the tools title", () => {
    const src = codeOnly(join(__dirname, "../app/infra/extensions.tsx"));
    const nameAt = src.indexOf('<div title={e.description || undefined}><strong data-copy="data">{e.name}</strong></div>');
    const idAt = src.indexOf("{e.id}, {e.version}");
    const cell = src.slice(nameAt, idAt);
    expect(nameAt).toBeGreaterThan(-1);
    expect(cell).toContain("<View id={e.id} sentence={extensionDescriptionSentence(e.description, e.name)}");
    expect(cell.indexOf("{e.name}")).toBeLessThan(cell.indexOf("<View"));
    expect(cell).not.toContain("extensionProblemSentence");
    expect(cell).not.toContain("e.tools");
    expect(src).toContain('title={e.tools.map((t) => `${t.name}: ${tierWords(t.riskTier)}`).join("\\n")}');
    expect(src).toContain("Tools: {e.tools.map((t) => t.name).join(\", \")}");
    expect(src).toContain("<ExtensionProblems sentence={extensionProblemSentence(e.trust.problems)} />");
    const viewSrc = src.slice(src.indexOf("function View"), src.indexOf("function ExtensionRowView"));
    expect(viewSrc).toContain("inf-ext-desc-");
    expect(viewSrc).toContain("data-user-text");
    expect(viewSrc).not.toContain("button");
    expect(viewSrc).not.toContain("retry");
    expect(viewSrc).not.toContain("route");
    expect(viewSrc).not.toContain("receipt");
    expect(viewSrc).not.toContain("reservation");
    expect(viewSrc).not.toContain("fetch");
  });
});
