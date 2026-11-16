/**
 * Built by your team draws five faces. A sixth builder is still named, and the card says how
 * many are past those faces. Five or fewer draw no plus count.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { nameOf } from "../app/agents/run-face";
import { AppsShelf } from "../app/command-center/rails";
import { parseCcApps, type CcApp } from "../lib/command-center-apps";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as { React?: typeof React }).React = React;

const SIX = ["engineering_captain", "backend_engineer", "frontend_engineer", "pm", "qa", "docs_writer"];

function card(builders: string[]): CcApp {
  const [row] = parseCcApps({
    apps: [{
      key: "crun_hidden", app: "Hidden Builders", run_id: "crun_hidden", builders,
      state: "pr_open", word: "PR open", pr_number: 12,
      pr_url: "https://github.com/acme/shop/pull/12", pr_state: "open",
      cost_usd: 1.2, included: false, builds: 1, run_ids: ["crun_hidden"],
      at: "2026-09-08T03:42:13Z", attempts: [{ run_id: "crun_hidden", state: "pr_open" }],
    }],
  });
  return row;
}

const names = (keys: string[]) => keys.map((k) => nameOf(k).name).join(", ");

let root: Root | null = null;
let host: HTMLElement | null = null;

async function mount(builders: string[]): Promise<void> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(<AppsShelf apps={[card(builders)]} orgId="org_1" projectId="p1" />); });
}

afterEach(() => {
  act(() => { root?.unmount(); });
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

describe("the app card says how many builders are hidden", () => {
  it("shows +1 and names the sixth builder when six built it", async () => {
    await mount(SIX);
    const app = document.querySelector(".cc-app")!;
    const faces = app.querySelector(".cc-minis")!;
    expect(app.textContent).toContain("+1");
    expect(faces.querySelectorAll(".ag-avatar").length).toBe(5);
    expect(faces.getAttribute("aria-label")).toBe(names(SIX));
    expect(faces.getAttribute("aria-label")).toContain(nameOf(SIX[5]).name);
  });

  it("shows no plus count when five built it", async () => {
    const five = SIX.slice(0, 5);
    await mount(five);
    const app = document.querySelector(".cc-app")!;
    const faces = app.querySelector(".cc-minis")!;
    expect(app.textContent ?? "").not.toMatch(/\+\d+/);
    expect(faces.querySelectorAll(".ag-avatar").length).toBe(5);
    expect(faces.getAttribute("aria-label")).toBe(names(five));
  });
});
