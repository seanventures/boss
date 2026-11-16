/**
 * The deploy tag says why it is not live, beside the word, not only on
 * the tag title. A blank reason says nothing. More than six words is a
 * paragraph. Six or fewer is a span. The sentence shows only beside that
 * tag.
 *
 * Breaks if the reason stays padded, a blank reason still renders, a long
 * reason is a span, the word or title is dropped, or the non-live branch
 * stops calling deployStateReasonSentence(e.deploy.reason).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));
vi.mock("../app/actions", () => ({
  promoteDeploymentAction: vi.fn(async () => ({ ok: true })),
}));

import * as deployments from "../app/projects/tab-deployments";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "the staging deploy has been waiting on a person since yesterday";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "probe down";

const deployStateReasonSentence = deployments.deployStateReasonSentence;
const DeployStateWhy = deployments.DeployStateWhy;

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

async function show(sentence: string | null, envKey = "staging"): Promise<HTMLElement> {
  clearHost();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<DeployStateWhy sentence={sentence} envKey={envKey} />);
  });
  return host;
}

function why(el: HTMLElement, envKey = "staging"): Element | null {
  return el.querySelector(`[data-testid='px-env-deploy-why-${envKey}']`);
}

function stateBranch(src: string): string {
  const start = src.indexOf('e.deploy && e.deploy.state !== "live"');
  expect(start).toBeGreaterThan(-1);
  const end = src.indexOf(") : null}", start);
  expect(end).toBeGreaterThan(start);
  return src.slice(start, end);
}

describe("deployStateReasonSentence", () => {
  it("is the trimmed reason, or nothing when the reason is blank", () => {
    expect(deployStateReasonSentence(`  ${LONG}  `)).toBe(LONG);
    expect(deployStateReasonSentence(SHORT)).toBe(SHORT);
    expect(deployStateReasonSentence(SIX)).toBe(SIX);
    expect(deployStateReasonSentence(SEVEN)).toBe(SEVEN);
    expect(deployStateReasonSentence(null)).toBeNull();
    expect(deployStateReasonSentence(undefined)).toBeNull();
    expect(deployStateReasonSentence("")).toBeNull();
    expect(deployStateReasonSentence(" \n\t ")).toBeNull();
  });
});

describe("the deploy tag says why", () => {
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

    const short = why(await show(SHORT, "failed"), "failed");
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.getAttribute("data-testid")).toBe("px-env-deploy-why-failed");
    expect(short?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when there is no sentence", async () => {
    const none = await show(null);
    expect(why(none)).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls deployStateReasonSentence(e.deploy.reason) beside the deploy word", () => {
    const src = codeOnly(join(__dirname, "../app/projects/tab-deployments.tsx"));
    const branch = stateBranch(src);
    expect(branch).toContain("deployStateReasonSentence(e.deploy.reason)");
    expect(branch).toContain("<DeployStateWhy sentence={deployStateReasonSentence(e.deploy.reason)}");
    expect(branch).toContain("envKey={e.key}");
    expect(branch).toContain("title={e.deploy.reason || undefined}");
    expect(branch).toContain("{e.deploy.word}");
    expect(branch).toContain("px-env-state-${e.key}");
    const whyStart = src.indexOf("function DeployStateWhy");
    const whyEnd = src.indexOf("const hourLocal");
    expect(whyStart).toBeGreaterThan(-1);
    expect(whyEnd).toBeGreaterThan(whyStart);
    const whyFn = src.slice(whyStart, whyEnd);
    expect(whyFn).toContain("px-env-deploy-why-${envKey}");
    expect(whyFn).toContain("data-user-text");
    expect(whyFn).toContain("split(/\\s+/)");
    const healthStart = src.indexOf("!e.deploy || e.deploy.live");
    expect(healthStart).toBeGreaterThan(-1);
    const healthBranch = src.slice(healthStart, src.indexOf(") : null}", healthStart));
    expect(healthBranch).toContain("deployHealthReasonSentence(e.health_reason)");
    expect(healthBranch).not.toContain("deployStateReasonSentence");
    expect(branch).not.toContain("deployHealthReasonSentence");
  });
});
