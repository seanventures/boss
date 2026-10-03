// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BRANCHED, linear, PALETTE } from "./_helpers/canvas-fixtures";

/**
 * The flow lane: the whole workflow, left to right, in one row that never wraps.
 *
 * Rendered for real in happy-dom against the real model (lib/workflow-graph.ts, step-view.ts).
 * Happy-dom does no layout, so the lane's available width is given to it by stubbing the
 * viewport's clientWidth; every position the lane draws is computed, not measured, so what is
 * asserted here is what a browser shows.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { FlowLane } = await import("../app/workflows/flow/flow-lane");
const { MiniLane, MINI_LANE_MAX } = await import("../app/workflows/flow/mini-lane");
const geometry = await import("../app/workflows/flow/geometry");
const { layoutFlow } = await import("../lib/workflow-graph");

/** The lane's width in the console at 1440px: the shell's icon rail (84px), the palette as a
 *  rail (48px), the editor's gutters (2 × 16px), the grid gap (12px) and the lane's border
 *  (2px) come off the window. */
const LANE_AT_1440 = 1440 - 84 - 48 - 32 - 12 - 2;

let laneWidth = LANE_AT_1440;
let root: Root | null = null;
let host: HTMLElement | null = null;
const realClientWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get() { return (this as HTMLElement).classList?.contains("wf-lane-viewport") ? laneWidth : 0; },
  });
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  laneWidth = LANE_AT_1440;
  if (realClientWidth) Object.defineProperty(HTMLElement.prototype, "clientWidth", realClientWidth);
  else delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth;
});

function render(el: React.ReactElement) {
  act(() => root!.render(el));
  return host!;
}

const nodes = (h: HTMLElement) => [...h.querySelectorAll<HTMLElement>('[data-testid="wf-node"]')];
const slot = (h: HTMLElement, id: string) => h.querySelector<HTMLElement>(`.wf-slot[data-slot="${id}"]`)!;

describe("the whole flow fits in one row", () => {
  it("draws an 11-step workflow in a single row with no scrolling at 1440px", () => {
    const h = render(<FlowLane spec={linear(11)} palette={PALETTE} mode="design" />);
    const lane = h.querySelector<HTMLElement>('[data-testid="wf-lane"]')!;
    expect(nodes(h)).toHaveLength(11);
    // One row: every slot at the same height, each to the right of the one before.
    const tops = new Set(nodes(h).map((n) => n.parentElement!.style.top));
    expect(tops.size).toBe(1);
    const lefts = nodes(h).map((n) => parseFloat(n.parentElement!.style.left));
    expect(lefts).toEqual([...lefts].sort((a, b) => a - b));
    expect(lane.dataset.scrolls).toBeUndefined();
    expect(h.querySelector('[data-testid="wf-minimap"]')).toBeNull();
    const scale = Number(h.querySelector<HTMLElement>(".wf-lane-canvas")!.dataset.scale);
    expect(scale).toBeGreaterThanOrEqual(geometry.minScale("compact"));
    // The sizer is what the viewport scrolls; at this scale it is no wider than the lane.
    expect(parseFloat(h.querySelector<HTMLElement>(".wf-lane-sizer")!.style.width)).toBeLessThanOrEqual(LANE_AT_1440);
  });

  it("uses Comfortable nodes when they fit and Compact when they do not", () => {
    laneWidth = 2000;
    expect(render(<FlowLane spec={linear(5)} palette={PALETTE} mode="design" />)
      .querySelector<HTMLElement>('[data-testid="wf-lane"]')!.dataset.density).toBe("comfortable");
    laneWidth = LANE_AT_1440;
    expect(render(<FlowLane spec={linear(11)} palette={PALETTE} mode="design" />)
      .querySelector<HTMLElement>('[data-testid="wf-lane"]')!.dataset.density).toBe("compact");
  });

  it("never shrinks below the readable minimum: a 24-step flow scrolls inside the lane and shows the minimap", () => {
    const h = render(<FlowLane spec={linear(24)} palette={PALETTE} mode="design" />);
    const lane = h.querySelector<HTMLElement>('[data-testid="wf-lane"]')!;
    expect(lane.dataset.scrolls).toBe("true");
    expect(Number(h.querySelector<HTMLElement>(".wf-lane-canvas")!.dataset.scale)).toBe(geometry.minScale("compact"));
    const minimap = h.querySelector('[data-testid="wf-minimap"]')!;
    expect(minimap.querySelectorAll(".wf-minimap-dot")).toHaveLength(24);
    // Still one row.
    expect(new Set(nodes(h).map((n) => n.parentElement!.style.top)).size).toBe(1);
  });

  it("100% draws at full size and scrolls; Fit brings it back", () => {
    const h = render(<FlowLane spec={linear(11)} palette={PALETTE} mode="design" />);
    const full = [...h.querySelectorAll<HTMLButtonElement>(".wf-zoom-btn")].find((b) => b.textContent === "100%")!;
    act(() => full.click());
    expect(Number(h.querySelector<HTMLElement>(".wf-lane-canvas")!.dataset.scale)).toBe(1);
    expect(h.querySelector<HTMLElement>('[data-testid="wf-lane"]')!.dataset.scrolls).toBe("true");
    const fit = [...h.querySelectorAll<HTMLButtonElement>(".wf-zoom-btn")].find((b) => b.textContent === "Fit")!;
    act(() => fit.click());
    expect(h.querySelector<HTMLElement>('[data-testid="wf-lane"]')!.dataset.scrolls).toBeUndefined();
  });

  it("scrolls sideways only inside itself: the lane is capped at its column and its viewport scrolls", () => {
    // The page-level half of "no horizontal scroll at 390px" is CSS; these are the rules it rests on.
    const css = readFileSync("app/workflows/flow.css", "utf8");
    expect(css).toMatch(/\.wf-lane \{[^}]*min-width: 0;[^}]*max-width: 100%;/);
    expect(css).toMatch(/\.wf-lane-viewport \{[^}]*overflow-x: auto;/);
    const editor = readFileSync("app/workflows/editor.css", "utf8");
    expect(editor).toMatch(/@media \(max-width: 719px\)[\s\S]*grid-template-columns: minmax\(0, 1fr\)/);
  });
});

describe("a node says what it is and who does it", () => {
  it("titles a step by its label, never its raw id", () => {
    const h = render(<FlowLane spec={linear(4)} palette={PALETTE} mode="design" />);
    const titles = nodes(h).map((n) => n.querySelector(".wf-node-title")!.textContent);
    expect(titles).toEqual(["Research", "Convene a panel", "PRD approval", "Build, review and test"]);
    expect(titles).not.toContain("meeting");
  });

  it("prefers the step's own title", () => {
    const h = render(<FlowLane spec={linear(2, (i) => (i === 1 ? { title: "Panel on scope" } : {}))}
                               palette={PALETTE} mode="design" />);
    expect(nodes(h)[1].querySelector(".wf-node-title")!.textContent).toBe("Panel on scope");
  });

  it("shows You on a built-in step with no agent, the agent's name when one is named, and A person on a gate", () => {
    const spec = linear(4, (i) => (i === 1 ? { captain: "qa" }
      : i === 2 ? { kind: "decision", binding: {}, question: "Ship it?" } : {}));
    const h = render(<FlowLane spec={spec} palette={PALETTE} mode="design" />);
    const agents = nodes(h).map((n) => n.querySelector(".wf-node-agent-name")!.textContent);
    expect(agents).toEqual(["You", "QA Lead", "A person", "You"]);
    expect(nodes(h)[0].querySelector<HTMLElement>('[data-testid="wf-node-agent"]')!.dataset.tip)
      .toBe("Built-in step: runs as whoever starts the workflow.");
  });

  it("marks a step with a problem and carries the first message as its tooltip", () => {
    const h = render(<FlowLane spec={linear(3)} palette={PALETTE} mode="design"
                               problems={[{ step_id: "meeting", field: null, message: "Missing input topic" },
                                          { step_id: "meeting", field: null, message: "second" }]} />);
    const bad = nodes(h)[1];
    expect(bad.dataset.problem).toBe("true");
    expect(bad.querySelector<HTMLElement>('[data-testid="wf-node-problem"]')!.dataset.tip).toBe("Missing input topic");
    expect(nodes(h)[0].dataset.problem).toBeUndefined();
  });

  it("shows badges as icons and numbers, with the sentence in the tooltip", () => {
    const spec = linear(10, (i) => (i === 2 ? { binding: { agent_work: true, skills: ["sk_trim"] } } : {}));
    const h = render(<FlowLane spec={spec} palette={PALETTE} mode="design" />);
    expect(nodes(h)[3].querySelector<HTMLElement>('[data-testid="wf-badge-inputs"]')!.dataset.tip)
      .toBe("1 input · 1 not connected");
    expect(nodes(h)[2].querySelector<HTMLElement>('[data-testid="wf-badge-skills"]')!.dataset.tip)
      .toBe("Limited to: trim");
    const deploy = nodes(h)[9];
    expect(deploy.querySelector<HTMLElement>('[data-testid="wf-badge-approval"]')!.dataset.tip)
      .toBe("Risk 3 — always needs a person.");
  });

  it("selects on click and rings the selected node", () => {
    const onSelect = vi.fn();
    const h = render(<FlowLane spec={linear(3)} palette={PALETTE} mode="design" selection="meeting" onSelect={onSelect} />);
    expect(nodes(h)[1].dataset.selected).toBe("true");
    act(() => nodes(h)[2].click());
    expect(onSelect).toHaveBeenCalledWith("spec_review");
  });
});

describe("branches are drawn faithfully", () => {
  it("stacks parallel siblings in one column and labels the fan-out and the yes/no edges", () => {
    const h = render(<FlowLane spec={BRANCHED} palette={PALETTE} mode="design" />);
    const top = (id: string) => parseFloat(slot(h, id).style.top);
    const left = (id: string) => parseFloat(slot(h, id).style.left);
    expect(left("review")).toBe(left("test"));
    expect(top("review")).not.toBe(top("test"));
    expect(left("deploy")).toBe(left("notify"));
    const labels = [...h.querySelectorAll(".wf-edge-label")].map((t) => t.textContent);
    expect(labels).toEqual(expect.arrayContaining(["parallel", "yes", "no"]));
    expect(h.querySelector<HTMLElement>('[data-testid="wf-lane"]')!.dataset.linear).toBe("false");
  });

  it("draws a bounded loop as a dashed back-edge with its limit", () => {
    const spec = {
      entry_step: "again",
      steps: [
        { id: "again", kind: "bounded_loop", body: "build", max_iterations: 5, expression: "x" },
        { id: "build", kind: "capability", binding: { capability: "build" } },
      ],
    };
    const h = render(<FlowLane spec={spec} palette={PALETTE} mode="design" />);
    expect(h.querySelector('.wf-edge[data-kind="back"]')).not.toBeNull();
    expect([...h.querySelectorAll(".wf-edge-label")].map((t) => t.textContent)).toContain("↻ ×5");
  });
});

describe("run mode", () => {
  it("shows where each step has got to, and no design controls", () => {
    const spec = linear(4);
    const runState = {
      research: { phase: "done", meta: "5m 43s" },
      meeting: { phase: "needs_you", meta: "needs you" },
      spec_review: { phase: "skipped", meta: "skipped", tip: "No research phase" },
      build: { phase: "future", meta: "pending" },
    };
    const h = render(<FlowLane spec={spec} palette={PALETTE} mode="run" runState={runState} />);
    expect(nodes(h).map((n) => n.dataset.phase)).toEqual(["done", "needs_you", "skipped", "future"]);
    expect(nodes(h).map((n) => n.querySelector('[data-testid="wf-node-status"]')!.textContent))
      .toEqual(["5m 43s", "needs you", "skipped", "pending"]);
    expect(nodes(h)[2].querySelector<HTMLElement>('[data-testid="wf-node-status"]')!.dataset.tip).toBe("No research phase");
    expect(h.querySelector(".wf-zoom")).toBeNull();
    expect(h.querySelector(".wf-node-badges")).toBeNull();
    // The line into a step is solid once work has flowed through it, moving into the live one.
    const flows = [...h.querySelectorAll<HTMLElement>(".wf-slot:not(.wf-slot-end) > .wf-conn")].map((c) => c.dataset.flow);
    expect(flows).toEqual(["past", "moving", "future", "future"]);
  });

  it("never wraps an 11-step run", () => {
    const h = render(<FlowLane spec={linear(11)} palette={PALETTE} mode="run" runState={{}} />);
    expect(new Set(nodes(h).map((n) => n.parentElement!.style.top)).size).toBe(1);
  });
});

describe("the geometry the lane is drawn from", () => {
  const types = new Map();
  it("puts an 11-step flow inside 1440px at no less than the minimum scale", () => {
    const g = geometry.chooseDensity(layoutFlow(linear(11)), types, LANE_AT_1440);
    expect(g.density).toBe("compact");
    const s = geometry.fitScale(g.width, LANE_AT_1440, g.density);
    expect(s).toBeGreaterThanOrEqual(geometry.minScale(g.density));
    expect(geometry.overflows(g.width, s, LANE_AT_1440)).toBe(false);
  });

  it("draws a Wait narrower than a step", () => {
    const g = geometry.laneGeometry(layoutFlow({ steps: [{ id: "w", kind: "timer" }] }), new Map([["w", "wait"]]), "comfortable");
    expect(g.boxes.get("w")!.w).toBe(geometry.WAIT_W.comfortable);
  });
});

/* ── Readable text, as a checked rule ─────────────────────────────────────────── */

/** Every rule in a stylesheet as [selector, declarations], out of any @media block. */
function cssRules(css: string): [string, string][] {
  const out: [string, string][] = [];
  const walk = (src: string) => {
    let i = 0;
    while (i < src.length) {
      const open = src.indexOf("{", i);
      if (open < 0) break;
      const head = src.slice(i, open).trim();
      let depth = 1;
      let j = open + 1;
      for (; j < src.length && depth; j += 1) {
        if (src[j] === "{") depth += 1;
        else if (src[j] === "}") depth -= 1;
      }
      const body = src.slice(open + 1, j - 1);
      if (head.startsWith("@media")) walk(body);
      else if (!head.startsWith("@")) out.push([head, body]);
      i = j;
    }
  };
  walk(css.replace(/\/\*[\s\S]*?\*\//g, ""));
  return out;
}

/** The font size a rule sets, in rem (16px = 1rem), from font-size or the font shorthand. */
function fontRem(body: string): number | null {
  const m = body.match(/font-size:\s*([\d.]+)(rem|px)/) ?? body.match(/(?:^|;|\s)font:\s[^;]*?([\d.]+)(rem|px)/);
  if (!m) return null;
  return m[2] === "px" ? Number(m[1]) / 16 : Number(m[1]);
}

/** Every piece of text drawn on the lane's canvas, which Fit scales with it. (The avatar's
 *  initials are a picture of the agent, aria-hidden, the name being its tooltip.) */
const CANVAS_TEXT = [".wf-node-title", ".wf-node-agent", ".wf-badge", ".wf-node-status", ".wf-flag", ".wf-edge-label"];

describe("text on the lane is never smaller than 0.75rem, at any scale Fit picks", () => {
  const rules = cssRules(readFileSync("app/workflows/flow.css", "utf8"))
    .flatMap(([sel, body]) => sel.split(",").map((one) => [one.trim(), body] as [string, string]));
  const sizes = (cls: string, compact: boolean) => rules
    .filter(([sel]) => new RegExp(`${cls.replace(".", "\\.")}(?![\\w-])`).test(sel)
                       && sel.includes('[data-density="compact"]') === compact)
    .map(([, body]) => fontRem(body)).filter((n): n is number => n !== null);

  it("sets every canvas text at least as large as geometry.ts says each density does", () => {
    for (const cls of CANVAS_TEXT) {
      expect(Math.min(...sizes(cls, false)), `${cls}, Comfortable`).toBeGreaterThanOrEqual(geometry.MIN_FONT_REM.comfortable);
      const compact = sizes(cls, true);
      expect(compact.length, `${cls} has a Compact size`).toBeGreaterThan(0);
      expect(Math.min(...compact), `${cls}, Compact`).toBeGreaterThanOrEqual(geometry.MIN_FONT_REM.compact);
    }
    // Nothing else in Compact sets a smaller font either.
    for (const [sel, body] of rules.filter(([sel]) => sel.includes('[data-density="compact"]'))) {
      const rem = fontRem(body);
      if (rem !== null) expect(rem, sel).toBeGreaterThanOrEqual(geometry.MIN_FONT_REM.compact);
    }
  });

  it("never scales a density's smallest text under the minimum", () => {
    for (const d of ["comfortable", "compact"] as const) {
      expect(geometry.MIN_FONT_REM[d] * geometry.minScale(d)).toBeGreaterThanOrEqual(geometry.MIN_TEXT_REM - 1e-9);
      expect(geometry.MIN_FONT_REM[d] * geometry.fitScale(100_000, 100, d)).toBeGreaterThanOrEqual(geometry.MIN_TEXT_REM - 1e-9);
    }
  });

  it("fits 11 steps at 1440px by geometry: the smallest text, as drawn, is 0.75rem or more", () => {
    const h = render(<FlowLane spec={linear(11)} palette={PALETTE} mode="design" />);
    const lane = h.querySelector<HTMLElement>('[data-testid="wf-lane"]')!;
    const density = lane.dataset.density as "comfortable" | "compact";
    const scale = Number(h.querySelector<HTMLElement>(".wf-lane-canvas")!.dataset.scale);
    expect(lane.dataset.scrolls).toBeUndefined();
    expect(geometry.MIN_FONT_REM[density] * scale).toBeGreaterThanOrEqual(geometry.MIN_TEXT_REM);
  });
});

describe("the library card rail", () => {
  it("is one line of icons whose names are tooltips, with +n past the limit", () => {
    const steps = Array.from({ length: MINI_LANE_MAX + 3 }, (_, i) => ({ id: `s${i}`, title: `Step ${i}`, capability: "build" }));
    const h = render(<MiniLane steps={steps} />);
    const icons = h.querySelectorAll<HTMLElement>(".wf-mini-step");
    expect(icons).toHaveLength(MINI_LANE_MAX);
    expect(icons[0].dataset.tip).toBe("Step 0");
    expect(h.querySelector(".wf-mini-more")!.textContent).toBe("+3");
  });
});
