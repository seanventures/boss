// @vitest-environment happy-dom
/**
 * A rejected option already shows its label and an ✕, and puts why only on the
 * title, falling back to "Not chosen". That why has to be readable beside the
 * label. Blank is nothing: the fallback stays a title and is not invented as
 * text. More than six words is a paragraph. Six or fewer is a span.
 *
 * Breaks if the why stays padded, a blank why still renders, "Not chosen" is
 * shown when nothing was stored, a long why is a span, the test id ignores the
 * option key, or the options stop calling optionWhySentence(r.why).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import * as screen from "../app/agent-ops/drawer";
import { codeOnly } from "./_helpers/source";

const optionWhySentence = (screen as {
  optionWhySentence: (why: string | null | undefined) => string | null;
}).optionWhySentence;

type OptionsModel = {
  chosen: string;
  options: Array<{ key: string; label: string }>;
  rejected: Array<{ option: string; why?: string | null }>;
};

const DecisionOptions = (screen as {
  DecisionOptions: (props: { d: OptionsModel }) => React.ReactNode;
}).DecisionOptions;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "kept the cheaper host and waited for the review";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "too expensive";

const OPTIONS: OptionsModel["options"] = [
  { key: "hold", label: "Hold the rollout" },
  { key: "ship_now", label: "Ship tonight" },
  { key: "wait", label: "Wait a week" },
  { key: "six", label: "Six words" },
  { key: "seven", label: "Seven words" },
  { key: "blank", label: "Leave it" },
  { key: "space", label: "Space it" },
  { key: "named", label: "Named fallback" },
];

let root: Root | null = null;
let host: HTMLElement | null = null;

function unmountMounted(): void {
  act(() => { root?.unmount(); });
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
}

afterEach(() => {
  unmountMounted();
});

async function show(rejected: OptionsModel["rejected"]): Promise<HTMLElement> {
  unmountMounted();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<DecisionOptions d={{ chosen: "hold", options: OPTIONS, rejected }} />);
  });
  return host;
}

function optionSpan(text: string): HTMLElement {
  const found = [...document.querySelectorAll(".aa-opt")].find((el) => el.textContent === text);
  expect(found).toBeTruthy();
  return found as HTMLElement;
}

function why(option: string): HTMLElement | null {
  return document.querySelector(`[data-testid="aa-opt-why-${option}"]`);
}

describe("optionWhySentence", () => {
  it("is the trimmed why, or nothing when the why is blank", () => {
    expect(optionWhySentence(`  ${LONG}  `)).toBe(LONG);
    expect(optionWhySentence(SHORT)).toBe(SHORT);
    expect(optionWhySentence(SIX)).toBe(SIX);
    expect(optionWhySentence(SEVEN)).toBe(SEVEN);
    expect(optionWhySentence("  too   expensive  ")).toBe("too   expensive");
    expect(optionWhySentence("Not chosen")).toBe("Not chosen");
    expect(optionWhySentence(" \n\t ")).toBeNull();
    expect(optionWhySentence("")).toBeNull();
    expect(optionWhySentence(null)).toBeNull();
    expect(optionWhySentence(undefined)).toBeNull();
  });
});

describe("the rejected option says why", () => {
  it("shows more than six words as a paragraph beside the label and keeps the title", async () => {
    await show([
      { option: "ship_now", why: `  ${LONG}  ` },
      { option: "seven", why: SEVEN },
    ]);
    const ship = optionSpan("✕ Ship tonight");
    expect(ship.getAttribute("title")).toBe(`  ${LONG}  `);
    expect(ship.textContent).toBe("✕ Ship tonight");
    const sentence = why("ship_now");
    expect(sentence).not.toBeNull();
    expect(sentence!.tagName).toBe("P");
    expect(sentence!.textContent).toBe(LONG);
    expect(sentence!.hasAttribute("data-user-text")).toBe(true);
    expect(ship.contains(sentence!)).toBe(false);
    expect(LONG.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);

    const seven = why("seven");
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);
    expect(optionSpan("✕ Seven words").getAttribute("title")).toBe(SEVEN);
    expect(optionSpan("✕ Seven words").contains(seven!)).toBe(false);
  });

  it("shows six or fewer words as a span", async () => {
    await show([
      { option: "ship_now", why: SHORT },
      { option: "six", why: `  ${SIX}  ` },
    ]);
    const short = why("ship_now");
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.hasAttribute("data-user-text")).toBe(true);
    expect(optionSpan("✕ Ship tonight").getAttribute("title")).toBe(SHORT);
    const six = why("six");
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six?.hasAttribute("data-user-text")).toBe(true);
    expect(optionSpan("✕ Six words").getAttribute("title")).toBe(`  ${SIX}  `);
    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);
  });

  it("keeps the chosen mark and does not invent a why when nothing was stored", async () => {
    const hostEl = await show([
      { option: "wait" },
      { option: "blank", why: "" },
      { option: "space", why: " \n\t " },
      { option: "named", why: "Not chosen" },
    ]);
    const chosen = hostEl.querySelector("[data-chosen='true']");
    expect(chosen?.getAttribute("title")).toBe("Chosen");
    expect(chosen?.textContent).toBe("✓ Hold the rollout");

    expect(why("wait")).toBeNull();
    expect(why("blank")).toBeNull();
    expect(why("space")).toBeNull();
    expect(optionSpan("✕ Wait a week").getAttribute("title")).toBe("Not chosen");
    expect(optionSpan("✕ Leave it").getAttribute("title")).toBe("Not chosen");
    expect(optionSpan("✕ Space it").getAttribute("title")).toBe(" \n\t ");
    expect(optionSpan("✕ Wait a week").textContent).toBe("✕ Wait a week");
    expect(optionSpan("✕ Leave it").textContent).toBe("✕ Leave it");
    expect(optionSpan("✕ Space it").textContent).toBe("✕ Space it");
    expect(optionSpan("✕ Wait a week").textContent).not.toContain("Not chosen");
    expect(optionSpan("✕ Leave it").textContent).not.toContain("Not chosen");
    expect(optionSpan("✕ Space it").textContent).not.toContain("Not chosen");

    const named = why("named");
    expect(named?.tagName).toBe("SPAN");
    expect(named?.textContent).toBe("Not chosen");
    expect(named?.hasAttribute("data-user-text")).toBe(true);
    expect(optionSpan("✕ Named fallback").getAttribute("title")).toBe("Not chosen");
    expect(optionSpan("✕ Named fallback").textContent).toBe("✕ Named fallback");
  });

  it("calls optionWhySentence(r.why) beside the label and the ✕", () => {
    const src = codeOnly(join(__dirname, "../app/agent-ops/drawer.tsx"));
    const start = src.indexOf("function DecisionOptions");
    const end = src.indexOf("function DecisionView");
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const options = src.slice(start, end);
    expect(options).toContain("optionWhySentence(r.why)");
    expect(options).not.toContain('optionWhySentence(r.why || "Not chosen")');
    expect(options).toContain('title={r.why || "Not chosen"}');
    expect(options).toContain("aa-opt-why-${r.option}");
    expect(options).toContain("✕");
    expect(options).toContain('title="Chosen"');
    expect(options).toContain("d.options.find((o) => o.key === r.option)?.label || r.option");
    const viewStart = src.indexOf("function DecisionView");
    const viewEnd = src.indexOf("export function NodeDrawer");
    expect(viewEnd).toBeGreaterThan(viewStart);
    const view = src.slice(viewStart, viewEnd);
    expect(view).toContain("<DecisionOptions d={d} />");
  });
});
