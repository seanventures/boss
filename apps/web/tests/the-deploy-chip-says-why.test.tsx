/**
 * The deploy chip says why, beside the chip, not only on the chip title.
 * A blank or whitespace reason says nothing. More than six words is a
 * paragraph. Six or fewer is a span.
 *
 * Breaks if the reason stays padded, a blank reason still renders, a long
 * reason is a span, or the header stops calling
 * deployChipReasonSentence(p.deploy.reason).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { DeployChipWhy, deployChipReasonSentence } from "../app/projects/project-page";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "the app did not answer as expected at the staging address";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "port held";

let root: Root | null = null;
let host: HTMLElement | null = null;

function unmountHost() {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
}

afterEach(() => {
  unmountHost();
  document.body.innerHTML = "";
});

async function show(sentence: string | null): Promise<HTMLElement> {
  unmountHost();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<DeployChipWhy sentence={sentence} />);
  });
  return host;
}

function why(el: HTMLElement): Element | null {
  return el.querySelector("[data-testid='px-head-deploy-why']");
}

describe("deployChipReasonSentence", () => {
  it("is the trimmed reason, or nothing when the reason is blank", () => {
    expect(deployChipReasonSentence(`  ${LONG}  `)).toBe(LONG);
    expect(deployChipReasonSentence(`  ${SHORT}  `)).toBe(SHORT);
    expect(deployChipReasonSentence(SIX)).toBe(SIX);
    expect(deployChipReasonSentence(`  ${SEVEN}  `)).toBe(SEVEN);
    expect(deployChipReasonSentence("")).toBeNull();
    expect(deployChipReasonSentence("   ")).toBeNull();
    expect(deployChipReasonSentence(" \n\t ")).toBeNull();
    expect(deployChipReasonSentence(null)).toBeNull();
    expect(deployChipReasonSentence(undefined)).toBeNull();
  });
});

describe("the deploy chip says why", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    const long = why(await show(LONG));
    expect(long?.tagName).toBe("P");
    expect(long?.textContent).toBe(LONG);
    expect(long?.hasAttribute("data-user-text")).toBe(true);
    expect(LONG.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);

    const seven = why(await show(SEVEN));
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);
    expect(SEVEN.split(/\s+/).filter(Boolean).length).toBe(7);

    const six = why(await show(SIX));
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six?.hasAttribute("data-user-text")).toBe(true);
    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);

    const short = why(await show(SHORT));
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when there is no sentence", async () => {
    const none = await show(null);
    expect(why(none)).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls deployChipReasonSentence with the deploy reason and keeps the chip words", () => {
    const src = codeOnly(join(__dirname, "../app/projects/project-page.tsx"));
    expect(src).toContain("deployChipReasonSentence(p.deploy.reason)");
    expect(src).toContain('<DeployChipWhy sentence={deployChipReasonSentence(p.deploy.reason)}');
    expect(src).toContain('title={[`${p.deploy.app || "The app"} · ${p.deploy.environment || "staging"}`, p.deploy.reason].filter(Boolean).join(": ") + " — opens Deployments"}');
    expect(src).toContain('`Deploying${p.deploy.total ? ` · ${p.deploy.index}/${p.deploy.total}` : ""}`');
    expect(src).toContain('"Deploy waiting for approval"');
    expect(src).toContain('"Last deploy failed"');
  });
});
