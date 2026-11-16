/**
 * The fix card says the rejected guess beside "First guess rejected", not only on the title.
 * Blank or whitespace guesses say nothing. Each guess trims hypothesis and why, joins those
 * two with ", " when both exist, and joins guesses with "; ".
 * More than six words is a paragraph; six or fewer is a span.
 * The First guess rejected words and their title stay.
 *
 * Breaks if the guess stays title-only, padding survives, a blank guess still renders,
 * a long sentence is a span, the rejected words are replaced, or the card stops calling
 * rejectedGuessSentence(rejected).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { SelfFixCard } from "../app/self-fix/approval-card";
import type { Card } from "../lib/self-fix";
import { codeOnly } from "./_helpers/source";

import * as approvalCard from "../app/self-fix/approval-card";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const rejectedGuessSentence = (
  approvalCard as unknown as {
    rejectedGuessSentence: (
      rejected: ReadonlyArray<{ hypothesis?: string; why?: string }> | null | undefined,
    ) => string | null;
  }
).rejectedGuessSentence;

const SHORT = "probe down";
const SIX = "one two three, four five six";
const SEVEN = "one two three, four five six seven";
const JOINED = "alpha, beta; gamma";

let root: Root | null = null;
let host: HTMLElement | null = null;

function fixCard(rejected: Card["rejected"]): Card {
  return {
    self_fix_id: "sfx_1",
    title: "fix the port",
    tier: 2,
    flags: [],
    what_broke: "the port",
    why: "the holder stayed",
    rejected,
    confidence: { level: "high", reasons: ["probe down"] },
    change: { files: [], added: 0, removed: 0, host_effect: [] },
    tests: { new: [], red_on_base: true, green_with_fix: true, affected: 0, p0: 0, none_weakened: true },
    review: {},
    security_on_another_engine: false,
    review_note: "",
    cost_usd: 0,
    minutes: 0,
    engine: "",
    on_approve: "",
    undo: "",
    people: "",
    command: "",
  };
}

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

async function show(rejected: Card["rejected"]): Promise<void> {
  await act(async () => { root?.unmount(); });
  host?.remove();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<SelfFixCard card={fixCard(rejected)} projectId="p1" />);
  });
}

function rejectedEl(): HTMLElement | null {
  return document.querySelector("[data-testid='sfx-card-rejected']");
}

function whyEl(): HTMLElement | null {
  return document.querySelector("[data-testid='sfx-card-rejected-why']");
}

describe("rejectedGuessSentence", () => {
  it("trims hypothesis and why, joins both with a comma, joins guesses with a semicolon, and is nothing when blank", () => {
    expect(rejectedGuessSentence([{ hypothesis: "  probe down  ", why: "  the holder stayed  " }])).toBe("probe down, the holder stayed");
    expect(rejectedGuessSentence([{ hypothesis: `  ${SHORT}  `, why: "" }])).toBe(SHORT);
    expect(rejectedGuessSentence([{ hypothesis: "  ", why: "  kept  " }])).toBe("kept");
    expect(rejectedGuessSentence([{ hypothesis: "one two three", why: "four five six" }])).toBe(SIX);
    expect(rejectedGuessSentence([{ hypothesis: "one two three", why: "four five six seven" }])).toBe(SEVEN);
    expect(rejectedGuessSentence([
      { hypothesis: "  alpha  ", why: "  beta  " },
      { hypothesis: "   ", why: "  " },
      { hypothesis: "gamma" },
    ])).toBe(JOINED);
    expect(rejectedGuessSentence(null)).toBeNull();
    expect(rejectedGuessSentence(undefined)).toBeNull();
    expect(rejectedGuessSentence([])).toBeNull();
    expect(rejectedGuessSentence([{ hypothesis: "", why: "" }])).toBeNull();
    expect(rejectedGuessSentence([{ hypothesis: "  ", why: "\n\t" }])).toBeNull();
    expect(rejectedGuessSentence([{ hypothesis: "   " }, { why: " \n " }])).toBeNull();
  });
});

describe("the fix card says the rejected guess", () => {
  it("shows more than six words as a paragraph and six or fewer as a span beside the rejected words", async () => {
    await show([
      { hypothesis: "  fix the port  ", why: "  the holder stayed  " },
      { hypothesis: "one two", why: "three four five six seven" },
    ]);
    const badge = rejectedEl();
    const seven = whyEl();
    expect(badge?.textContent).toContain("First guess rejected");
    expect(badge?.textContent).not.toContain("fix the port");
    expect(badge?.getAttribute("title")).toBe("  fix the port  ,   the holder stayed  \none two, three four five six seven");
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe("fix the port, the holder stayed; one two, three four five six seven");
    expect(seven!.textContent!.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);
    expect(badge?.contains(seven)).toBe(false);
    expect(seven?.parentElement).toBe(badge?.parentElement);

    await show([{ hypothesis: "one two three", why: "four five six" }]);
    const six = whyEl();
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six!.textContent!.split(/\s+/).filter(Boolean).length).toBe(6);
    expect(six?.hasAttribute("data-user-text")).toBe(true);
    expect(rejectedEl()?.textContent).toContain("First guess rejected");
    expect(rejectedEl()?.getAttribute("title")).toBe("one two three, four five six");
    expect(rejectedEl()?.contains(six)).toBe(false);

    await show([{ hypothesis: `  ${SHORT}  `, why: "" }]);
    const short = whyEl();
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.hasAttribute("data-user-text")).toBe(true);
    expect(rejectedEl()?.textContent).toContain("First guess rejected");
    expect(rejectedEl()?.getAttribute("title")).toBe(`  ${SHORT}  `);
    expect(rejectedEl()?.contains(short)).toBe(false);
    expect(short?.parentElement).toBe(rejectedEl()?.parentElement);
  });

  it("says nothing when the guess is blank and keeps the rejected words", async () => {
    await show([{ hypothesis: "  ", why: "\n\t" }]);
    expect(whyEl()).toBeNull();
    const blank = rejectedEl();
    expect(blank?.textContent).toContain("First guess rejected");
    expect(blank?.getAttribute("title")).toBe("  , \n\t");
    expect(document.querySelector("[data-testid='sfx-card-why']")?.textContent).toContain("the holder stayed");

    await show([]);
    expect(whyEl()).toBeNull();
    expect(rejectedEl()).toBeNull();
    expect(document.querySelector("[data-testid='sfx-card-why']")?.textContent).toContain("the holder stayed");
    expect(document.querySelector("[data-testid='sfx-card-why']")?.textContent).not.toContain("First guess rejected");
  });

  it("calls rejectedGuessSentence(rejected) beside the rejected guess", () => {
    const src = codeOnly(join(__dirname, "../app/self-fix/approval-card.tsx"));
    const start = src.indexOf('data-testid="sfx-card-rejected"');
    expect(start).toBeGreaterThan(-1);
    const end = src.indexOf("<dt>Confidence</dt>", start);
    expect(end).toBeGreaterThan(start);
    const block = src.slice(start, end);
    expect(block).toContain("First guess rejected");
    expect(block).toContain("rejectedGuessSentence(rejected)");
    expect(block).toContain("<RejectedGuessWhy sentence={rejectedGuessSentence(rejected)}");
    expect(block).toContain('title={rejected.map((r) => `${r.hypothesis ?? ""}${r.why ? `, ${r.why}` : ""}`).join("\\n")}');
    const whyFn = src.slice(src.indexOf("function RejectedGuessWhy"), src.indexOf("export function SelfFixCard"));
    expect(whyFn).toContain('data-testid="sfx-card-rejected-why"');
    expect(whyFn).toContain("data-user-text");
    expect(whyFn).toContain("split(/\\s+/)");
    const confidence = src.slice(src.indexOf("export function confidenceReasonsSentence"), src.indexOf("function ConfidenceReasonsWhy"));
    expect(confidence).toContain('kept.join(", ")');
    expect(confidence).not.toContain("rejectedGuessSentence");
  });
});
