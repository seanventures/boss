// @vitest-environment happy-dom
/**
 * The scorecard's outcome tiles (BOSSAPP-912: GET /v1/ambient/scorecard → `outcomes`) and the
 * "Load demo" control: shown only when GET /v1/ambient/demo/status says the project can take it
 * AND the person may manage autonomy (owner / admin); it posts {project_id} to
 * /v1/ambient/demo/load and refreshes the list and scorecard; a refusal says why.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { parseScorecard } from "../../lib/ambient";
import { demoAnswer } from "../../lib/ambient-demo";
import { setConsoleLive } from "../../lib/console-live";
import { click, mountAs, settle, tid, unmount, type Role } from "../_helpers/multirepo-fixtures";
import type { Answer, Call } from "../_helpers/multirepo-fixtures";
import { fakeAmbientApi, writes } from "./_fixtures";

const { AgentOpsHome } = await import("../../app/agent-ops/agent-ops-home");

const toasts: string[] = [];
const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);
window.addEventListener("boss:toast", onToast);
afterEach(() => { unmount(); window.history.replaceState({}, "", "/"); toasts.length = 0; });

const STATUS = { available: true, reason: "", loaders: ["morning", "order_api_incident", "squad_cve", "roadmap"] };

async function open(role: Role = "owner", override?: (c: Call) => Answer | null) {
  const calls = fakeAmbientApi(override);
  setConsoleLive(true);
  window.history.replaceState({}, "", "/?area=agent_ops");
  await mountAs(<AgentOpsHome projectId="p1" actor="ceo" />, role);
  await settle();
  return calls;
}
const withStatus = (status: unknown, load?: Answer) => (c: Call): Answer | null | undefined => {
  if (c.path === "/v1/ambient/demo/status") return { body: status };
  if (c.path === "/v1/ambient/demo/load" && load) return load;
  return undefined;
};

describe("outcome tiles", () => {
  it("parses the outcomes block and an older API without one", () => {
    const card = parseScorecard(demoAnswer("/v1/ambient/scorecard?project_id=p1"));
    expect(card.outcomes).toMatchObject({ merged: 18, revert_pct: 5.3, roadmap: { acceptance_pct: 60, recurred: 1 }, boss_started: { pct: 53.5 } });
    expect(parseScorecard({ days: 7 }).outcomes).toBeNull();
  });

  it("shows merged, reverted, roadmap taken and BOSS-started with short labels and tooltips", async () => {
    await open();
    expect(tid("aa-score-merged")?.textContent).toContain("18");
    expect(tid("aa-score-merged")?.textContent).toContain("Merged");
    expect(tid("aa-score-reverted")?.textContent).toContain("5%");
    expect(tid("aa-score-reverted")?.querySelector("svg.aa-ring")).toBeTruthy();
    expect(tid("aa-score-roadmap")?.textContent).toContain("60%");
    expect(tid("aa-score-roadmap")?.getAttribute("title")).toContain("1 recurred");
    expect(tid("aa-score-boss-started")?.textContent).toContain("54%");
    for (const id of ["aa-score-merged", "aa-score-reverted", "aa-score-roadmap", "aa-score-boss-started"]) {
      const label = tid(id)!.querySelector(".aa-tile-k")!.textContent!;
      expect(label.split(/\s+/).length).toBeLessThanOrEqual(2);
      expect(tid(id)!.getAttribute("title")).toBeTruthy();
    }
  });
});

describe("Load demo", () => {
  it("is hidden when the project is not a demo project", async () => {
    await open("owner", withStatus({ available: false, reason: "not a demo project", loaders: [] }));
    expect(tid("aa-load-demo")).toBeNull();
  });

  it("is hidden when the API has no demo route", async () => {
    await open();
    expect(tid("aa-load-demo")).toBeNull();
  });

  it("is hidden from someone who may not manage autonomy, and never asks", async () => {
    const calls = await open("operator", withStatus(STATUS));
    expect(tid("aa-load-demo")).toBeNull();
    expect(calls.some((c) => c.path === "/v1/ambient/demo/status")).toBe(false);
  });

  it("loads the demo for an owner and refreshes the list and scorecard", async () => {
    const calls = await open("owner", withStatus(STATUS, { body: { loaded: { morning: { ok: 1 }, order_api_incident: { ok: 1 }, squad_cve: null, roadmap: { ok: 1 } }, skipped: { squad_cve: "already loaded" } } }));
    const btn = tid("aa-load-demo")!;
    expect(btn).toBeTruthy();
    expect(btn.getAttribute("title")).toContain("Order API incident");
    const lists = calls.filter((c) => c.path === "/v1/ambient/work-items").length;
    const cards = calls.filter((c) => c.path === "/v1/ambient/scorecard").length;
    await click(btn);
    await settle();
    expect(writes(calls, "/v1/ambient/demo/load").map((c) => c.body)).toEqual([{ project_id: "p1" }]);
    expect(calls.filter((c) => c.path === "/v1/ambient/work-items").length).toBeGreaterThan(lists);
    expect(calls.filter((c) => c.path === "/v1/ambient/scorecard").length).toBeGreaterThan(cards);
    expect(toasts.join(" ")).toContain("Demo loaded: 3 · skipped CVE squad");
  });

  it("says why when the API refuses", async () => {
    await open("admin", withStatus(STATUS, { status: 403, body: { detail: "only a demo project can load the demo" } }));
    await click(tid("aa-load-demo"));
    await settle();
    expect(toasts.join(" ")).toContain("only a demo project can load the demo");
  });
});
