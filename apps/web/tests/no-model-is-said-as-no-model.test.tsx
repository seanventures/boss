// @vitest-environment happy-dom
/**
 * NO MODEL IS SAID AS NO MODEL (stranger test r3, 2026-11-18). With no model connected, Easy Mode
 * said the budget cap was reached at $0.00, and sample/new-app builds read as running. Now:
 *   - Easy Mode's 409 needs_model shows the sentence and an "Add a model" link to Setup › Models;
 *   - a run's WhyLine that waits for a model links to Setup › Models.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WhyLine } from "../components/stuck-work";
import { parseWaitReason } from "../lib/stuck-work";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { BuildHome } = await import("../app/build-home");

let root: Root | null = null;
let host: HTMLElement | null = null;

async function mount(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(node); });
  for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

const SAID = "No model is connected, so nothing was started or reserved. Add one in Setup › Models, then press Easy Mode again.";

describe("Easy Mode with no model", () => {
  it("says no model, links to Setup › Models, and never mentions the budget", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/factory/templates")) return Response.json({ templates: [] });
      // As the launch proxy forwards it: the API's sentence in `error`, the fix link kept.
      return Response.json({ error: SAID, message: SAID, fix_href: "?area=setup&focus=models" }, { status: 409 });
    }));
    await mount(<BuildHome />);
    const press = document.querySelector("[data-testid=build-lucky]") as HTMLElement;
    await act(async () => { press.click(); });
    for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    const line = document.querySelector("[data-testid=build-launch-error]")?.textContent ?? "";
    expect(line).toContain(SAID);
    expect(line.toLowerCase()).not.toContain("budget");
    const fix = document.querySelector<HTMLAnchorElement>("[data-testid=build-launch-fix]");
    expect(fix?.textContent).toBe("Add a model");
    expect(fix?.getAttribute("href")).toBe("?area=setup&focus=models");
    expect(document.querySelector("[data-testid=build-launch]")).toBeNull();
  });
});

describe("a run waiting for a model", () => {
  it("names the wait and links to Setup › Models", async () => {
    const why = parseWaitReason({
      code: "needs_model", who: "you",
      words: "Waiting for a model: no model is connected. Add one in Setup › Models, then press Retry.",
      detail: "", since: null, eta_minutes: null, href: "?area=setup&focus=models",
    });
    await mount(<WhyLine why={why} />);
    expect(document.querySelector("[data-testid=why-line]")?.getAttribute("data-who")).toBe("you");
    expect(document.body.textContent).toContain("Waiting for a model");
    const fix = document.querySelector<HTMLAnchorElement>("[data-testid=why-line-fix]");
    expect(fix?.textContent).toBe("Add a model");
    expect(fix?.getAttribute("href")).toBe("?area=setup&focus=models");
  });

  it("a wait with no fix link draws none", async () => {
    await mount(<WhyLine why={parseWaitReason({ code: "slot", who: "queue", words: "Waiting for a build slot" })} />);
    expect(document.querySelector("[data-testid=why-line-fix]")).toBeNull();
  });
});
