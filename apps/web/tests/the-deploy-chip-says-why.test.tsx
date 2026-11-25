// @vitest-environment happy-dom
/**
 * The deploy chip says why, beside the chip, not only on the chip title:
 * a small "Why?" toggle that opens the reason below the chips (operator
 * 2026-11-21: never a paragraph of engine prose in the header). A blank or
 * whitespace reason says nothing.
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
  it("is a small Why? toggle for any reason, long or short — never the reason inline", async () => {
    for (const text of [LONG, SEVEN, SIX, SHORT]) {
      const el = why(await show(text));
      expect(el?.tagName).toBe("BUTTON");
      expect(el?.textContent).toBe("Why?");
      expect(el?.getAttribute("aria-expanded")).toBe("false");
      expect(el?.getAttribute("aria-controls")).toBe("px-head-why-block");
    }
  });

  it("says nothing when there is no sentence", async () => {
    const none = await show(null);
    expect(why(none)).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls deployChipReasonSentence with the deploy reason and keeps the chip words", () => {
    const src = codeOnly(join(__dirname, "../app/projects/project-page.tsx"));
    expect(src).toContain("deployChipReasonSentence(p.deploy.reason)");
    expect(src).toContain('<DeployChipWhy sentence={deployWhy} open={why === "deploy"}');
    expect(src).toContain('title={[`${p.deploy.app || "The app"} · ${p.deploy.environment || "staging"}`, p.deploy.reason].filter(Boolean).join(": ") + " — opens Deployments"}');
    expect(src).toContain('`Deploying${p.deploy.total ? ` · ${p.deploy.index}/${p.deploy.total}` : ""}`');
    expect(src).toContain('"Deploy waiting for approval"');
    expect(src).toContain('"Last deploy failed"');
  });
});
