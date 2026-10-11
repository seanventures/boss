// @vitest-environment happy-dom
/**
 * Every door into Repo and models lands on its row: `focus` opens the row, `add` opens Add a
 * model on that provider, `source` opens that model's menu, and the legacy `step=` still works.
 * Readiness, model failures, the composer and the dock all link this way.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { resolveLegacyArea } from "../app/legacy-areas";
import { fakeApi, mountBoard, q, type Mounted } from "./_helpers/setup-board";
import { board, firstRun, tryStep } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; });

describe("focus lands on the row", () => {
  for (const row of ["repository", "models", "jobs", "budget", "try"]) {
    it(row, async () => {
      fakeApi(board());
      mounted = await mountBoard(board(), `?area=setup&focus=${row}`);
      expect(q(document.body, `su-row-${row}`)?.getAttribute("data-focused")).toBe("true");
    });
  }

  it("focus=jobs opens the More jobs too, even in first run", async () => {
    fakeApi(firstRun());
    mounted = await mountBoard(firstRun(), "?area=setup&focus=jobs");
    expect(q(document.body, "su-job-hard")).not.toBeNull();
  });
});

describe("add and source", () => {
  it("add=openai opens Add a model on OpenAI's API key form", async () => {
    fakeApi(board());
    mounted = await mountBoard(board(), "?area=setup&add=openai");
    expect(q(document.body, "su-model-drawer")).not.toBeNull();
    expect(q(document.body, "su-tile-api_key")?.getAttribute("aria-selected")).toBe("true");
    expect(q(document.body, "su-provider-openai")?.getAttribute("aria-checked")).toBe("true");
    expect(q(document.body, "su-model-key")).not.toBeNull();
  });

  it("add works in first run too, before a repository is connected", async () => {
    fakeApi(firstRun());
    mounted = await mountBoard(firstRun(), "?area=setup&add=openrouter");
    expect(q(document.body, "su-provider-openrouter")?.getAttribute("aria-checked")).toBe("true");
  });

  it("source opens that model's menu", async () => {
    fakeApi(board());
    mounted = await mountBoard(board(), "?area=setup&focus=models&source=setup-openrouter");
    const row = q(document.body, "su-model-setup-openrouter")!;
    expect(row.querySelector('[role="menu"]')).not.toBeNull();
    const other = q(document.body, "su-model-anthropic_claude_code")!;
    expect(other.querySelector('[role="menu"]')).toBeNull();
  });
});

describe("the legacy step= names", () => {
  it("are rewritten to focus=", () => {
    expect(String(resolveLegacyArea(new URLSearchParams("area=setup&step=model&org=o"))))
      .toBe("area=setup&org=o&focus=models");
    expect(String(resolveLegacyArea(new URLSearchParams("area=setup&step=github"))))
      .toBe("area=setup&focus=repository");
    expect(resolveLegacyArea(new URLSearchParams("area=setup&focus=budget"))).toBeNull();
  });
});

describe("a failed load", () => {
  it("says so with Retry, and Retry loads the board", async () => {
    const { act } = await import("react");
    const { createRoot } = await import("react-dom/client");
    const { SetupBoard } = await import("../app/setup/setup-board");
    const api = fakeApi(board());
    let fail = true;
    api.answer = (call) => (call.method === "GET" && fail
      ? { status: 502, body: { error: "Response.clone: Body has already been consumed" } } : null);
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => { root.render(<SetupBoard projectId="p" />); });
    await new Promise((r) => setTimeout(r, 20));
    expect(q(document.body, "su-load-error")?.textContent).toContain("Not loaded");
    fail = false;
    await act(async () => { (q(document.body, "su-load-retry") as HTMLElement).click(); });
    await new Promise((r) => setTimeout(r, 20));
    expect(q(document.body, "su-verdict")?.textContent).toBe("Ready");
    act(() => root.unmount());
    container.remove();
  });
});

describe("the seam to Skills and MCP", () => {
  it("a Ready board and a Ready stepper both say what is next, and nothing before", async () => {
    // A finished board points at Skills; a Ready stepper with no first PR yet points at Try it
    // (the first build), never past it.
    fakeApi(board());
    mounted = await mountBoard(board());
    const next = q(document.body, "su-next-skills") as HTMLAnchorElement | null;
    expect(next?.textContent).toBe("Next: add skills");
    expect(next?.getAttribute("href")).toBe("?area=skills&setup=1");
    mounted.unmount(); mounted = null; document.body.innerHTML = "";
    fakeApi(tryStep());
    mounted = await mountBoard(tryStep());
    const first = q(document.body, "su-next-try") as HTMLAnchorElement | null;
    expect(first?.textContent).toBe("Next: open a PR");
    expect(q(document.body, "su-next-skills")).toBeNull();
    mounted.unmount(); mounted = null; document.body.innerHTML = "";
    fakeApi(firstRun());
    mounted = await mountBoard(firstRun());
    expect(q(document.body, "su-next-skills")).toBeNull();
  });
});
