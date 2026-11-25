// @vitest-environment happy-dom
/**
 * The fix card says its confidence reasons beside the level, not only on the title.
 * Blank or whitespace reasons say nothing. Each reason is trimmed, then joined with ", ".
 * More than six words is a paragraph; six or fewer is a span.
 * The level and its title stay. The rejected-guess title stays.
 *
 * Breaks if the reasons stay title-only, padding survives, a blank reason still renders,
 * a long sentence is a span, the level is replaced, or the card stops calling
 * confidenceReasonsSentence(card.confidence?.reasons).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { SelfFixCard, confidenceReasonsSentence } from "../app/self-fix/approval-card";
import type { Card } from "../lib/self-fix";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SHORT = "probe down";
const SIX = "one two three, four five six";
const SEVEN = "one two three, four five six seven";

let root: Root | null = null;
let host: HTMLElement | null = null;

function fixCard(reasons: string[] | null | undefined, level = "high"): Card {
  return {
    self_fix_id: "sfx_1",
    title: "fix the port",
    tier: 2,
    flags: [],
    what_broke: "the port",
    why: "the holder stayed",
    rejected: [{ hypothesis: "fix the single-container retire step", why: "the holder was in the shared project" }],
    confidence: { level, reasons: reasons as string[] },
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

async function show(reasons: string[] | null | undefined, level = "high"): Promise<void> {
  await act(async () => { root?.unmount(); });
  host?.remove();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<SelfFixCard card={fixCard(reasons, level)} projectId="p1" />);
  });
}

function levelEl(): HTMLElement | null {
  return document.querySelector("[data-testid='sfx-card-confidence']");
}

function whyEl(): HTMLElement | null {
  return document.querySelector("[data-testid='sfx-card-confidence-why']");
}

describe("confidenceReasonsSentence", () => {
  it("joins trimmed reasons, and is nothing when they are blank or whitespace", () => {
    expect(confidenceReasonsSentence(["  probe down  "])).toBe("probe down");
    expect(confidenceReasonsSentence([`  ${SHORT}  `, "  again  "])).toBe("probe down, again");
    expect(confidenceReasonsSentence(["  one two three  ", "  four five six  "])).toBe(SIX);
    expect(confidenceReasonsSentence(["one two three", "four five six seven"])).toBe(SEVEN);
    expect(confidenceReasonsSentence(["  alpha  ", " ", "  beta  "])).toBe("alpha, beta");
    expect(confidenceReasonsSentence(null)).toBeNull();
    expect(confidenceReasonsSentence(undefined)).toBeNull();
    expect(confidenceReasonsSentence([])).toBeNull();
    expect(confidenceReasonsSentence([""])).toBeNull();
    expect(confidenceReasonsSentence(["  ", "\n\t"])).toBeNull();
  });
});

describe("the fix card says its confidence reasons", () => {
  it("shows more than six words as a paragraph and six or fewer as a span beside the level", async () => {
    await show(["  one two three  ", "  four five six seven  "]);
    const level = levelEl();
    expect(level?.textContent).toBe("high");
    expect(level?.getAttribute("title")).toBe("  one two three  ,   four five six seven  ");
    const seven = whyEl();
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven!.textContent!.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);
    expect(level?.contains(seven)).toBe(false);
    expect(seven?.parentElement).toBe(level?.parentElement);

    await show(["one two three", "four five six"]);
    const six = whyEl();
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six!.textContent!.split(/\s+/).filter(Boolean).length).toBe(6);
    expect(six?.hasAttribute("data-user-text")).toBe(true);
    expect(levelEl()?.textContent).toBe("high");
    expect(levelEl()?.getAttribute("title")).toBe("one two three, four five six");

    await show([`  ${SHORT}  `]);
    const short = whyEl();
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.hasAttribute("data-user-text")).toBe(true);
    expect(levelEl()?.textContent).toBe("high");
    expect(levelEl()?.getAttribute("title")).toBe(`  ${SHORT}  `);
    expect(levelEl()?.contains(short)).toBe(false);
  });

  it("says nothing when the reasons are blank and keeps the rejected-guess title", async () => {
    await show(["  ", "\n\t"]);
    expect(whyEl()).toBeNull();
    expect(levelEl()?.textContent).toBe("high");
    expect(levelEl()?.getAttribute("title")).toBe("  , \n\t");
    const rejected = document.querySelector("[data-testid='sfx-card-rejected']");
    expect(rejected?.getAttribute("title")).toBe("fix the single-container retire step, the holder was in the shared project");
    expect(rejected?.textContent).toContain("First guess rejected");
    expect(rejected?.textContent).not.toContain("fix the single-container retire step");

    await show([], "low");
    expect(whyEl()).toBeNull();
    expect(levelEl()?.textContent).toBe("low");
    expect(levelEl()?.getAttribute("title")).toBe("");

    await show(null, "medium");
    expect(whyEl()).toBeNull();
    expect(levelEl()?.textContent).toBe("medium");
    expect(levelEl()?.getAttribute("title")).toBe("");
  });

  it("calls confidenceReasonsSentence(card.confidence?.reasons) beside the level", () => {
    const src = codeOnly(join(__dirname, "../app/self-fix/approval-card.tsx"));
    const start = src.indexOf('data-testid="sfx-card-confidence"');
    expect(start).toBeGreaterThan(-1);
    const end = src.indexOf("<dt>Change</dt>", start);
    expect(end).toBeGreaterThan(start);
    const block = src.slice(start, end);
    expect(block).toContain("confidenceReasonsSentence(card.confidence?.reasons)");
    expect(block).toContain("<ConfidenceReasonsWhy sentence={confidenceReasonsSentence(card.confidence?.reasons)}");
    expect(block).toContain('title={(card.confidence?.reasons ?? []).join(", ")}');
    expect(block).toContain("card.confidence?.level");
    const whyFn = src.slice(src.indexOf("function ConfidenceReasonsWhy"), src.indexOf("export function SelfFixCard"));
    expect(whyFn).toContain('data-testid="sfx-card-confidence-why"');
    expect(whyFn).toContain("data-user-text");
    expect(whyFn).toContain("split(/\\s+/)");
    const rejectedAt = src.indexOf('data-testid="sfx-card-rejected"');
    expect(rejectedAt).toBeGreaterThan(-1);
    const rejected = src.slice(rejectedAt, src.indexOf("First guess rejected", rejectedAt));
    expect(rejected).toContain("title={rejected.map((r) => `${r.hypothesis ?? \"\"}${r.why ? `, ${r.why}` : \"\"}`).join(\"\\n\")}");
  });
});
