/**
 * A recent finished run that failed already puts r.error on the title only.
 * The first line of that error is also visible beside the name. Blank and
 * whitespace say nothing. Six words or fewer is a span; more is a paragraph.
 * The name and the existing title stay.
 *
 * Breaks if the sentence keeps a later line, skips the trim, draws the wrong
 * tag, drops the name, or the row stops calling recentRunErrorSentence(r.error).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { RecentFinishedRun, recentRunErrorSentence } from "../app/command-center/cards";
import type { RecentRun } from "../lib/command-center-view";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const NOW = Date.parse("2026-10-04T15:40:00Z");
let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

function run(over: Partial<RecentRun>): RecentRun {
  return {
    runId: "wfr_170",
    name: "Audit deps",
    workflowName: "Audit",
    outcome: "failed",
    finishedAt: "2026-10-04T12:40:00Z",
    error: null,
    ...over,
  };
}

async function mount(r: RecentRun): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<RecentFinishedRun r={r} now={NOW} orgId="org_1" />);
  });
  return host;
}

function row(el: HTMLElement): HTMLAnchorElement {
  const node = el.querySelector('[data-testid="cc2-recent-row"]');
  if (!node) throw new Error("recent row missing");
  return node as HTMLAnchorElement;
}

describe("recentRunErrorSentence", () => {
  it("is null when the error is blank", () => {
    expect(recentRunErrorSentence(null)).toBeNull();
    expect(recentRunErrorSentence(undefined)).toBeNull();
    expect(recentRunErrorSentence("")).toBeNull();
  });

  it("is null when the first line is whitespace", () => {
    expect(recentRunErrorSentence("   ")).toBeNull();
    expect(recentRunErrorSentence("\nsecond line")).toBeNull();
    expect(recentRunErrorSentence(" \t \n second line")).toBeNull();
  });

  it("returns the first line trimmed", () => {
    expect(recentRunErrorSentence("  disk full  ")).toBe("disk full");
    expect(recentRunErrorSentence("  disk full  \nstack frame at main")).toBe("disk full");
  });
});

describe("the recent finished run says its error", () => {
  it("shows six words or fewer beside the name, in a span", async () => {
    const error = "  one two three four five six  \nrest of the stack";
    const el = await mount(run({ error }));
    const link = row(el);
    const said = link.querySelector('[data-testid="cc2-recent-error"]');
    const name = link.querySelector("span[data-user-text]");
    expect(link.getAttribute("title")).toBe("Audit,   one two three four five six  ");
    expect(link.getAttribute("data-outcome")).toBe("failed");
    expect(name?.textContent).toBe("Audit deps");
    expect(name?.nextElementSibling).toBe(said);
    expect(said?.tagName).toBe("SPAN");
    expect(said?.textContent).toBe("one two three four five six");
    expect(said?.hasAttribute("data-user-text")).toBe(true);
    expect(link.querySelector("em")?.textContent).toBe("3h");
  });

  it("shows more than six words beside the name, in a paragraph", async () => {
    const error = "  one two three four five six seven  \nstack";
    const el = await mount(run({ error, outcome: "deadlettered" }));
    const link = row(el);
    const said = link.querySelector('[data-testid="cc2-recent-error"]');
    expect(link.getAttribute("title")).toBe("Audit,   one two three four five six seven  ");
    expect(link.querySelector("span[data-user-text]")?.textContent).toBe("Audit deps");
    expect(said?.tagName).toBe("P");
    expect(said?.textContent).toBe("one two three four five six seven");
    expect(said?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when the title does not include an error", async () => {
    const succeeded = await mount(run({
      outcome: "succeeded",
      error: "one two three four five six seven",
    }));
    const ok = row(succeeded);
    expect(ok.getAttribute("title")).toBe("Audit, succeeded");
    expect(ok.querySelector("span[data-user-text]")?.textContent).toBe("Audit deps");
    expect(ok.querySelector('[data-testid="cc2-recent-error"]')).toBeNull();
    expect(ok.querySelector("span[data-user-text]")?.nextElementSibling?.tagName).toBe("EM");
    act(() => root?.unmount());
    host?.remove();
    root = null;
    host = null;

    const blank = await mount(run({ error: " \t \nsecond line stays off" }));
    const failed = row(blank);
    expect(failed.getAttribute("title")).toBe("Audit,  \t ");
    expect(failed.querySelector("span[data-user-text]")?.textContent).toBe("Audit deps");
    expect(failed.querySelector('[data-testid="cc2-recent-error"]')).toBeNull();
  });

  it("calls recentRunErrorSentence with the run error", () => {
    const src = readFileSync(join(__dirname, "../app/command-center/cards.tsx"), "utf8");
    expect(src).toMatch(/recentRunErrorSentence\(r\.error\)/);
  });
});
