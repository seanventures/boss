/**
 * The workforce says why it cannot reach the control plane, beside that sentence,
 * not only on the title. Blank or whitespace says nothing. More than six words is
 * a paragraph; six or fewer is a span. The sentence is
 * blockedReasonSentence(workforce.blockedReason). No retry.
 *
 * Breaks if the reason stays title-only, padding survives, a blank reason still
 * renders, a long sentence is a span, the control-plane sentence or its title is
 * replaced, a retry appears, or the screen stops calling
 * blockedReasonSentence(workforce.blockedReason).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import * as workforceUi from "../app/workforce-ui";
import { codeOnly } from "./_helpers/source";

const blockedReasonSentence = workforceUi.blockedReasonSentence;
const ControlPlaneUnreachable = workforceUi.ControlPlaneUnreachable!;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SRC = codeOnly(join(__dirname, "../app/workforce-ui.tsx"));

const SHORT = "gate timed out";
const SIX = "one two three four five six";
const SEVEN = "one two three four five six seven";

let root: Root | null = null;
let host: HTMLElement | null = null;

function clearHost(): void {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
}

afterEach(() => {
  clearHost();
  document.body.innerHTML = "";
});

async function show(reason: string | null | undefined): Promise<HTMLElement> {
  clearHost();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(
      <ControlPlaneUnreachable title={reason} sentence={blockedReasonSentence(reason)} />,
    );
  });
  return host;
}

function detailOf(hostEl: HTMLElement): HTMLElement {
  const detail = hostEl.querySelector<HTMLElement>("p.detail");
  expect(detail).toBeTruthy();
  return detail as HTMLElement;
}

function whyOf(hostEl: HTMLElement): HTMLElement | null {
  return hostEl.querySelector<HTMLElement>('[data-testid="wf-reach-why"]');
}

describe("blockedReasonSentence", () => {
  it("is the trimmed reason, or nothing when blank or whitespace", () => {
    expect(blockedReasonSentence(`  ${SEVEN}  `)).toBe(SEVEN);
    expect(blockedReasonSentence(`  ${SHORT}  `)).toBe(SHORT);
    expect(blockedReasonSentence(`  ${SIX}  `)).toBe(SIX);
    expect(blockedReasonSentence("  one   two  ")).toBe("one   two");
    expect(blockedReasonSentence("\n  gate timed out  \t")).toBe(SHORT);
    expect(blockedReasonSentence(null)).toBeNull();
    expect(blockedReasonSentence(undefined)).toBeNull();
    expect(blockedReasonSentence("")).toBeNull();
    expect(blockedReasonSentence(" ")).toBeNull();
    expect(blockedReasonSentence(" \n\t ")).toBeNull();
    expect(blockedReasonSentence("\n\t")).toBeNull();
  });
});

describe("the workforce says why it cannot reach", () => {
  it("shows a reason longer than six words as a paragraph beside the sentence", async () => {
    const shown = await show(`  ${SEVEN}  `);
    const detail = detailOf(shown);
    const why = whyOf(shown);
    expect(detail.textContent).toBe("Could not reach the control plane");
    expect(detail.getAttribute("title")).toBe(`  ${SEVEN}  `);
    expect(why?.tagName).toBe("P");
    expect(why?.textContent).toBe(SEVEN);
    expect(why!.textContent!.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
    expect(why?.hasAttribute("data-user-text")).toBe(true);
    expect(detail.contains(why)).toBe(false);
    expect(why?.parentElement).toBe(detail.parentElement);
    expect(detail.compareDocumentPosition(why!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(shown.querySelector("button")).toBeNull();
    expect(shown.textContent?.toLowerCase()).not.toContain("retry");
  });

  it("shows six words or fewer as a span and keeps the sentence and title", async () => {
    const six = await show(`  ${SIX}  `);
    const sixDetail = detailOf(six);
    const sixWhy = whyOf(six);
    expect(sixDetail.textContent).toBe("Could not reach the control plane");
    expect(sixDetail.getAttribute("title")).toBe(`  ${SIX}  `);
    expect(sixWhy?.tagName).toBe("SPAN");
    expect(sixWhy?.textContent).toBe(SIX);
    expect(sixWhy!.textContent!.split(/\s+/).filter(Boolean).length).toBe(6);
    expect(sixWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(sixDetail.contains(sixWhy)).toBe(false);
    expect(sixWhy?.parentElement).toBe(sixDetail.parentElement);

    const short = await show(SHORT);
    const shortDetail = detailOf(short);
    const shortWhy = whyOf(short);
    expect(shortDetail.textContent).toBe("Could not reach the control plane");
    expect(shortDetail.getAttribute("title")).toBe(SHORT);
    expect(shortWhy?.tagName).toBe("SPAN");
    expect(shortWhy?.textContent).toBe(SHORT);
    expect(shortWhy?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when the reason is blank and keeps the sentence and title", async () => {
    const blank = await show(" \n\t ");
    const blankDetail = detailOf(blank);
    expect(whyOf(blank)).toBeNull();
    expect(blankDetail.textContent).toBe("Could not reach the control plane");
    expect(blankDetail.getAttribute("title")).toBe(" \n\t ");
    expect(blank.textContent).toBe("Could not reach the control plane");
    expect(blank.querySelector("button")).toBeNull();

    const missing = await show(null);
    const missingDetail = detailOf(missing);
    expect(whyOf(missing)).toBeNull();
    expect(missingDetail.textContent).toBe("Could not reach the control plane");
    expect(missingDetail.hasAttribute("title")).toBe(false);
    expect(missing.textContent).toBe("Could not reach the control plane");

    const unset = await show(undefined);
    expect(whyOf(unset)).toBeNull();
    expect(detailOf(unset).textContent).toBe("Could not reach the control plane");
    expect(detailOf(unset).hasAttribute("title")).toBe(false);
  });

  it("calls blockedReasonSentence with the blocked reason", () => {
    const raw = readFileSync(join(__dirname, "../app/workforce-ui.tsx"), "utf8");
    const start = raw.indexOf("if (!workforce.apiReachable)");
    const end = raw.indexOf("const openMap", start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const block = raw.slice(start, end);
    expect(block).toContain("blockedReasonSentence(workforce.blockedReason)");
    expect(block).toContain("title={workforce.blockedReason}");
    expect(block).toContain("<ControlPlaneUnreachable");
    expect(block.toLowerCase()).not.toContain("retry");
    const code = SRC.slice(
      SRC.indexOf("function ControlPlaneUnreachable"),
      SRC.indexOf("export function WorkforceArea"),
    );
    expect(code).toContain("Could not reach the control plane");
    expect(code).toContain('className="detail"');
    expect(code).toContain('data-testid="wf-reach-why"');
    expect(code).toContain("data-user-text");
    expect(code).toContain('sentence.split(/\\s+/).filter(Boolean).length > 6');
    expect(code.toLowerCase()).not.toContain("retry");
    const call = SRC.slice(SRC.indexOf("if (!workforce.apiReachable)"), SRC.indexOf("const openMap"));
    expect(call).toContain("blockedReasonSentence(workforce.blockedReason)");
    expect(call).toContain("title={workforce.blockedReason}");
    expect(call).not.toContain("Could not reach the control plane");
  });
});
