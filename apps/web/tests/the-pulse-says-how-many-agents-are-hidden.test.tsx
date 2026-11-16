/**
 * The pulse draws five most-active agents. When the count is higher, a line under that list
 * says how many are not on the page. Five or fewer say nothing. The count is every agent
 * the page was given, not the five it draws.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { PulseRow, pulseTopRestSentence } from "../app/command-center/pulse";
import type { Pulse } from "../lib/command-center-view";

function pulseWith(count: number): Pulse {
  return {
    weeks: 14,
    start: "2026-06-29",
    days: [{ date: "2026-09-03", n: 12 }],
    total: 12,
    busiest: { date: "2026-09-03", n: 12 },
    top: Array.from({ length: count }, (_, i) => ({ roleKey: `role_${i}`, n: count - i })),
    latest: [],
    lastActAt: null,
  };
}

function draw(count: number): HTMLElement {
  document.body.innerHTML = renderToStaticMarkup(
    <PulseRow pulse={pulseWith(count)} prsOpen={null} now={null} />,
  );
  return document.body;
}

describe("the pulse says how many agents are hidden", () => {
  it("says five are on the page and how many are not, when more than five were counted", () => {
    expect(pulseTopRestSentence(8)).toBe("5 agents are on this page. 3 more are not.");
    expect(pulseTopRestSentence(6)).toBe("5 agents are on this page. 1 more are not.");
  });

  it("says nothing when five or fewer were counted", () => {
    expect(pulseTopRestSentence(5)).toBeNull();
    expect(pulseTopRestSentence(4)).toBeNull();
    expect(pulseTopRestSentence(1)).toBeNull();
    expect(pulseTopRestSentence(0)).toBeNull();
  });

  it("draws that sentence in a paragraph under the five most-active agents", () => {
    const body = draw(8);
    const tops = body.querySelector("[data-testid='cc-tops']");
    expect(tops).not.toBeNull();
    expect(tops!.querySelectorAll(".cc-top-row").length).toBe(5);
    const note = tops!.nextElementSibling;
    expect(note?.tagName).toBe("P");
    expect(note?.textContent).toBe("5 agents are on this page. 3 more are not.");
  });

  it("draws no such paragraph when five or fewer are active", () => {
    const body = draw(5);
    expect(body.querySelectorAll(".cc-top-row").length).toBe(5);
    expect(body.textContent).not.toContain("more are not");
    expect(body.querySelector("[data-testid='cc-tops']")?.nextElementSibling?.tagName).not.toBe("P");
  });

  it("counts every agent the pulse was given, not the five it draws", () => {
    const src = readFileSync(join(__dirname, "../app/command-center/pulse.tsx"), "utf8");
    expect(src).toContain("pulseTopRestSentence(pulse.top.length)");
    expect(src).not.toContain("pulseTopRestSentence(top.length)");
  });
});
