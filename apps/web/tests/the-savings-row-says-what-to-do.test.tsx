/**
 * A savings row says the action, not only in the title.
 * A blank action says nothing. A sentence over six words is a paragraph.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { tip14 } from "../lib/govern";
import { fakeGovernApi, newGovern } from "./_helpers/govern-fixtures";
import { $, mount, unmount } from "./_helpers/live-render";
import { codeOnly } from "./_helpers/source";
import { join } from "node:path";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { SpendTab, savingsActionSentence } = await import("../app/govern/spend-tab");

const SRC = codeOnly(join(__dirname, "../app/govern/spend-tab.tsx"));

const SHORT = "Raise the cache ratio now";
const SIX = "Turn caching on for this project";
const SEVEN = "Turn prompt caching on for this project";
const LONG = "Cache hit ratio is twelve percent across a great many tokens in the last seven days of work by every agent";

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

function spendWith(savings: { kind: string; finding: string; action?: string }[]) {
  const g = newGovern();
  g.spend = {
    window: "7d", money_usd_micros: 3_000_000, included_usd_micros: 0,
    daily: [{ day: "2026-09-01", money_usd_micros: 3_000_000, included_usd_micros: 0 }],
    daily_cap_usd_micros: 10_000_000,
    scorecard: { calls: 9 },
    savings: savings.map((x) => ({ severity: "info", ...x })),
    top_drivers: [],
    by_agent: [],
    by_model: [],
    spent_vs_cap: { committed_usd_micros: 3_000_000, cap_usd_micros: 10_000_000, state: "green", state_label: "Under cap" },
  };
  return g;
}

async function show(savings: { kind: string; finding: string; action?: string }[]) {
  vi.stubGlobal("fetch", fakeGovernApi(spendWith(savings)));
  await mount(<SpendTab projectId="p1" paused={false} />);
  const card = $("[data-testid=gv-savings]");
  expect(card).not.toBeNull();
  return card as HTMLElement;
}

function row(card: HTMLElement, finding: string): HTMLLIElement {
  const found = [...card.querySelectorAll("li")].find((li) => li.textContent?.includes(finding));
  expect(found, finding).toBeTruthy();
  return found as HTMLLIElement;
}

function sentenceEl(li: HTMLElement, sentence: string): Element | undefined {
  return [...li.querySelectorAll("p, span")].find((el) => el.textContent === sentence);
}

describe("the savings row says what to do", () => {
  it("shows the action as a sentence, a paragraph when it is longer than six words", async () => {
    const card = await show([
      { kind: "short", finding: "Short finding", action: `  ${SHORT}  ` },
      { kind: "six", finding: "Six finding", action: SIX },
      { kind: "seven", finding: "Seven finding", action: SEVEN },
      { kind: "long", finding: "Long finding", action: LONG },
      { kind: "blank", finding: "Blank finding", action: "   " },
      { kind: "none", finding: "None finding" },
    ]);
    const short = tip14(SHORT)!;
    const six = tip14(SIX)!;
    const seven = tip14(SEVEN)!;
    const long = tip14(LONG)!;
    const shortRow = row(card, "Short finding");
    const sixRow = row(card, "Six finding");
    const sevenRow = row(card, "Seven finding");
    const longRow = row(card, "Long finding");
    expect(shortRow.textContent).toContain(short);
    expect(sentenceEl(shortRow, short)?.tagName).toBe("SPAN");
    expect(short.split(/\s+/).filter(Boolean).length).toBeLessThanOrEqual(6);
    expect(sixRow.textContent).toContain(six);
    expect(sentenceEl(sixRow, six)?.tagName).toBe("SPAN");
    expect(sevenRow.textContent).toContain(seven);
    expect(sentenceEl(sevenRow, seven)?.tagName).toBe("P");
    expect(longRow.textContent).toContain(long);
    expect(sentenceEl(longRow, long)?.tagName).toBe("P");
    expect(long.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
    expect(shortRow.textContent).toContain("What to do");
    expect(row(card, "Blank finding").textContent ?? "").not.toContain(short);
    expect(sentenceEl(row(card, "Blank finding"), short)).toBeUndefined();
    expect([...row(card, "Blank finding").querySelectorAll("p")]).toEqual([]);
    expect(row(card, "None finding").textContent ?? "").not.toContain("What to do");
    expect([...row(card, "None finding").querySelectorAll("p")]).toEqual([]);
  });

  it("calls savingsActionSentence with the action", () => {
    expect(SRC).toContain("savingsActionSentence(x.action)");
  });
});

describe("savingsActionSentence", () => {
  it("is tip14 of a trimmed action, or nothing", () => {
    expect(savingsActionSentence(`  ${SHORT}  `)).toBe(tip14(`  ${SHORT}  `));
    expect(savingsActionSentence(LONG)).toBe(tip14(LONG));
    expect(savingsActionSentence(SIX)).toBe(SIX);
    expect(savingsActionSentence(null)).toBeNull();
    expect(savingsActionSentence(undefined)).toBeNull();
    expect(savingsActionSentence("")).toBeNull();
    expect(savingsActionSentence(" \n\t ")).toBeNull();
  });
});
