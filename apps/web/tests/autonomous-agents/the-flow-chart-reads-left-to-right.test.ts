/**
 * The flow chart's layout (lib/ambient-layout.ts): a work item reads left to right — signal
 * first, the outcome last, a chain of handoffs as a chain of columns — with no two nodes on top
 * of each other, an override drawn as an arc (never ranked backwards), duplicate arrows merged,
 * and a dissenting vote kept as its own arrow. Labels reach the canvas clamped to ≤ 4 words.
 */
import { describe, expect, it } from "vitest";

import { EDGE_WORDS, NODE_WORDS, clampWords, parseGraph, parseList } from "../../lib/ambient";
import { demoGraph, demoList } from "../../lib/ambient-demo";
import { activeNodeId, layoutFlow } from "../../lib/ambient-layout";

const graphOf = (id: string) => parseGraph(demoGraph(id));

describe("the flow chart layout", () => {
  it("puts the signal first, the outcome last, and a handoff chain in successive columns", () => {
    const g = graphOf("work_order_api");
    const l = layoutFlow(g.nodes, g.edges);
    const col = (id: string) => l.nodes.find((n) => n.id === id)!.col;
    expect(col("signal:sig_5xx")).toBe(0);
    expect(col("outcome")).toBe(l.cols - 1);
    expect(col("party:agent:chief_of_staff")).toBeLessThan(col("party:agent:sre"));
    expect(col("party:agent:sre")).toBeLessThan(col("party:agent:investigator"));
    expect(col("party:agent:investigator")).toBeLessThan(col("proposal:wim_rb"));
    expect(col("proposal:wim_rb")).toBeLessThan(col("decision:act_rb"));
  });

  it("never overlaps two nodes", () => {
    for (const id of ["work_order_api", "work_payments", "work_dep", "work_ci"]) {
      const g = graphOf(id);
      const l = layoutFlow(g.nodes, g.edges);
      for (const a of l.nodes) for (const b of l.nodes) {
        if (a === b) continue;
        const apart = a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y;
        expect(apart, `${a.id} overlaps ${b.id}`).toBe(true);
      }
      expect(l.width).toBeGreaterThan(0);
      for (const n of l.nodes) expect(n.y).toBeGreaterThanOrEqual(0);
    }
  });

  it("draws an override as an arc above, not a backwards column", () => {
    const g = graphOf("work_dep");
    const l = layoutFlow(g.nodes, g.edges);
    const over = l.edges.find((e) => e.kind === "overrides")!;
    expect(over.back).toBe(true);
    expect(over.dissent).toBe(true);
    expect(over.d.startsWith("M ")).toBe(true);
  });

  it("keeps a dissenting vote as its own red arrow among the agreeing ones", () => {
    const g = graphOf("work_order_api");
    const l = layoutFlow(g.nodes, g.edges);
    const votes = l.edges.filter((e) => e.kind === "vote" && e.to === "proposal:wim_rb");
    expect(votes).toHaveLength(3);
    expect(votes.filter((e) => e.dissent).map((e) => e.from)).toEqual(["party:agent:adversarial_reviewer"]);
    // The fan-in spreads its ports: three arrows, three different entry heights.
    const ends = new Set(votes.map((e) => e.d.split(" ").slice(-1)[0]));
    expect(ends.size).toBe(3);
  });

  it("merges identical arrows into one with a count", () => {
    const g = graphOf("work_ci");
    const twice = [...g.edges, g.edges[0]];
    const l = layoutFlow(g.nodes, twice);
    expect(l.edges.find((e) => e.from === g.edges[0].from && e.to === g.edges[0].to)!.count).toBe(2);
  });

  it("survives a cycle (an escalation answered by a handoff)", () => {
    const g = graphOf("work_docs");
    const cyc = [...g.edges, { from: "party:agent:docs_writer", to: "party:agent:chief_of_staff", kind: "escalate" as const, label: "escalated", dissent: false }];
    const l = layoutFlow(g.nodes, cyc);
    expect(l.nodes).toHaveLength(g.nodes.length);
    expect(l.edges.find((e) => e.kind === "escalate")!.back).toBe(true);
  });

  it("pulses Needs you on a waiting item, the newest node on a running one, nothing on a closed one", () => {
    const wait = graphOf("work_payments");
    expect(activeNodeId(layoutFlow(wait.nodes, wait.edges), "needs_you")).toBe("needs_you");
    const run = graphOf("work_flaky");
    expect(activeNodeId(layoutFlow(run.nodes, run.edges), "in_progress")).toBe("code_task:ctr_fix");
    const done = graphOf("work_ci");
    expect(activeNodeId(layoutFlow(done.nodes, done.edges), "resolved")).toBeNull();
  });
});

describe("labels are labels, not sentences", () => {
  it("clamps a node to 4 words and an arrow to 3, marking the cut", () => {
    expect(clampWords("Roll back production payments to the last good release", NODE_WORDS)).toBe("Roll back production payments…");
    expect(clampWords("handed it off to", EDGE_WORDS)).toBe("handed it off…");
    expect(clampWords("  Needs   you ", NODE_WORDS)).toBe("Needs you");
  });

  it("clamps whatever the API sends, on nodes, arrows and thumbnails", () => {
    const raw = demoGraph("work_order_api") as { nodes: Array<{ label: string }>; edges: Array<{ label: string }> };
    raw.nodes[0].label = "an extremely long signal title that is really a paragraph";
    raw.edges[0].label = "raised it up to the chief";
    const g = parseGraph(raw);
    for (const n of g.nodes) expect(n.label.replace("…", "").split(" ").length).toBeLessThanOrEqual(NODE_WORDS);
    for (const e of g.edges) expect(e.label.replace("…", "").split(" ").length).toBeLessThanOrEqual(EDGE_WORDS);
    const list = parseList(demoList("all"));
    for (const it_ of list.items) {
      expect(it_.strip.length).toBeLessThanOrEqual(6);
      for (const s of it_.strip) expect(s.label.replace("…", "").split(" ").length).toBeLessThanOrEqual(NODE_WORDS);
    }
  });

  it("drops an arrow whose ends are not on the chart", () => {
    const raw = demoGraph("work_ci") as { edges: Array<Record<string, unknown>> };
    raw.edges.push({ from: "nowhere", to: "outcome", kind: "raised", label: "raised" });
    expect(parseGraph(raw).edges.some((e) => e.from === "nowhere")).toBe(false);
  });
});
