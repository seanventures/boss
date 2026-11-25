// @vitest-environment happy-dom
/**
 * The paused rule says why, beside the list switch, not only on its title.
 * A blank reason says nothing. More than six words is a paragraph. Six or
 * fewer is a span. The editor chip keeps the reason on its tip.
 *
 * Breaks if the reason stays title-only, padding survives, a blank reason
 * still renders, a long reason is a span, the switch title changes, or the
 * editor chip prints the reason as its text.
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

type WhyFn = (reason: string | null | undefined) => string | null;
type WhyView = (props: { id: string; sentence: string | null }) => React.ReactNode;

const rules = await import("../app/events/rules") as {
  pausedReasonSentence?: WhyFn;
  PausedReason?: WhyView;
};
const pausedReasonSentence = rules.pausedReasonSentence;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "this rule is paused because the channel is quiet until morning";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SPACED = "one  two  three  four  five  six";
const SHORT = "channel quiet";

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
  const PausedReason = rules.PausedReason!;
  await act(async () => {
    root!.render(<PausedReason id={id} sentence={sentence} />);
  });
  return host;
}

function why(el: HTMLElement, id: string): Element | null {
  return el.querySelector(`[data-testid='ev-rule-paused-why-${id}']`);
}

describe("pausedReasonSentence", () => {
  it("is the trimmed reason, or nothing when the reason is blank", () => {
    expect(typeof pausedReasonSentence).toBe("function");
    expect(pausedReasonSentence!(`  ${LONG}  `)).toBe(LONG);
    expect(pausedReasonSentence!(SHORT)).toBe(SHORT);
    expect(pausedReasonSentence!(SIX)).toBe(SIX);
    expect(pausedReasonSentence!(SEVEN)).toBe(SEVEN);
    expect(pausedReasonSentence!(null)).toBeNull();
    expect(pausedReasonSentence!(undefined)).toBeNull();
    expect(pausedReasonSentence!("")).toBeNull();
    expect(pausedReasonSentence!(" \n\t ")).toBeNull();
  });
});

describe("the paused rule says why", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    const long = why(await show("r1", LONG), "r1");
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

    const short = why(await show("r0", SHORT), "r0");
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when there is no sentence", async () => {
    const none = await show("r0", null);
    expect(why(none, "r0")).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls pausedReasonSentence beside the list switch and leaves the editor chip as a tip", () => {
    const src = codeOnly(join(__dirname, "../app/events/rules.tsx"));
    expect(src).toContain("pausedReasonSentence(r.paused_reason)");
    expect(src).toContain("<PausedReason id={r.id} sentence={pausedReasonSentence(r.paused_reason)}");
    expect(src).toContain('title={r.paused_reason || (r.on ? "On" : "Off")}');
    expect(src).toContain("data-testid={`ev-rule-switch-${r.id}`}");
    expect(src).toContain('{rule ? <Chip tone={rule.on ? "green" : "grey"} tip={rule.paused_reason || undefined}>{rule.on ? "On" : "Off"}</Chip> : null}');
  });
});
