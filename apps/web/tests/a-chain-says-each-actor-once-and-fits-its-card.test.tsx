// @vitest-environment happy-dom

import { readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { wireAct } from "./_helpers/run-acts-fixtures";

(globalThis as Record<string, unknown>).React = React;
const { toChain } = await import("../app/agents/act-wire");
const { actorsShown, chainItems, foldMiddle, mergeRepeats } = await import("../app/agents/chain-model");
const { RunChain } = await import("../app/agents/run-chain");

/**
 * "It keeps scrolling, I don't know whether it is a bug or we have used so many infinite agents."
 *
 * The HelpDesk run's strip drew one face per step — Seed Owner → Seed Owner → Juno → Seed Owner →
 * Forge · 28 attempts → BOSS → Seed Owner → … — ran off the card into a horizontal scroll, and
 * said "8 agents" over more faces than that. Now the same actor back to back is one stop, the
 * count is the actors drawn, and a long strip wraps and folds its middle.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement | null = null;
afterEach(() => { host?.remove(); host = null; });
function render(node: React.ReactElement): HTMLElement {
  host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => root.render(node));
  return host;
}

const OWNER = "usr_owner";
/** root → steps, each by a person (null role) or an agent, in order. */
function runOf(steps: [string, string | null, string?][]) {
  const root = wireAct({ act_id: "act_r", kind: "run", how: "started", handed_by_kind: "person",
                         handed_by_id: OWNER, accountable: OWNER, label: "Run · HelpDesk" });
  const acts = steps.map(([id, role, by], i) => wireAct({
    act_id: `act_${id}`, kind: "step", role_key: role, step_id: id, parent_act_id: i === 0 ? "act_r" : `act_${steps[i - 1][0]}`,
    how: role ? "named" : "run_actor", handed_by_kind: by === "boss" ? "boss" : "person",
    handed_by_id: by === "boss" ? "" : OWNER, accountable: OWNER, label: `${id} · HelpDesk`,
    started_at: new Date(Date.UTC(2026, 9, 12, 1, i)).toISOString() }));
  return toChain({ acts: [root, ...acts], accountable: OWNER, totals: {}, credits: [] });
}

describe("one actor, one stop", () => {
  it("merges the same actor back to back and keeps the steps it covers", () => {
    const items = chainItems(runOf([["prd", null], ["architecture", null], ["meeting", "pm"],
                                    ["approve", null], ["deploy", null]]), "viewer");
    expect(items.map((it) => (it.kind === "hop" ? it.who[0].roleKey ?? "person" : "loop")))
      .toEqual(["person", "pm", "person"]);
    expect(items[0].kind === "hop" && items[0].steps).toEqual(["Run", "prd", "architecture"]);
    expect(items[2].kind === "hop" && items[2].steps).toEqual(["approve", "deploy"]);
  });

  it("never merges across a different actor", () => {
    const items = chainItems(runOf([["a", "pm"], ["b", null], ["c", "pm"]]), "viewer");
    expect(mergeRepeats(items)).toHaveLength(4);
  });
});

describe("the count is what the strip draws", () => {
  it("counts each distinct actor drawn, people included", () => {
    const chain = runOf([["prd", null], ["meeting", "pm"], ["build", "engineering_captain"], ["deploy", null]]);
    expect(actorsShown(chainItems(chain, "viewer"))).toHaveLength(3);
    const el = render(<RunChain chain={chain} viewer="viewer" />);
    expect(el.querySelector('[data-testid="ag-chain-count"]')?.textContent).toBe("3 taking part");
    expect(el.querySelector('[data-testid="ag-chain-steps"]')?.getAttribute("data-tip")).toBe("Run\nprd");
  });
});

describe("a long chain fits its card", () => {
  const ROLES = ["pm", "chief_architect", "engineering_captain", "qa", "release_manager", "sre", "ux"];
  const long = () => runOf(ROLES.map((r, i) => [`s${i}`, r] as [string, string]));

  it("keeps the first two and the last three stops, folding the middle into +N with its names", () => {
    expect(foldMiddle([1, 2, 3, 4, 5, 6])).toEqual({ head: [1, 2, 3, 4, 5, 6], middle: [], tail: [] });
    expect(foldMiddle([1, 2, 3, 4, 5, 6, 7, 8])).toEqual({ head: [1, 2], middle: [3, 4, 5], tail: [6, 7, 8] });
    const el = render(<RunChain chain={long()} viewer="viewer" />);
    const drawn = () => el.querySelectorAll('[data-testid="ag-chain-strip"] > [data-testid="ag-chain-hop"]').length;
    expect(drawn()).toBe(5);
    const more = el.querySelector('[data-testid="ag-chain-more"]') as HTMLButtonElement;
    expect(more.textContent).toBe("+3 more");
    expect(more.getAttribute("data-tip")?.split("\n")).toHaveLength(3);
    act(() => more.click());
    expect(drawn()).toBe(8);
  });

  it("wraps instead of scrolling", () => {
    const css = readFileSync(join(__dirname, "../app/agents/agent-world.css"), "utf8");
    const rule = css.slice(css.indexOf(".ag-chain-strip {"), css.indexOf("}", css.indexOf(".ag-chain-strip {")));
    expect(rule).toContain("flex-wrap: wrap");
    expect(rule).not.toMatch(/overflow-x:\s*auto/);
  });
});
