/**
 * The deploy card says why health is what it is, beside the health tag,
 * not only on the tag title. A blank reason says nothing. More than six
 * words is a paragraph. Six or fewer is a span. The sentence shows only
 * in the branch that renders that tag.
 *
 * Breaks if the reason stays padded, a blank reason still renders, a long
 * reason is a span, or the health-tag branch stops calling
 * deployHealthReasonSentence(e.health_reason).
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

import { DeployHealthWhy, deployHealthReasonSentence } from "../app/projects/tab-deployments";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "the staging probes have been failing since last night";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "probe down";

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

async function show(sentence: string | null, envKey = "staging"): Promise<HTMLElement> {
  await act(async () => { root?.unmount(); });
  host?.remove();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<DeployHealthWhy sentence={sentence} envKey={envKey} />);
  });
  return host;
}

function why(el: HTMLElement, envKey = "staging"): Element | null {
  return el.querySelector(`[data-testid='px-env-health-why-${envKey}']`);
}

function healthTagBranch(src: string): string {
  const start = src.indexOf("!e.deploy || e.deploy.live");
  expect(start).toBeGreaterThan(-1);
  const end = src.indexOf(") : null}", start);
  expect(end).toBeGreaterThan(start);
  return src.slice(start, end);
}

describe("deployHealthReasonSentence", () => {
  it("is the trimmed reason, or nothing when the reason is blank", () => {
    expect(deployHealthReasonSentence(`  ${LONG}  `)).toBe(LONG);
    expect(deployHealthReasonSentence(SHORT)).toBe(SHORT);
    expect(deployHealthReasonSentence(SIX)).toBe(SIX);
    expect(deployHealthReasonSentence(SEVEN)).toBe(SEVEN);
    expect(deployHealthReasonSentence(null)).toBeNull();
    expect(deployHealthReasonSentence(undefined)).toBeNull();
    expect(deployHealthReasonSentence("")).toBeNull();
    expect(deployHealthReasonSentence(" \n\t ")).toBeNull();
  });
});

describe("the deploy card says why health is what it is", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    const long = why(await show(LONG));
    expect(long?.tagName).toBe("P");
    expect(long?.textContent).toBe(LONG);
    expect(long?.hasAttribute("data-user-text")).toBe(true);

    const seven = why(await show(SEVEN));
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);

    const six = why(await show(SIX));
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six?.hasAttribute("data-user-text")).toBe(true);

    const short = why(await show(SHORT, "live"), "live");
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.getAttribute("data-testid")).toBe("px-env-health-why-live");
    expect(short?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when there is no sentence", async () => {
    const none = await show(null);
    expect(why(none)).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls deployHealthReasonSentence(e.health_reason) in the health-tag branch", () => {
    const src = codeOnly(join(__dirname, "../app/projects/tab-deployments.tsx"));
    const branch = healthTagBranch(src);
    expect(branch).toContain("deployHealthReasonSentence(e.health_reason)");
    expect(branch).toContain("<DeployHealthWhy sentence={deployHealthReasonSentence(e.health_reason)}");
    expect(branch).toContain("title={e.health_reason || undefined}");
    expect(branch).toContain("HEALTH_WORD[e.health]");
    const stateStart = src.indexOf('e.deploy && e.deploy.state !== "live"');
    expect(stateStart).toBeGreaterThan(-1);
    const stateBranch = src.slice(stateStart, src.indexOf(") : null}", stateStart));
    expect(stateBranch).not.toContain("deployHealthReasonSentence");
  });
});
