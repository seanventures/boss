// @vitest-environment happy-dom
/**
 * The alert says what it is about, beside the label span, not only on its title.
 * The rule button stays a button. Blank is nothing. Description wins unless it
 * repeats the label, then the summary, unless that repeats the label too.
 * More than six words is a paragraph. Six or fewer is a span.
 *
 * Breaks if the sentence stays title-only, the rule button grows one, a blank
 * or repeated line still renders, description loses to summary, a long line is
 * a span, or the title changes.
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

type WhatFn = (
  description: string | null | undefined,
  summary: string | null | undefined,
  label: string | null | undefined,
) => string | null;
type WhatView = (props: { id: string; sentence: string | null }) => React.ReactNode;

const mod = await import("../app/observability/alerts-tab") as {
  alertWhatSentence?: WhatFn;
  View?: WhatView;
};
const alertWhatSentence = mod.alertWhatSentence;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SPACED = "one  two  three  four  five  six";
const LABEL = "Error rate";
const DESC = "the disk filled up overnight";
const SUMMARY = "the queue backed up after midnight";

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
  return el.querySelector(`[data-testid='obs-alert-why-${id}']`);
}

describe("alertWhatSentence", () => {
  it("prefers a description that is not the label, else a different summary, else nothing", () => {
    expect(typeof alertWhatSentence).toBe("function");
    expect(alertWhatSentence!(`  ${DESC}  `, SUMMARY, LABEL)).toBe(DESC);
    expect(alertWhatSentence!(DESC, SUMMARY, LABEL)).toBe(DESC);
    expect(alertWhatSentence!("disk full", SEVEN, LABEL)).toBe("disk full");
    expect(alertWhatSentence!(SEVEN, SIX, LABEL)).toBe(SEVEN);
    expect(alertWhatSentence!(SIX, SEVEN, LABEL)).toBe(SIX);
    expect(alertWhatSentence!(LABEL, `  ${SUMMARY}  `, `  ${LABEL}  `)).toBe(SUMMARY);
    expect(alertWhatSentence!(`  ${LABEL}  `, SUMMARY, LABEL)).toBe(SUMMARY);
    expect(alertWhatSentence!("", SUMMARY, LABEL)).toBe(SUMMARY);
    expect(alertWhatSentence!(null, SUMMARY, LABEL)).toBe(SUMMARY);
    expect(alertWhatSentence!(undefined, SEVEN, LABEL)).toBe(SEVEN);
    expect(alertWhatSentence!("   ", SIX, LABEL)).toBe(SIX);
    expect(alertWhatSentence!(LABEL, LABEL, LABEL)).toBeNull();
    expect(alertWhatSentence!("", `  ${LABEL}  `, LABEL)).toBeNull();
    expect(alertWhatSentence!(null, LABEL, LABEL)).toBeNull();
    expect(alertWhatSentence!(LABEL, "", LABEL)).toBeNull();
    expect(alertWhatSentence!(LABEL, "   ", LABEL)).toBeNull();
    expect(alertWhatSentence!("   ", `  ${LABEL}  `, LABEL)).toBeNull();
    expect(alertWhatSentence!(null, null, LABEL)).toBeNull();
    expect(alertWhatSentence!(undefined, undefined, LABEL)).toBeNull();
    expect(alertWhatSentence!("", "", LABEL)).toBeNull();
    expect(alertWhatSentence!(" \n\t ", " \n\t ", LABEL)).toBeNull();
    expect(alertWhatSentence!(SEVEN, SEVEN, SEVEN)).toBeNull();
    expect(alertWhatSentence!(SIX, SIX, SIX)).toBeNull();
  });
});

describe("the alert says what it is about", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    expect(typeof mod.View).toBe("function");
    const seven = why(await show("a7", SEVEN), "a7");
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);

    const six = why(await show("a6", SIX), "a6");
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six?.hasAttribute("data-user-text")).toBe(true);

    const spaced = why(await show("as", SPACED), "as");
    expect(spaced?.tagName).toBe("SPAN");
    expect(spaced?.textContent).toBe(SPACED);
    expect(spaced?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when there is no sentence", async () => {
    expect(typeof mod.View).toBe("function");
    const none = await show("a0", null);
    expect(why(none, "a0")).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls alertWhatSentence beside the label span, not the rule button, and keeps the title", () => {
    const src = codeOnly(join(__dirname, "../app/observability/alerts-tab.tsx"));
    const at = src.indexOf("title={a.description ?? a.summary}");
    expect(at).toBeGreaterThan(-1);
    const open = src.lastIndexOf("<span", at);
    const close = src.indexOf("</span>", at);
    const span = src.slice(open, close);
    expect(span).toContain("{a.label}");
    expect(span).not.toContain("alertWhatSentence");
    expect(span).not.toContain("<View");
    expect(src.slice(close, close + 240)).toContain(
      "<View id={a.id} sentence={alertWhatSentence(a.description, a.summary, a.label)}",
    );
    const viewAt = src.indexOf("<View id={a.id} sentence={alertWhatSentence(a.description, a.summary, a.label)}");
    const lineAt = src.lastIndexOf("className=\"obs-line\"", viewAt);
    expect(src.slice(lineAt - 5, lineAt)).toBe("<div ");

    const ruleAt = src.indexOf('data-testid="obs-alert-rule"');
    const ruleOpen = src.lastIndexOf("<button", ruleAt);
    const ruleEnd = src.indexOf("</button>", ruleAt);
    const rule = src.slice(ruleOpen, ruleEnd);
    expect(rule).not.toContain("alertWhatSentence");
    expect(rule).not.toContain("<View");
    expect(src).toContain("title={`${a.alertname}: its rule (Respond automatically, Triage, Silence, Edit)`}");
  });
});
