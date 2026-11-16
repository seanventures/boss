/**
 * The starter says its description, beside the name, not only on the title.
 * A blank description says nothing. The same words as the name say nothing.
 * More than six words is a paragraph. Six or fewer is a span.
 *
 * Breaks if the description stays title-only, padding survives, a blank or
 * repeated name still renders, a long description is a span, either call
 * site drops starterDescriptionSentence, or either title changes.
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

type SayFn = (
  description: string | null | undefined,
  name: string | null | undefined,
) => string | null;
type SayView = (props: { id: string; sentence: string | null }) => React.ReactNode;

const mod = await import("../app/agent-quick-create") as {
  starterDescriptionSentence?: SayFn;
  View?: SayView;
};
const starterDescriptionSentence = mod.starterDescriptionSentence;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const NAME = "QA Lead";
const DIFFERENT = "Files what broke";
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

function why(el: HTMLElement, id: string): Element | null {
  return el.querySelector(`[data-testid='${id}']`);
}

describe("starterDescriptionSentence", () => {
  it("is a function", () => {
    expect(typeof starterDescriptionSentence).toBe("function");
  });

  it("is the trimmed description when it is not the name, and nothing when blank or the same words", () => {
    expect(typeof starterDescriptionSentence).toBe("function");
    const say = starterDescriptionSentence!;
    expect(say(`  ${DIFFERENT}  `, NAME)).toBe(DIFFERENT);
    expect(say(SIX, NAME)).toBe(SIX);
    expect(say(SEVEN, NAME)).toBe(SEVEN);
    expect(say(SPACED, NAME)).toBe(SPACED);
    expect(say(null, NAME)).toBeNull();
    expect(say(undefined, NAME)).toBeNull();
    expect(say("", NAME)).toBeNull();
    expect(say(" \n\t ", NAME)).toBeNull();
    expect(say(`  ${NAME}  `, NAME)).toBeNull();
    expect(say(NAME, `  ${NAME}  `)).toBeNull();
    expect(say(NAME, NAME)).toBeNull();
    expect(say(null, null)).toBeNull();
    expect(say(undefined, undefined)).toBeNull();
  });
});

describe("the starter says its description", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    expect(typeof starterDescriptionSentence).toBe("function");
    const seven = why(await show("aq-orphan-why-k7", SEVEN), "aq-orphan-why-k7");
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);

    const six = why(await show("aq-template-why-t6", SIX), "aq-template-why-t6");
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six?.hasAttribute("data-user-text")).toBe(true);

    const spaced = why(await show("aq-orphan-why-ks", SPACED), "aq-orphan-why-ks");
    expect(spaced?.tagName).toBe("SPAN");
    expect(spaced?.textContent).toBe(SPACED);
    expect(spaced?.hasAttribute("data-user-text")).toBe(true);

    const different = why(await show("aq-template-why-td", DIFFERENT), "aq-template-why-td");
    expect(different?.tagName).toBe("SPAN");
    expect(different?.textContent).toBe(DIFFERENT);
    expect(different?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when the sentence is blank or only the name", async () => {
    expect(typeof starterDescriptionSentence).toBe("function");
    const say = starterDescriptionSentence!;
    const blank = await show("aq-orphan-why-blank", say(" \n\t ", NAME));
    expect(why(blank, "aq-orphan-why-blank")).toBeNull();
    expect(blank.textContent).toBe("");

    const same = await show("aq-template-why-same", say(`  ${NAME}  `, NAME));
    expect(why(same, "aq-template-why-same")).toBeNull();
    expect(same.textContent).toBe("");

    const missing = await show("aq-orphan-why-none", say(null, NAME));
    expect(why(missing, "aq-orphan-why-none")).toBeNull();
    expect(missing.textContent).toBe("");
  });

  it("calls starterDescriptionSentence beside both names and leaves both titles", () => {
    expect(typeof starterDescriptionSentence).toBe("function");
    const src = codeOnly(join(__dirname, "../app/agent-quick-create.tsx"));
    const list = src.slice(src.indexOf("aq-template-list"), src.indexOf("No match"));
    const orphanAt = list.indexOf("aq-orphan-${a.agent_key}");
    const templateAt = list.indexOf("aq-template-${t.id}");
    expect(orphanAt).toBeGreaterThan(-1);
    expect(templateAt).toBeGreaterThan(orphanAt);
    const orphan = list.slice(orphanAt, templateAt);
    const template = list.slice(templateAt);
    expect(orphan).toContain("title={a.description || undefined}");
    expect(orphan).toContain("{a.name}");
    expect(orphan).toContain("starterDescriptionSentence(a.description, a.name)");
    expect(orphan).toContain("<View id={`aq-orphan-why-${a.agent_key}`} sentence={starterDescriptionSentence(a.description, a.name)}");
    expect(template).toContain("title={t.description || undefined}");
    expect(template).toContain("{t.name}");
    expect(template).toContain("starterDescriptionSentence(t.description, t.name)");
    expect(template).toContain("<View id={`aq-template-why-${t.id}`} sentence={starterDescriptionSentence(t.description, t.name)}");
  });
});
