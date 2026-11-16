/**
 * The plugin says its description, beside the name, not only on the title.
 * Blank is nothing. The same words as the name are nothing. A different
 * description is the trimmed sentence. More than six words is a paragraph.
 * Six or fewer is a span. The Blocked line and pluginProblemSentence stay.
 *
 * Breaks if the description stays title-only, padding survives, a blank or
 * repeated name still renders, a long description is a span, the title
 * changes, or the blocked sentence is edited.
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

type SentenceFn = (
  description: string | null | undefined,
  name: string | null | undefined,
) => string | null;
type SentenceView = (props: { id: string; sentence: string | null }) => React.ReactNode;

const mod = await import("../app/infra/plugins") as {
  pluginDescriptionSentence?: SentenceFn;
  View?: SentenceView;
};
const pluginDescriptionSentence = mod.pluginDescriptionSentence;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const NAME = "PostgreSQL";
const DIFFERENT = "Ships with the install";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SPACED = "one  two  three  four  five  six";

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
  return el.querySelector(`[data-testid='inf-plugin-desc-${id}']`);
}

describe("pluginDescriptionSentence", () => {
  it("is the trimmed description when it differs from the name", () => {
    expect(typeof pluginDescriptionSentence).toBe("function");
    expect(pluginDescriptionSentence!(`  ${DIFFERENT}  `, NAME)).toBe(DIFFERENT);
    expect(pluginDescriptionSentence!(SIX, NAME)).toBe(SIX);
    expect(pluginDescriptionSentence!(SEVEN, NAME)).toBe(SEVEN);
    expect(pluginDescriptionSentence!(`  ${SEVEN}  `, `  ${NAME}  `)).toBe(SEVEN);
  });

  it("is nothing when the description is blank", () => {
    expect(typeof pluginDescriptionSentence).toBe("function");
    expect(pluginDescriptionSentence!(null, NAME)).toBeNull();
    expect(pluginDescriptionSentence!(undefined, NAME)).toBeNull();
    expect(pluginDescriptionSentence!("", NAME)).toBeNull();
    expect(pluginDescriptionSentence!("   ", NAME)).toBeNull();
    expect(pluginDescriptionSentence!(" \n\t ", NAME)).toBeNull();
  });

  it("is nothing when the description equals the trimmed name", () => {
    expect(typeof pluginDescriptionSentence).toBe("function");
    expect(pluginDescriptionSentence!(NAME, NAME)).toBeNull();
    expect(pluginDescriptionSentence!(`  ${NAME}  `, NAME)).toBeNull();
    expect(pluginDescriptionSentence!(NAME, `  ${NAME}  `)).toBeNull();
    expect(pluginDescriptionSentence!(" \n PostgreSQL \t ", " PostgreSQL ")).toBeNull();
  });
});

describe("the plugin says its description", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    expect(typeof mod.View).toBe("function");
    const seven = said(await show("plug-7", SEVEN), "plug-7");
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);
    expect(SEVEN.split(/\s+/).filter(Boolean).length).toBe(7);

    const six = said(await show("plug-6", SIX), "plug-6");
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six?.hasAttribute("data-user-text")).toBe(true);
    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);

    const spaced = said(await show("plug-spaced", SPACED), "plug-spaced");
    expect(spaced?.tagName).toBe("SPAN");
    expect(spaced?.textContent).toBe(SPACED);
    expect(spaced?.hasAttribute("data-user-text")).toBe(true);
    expect(SPACED.split(/\s+/).filter(Boolean).length).toBe(6);

    const different = said(await show("plug-diff", DIFFERENT), "plug-diff");
    expect(different?.tagName).toBe("SPAN");
    expect(different?.textContent).toBe(DIFFERENT);
    expect(different?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when there is no sentence", async () => {
    expect(typeof mod.View).toBe("function");
    const none = await show("plug-0", null);
    expect(said(none, "plug-0")).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls pluginDescriptionSentence beside the name and leaves pluginProblemSentence unchanged", () => {
    const src = codeOnly(join(__dirname, "../app/infra/plugins.tsx"));
    const nameAt = src.indexOf("title={p.description || undefined}");
    const faintAt = src.indexOf("inf-faint", nameAt);
    expect(nameAt).toBeGreaterThan(-1);
    expect(faintAt).toBeGreaterThan(nameAt);
    const nameCell = src.slice(nameAt, faintAt);
    expect(nameCell).toContain('<strong data-copy="data">{p.name}</strong>');
    expect(nameCell).toContain("<View id={p.id} sentence={pluginDescriptionSentence(p.description, p.name)}");
    expect(nameCell.indexOf("{p.name}")).toBeLessThan(nameCell.indexOf("pluginDescriptionSentence(p.description, p.name)"));
    expect(src).toContain("export function pluginProblemSentence(problems: readonly string[] | null | undefined): string | null {");
    expect(src).toContain("const kept = (problems ?? []).map((problem) => problem.trim()).filter(Boolean);");
    expect(src).toContain('return kept.length ? kept.join("; ") : null;');
    expect(src).toContain("pluginProblemSentence(p.problems)");
    expect(src).toContain('title={p.problems.join("\\n")}>Blocked</span>');
    expect(src).toContain('data-testid="inf-plugin-problems"');
  });
});
