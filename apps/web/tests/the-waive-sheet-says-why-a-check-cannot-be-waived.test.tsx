// @vitest-environment happy-dom
/**
 * A check that cannot be waived says why, beside its label, not only on the title.
 * A waivable check, or a blank reason, says nothing. A sentence over six words is a paragraph.
 * The title stays, including the "Can't be waived" fallback, and that fallback is not visible text.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { WaiveSheet, waiveBlockSentence } from "../app/factory/waive-sheet";
import type { FailingGate } from "../lib/agents-live-board";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "the staging probes have been failing since last night";
const SIX = "one two three four five six";
const SHORT = "probe down";

function gate(name: string, waivable: boolean, reason?: string | null, label = name): FailingGate {
  return { name, label, waivable, reason: reason ?? undefined };
}

let root: Root | null = null;
let host: HTMLElement | null = null;

async function show(failing: FailingGate[]): Promise<void> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(
      <WaiveSheet runId="crun_1" app="Team inbox" failing={failing} projectId="p1" actor="ceo" onClose={() => {}} />,
    );
  });
}

function labelFor(name: string): HTMLElement {
  const label = document.querySelector(`[data-testid="fx-waive-${name}"]`)?.closest("label");
  expect(label).not.toBeNull();
  return label as HTMLElement;
}

function why(name: string): HTMLElement | null {
  return document.querySelector(`[data-testid="fx-waive-why-${name}"]`);
}

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

describe("waiveBlockSentence", () => {
  it("is the trimmed reason only when the check cannot be waived", () => {
    expect(waiveBlockSentence(false, `  ${LONG}  `)).toBe(LONG);
    expect(waiveBlockSentence(false, SHORT)).toBe(SHORT);
    expect(waiveBlockSentence(false, SIX)).toBe(SIX);
    expect(waiveBlockSentence(true, LONG)).toBeNull();
    expect(waiveBlockSentence(true, `  ${SHORT}  `)).toBeNull();
    expect(waiveBlockSentence(true, null)).toBeNull();
    expect(waiveBlockSentence(true, undefined)).toBeNull();
    expect(waiveBlockSentence(false, null)).toBeNull();
    expect(waiveBlockSentence(false, undefined)).toBeNull();
    expect(waiveBlockSentence(false, "")).toBeNull();
    expect(waiveBlockSentence(false, " \n\t ")).toBeNull();
  });
});

describe("the waive sheet says why a check cannot be waived", () => {
  it("shows a long reason as a paragraph beside the label and keeps the title", async () => {
    await show([gate("long", false, `  ${LONG}  `, "App starts")]);
    const label = labelFor("long");
    expect(label.getAttribute("title")).toBe(`  ${LONG}  `);
    expect(label.querySelector("span")?.textContent).toBe("App starts");
    const sentence = why("long");
    expect(sentence).not.toBeNull();
    expect(sentence!.tagName).toBe("P");
    expect(sentence!.textContent).toBe(LONG);
    expect(sentence!.hasAttribute("data-user-text")).toBe(true);
    expect(sentence!.textContent).not.toBe("Can't be waived");
  });

  it("shows a shorter reason as a span, including a sentence of six words", async () => {
    await show([
      gate("short", false, SHORT, "Lint"),
      gate("six", false, SIX, "Types"),
    ]);
    const short = why("short");
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.hasAttribute("data-user-text")).toBe(true);
    expect(labelFor("short").getAttribute("title")).toBe(SHORT);
    expect(labelFor("short").querySelector("span")?.textContent).toBe("Lint");
    const six = why("six");
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when the check can be waived", async () => {
    await show([gate("lint", true, LONG, "Lint")]);
    expect(why("lint")).toBeNull();
    const label = labelFor("lint");
    expect(label.getAttribute("title")).toBeNull();
    expect(label.textContent).toBe("Lint");
    expect(label.textContent).not.toContain(LONG);
  });

  it("says nothing when the reason is blank and does not invent the title fallback as text", async () => {
    await show([
      gate("empty", false, "", "Empty"),
      gate("none", false, null, "None"),
      gate("space", false, " \n\t ", "Space"),
    ]);
    expect(why("empty")).toBeNull();
    expect(why("none")).toBeNull();
    expect(why("space")).toBeNull();
    expect(labelFor("empty").getAttribute("title")).toBe("Can't be waived");
    expect(labelFor("none").getAttribute("title")).toBe("Can't be waived");
    expect(labelFor("space").getAttribute("title")).toBe(" \n\t ");
    expect(labelFor("empty").textContent).toBe("Empty");
    expect(labelFor("none").textContent).toBe("None");
    expect(labelFor("space").textContent).toBe("Space");
    expect(document.body.textContent).not.toContain("Can't be waived");
  });
});
