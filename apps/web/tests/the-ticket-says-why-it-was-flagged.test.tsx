// @vitest-environment happy-dom
/**
 * The ticket says why it was flagged. The flag sentence stays. The reasons
 * that were only on the untrusted note title are visible beside it. Blank
 * reasons say nothing. More than six words is a paragraph. Six or fewer is
 * a span.
 *
 * Breaks if a padded reason stays padded, a blank reason still renders, a
 * long reason is a span, the flag sentence disappears, or the untrusted
 * note stops calling ticketFlagSentence(t.screen.reasons).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { ticketFlagSentence, TicketFlagWhy } from "../app/tickets/ticket-parts";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "the ticket text tells the agent to ignore its rules";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "prompt injection";
const FLAG = "BOSS flagged text in this ticket. Agents treat it as outside input only.";

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

async function show(sentence: string | null, testid = "tk-card"): Promise<HTMLElement> {
  await act(async () => { root?.unmount(); });
  host?.remove();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<TicketFlagWhy sentence={sentence} testid={testid} />);
  });
  return host;
}

function why(el: HTMLElement): Element | null {
  return el.querySelector("[data-testid$='-flag-why']");
}

function untrustedBranch(src: string): string {
  const start = src.indexOf("t.untrusted");
  expect(start).toBeGreaterThan(-1);
  const end = src.indexOf("children", start);
  expect(end).toBeGreaterThan(start);
  return src.slice(start, end);
}

describe("ticketFlagSentence", () => {
  it("joins trimmed reasons with a semicolon, or nothing when they are blank", () => {
    expect(ticketFlagSentence([`  ${LONG}  `, "   ", `  ${SHORT}  `])).toBe(`${LONG}; ${SHORT}`);
    expect(ticketFlagSentence([`  ${SHORT}  `])).toBe(SHORT);
    expect(ticketFlagSentence([SIX])).toBe(SIX);
    expect(ticketFlagSentence([SEVEN])).toBe(SEVEN);
    expect(ticketFlagSentence([" \n\t ", ""])).toBeNull();
    expect(ticketFlagSentence([])).toBeNull();
    expect(ticketFlagSentence(null)).toBeNull();
    expect(ticketFlagSentence(undefined)).toBeNull();
  });
});

describe("the ticket says why it was flagged", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    const long = why(await show(LONG));
    expect(long?.tagName).toBe("P");
    expect(long?.textContent).toBe(LONG);
    expect(long?.getAttribute("data-testid")).toBe("tk-card-flag-why");
    expect(long?.hasAttribute("data-user-text")).toBe(true);

    const seven = why(await show(SEVEN));
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);

    const six = why(await show(SIX));
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six?.hasAttribute("data-user-text")).toBe(true);

    const short = why(await show(SHORT, "lend-card"));
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.getAttribute("data-testid")).toBe("lend-card-flag-why");
    expect(short?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when there is no sentence", async () => {
    const none = await show(null);
    expect(why(none)).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls ticketFlagSentence(t.screen.reasons) beside the flag sentence", () => {
    const src = codeOnly(join(__dirname, "../app/tickets/ticket-parts.tsx"));
    const branch = untrustedBranch(src);
    expect(branch).toContain("ticketFlagSentence(t.screen.reasons)");
    expect(branch).toContain("sentence={ticketFlagSentence(t.screen.reasons)}");
    expect(branch).toContain("testid={testid}");
    expect(branch).toContain('title={t.screen.reasons.join("; ") || undefined}');
    expect(branch).toContain(FLAG);
    expect(branch).toContain('data-testid={`${testid}-untrusted`}');
  });
});
