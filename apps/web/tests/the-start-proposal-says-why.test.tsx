import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * The plan header already says Why, with the reason only on title and data-tip.
 * The same reason has to be readable. Blank is nothing. More than six words is a paragraph.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

const { ProposalCard, proposalWhySentence } = await import("../app/agents/converse-parts");
const { mapProposal } = await import("../lib/agent-converse");

const LONG = "This workflow cuts the launch video before the stream";
const PADDED = `  ${LONG}  `;
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "Cuts the launch video";

let root: Root | null = null;
let host: HTMLElement | null = null;

function clearHost() {
  act(() => root?.unmount());
  host?.remove();
  document.body.innerHTML = "";
  root = null;
  host = null;
}

afterEach(() => {
  clearHost();
});

function wire(why: string) {
  return {
    proposal_id: "prp_1", workflow_key: "video_edit", title: "Cut the launch video",
    directive: "Trim it", inputs: {}, acceptance_criteria: [],
    agents: [], why, status: "proposed",
  };
}

function mount(why: string): HTMLElement {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(<ProposalCard proposal={mapProposal(wire(why))} onStart={vi.fn(async () => "wfr_1")} />));
  return host;
}

function whyChip(card: HTMLElement): HTMLElement {
  const chip = card.querySelector<HTMLElement>(".ag-why");
  expect(chip).not.toBeNull();
  return chip as HTMLElement;
}

function sentenceEl(card: HTMLElement): HTMLElement | null {
  return card.querySelector<HTMLElement>("[data-testid='ag-proposal-why']");
}

describe("proposalWhySentence", () => {
  it("is the trimmed why, or nothing when the why is blank", () => {
    expect(proposalWhySentence(PADDED)).toBe(LONG);
    expect(proposalWhySentence(SHORT)).toBe(SHORT);
    expect(proposalWhySentence(SIX)).toBe(SIX);
    expect(proposalWhySentence(SEVEN)).toBe(SEVEN);
    expect(proposalWhySentence(null)).toBeNull();
    expect(proposalWhySentence(undefined)).toBeNull();
    expect(proposalWhySentence("")).toBeNull();
    expect(proposalWhySentence(" \n\t ")).toBeNull();
  });
});

describe("the start proposal says why", () => {
  it("keeps the word Why and shows a long reason as a paragraph", () => {
    const card = mount(PADDED);
    const chip = whyChip(card);
    expect(chip.textContent).toBe("Why");
    expect(chip.getAttribute("data-tip")).toBe(PADDED);
    expect(chip.getAttribute("title")).toBe(PADDED);
    const sentence = sentenceEl(card);
    expect(sentence).not.toBeNull();
    expect(sentence!.textContent).toBe(LONG);
    expect(sentence!.tagName).toBe("P");
    expect(sentence!.hasAttribute("data-user-text")).toBe(true);
    expect(LONG.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
  });

  it("shows six words as a span and seven as a paragraph", () => {
    const six = mount(SIX);
    expect(whyChip(six).textContent).toBe("Why");
    const sixSentence = sentenceEl(six);
    expect(sixSentence!.textContent).toBe(SIX);
    expect(sixSentence!.tagName).toBe("SPAN");
    expect(sixSentence!.hasAttribute("data-user-text")).toBe(true);
    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);

    clearHost();

    const seven = mount(SEVEN);
    expect(whyChip(seven).textContent).toBe("Why");
    expect(sentenceEl(seven)!.textContent).toBe(SEVEN);
    expect(sentenceEl(seven)!.tagName).toBe("P");
    expect(SEVEN.split(/\s+/).filter(Boolean).length).toBe(7);

    clearHost();

    const short = mount(SHORT);
    expect(whyChip(short).textContent).toBe("Why");
    expect(sentenceEl(short)!.tagName).toBe("SPAN");
    expect(sentenceEl(short)!.textContent).toBe(SHORT);
  });

  it("shows no sentence when the why is blank", () => {
    const blank = mount(" \n\t ");
    expect(whyChip(blank).textContent).toBe("Why");
    expect(whyChip(blank).getAttribute("title")).toBe(" \n\t ");
    expect(sentenceEl(blank)).toBeNull();

    clearHost();

    const empty = mount("");
    expect(empty.querySelector(".ag-why")).toBeNull();
    expect(sentenceEl(empty)).toBeNull();
  });
});
