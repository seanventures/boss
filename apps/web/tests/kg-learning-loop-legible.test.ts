/**
 * THE LEARNING LOOP, LEGIBLE (northpeak-lending, 2026-10-25): in Focus on a precedent the canvas
 * showed its neighbours but not what each edge was, and the panel did not say the loop in words.
 *
 * Breaks covered: a learning edge without its word, or two lines over each other for one pair
 * (a conversation that consulted then reused); "cited" lost; the story line naming a raw id, a
 * use in the run that learned it, or consulted before reused; the reuse card's baseline being the
 * run itself.
 */
import { describe, expect, it } from "vitest";

import { chipSpots, learningHow, mergeLearningEdges } from "../lib/kg-layout";
import { storyWords } from "../lib/kg-provenance";
import { learningWords } from "../app/knowledge/graph-canvas";
import { reuseBaseline } from "../app/knowledge/node-panel";
import type { KgLineage, KgProvenance, KgRunProof } from "../lib/kg-view-types";

const B3 = "athr_07fbefa3e030461b9418";
const B4 = "athr_b74e563c278d45978a1e";

describe("learning edges on the canvas", () => {
  it("says what each learning edge is, and leaves code edges alone", () => {
    expect(learningHow({ kind: "reused" })).toBe("reused");
    expect(learningHow({ kind: "consulted", how: "cited" })).toBe("cited");
    expect(learningHow({ kind: "consulted" })).toBe("consulted");
    expect(learningHow({ kind: "produced" })).toBe("produced");
    expect(learningHow({ kind: "about" })).toBe("about");
    expect(learningHow({ kind: "calls" })).toBeNull();
  });

  it("draws one line per pair, carrying its most telling word and the rest on hover", () => {
    const edges = [
      { src: "b4", dst: "p", kind: "consulted", how: "cited", weight: 1, label: null },
      { src: "b4", dst: "p", kind: "reused", weight: 1, label: null },
      { src: "b3", dst: "p", kind: "consulted", weight: 1, label: null },
      { src: "b3", dst: "p", kind: "produced", weight: 1, label: null },
      { src: "m1", dst: "m2", kind: "calls", weight: 4, label: "4 calls" },
    ];
    const merged = mergeLearningEdges(edges);
    expect(merged).toHaveLength(3);
    const b4 = merged.find((e) => e.src === "b4")!;
    expect(b4.kind).toBe("reused");
    expect(learningWords(b4)).toBe("reused · also cited");
    expect(merged.find((e) => e.src === "b3")!.kind).toBe("produced");
    expect(merged.find((e) => e.kind === "calls")).toEqual(edges[4]);
  });

  it("a live addition of an edge already drawn stays one line with one word", () => {
    const reused = { src: "b4", dst: "p", kind: "reused", weight: 1, label: null };
    const merged = mergeLearningEdges([reused, { ...reused }]);
    expect(merged).toHaveLength(1);
    expect(merged[0].also).toBeUndefined();
  });
});

describe("the story line", () => {
  const provenance = { conversation_title: "B3 · Incident: repayments post more than the customer paid" } as KgProvenance;
  const lineage: KgLineage = {
    learned: { run_id: null, agent_role: "sre", agent_name: "Site Reliability Engineer", step: "chat", at: null },
    used: [
      { run_id: B3, agent_role: "sre", agent_name: "Site Reliability Engineer", how: "consulted", at: "2026-10-25T16:14:46Z", run_title: "B3 · Incident: repayments post more than the customer paid" },
      { run_id: B4, agent_role: "sre", agent_name: "Site Reliability Engineer", how: "cited", at: "2026-10-25T16:15:23Z", run_title: "B4 · Incident: early repayments post more than the customer paid" },
      { run_id: B4, agent_role: "sre", agent_name: "Site Reliability Engineer", how: "reused", at: "2026-10-25T16:15:23Z", run_title: "B4 · Incident: early repayments post more than the customer paid" },
    ],
  };

  it("says who learned it, where, and where it was reused — from the usage edges", () => {
    expect(storyWords({ provenance, lineage })).toBe(
      "Learned by Site Reliability Engineer (SRE) in “B3 · Incident: repayments post more than the customer paid”"
      + " · reused by “B4 · Incident: early repayments post more than the customer paid”");
  });

  it("names another agent that reused it, and never a raw id", () => {
    const other = { ...lineage, used: [{ ...lineage.used[2], agent_name: "Atlas", agent_role: "chief_architect" },
                                       { ...lineage.used[2], run_id: "athr_x", run_title: "Conversation athr_x" }] };
    const s = storyWords({ provenance, lineage: other })!;
    expect(s).toContain("reused by “B4 · Incident: early repayments post more than the customer paid” by Atlas");
    expect(s).not.toMatch(/athr_/);
  });

  it("says when nothing later used it, and nothing for a node with no lineage", () => {
    expect(storyWords({ provenance, lineage: { ...lineage, used: [lineage.used[0]] } })).toMatch(/· not reused yet$/);
    expect(storyWords({ provenance, lineage: null })).toBeNull();
  });
});

describe("the reuse card's baseline", () => {
  const item = (learned_in: string | null, kind = "precedent") => ({ id: `kgn_${kind}${learned_in}`, kind, title: kind, how: "cited" as const, learned_in, learned_by: "SRE" });
  const proof = (o: Partial<KgRunProof>): KgRunProof => ({
    run_id: B4, status: null, knowledge_graph: "on", steps: 7, turns: 1, tokens: 0, cost_usd: 0, wall_seconds: null,
    repair_rounds: 0, learned: 1, consulted: [], reused: [], ...o,
  });

  it("is the run that produced what it reused, then what it consulted — never itself", () => {
    expect(reuseBaseline(proof({ consulted: [item(B4, "root_cause"), item("athr_other")], reused: [item(B3)] }))).toBe(B3);
    expect(reuseBaseline(proof({ consulted: [item(B4, "root_cause"), item(B3, "investigation")] }))).toBe(B3);
    expect(reuseBaseline(proof({ consulted: [item(B4, "root_cause"), item(null, "rule")] }))).toBeNull();
  });
});

describe("a learning edge's chip when its midpoint is taken", () => {
  it("offers places along the line and beside it, clear of the line", () => {
    const spots = chipSpots({ x: 0, y: 0 }, { x: 100, y: 0 }, 60, 18);
    expect(spots.slice(0, 2)).toEqual([{ x: 35, y: 0 }, { x: 65, y: 0 }]);
    // Beside a horizontal line: above and below by half the chip's height and a margin.
    expect(spots).toContainEqual({ x: 50, y: 13 });
    expect(spots).toContainEqual({ x: 50, y: -13 });
    // Beside a vertical line: left and right by half its width and a margin.
    expect(chipSpots({ x: 0, y: 0 }, { x: 0, y: 100 }, 60, 18)).toContainEqual({ x: -34, y: 50 });
  });
});
