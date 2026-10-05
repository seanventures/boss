// @vitest-environment happy-dom
/**
 * D13/D16: `kind:` suggests the window's real kinds (the server counts them); an agent turn or
 * model call names its pipeline Stage in the drawer; a deploy names its App there — an app is
 * not a process, and PROCESS lists only API and worker.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { FACETS, fakeLogApi, newLog, row } from "./_helpers/log-fixtures";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { LogExplorer } = await import("../app/activity/log-explorer");

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

async function open(search: string, items = [row("r1")], facets: Record<string, unknown> = FACETS) {
  vi.stubGlobal("fetch", fakeLogApi(newLog({ page: { items }, facets })));
  setConsoleLive(true);
  window.history.replaceState({}, "", `/${search}`);
  await mount(<LogExplorer projectId="p1" actor="ceo" />);
}

const facts = () => Object.fromEntries($$("[data-testid=lx-drawer] .lx-fact").map((f) => [
  f.querySelector("dt")!.textContent, f.querySelector(".lx-fact-value")!.textContent]));

describe("kind: autocomplete", () => {
  it("suggests the window's real kinds, most common first", async () => {
    await open("?area=activity", [row("r1")], { ...FACETS, kinds: [{ kind: "code_task.stage_completed", n: 40 },
                                                                 { kind: "job.failed", n: 3 }] });
    const input = $("[data-testid=lx-query-input]") as HTMLInputElement;
    await act(async () => { input.focus(); input.dispatchEvent(new FocusEvent("focus")); });
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "kind:");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await settle();
    const offered = $$("[data-testid=lx-suggest] li").map((li) => li.textContent);
    expect(offered[0]).toContain("code_task.stage_completed");
    expect(offered[1]).toContain("job.failed");
  });
});

describe("the drawer's facts", () => {
  it("an agent turn and a model call name their Stage", async () => {
    await open("?area=activity&row=t1", [row("t1", { source: "agent_turn", kind: "agent.turn", stage: "write_tests",
                                                      agent: "software_engineer" })]);
    expect(facts().Stage).toBe("write tests");
  });

  it("a deploy names its App, not a Process", async () => {
    await open("?area=activity&row=d1", [row("d1", { source: "deployment", kind: "deploy", service: "",
                                                      data: { app: "checkout", environment: "production" } })]);
    expect(facts().App).toBe("checkout");
    expect(facts().Process).toBeUndefined();
  });
});
