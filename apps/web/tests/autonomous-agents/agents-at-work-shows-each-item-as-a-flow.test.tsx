// @vitest-environment happy-dom
/**
 * Agents at work (design §11.0, §11.3; spec §7.1): the five tabs filter the list, each item is a
 * small diagram, an item opens as a flow chart whose nodes carry only labels and badges, a
 * dissent is a red arrow you can press, Needs you is an amber node with one button, and all the
 * text — rationale, votes, evidence — lives in the drawer a node opens. Steering is gated.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { setConsoleLive } from "../../lib/console-live";
import { AREA_IDS } from "../../lib/area-ids";
import { AREA_ACCESS } from "../../lib/identity/areas";
import { $$, click, mountAs, settle, tid, type, unmount, type Role } from "../_helpers/multirepo-fixtures";
import { fakeAmbientApi, writes } from "./_fixtures";

const { AgentOpsHome } = await import("../../app/agent-ops/agent-ops-home");

afterEach(() => { unmount(); window.history.replaceState({}, "", "/"); });

async function open(search = "?area=agent_ops", role: Role = "owner", api = fakeAmbientApi) {
  const calls = api();
  setConsoleLive(true);
  window.history.replaceState({}, "", `/${search}`);
  await mountAs(<AgentOpsHome projectId="p1" actor="ceo" />, role);
  await settle();
  return calls;
}
const words = (s: string) => s.replace(/…/g, "").trim().split(/\s+/).filter(Boolean).length;

describe("Agents at work", () => {
  it("is a console area, read with work_item:read", () => {
    expect(AREA_IDS).toContain("agent_ops");
    expect(AREA_ACCESS.agent_ops).toEqual({ permission: "work_item:read" });
  });

  it("has the five tabs with counts and starts on Needs you", async () => {
    const calls = await open();
    expect($$('[role="tab"][data-tab]').map((t) => t.dataset.tab)).toEqual(["needs_you", "running", "resolved_by_agents", "overridden", "failed"]);
    expect(tid("aa-tab-needs_you")?.getAttribute("aria-selected")).toBe("true");
    expect(tid("aa-tab-running")?.textContent).toContain("2");
    expect(calls.some((c) => c.path === "/v1/ambient/work-items" && c.search.includes("filter=needs_you"))).toBe(true);
    expect($$('[data-testid="aa-item"]').map((i) => i.dataset.item)).toEqual(["work_payments"]);
  });

  it("filters by tab: each tab asks for its own items", async () => {
    const calls = await open();
    await click(tid("aa-tab-resolved_by_agents"));
    expect(calls.some((c) => c.search.includes("filter=resolved_by_agents"))).toBe(true);
    expect($$('[data-testid="aa-item"]').map((i) => i.dataset.item).sort()).toEqual(["work_ci", "work_order_api"]);
    expect(new URLSearchParams(window.location.search).get("aat")).toBe("resolved_by_agents");
    await click(tid("aa-tab-failed"));
    expect($$('[data-testid="aa-item"]').map((i) => i.dataset.outcome)).toEqual(["failed"]);
  });

  it("draws each item as a small diagram with badges, not a row of text", async () => {
    await open("?area=agent_ops&aat=resolved_by_agents");
    const card = $$('[data-testid="aa-item"]').find((c) => c.dataset.item === "work_order_api")!;
    expect(card.querySelector('[data-testid="aa-thumb"]')).toBeTruthy();
    expect(card.querySelectorAll(".aa-thumb-node").length).toBeGreaterThanOrEqual(4);
    expect(card.querySelector('[data-testid="aa-badge-outcome"]')?.textContent).toBe("Resolved by agents");
    expect(card.querySelector('[data-testid="aa-badge-cost"]')?.textContent).toBe("$1.84");
    expect(card.querySelector('[data-testid="aa-badge-level"]')?.textContent).toBe("A2");
    expect(card.querySelector('[data-testid="aa-badge-dissent"]')).toBeTruthy();
  });

  it("opens an item as a flow chart: nodes, arrows, a diamond decision, a red dissent", async () => {
    await open("?area=agent_ops&aat=resolved_by_agents");
    await click($$('[data-testid="aa-item"]').find((c) => c.dataset.item === "work_order_api")!);
    expect(new URLSearchParams(window.location.search).get("item")).toBe("work_order_api");
    const nodes = $$('[data-testid="aa-node"]');
    expect(nodes.map((n) => n.dataset.type)).toEqual(expect.arrayContaining(["signal", "agent", "action", "decision", "run", "outcome"]));
    expect(nodes.find((n) => n.dataset.type === "decision")!.querySelector(".aa-diamond")).toBeTruthy();
    // Agents wear their own faces.
    expect(nodes.filter((n) => n.dataset.type === "agent").every((n) => n.querySelector(".ag-avatar"))).toBe(true);
    expect($$('[data-testid="aa-edge"]').length).toBeGreaterThanOrEqual(10);
    expect($$('[data-testid="aa-edge"][data-dissent]')).toHaveLength(1);
    const dissentLabel = $$('[data-testid="aa-edge-label"]').find((l) => l.dataset.dissent !== undefined)!;
    expect(dissentLabel.textContent).toBe("✕ reject");
    expect(tid("aa-legend")).toBeTruthy();
  });

  it("puts no paragraph on the canvas: every node label is ≤ 4 words, and the rationale is only in the drawer", async () => {
    await open("?area=agent_ops&item=work_order_api");
    for (const l of $$('[data-testid="aa-node-label"]')) expect(words(l.textContent ?? ""), l.textContent ?? "").toBeLessThanOrEqual(4);
    for (const l of $$('[data-testid="aa-edge-label"]')) expect(words((l.textContent ?? "").replace(/[✕×\d]/g, ""))).toBeLessThanOrEqual(3);
    expect(tid("aa-canvas")?.textContent).not.toContain("Errors began 2 minutes after");
    await click($$('[data-testid="aa-node"]').find((n) => n.dataset.type === "decision")!);
    const drawer = tid("aa-drawer")!;
    expect(tid("aa-rationale")?.textContent).toContain("Errors began 2 minutes after 6bf2672");
    expect(drawer.querySelector('[data-testid="aa-votes"]')?.querySelectorAll("li")).toHaveLength(3);
    expect(tid("aa-dissent")?.textContent).toContain("A forward fix is one line");
    expect(tid("aa-evidence")?.textContent).toContain("6bf2672");
    await click(tid("aa-drawer-close"));
    expect(tid("aa-drawer")).toBeNull();
  });

  it("opens the dissenting vote's message from its red arrow", async () => {
    await open("?area=agent_ops&item=work_order_api");
    await click($$('[data-testid="aa-edge-label"]').find((l) => l.dataset.dissent !== undefined)!);
    expect(tid("aa-drawer-message")?.textContent).toContain("rollback drops two other fixes");
  });

  it("opens an agent's messages from its node", async () => {
    await open("?area=agent_ops&item=work_order_api");
    await click($$('[data-testid="aa-node"]').find((n) => n.dataset.nodeId === "party:agent:investigator")!);
    expect(tid("aa-drawer-party")?.textContent).toContain("N+1 query");
  });

  it("shows Needs you as an amber node with its one button, pulsing", async () => {
    await open("?area=agent_ops&item=work_payments");
    const need = $$('[data-testid="aa-node"]').find((n) => n.dataset.type === "needs_you")!;
    expect(need.dataset.active).toBe("true");
    expect(need.querySelector('[data-testid="aa-active"]')).toBeTruthy();
    expect($$('[data-testid="aa-needs-you-answer"]')).toHaveLength(1);
    expect(tid("aa-needs-you-answer")?.textContent).toBe("Answer");
  });

  it("lands an Ask BOSS citation (?act=) on its item, with that decision open", async () => {
    await open("?area=agent_ops&act=act_rb");
    await settle();
    expect(tid("aa-item-view")).toBeTruthy();
    expect(new URLSearchParams(window.location.search).get("item")).toBe("work_order_api");
    expect(tid("aa-drawer-decision")).toBeTruthy();
  });

  it("lets an operator take over or dismiss a live item, and gates both for a viewer", async () => {
    const calls = await open("?area=agent_ops&item=work_payments", "operator");
    await click(tid("aa-take-over"));
    expect(writes(calls, "/v1/ambient/work-items/work_payments/take-over")).toHaveLength(1);
    await click(tid("aa-dismiss"));
    await type(tid("aa-dismiss-form")!.querySelector("input"), "duplicate of INC-31");
    await click(tid("aa-dismiss-form")!.querySelector('button[type="submit"]') as HTMLElement);
    expect(writes(calls, "/v1/ambient/work-items/work_payments/dismiss")[0].body).toMatchObject({ project_id: "p1", reason: "duplicate of INC-31" });
    unmount();
    const viewer = await open("?area=agent_ops&item=work_payments", "viewer");
    expect(tid("aa-take-over")?.getAttribute("aria-disabled")).toBe("true");
    await click(tid("aa-take-over"));
    expect(writes(viewer, /take-over/)).toHaveLength(0);
  });

  it("gates Override to a person who may approve decisions", async () => {
    await open("?area=agent_ops&item=work_order_api", "viewer");
    await click($$('[data-testid="aa-node"]').find((n) => n.dataset.type === "decision")!);
    expect(tid("aa-override")?.getAttribute("aria-disabled")).toBe("true");
  });

  it("says when this install has no ambient API yet, and offers the sample", async () => {
    await open("?area=agent_ops", "owner", () => fakeAmbientApi(() => null));
    expect(tid("aa-missing")).toBeTruthy();
    expect(tid("aa-see-sample")?.getAttribute("href")).toContain("aa_demo=1");
  });

  it("shows the scorecard as pictures: rings, numbers and level bars", async () => {
    await open();
    expect(tid("aa-score-accepted")?.querySelector("svg.aa-ring")).toBeTruthy();
    expect(tid("aa-score-noise")?.textContent).toContain("13%");
    expect(tid("aa-score-mttr")?.textContent).toContain("4m");
    expect(tid("aa-score-cost")?.textContent).toContain("$0.63");
    expect($$('[data-testid="aa-bar"]')).toHaveLength(7);
    expect(tid("aa-score-accepted")?.getAttribute("title")).toContain("80%");
  });
});
