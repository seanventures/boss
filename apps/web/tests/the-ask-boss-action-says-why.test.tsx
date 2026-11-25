// @vitest-environment happy-dom
/**
 * An Ask BOSS action already puts why on the button title and shows only the
 * label. The same why has to be readable beside that label. Blank is nothing.
 * More than six words is a paragraph. Six or fewer is a span. The title and
 * the label stay.
 *
 * Breaks if the why stays padded, a blank why still renders, a long why is a
 * span, the test id ignores the action id, or the action row stops calling
 * askActionWhySentence(a.why).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { AskActionWhy, askActionWhySentence } from "../app/ask-boss-hero";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "this clears the stale url and does not redeploy";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "clears the stale url";

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

async function show(sentence: string | null, id?: string | null, tool?: string | null): Promise<HTMLElement> {
  await act(async () => { root?.unmount(); });
  host?.remove();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<AskActionWhy sentence={sentence} id={id} tool={tool} />);
  });
  return host;
}

function why(el: HTMLElement, testId: string): Element | null {
  return el.querySelector(`[data-testid="${testId}"]`);
}

function actionBranch(src: string): string {
  const start = src.indexOf('data-testid="askboss-actions"');
  expect(start).toBeGreaterThan(-1);
  const end = src.indexOf("askboss-foot", start);
  expect(end).toBeGreaterThan(start);
  return src.slice(start, end);
}

describe("askActionWhySentence", () => {
  it("is the trimmed why, or nothing when the why is blank", () => {
    expect(askActionWhySentence(`  ${LONG}  `)).toBe(LONG);
    expect(askActionWhySentence(SHORT)).toBe(SHORT);
    expect(askActionWhySentence(SIX)).toBe(SIX);
    expect(askActionWhySentence(SEVEN)).toBe(SEVEN);
    expect(askActionWhySentence(" \n\t ")).toBeNull();
    expect(askActionWhySentence("")).toBeNull();
    expect(askActionWhySentence(null)).toBeNull();
    expect(askActionWhySentence(undefined)).toBeNull();
  });
});

describe("the ask action says why", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    const long = why(await show(LONG, "act_long", "clear_url"), "askboss-why-act_long");
    expect(long?.tagName).toBe("P");
    expect(long?.textContent).toBe(LONG);
    expect(long?.hasAttribute("data-user-text")).toBe(true);

    const seven = why(await show(SEVEN, "act_seven", "clear_url"), "askboss-why-act_seven");
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);

    const six = why(await show(SIX, "act_six", "clear_url"), "askboss-why-act_six");
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six?.hasAttribute("data-user-text")).toBe(true);

    const short = why(await show(SHORT, "act_short", "clear_url"), "askboss-why-act_short");
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.hasAttribute("data-user-text")).toBe(true);
  });

  it("uses the tool when the action has no id", async () => {
    const fromTool = why(await show(SHORT, "", "clear_url"), "askboss-why-clear_url");
    expect(fromTool?.tagName).toBe("SPAN");
    expect(fromTool?.textContent).toBe(SHORT);
    const missing = why(await show(SHORT, null, "redeploy"), "askboss-why-redeploy");
    expect(missing?.getAttribute("data-testid")).toBe("askboss-why-redeploy");
  });

  it("says nothing when there is no why", async () => {
    const none = await show(null, "act_blank", "clear_url");
    expect(none.querySelector("[data-testid^='askboss-why-']")).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls askActionWhySentence(a.why) beside the action that already titles that why", () => {
    const branch = actionBranch(codeOnly(join(__dirname, "../app/ask-boss-hero.tsx")));
    expect(branch).toContain("askActionWhySentence(a.why)");
    expect(branch).toContain("title={a.why}");
    expect(branch).toContain("a.label");
    expect(branch).toContain("id={a.id}");
    expect(branch).toContain("tool={a.tool}");
  });
});
