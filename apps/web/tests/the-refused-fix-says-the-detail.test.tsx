/**
 * A refused fix already shows its refusal, underscores as spaces, in parentheses.
 * The title is the detail or, failing that, the refusal. The detail has to be
 * readable beside those words when it says something else. Blank is nothing.
 * The same words as the refusal, or as the refusal with underscores turned into
 * spaces, are nothing. More than six words is a paragraph. Six or fewer is a span.
 * The refusal words and the chip title stay.
 *
 * Breaks if padding survives, a blank detail still renders, the detail repeats
 * the refusal, a long detail is a span, the refusal words change, or the row
 * stops calling refusalDetailSentence(it.refusal_detail, it.refusal).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import * as fixes from "../app/self-fix/self-fixes-home";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { refusalDetailSentence } = fixes;

const LONG = "the budget gate refused a change to a protected path";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SIX_GAP = "one  two three four five six";
const SHORT = "needs a change to grants";

let root: Root | null = null;
let host: HTMLElement | null = null;

function removeHost(node: HTMLElement | null): void {
  node?.remove();
}

afterEach(() => {
  act(() => { root?.unmount(); });
  removeHost(host);
  root = null;
  host = null;
  document.body.innerHTML = "";
});

async function show(refusal: string, title: string, sentence: string | null): Promise<HTMLElement> {
  await act(async () => { root?.unmount(); });
  removeHost(host);
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const Note = fixes.RefusedFixNote!;
  await act(async () => {
    root!.render(<Note refusal={refusal} title={title} sentence={sentence} />);
  });
  return host;
}

function refusalSpan(scope: ParentNode): HTMLElement | null {
  return scope.querySelector("span.sfx-sub");
}

function whyEl(scope: ParentNode): HTMLElement | null {
  return scope.querySelector("[data-testid='sfx-row-refusal-why']");
}

describe("refusalDetailSentence", () => {
  it("is the trimmed detail when that says more than the refusal", () => {
    expect(refusalDetailSentence(`  ${LONG}  `, "protected_path")).toBe(LONG);
    expect(refusalDetailSentence(`  ${SHORT}  `, "protected_path")).toBe(SHORT);
    expect(refusalDetailSentence(`  ${SIX}  `, "budget_refused")).toBe(SIX);
    expect(refusalDetailSentence(SEVEN, "budget_refused")).toBe(SEVEN);
    expect(refusalDetailSentence("not  a bug", "not_a_bug")).toBe("not  a bug");
    expect(refusalDetailSentence(SHORT, null)).toBe(SHORT);
    expect(refusalDetailSentence(SHORT, undefined)).toBe(SHORT);
    expect(refusalDetailSentence("protected path", " protected_path")).toBe("protected path");
  });

  it("is nothing for blank, whitespace, the refusal, or the refusal with spaces", () => {
    expect(refusalDetailSentence("", "protected_path")).toBeNull();
    expect(refusalDetailSentence("   ", "protected_path")).toBeNull();
    expect(refusalDetailSentence(" \n\t ", "protected_path")).toBeNull();
    expect(refusalDetailSentence(null, "protected_path")).toBeNull();
    expect(refusalDetailSentence(undefined, "protected_path")).toBeNull();
    expect(refusalDetailSentence("   ", null)).toBeNull();
    expect(refusalDetailSentence(null, null)).toBeNull();
    expect(refusalDetailSentence("  protected_path  ", "protected_path")).toBeNull();
    expect(refusalDetailSentence("protected_path", "  protected_path  ")).toBeNull();
    expect(refusalDetailSentence("protected path", "protected_path")).toBeNull();
    expect(refusalDetailSentence("  protected path  ", "protected_path")).toBeNull();
    expect(refusalDetailSentence("not a bug", "not_a_bug")).toBeNull();
    expect(refusalDetailSentence("budget refused", "budget_refused")).toBeNull();
  });
});

describe("the refused fix says the detail", () => {
  it("shows more than six words as a paragraph and six or fewer as a span beside the refusal", async () => {
    const long = await show("protected_path", LONG, LONG);
    const longWhy = whyEl(long);
    const longWords = refusalSpan(long);
    expect(longWords?.textContent).toBe(" (protected path)");
    expect(longWords?.getAttribute("title")).toBe(LONG);
    expect(longWhy?.tagName).toBe("P");
    expect(longWhy?.textContent).toBe(LONG);
    expect(longWhy!.textContent!.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
    expect(longWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(longWords?.contains(longWhy)).toBe(false);
    expect(longWhy?.parentElement).toBe(longWords?.parentElement);

    const seven = await show("budget_refused", SEVEN, SEVEN);
    const sevenWhy = whyEl(seven);
    expect(sevenWhy?.tagName).toBe("P");
    expect(sevenWhy?.textContent).toBe(SEVEN);
    expect(sevenWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(refusalSpan(seven)?.textContent).toBe(" (budget refused)");
    expect(refusalSpan(seven)?.getAttribute("title")).toBe(SEVEN);

    const six = await show("budget_refused", SIX, SIX);
    const sixWhy = whyEl(six);
    expect(sixWhy?.tagName).toBe("SPAN");
    expect(sixWhy?.textContent).toBe(SIX);
    expect(sixWhy!.textContent!.split(/\s+/).filter(Boolean).length).toBe(6);
    expect(sixWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(refusalSpan(six)?.textContent).toBe(" (budget refused)");

    const gapped = await show("budget_refused", SIX_GAP, SIX_GAP);
    expect(whyEl(gapped)?.tagName).toBe("SPAN");
    expect(whyEl(gapped)?.textContent).toBe(SIX_GAP);
    expect(whyEl(gapped)!.textContent!.split(/\s+/).filter(Boolean).length).toBe(6);

    const short = await show("protected_path", SHORT, refusalDetailSentence(SHORT, "protected_path"));
    const shortWhy = whyEl(short);
    expect(shortWhy?.tagName).toBe("SPAN");
    expect(shortWhy?.textContent).toBe(SHORT);
    expect(shortWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(refusalSpan(short)?.textContent).toBe(" (protected path)");
    expect(refusalSpan(short)?.getAttribute("title")).toBe(SHORT);
    expect(short.textContent).toContain("(protected path)");
    expect(short.textContent).toContain(SHORT);
  });

  it("says nothing beside the refusal when the detail repeats it or is blank", async () => {
    const same = await show("protected_path", "protected_path", refusalDetailSentence("protected path", "protected_path"));
    expect(whyEl(same)).toBeNull();
    expect(refusalSpan(same)?.textContent).toBe(" (protected path)");
    expect(refusalSpan(same)?.getAttribute("title")).toBe("protected_path");
    expect(same.textContent).toContain("(protected path)");
    expect(same.textContent).not.toContain("sfx-row-refusal-why");

    const blank = await show("protected_path", "protected_path", refusalDetailSentence("  ", "protected_path"));
    expect(whyEl(blank)).toBeNull();
    expect(refusalSpan(blank)?.textContent).toBe(" (protected path)");
    expect(refusalSpan(blank)?.getAttribute("title")).toBe("protected_path");

    const none = await show("not_a_bug", "not_a_bug", null);
    expect(whyEl(none)).toBeNull();
    expect(refusalSpan(none)?.textContent).toBe(" (not a bug)");
    expect(none.textContent).toContain("not a bug");
  });

  it("calls refusalDetailSentence(it.refusal_detail, it.refusal) beside the refusal and leaves the chip title", () => {
    const src = codeOnly(join(__dirname, "../app/self-fix/self-fixes-home.tsx"));
    const chipAt = src.indexOf('data-testid="sfx-row-chip"');
    expect(chipAt).toBeGreaterThan(-1);
    const chip = src.slice(src.lastIndexOf("<td>", chipAt), chipAt + 40);
    expect(chip).toContain("title={CHIP_TONE[chip].tip}");
    expect(chip).not.toContain("refusal_detail");
    expect(chip).not.toContain("refusalDetailSentence");

    const openAt = src.indexOf('data-testid="sfx-row-open"');
    const upstreamAt = src.indexOf('data-testid="sfx-row-upstream"', openAt);
    expect(openAt).toBeGreaterThan(-1);
    expect(upstreamAt).toBeGreaterThan(openAt);
    const row = src.slice(openAt, upstreamAt);
    expect(row).toContain("title={it.line}");
    expect(row).toContain('chip === "Refused" && it.refusal');
    expect(row).toContain("refusalDetailSentence(it.refusal_detail, it.refusal)");
    expect(row).toContain("title={it.refusal_detail || it.refusal}");
    expect(row).toContain("refusal={it.refusal}");
    expect(row).not.toContain("CHIP_TONE");

    const noteAt = src.indexOf("function RefusedFixNote");
    const homeAt = src.indexOf("export function SelfFixesHome");
    expect(noteAt).toBeGreaterThan(-1);
    expect(homeAt).toBeGreaterThan(noteAt);
    const note = src.slice(noteAt, homeAt);
    expect(note).toContain('refusal.replace(/_/g, " ")');
    expect(note).toContain("title={title}");
    expect(note).toContain('data-testid="sfx-row-refusal-why"');
    expect(note).toContain("data-user-text");
    expect(note).toContain("split(/\\s+/)");
    expect(note).toContain(".filter(Boolean).length > 6");
  });
});
