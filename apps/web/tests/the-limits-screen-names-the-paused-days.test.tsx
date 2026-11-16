/**
 * The limits screen names the paused days beside the count.
 * Blanks are dropped. A line of more than six words is a paragraph.
 */
import { join } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { PausedDayNames, pausedDayNames } from "../app/govern/limits-screen";
import { codeOnly } from "./_helpers/source";

const SRC = codeOnly(join(__dirname, "../app/govern/limits-screen.tsx"));

function namesLine(groups: readonly (readonly string[])[]): HTMLElement | null {
  document.body.innerHTML = renderToStaticMarkup(<PausedDayNames groups={groups} />);
  return document.querySelector("[data-testid='gv-paused-day-names']");
}

describe("pausedDayNames", () => {
  it("keeps the first trimmed spelling, in the order the days were seen", () => {
    expect(pausedDayNames([
      ["  Mon ", "Tue"],
      ["Tue", "  Wed  "],
      ["Mon", "Thu"],
    ])).toEqual(["Mon", "Tue", "Wed", "Thu"]);
  });

  it("drops blanks and does not repeat a day", () => {
    expect(pausedDayNames([
      ["", "  ", "Mon"],
      ["\n", "Mon", " \t "],
      ["Tue", "Tue"],
    ])).toEqual(["Mon", "Tue"]);
  });

  it("is empty when every name is blank", () => {
    expect(pausedDayNames([[], ["", "   "], ["\n\t"]])).toEqual([]);
  });
});

describe("the limits screen names the paused days", () => {
  it("shows the unique names, unformatted, as a span through six words and a paragraph after that", () => {
    const short = namesLine([["  2026-01-02 ", "2026-01-02"], ["2026-01-03"]]);
    expect(short).not.toBeNull();
    expect(short!.tagName).toBe("SPAN");
    expect(short!.textContent).toBe("2026-01-02, 2026-01-03");
    expect(short!.hasAttribute("data-user-text")).toBe(true);
    expect(short!.textContent).not.toMatch(/January/);

    const six = namesLine([["one", "two", "three", "four", "five", "six"]]);
    expect(six!.tagName).toBe("SPAN");
    expect(six!.textContent).toBe("one, two, three, four, five, six");
    expect(six!.textContent!.split(/\s+/).filter(Boolean).length).toBe(6);

    const seven = namesLine([["one", "two", "three", "four", "five", "six", "seven"]]);
    expect(seven!.tagName).toBe("P");
    expect(seven!.textContent).toBe("one, two, three, four, five, six, seven");
    expect(seven!.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when the only names are blank", () => {
    expect(namesLine([[""], ["  ", "\n"]])).toBeNull();
    expect(namesLine([])).toBeNull();
  });

  it("calls pausedDayNames and leaves the count sentence beside the names", () => {
    expect(SRC).toContain("pausedDayNames(");
    expect(SRC).toContain('data-testid="gv-paused-days"');
    expect(SRC).toContain("With these limits, last month would have paused work on ${paused} day${paused === 1 ? \"\" : \"s\"}");
    expect(SRC).toContain("With these limits, last month would have paused work on no days");
    const at = SRC.indexOf('data-testid="gv-paused-days"');
    expect(SRC.slice(at, at + 700)).toContain("<PausedDayNames");
  });
});
