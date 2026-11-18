// @vitest-environment happy-dom
/**
 * The journey's detail sentence is the node's tooltip, never text inside the strip.
 *
 * Stranger test r2 (2026-11-17): the detail rendered as an unstyled <p>/<span> flex sibling of
 * each node, so every project Overview showed large overlapping text ("Ready for work: 4 of 7
 * checks", "Backlog: 1 items proposed, you accepted 0") at 390/1280/1440/1920 px.
 *
 * Breaks if the strip holds anything but node buttons and wires, a node button holds a
 * sentence-length text, the tooltip loses the detail, or a blank / repeated detail still
 * becomes a tooltip.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { Journey, journeyDetailSentence } from "../app/projects/kit";
import type { Node } from "../lib/project-experience";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "Ready for work: 4 of 7 checks";
const BACKLOG = "Backlog: 1 items proposed, you accepted 0";

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => { root?.unmount(); });
  host?.remove();
  host = null;
  root = null;
});

async function show(nodes: Node[]): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(<Journey nodes={nodes} onOpen={() => {}} />); });
  return host;
}

const node = (key: string, label: string, sub: string, detail?: string): Node =>
  ({ key, label, state: "todo", sub, href: "", detail });

describe("journeyDetailSentence", () => {
  it("is the trimmed detail, or nothing when it is blank or already the label or the sub", () => {
    expect(journeyDetailSentence(`  ${LONG}  `, "Ready", "4/7")).toBe(LONG);
    expect(journeyDetailSentence(null, "Ready", "6/6")).toBeNull();
    expect(journeyDetailSentence(undefined, "Ready", "6/6")).toBeNull();
    expect(journeyDetailSentence(" \n\t ", "Ready", "6/6")).toBeNull();
    expect(journeyDetailSentence("  Ready  ", "Ready", "6/6")).toBeNull();
    expect(journeyDetailSentence("6/6", "Ready", "  6/6  ")).toBeNull();
  });
});

describe("the journey strip", () => {
  it("holds only node buttons and wires; the detail is each node's tooltip", async () => {
    const el = await show([
      node("ready", "Ready", "4/7", LONG),
      node("backlog", "Backlog", "1", BACKLOG),
      node("working", "Working", "", "Working"),
      node("staging", "Staging", ""),
    ]);
    const strip = el.querySelector("[data-testid='px-journey']")!;
    expect(strip).not.toBeNull();
    const kids = Array.from(strip.children);
    expect(kids.length).toBe(7); // 4 nodes + 3 wires
    for (const k of kids) {
      const ok = (k.tagName === "BUTTON" && k.classList.contains("px-jr-node")) || k.classList.contains("px-jr-wire");
      expect(ok, `unexpected strip child <${k.tagName.toLowerCase()} class="${k.className}">`).toBe(true);
    }
    expect(strip.querySelector("p")).toBeNull();
    expect(strip.querySelector("[data-testid^='px-journey-why']")).toBeNull();

    const ready = strip.querySelector<HTMLButtonElement>("[data-testid='px-journey-ready']")!;
    expect(ready.title).toBe(LONG);
    expect(ready.textContent).not.toContain(LONG);
    expect(strip.querySelector<HTMLButtonElement>("[data-testid='px-journey-backlog']")!.title).toBe(BACKLOG);
    // A detail equal to the label, or none, is no tooltip.
    expect(strip.querySelector<HTMLButtonElement>("[data-testid='px-journey-working']")!.hasAttribute("title")).toBe(false);
    expect(strip.querySelector<HTMLButtonElement>("[data-testid='px-journey-staging']")!.hasAttribute("title")).toBe(false);
    // The strip's visible text is only labels and subs.
    expect(strip.textContent).not.toContain("checks");
    expect(strip.textContent).not.toContain("proposed");
  });
});
