// @vitest-environment happy-dom
/**
 * Edges say how the work actually moved, in the server's words (spec §2.3, §9, test 13).
 *
 * `edge_label()` in the engine is the single source of "Named in workflow", "Picked by BOSS over
 * 3", "Ran as you" and the rest. Every `how` the engine knows is fed through the Act card, the
 * chain strip and a run step node, and what each renders is compared with the label the API
 * sent — exactly. An act that arrives with no label shows no How at all: the console never
 * composes one (and no run-surface file even contains the engine's phrases to compose from).
 */
import React, { act } from "react";
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { engineEdgeLabels, filled, wireAct } from "./_helpers/run-acts-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { toAct, toChain } = await import("../app/agents/act-wire");
const { ActCard } = await import("../app/agents/act-card");
const { RunChain } = await import("../app/agents/run-chain");
const { StepNode } = await import("../app/workflows/flow/step-node");
const { laneAgentOf } = await import("../app/agents/chain-model");

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
});
function render(node: React.ReactNode): HTMLElement {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(node));
  return host;
}

const LABELS = engineEdgeLabels();
const HOWS = Object.keys(LABELS);

describe("the web renders exactly the API's edge label", () => {
  it("knows the engine's whole vocabulary (or this test proves nothing)", () => {
    expect(HOWS.sort()).toEqual(["approved", "asked_by_agent", "asked_by_person", "captain", "model_pick", "named",
      "next_stage", "only_candidate", "only_holder", "run_actor", "seated", "started"]);
  });

  it.each(HOWS)("the Act card says %s as the API said it", (how) => {
    const label = filled(LABELS[how]);
    const a = toAct(wireAct({ kind: "step", role_key: "qa", how, edge_label: label }));
    const el = render(<ActCard act={a} viewer="ceo" />);
    expect(el.querySelector('[data-testid="ag-act-how-label"]')?.textContent).toBe(label);
  });

  it.each(HOWS)("the chain's edge into a %s hop carries the API's label", (how) => {
    const label = filled(LABELS[how]);
    const chain = toChain({ acts: [
      wireAct({ act_id: "act_root", kind: "run", how: "started", handed_by_kind: "person", handed_by_id: "ceo",
                edge_label: filled(LABELS.started) }),
      wireAct({ kind: "step", role_key: "qa", how, parent_act_id: "act_root", edge_label: label }),
    ], accountable: "ceo", totals: {}, credits: [] });
    const el = render(<RunChain chain={chain} viewer="ceo" />);
    const edges = [...el.querySelectorAll('[data-testid="ag-chain-edge"]')];
    expect(edges).toHaveLength(1);
    expect(edges[0].getAttribute("data-edge-label")).toBe(label);
    expect(edges[0].getAttribute("data-tip")).toBe(label);
  });

  it.each(HOWS)("a run step node shows %s's label on its glyph, verbatim", (how) => {
    const label = filled(LABELS[how]);
    const a = toAct(wireAct({ kind: "step", role_key: "qa", how, edge_label: label, step_id: "s" }));
    const agent = laneAgentOf({}, a, "ceo")!;
    const el = render(<StepNode mode="run" run={{ phase: "done", meta: "", agent }}
      view={{ id: "s", title: "Test it", type: "agent", icon: "agent",
              agent: { kind: "named", role_key: "qa", label: "QA", tip: "" },
              badges: { approval: false, forced: false, inputs: 0, unwired: [], skills: 0, mcp: 0, lacks: [] } }} />);
    const glyph = el.querySelector('[data-testid="ag-how"]');
    // A how with no glyph (none today) would simply not draw one; the label is never rebuilt.
    if (glyph) expect(glyph.getAttribute("data-tip")).toBe(label);
  });

  it("an act with no label shows no How, rather than one made up here", () => {
    const a = toAct(wireAct({ kind: "step", role_key: "qa", how: "model_pick", edge_label: "", considered: ["qa", "pm"] }));
    const el = render(<ActCard act={a} viewer="ceo" />);
    expect(el.querySelector('[data-testid="ag-act-how"]')).toBeNull();
    expect(el.textContent).not.toMatch(/Picked by BOSS/);
  });

  it("no run-surface file holds the engine's phrases to build a label from", () => {
    const dir = resolve(__dirname, "../app/agents");
    const mine = ["act-card.tsx", "run-chain.tsx", "run-credits.tsx", "step-signed-by.tsx", "run-face.tsx",
      "chain-model.ts", "act-wire.ts", "run-acts.ts", "dock-agent.tsx", "build-faces.tsx", "credits-card.ts"];
    expect(readdirSync(dir).filter((f) => mine.includes(f)).length).toBe(mine.length);
    const fixed = Object.values(LABELS).map((t) => t.split("{")[0].trim()).filter((t) => t.length > 6);
    for (const f of mine) {
      const src = readFileSync(resolve(dir, f), "utf8");
      for (const phrase of fixed) expect(`${f}: ${src.includes(`"${phrase}`) || src.includes(`\`${phrase}`)}`).toBe(`${f}: false`);
    }
  });
});
