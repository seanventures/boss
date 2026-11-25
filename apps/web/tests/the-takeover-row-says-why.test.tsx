// @vitest-environment happy-dom
/**
 * The takeover row says why, beside the title, not only on the title attribute.
 * A blank or whitespace question and reason say nothing. A question that repeats
 * the visible title falls through to the reason. More than six words is a paragraph.
 *
 * Breaks if the question stays padded, a duplicate of the title still renders,
 * a long reason is a span, or the row stops calling
 * takeoverWhySentence(offer.title || offer.run_id, offer.question, offer.reason).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { TakeoverQueue, takeoverWhySentence } from "../app/agents/org-chain";
import type { TakeoverOffer } from "../lib/v2/workforce";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "the captain needs a decision before the build resumes";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "Which API?";

const names = new Map<string, string>([
  ["qa", "QA"],
  ["captain", "Captain"],
]);

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

function offer(runId: string, over: Partial<TakeoverOffer> = {}): TakeoverOffer {
  return {
    id: `orgesc_${runId}`,
    run_id: runId,
    work_item_id: "wi_1",
    subject: "agent:qa",
    offered_to: "agent:captain",
    reason: "supervisor_reach_out",
    question: SHORT,
    deadline: null,
    chain: ["agent:qa", "agent:captain"],
    origin: "person_wait",
    title: "Fix the orders contract",
    ...over,
  };
}

async function show(offers: TakeoverOffer[]): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<TakeoverQueue offers={offers} names={names} busy={false} onTakeOver={() => undefined} />);
  });
  return host;
}

function row(el: HTMLElement, runId: string): HTMLElement {
  const found = el.querySelector<HTMLElement>(`[data-testid="org-takeover-${runId}"]`);
  expect(found, runId).toBeTruthy();
  return found as HTMLElement;
}

function why(el: HTMLElement, runId: string): HTMLElement | null {
  return el.querySelector<HTMLElement>(`[data-testid="org-takeover-why-${runId}"]`);
}

describe("takeoverWhySentence", () => {
  it("is the trimmed question when that is non-empty and different from the visible title", () => {
    expect(takeoverWhySentence("Fix the orders contract", `  ${SHORT}  `, "supervisor_reach_out")).toBe(SHORT);
    expect(takeoverWhySentence("Fix the orders contract", SHORT, SEVEN)).toBe(SHORT);
    expect(takeoverWhySentence("  Fix the orders  ", "Fix the orders", "other reason")).toBe("Fix the orders");
  });

  it("is the trimmed reason when the question is blank or the same as the visible title", () => {
    expect(takeoverWhySentence("Fix the orders contract", "", "  waiting on captain  ")).toBe("waiting on captain");
    expect(takeoverWhySentence("Fix the orders contract", "   ", "  waiting on captain  ")).toBe("waiting on captain");
    expect(takeoverWhySentence("Fix the orders contract", " \n\t ", "still waiting")).toBe("still waiting");
    expect(takeoverWhySentence(SHORT, SHORT, "  the captain is out  ")).toBe("the captain is out");
    expect(takeoverWhySentence("Fix the orders", "  Fix the orders  ", "still waiting")).toBe("still waiting");
    expect(takeoverWhySentence("crun_9", "", `  ${SIX}  `)).toBe(SIX);
  });

  it("is nothing when the question and the reason are blank or repeat the visible title", () => {
    expect(takeoverWhySentence("Fix the orders", "", "")).toBeNull();
    expect(takeoverWhySentence("Fix the orders", "   ", " \n\t ")).toBeNull();
    expect(takeoverWhySentence("Fix the orders", null, undefined)).toBeNull();
    expect(takeoverWhySentence(SHORT, `  ${SHORT}  `, SHORT)).toBeNull();
    expect(takeoverWhySentence("Same words", "  Same words  ", "Same words")).toBeNull();
  });
});

describe("the takeover row says why", () => {
  it("keeps the title and shows the question beside it", async () => {
    const shown = await show([
      offer("short", { question: `  ${SHORT}  `, reason: "supervisor_reach_out" }),
      offer("six", { title: "Orders", question: SIX, reason: "unused reason text" }),
      offer("seven", { title: "Orders", question: SEVEN, reason: "nope" }),
      offer("long", { title: "Orders", question: `  ${LONG}  `, reason: "short reason" }),
    ]);

    const short = row(shown, "short");
    const shortTitle = short.querySelector(".org-takeover-title");
    expect(shortTitle?.textContent).toBe("Fix the orders contract");
    expect(shortTitle?.getAttribute("title")).toBe(`  ${SHORT}  `);
    expect(short.textContent).toContain("Take over");
    const shortWhy = why(shown, "short");
    expect(shortWhy?.tagName).toBe("SPAN");
    expect(shortWhy?.textContent).toBe(SHORT);
    expect(shortWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(shortTitle?.contains(shortWhy)).toBe(false);

    const sixWhy = why(shown, "six");
    expect(sixWhy?.tagName).toBe("SPAN");
    expect(sixWhy?.textContent).toBe(SIX);
    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);
    expect(row(shown, "six").querySelector(".org-takeover-title")?.textContent).toBe("Orders");

    const sevenWhy = why(shown, "seven");
    expect(sevenWhy?.tagName).toBe("P");
    expect(sevenWhy?.textContent).toBe(SEVEN);
    expect(sevenWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(SEVEN.split(/\s+/).filter(Boolean).length).toBe(7);

    const longWhy = why(shown, "long");
    expect(longWhy?.tagName).toBe("P");
    expect(longWhy?.textContent).toBe(LONG);
    expect(longWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(LONG.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
    expect(row(shown, "long").querySelector(".org-takeover-title")?.getAttribute("title")).toBe(`  ${LONG}  `);
  });

  it("shows the reason when the question repeats the title, and nothing when both are blank", async () => {
    const shown = await show([
      offer("reason", { title: SHORT, question: SHORT, reason: "  the captain is out  " }),
      offer("reason-long", { title: "Orders", question: "   ", reason: `  ${SEVEN}  ` }),
      offer("blank", { title: "Still named", question: "", reason: "" }),
      offer("space", { title: "Still named", question: " \n\t ", reason: " \n\t " }),
      offer("same", { title: "Same words", question: "  Same words  ", reason: "Same words" }),
      offer("crun_notitle", { title: null, question: "Need a manager", reason: "stalled" }),
    ]);

    const reasonWhy = why(shown, "reason");
    expect(reasonWhy?.tagName).toBe("SPAN");
    expect(reasonWhy?.textContent).toBe("the captain is out");
    expect(reasonWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(row(shown, "reason").querySelector(".org-takeover-title")?.textContent).toBe(SHORT);
    expect(row(shown, "reason").querySelector(".org-takeover-title")?.getAttribute("title")).toBe(SHORT);

    const reasonLong = why(shown, "reason-long");
    expect(reasonLong?.tagName).toBe("P");
    expect(reasonLong?.textContent).toBe(SEVEN);
    expect(row(shown, "reason-long").querySelector(".org-takeover-title")?.getAttribute("title")).toBe("   ");

    expect(why(shown, "blank")).toBeNull();
    expect(row(shown, "blank").querySelector(".org-takeover-title")?.textContent).toBe("Still named");
    expect(row(shown, "blank").querySelector(".org-takeover-title")?.getAttribute("title")).toBe("");

    expect(why(shown, "space")).toBeNull();
    expect(row(shown, "space").querySelector(".org-takeover-title")?.getAttribute("title")).toBe(" \n\t ");

    expect(why(shown, "same")).toBeNull();
    expect(row(shown, "same").querySelector(".org-takeover-title")?.textContent).toBe("Same words");

    const bare = row(shown, "crun_notitle");
    expect(bare.querySelector(".org-takeover-title")?.textContent).toBe("crun_notitle");
    expect(bare.querySelector(".org-takeover-title")?.getAttribute("title")).toBe("Need a manager");
    expect(why(shown, "crun_notitle")?.textContent).toBe("Need a manager");
    expect(why(shown, "crun_notitle")?.tagName).toBe("SPAN");
  });

  it("calls takeoverWhySentence with the visible title, question, and reason", () => {
    const src = codeOnly(join(__dirname, "../app/agents/org-chain.tsx"));
    expect(src).toContain("takeoverWhySentence(offer.title || offer.run_id, offer.question, offer.reason)");
  });
});
