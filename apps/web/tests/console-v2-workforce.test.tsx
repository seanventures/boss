import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { HiveScreen } from "../components/v2/HiveScreen";
import { OrgScreen } from "../components/v2/OrgScreen";
import { WorkforceScreen } from "../components/v2/WorkforceScreen";
import type { OrgChart, V2Agent, Workforce } from "../lib/v2/workforce";

/**
 * Workforce, Hive and Org Chart.
 *
 * v1 showed "Agents active 0/21" — a number that counted roles owning a task, when only 23 of
 * 187 tasks had an owner at all. It described the schema and said nothing about the agents.
 * These tests are about the screens describing agents.
 */

const agent = (over: Partial<V2Agent> = {}): V2Agent => ({
  id: "coder",
  name: "Coder",
  role: "Coder",
  status: "WORKING",
  task: "implement",
  model: "claude-opus-5",
  tokens: null,
  spend_usd: null,
  calls: null,
  autonomy_level: 1,
  trust_score: 50,
  successful_runs: 4,
  violations: 0,
  hidden_failures: 0,
  last_seen_seconds: 42,
  run_id: "run_1",
  project: "Shorty",
  last_touched: "src/routes/shorten.ts",
  ...over,
});

const wf = (over: Partial<Workforce> = {}): Workforce => ({
  project_id: "boss-dogfood",
  window_hours: 24,
  agents: [agent()],
  handoffs: [],
  counts: { WORKING: 1, STUCK: 0, IDLE: 0, "AT GATE": 0, total: 1 },
  apiReachable: true,
  ...over,
});

const wfHtml = (w: Workforce) => renderToStaticMarkup(<WorkforceScreen workforce={w} />);
const hiveHtml = (w: Workforce) => renderToStaticMarkup(<HiveScreen workforce={w} />);

describe("Workforce", () => {
  it("the headline count and the list come from the same place", () => {
    const out = wfHtml(wf({
      agents: [agent({ id: "a" }), agent({ id: "b", status: "STUCK" })],
      counts: { WORKING: 1, STUCK: 1, IDLE: 0, "AT GATE": 0, total: 2 },
    }));
    expect(out).toMatch(/2 agents/);
    expect(out).toContain('data-testid="v2-agent-a"');
    expect(out).toContain('data-testid="v2-agent-b"');
  });

  it("stuck agents are called out in the header, not left in the list", () => {
    // One sat wedged for 22 minutes before a human noticed. It should not need finding.
    const out = wfHtml(wf({ counts: { WORKING: 3, STUCK: 2, IDLE: 0, "AT GATE": 0, total: 5 } }));
    expect(out).toContain('data-testid="v2-workforce-stuck"');
    expect(out).toMatch(/2 stuck/);
  });

  it("a healthy workforce raises no alarm", () => {
    expect(wfHtml(wf())).not.toContain('data-testid="v2-workforce-stuck"');
  });

  it("shows the model that actually ran", () => {
    expect(wfHtml(wf())).toContain('data-testid="v2-agent-model-coder"');
  });

  it("says so when nothing ran, rather than showing a configured model", () => {
    // A configured model that never executed is a claim about intent, not about work.
    const out = wfHtml(wf({ agents: [agent({ model: null })] }));
    expect(out).toContain('data-testid="v2-agent-nomodel-coder"');
    expect(out).toMatch(/no calls recorded/);
  });

  it("surfaces violations", () => {
    // BOSS has recorded these all along and v1 never displayed them.
    const out = wfHtml(wf({ agents: [agent({ violations: 2, hidden_failures: 1 })] }));
    // The COUNT, not just the element. An empty badge renders as a testid with nothing in it,
    // which passed an earlier version of this test while showing the operator nothing.
    const badge = (id: string) =>
      out.match(new RegExp(`<span[^>]*data-testid="${id}"[^>]*>([^<]*)</span>`))![1];
    expect(badge("v2-agent-violations-coder")).toMatch(/2 violations/);
    expect(badge("v2-agent-hidden-coder")).toMatch(/1 hidden failure/);
  });

  it("a clean agent shows no violation badge", () => {
    expect(wfHtml(wf())).not.toContain('data-testid="v2-agent-violations-coder"');
  });

  it("never shows a per-agent cost, because BOSS does not record one", () => {
    // A time-window join faked this once and put the same $61.3802 on two different agents.
    // A zero would read as "this agent costs nothing" rather than "nobody knows".
    const out = wfHtml(wf({
      spend_attributed: false,
      spend_note: "BOSS does not record which agent an LLM call belongs to.",
    }));
    expect(out).toContain('data-testid="v2-workforce-no-spend"');
    expect(out).not.toMatch(/\$0\.00/);
  });

  it("the card says which work the agent is on and what it last touched", () => {
    // Both were missing. BOSS records them; the card simply did not read them, so it was
    // thinner than the data behind it.
    const out = wfHtml(wf());
    expect(out).toContain('data-testid="v2-agent-project-coder"');
    expect(out).toContain("Shorty");
    expect(out).toContain('data-testid="v2-agent-touched-coder"');
    expect(out).toContain("src/routes/shorten.ts");
  });

  it("shows a check-in time", () => {
    expect(wfHtml(wf())).toContain('data-testid="v2-agent-checkin-coder"');
  });

  it("shows no artefact line when the step recorded nothing", () => {
    // A plausible filename cannot be told apart from a real one.
    expect(wfHtml(wf({ agents: [agent({ last_touched: null })] })))
      .not.toContain('data-testid="v2-agent-touched-coder"');
  });

  it("an unreachable API shows no counts", () => {
    const out = wfHtml(wf({ apiReachable: false, blockedReason: "API returned 503", agents: [] }));
    expect(out).toContain('data-testid="v2-workforce-unreachable"');
    expect(out).not.toMatch(/0 agents/);
  });

  it("no roles at all is different from an outage", () => {
    const out = wfHtml(wf({ agents: [], counts: { WORKING: 0, STUCK: 0, IDLE: 0, "AT GATE": 0, total: 0 } }));
    expect(out).toContain('data-testid="v2-workforce-none"');
  });
});

describe("Hive", () => {
  it("draws a node per agent, whatever the count", () => {
    // The artifact hardcodes thirteen. A fixed layout would drop the extras or leave holes.
    const many = Array.from({ length: 9 }, (_, i) => agent({ id: `a${i}`, name: `A${i}` }));
    const out = hiveHtml(wf({ agents: many }));
    for (let i = 0; i < 9; i++) expect(out).toContain(`data-testid="v2-hive-node-a${i}"`);
  });

  it("every edge is a handoff that happened", () => {
    const out = hiveHtml(wf({
      agents: [agent({ id: "a" }), agent({ id: "b" })],
      handoffs: [{ source: "a", target: "b", times: 4, last_action: "review", last_at: null }],
    }));
    expect(out).toContain('data-testid="v2-hive-edge-a-b"');
    expect(out).toMatch(/4 handoffs/);
  });

  it("a busy route is drawn thicker than a quiet one", () => {
    // Every line the same weight makes the shape of the work invisible.
    const out = hiveHtml(wf({
      agents: [agent({ id: "a" }), agent({ id: "b" }), agent({ id: "c" })],
      handoffs: [
        { source: "a", target: "b", times: 20, last_action: null, last_at: null },
        { source: "a", target: "c", times: 1, last_action: null, last_at: null },
      ],
    }));
    const element = (id: string) =>
      out.match(new RegExp(`<path[^>]*data-testid="${id}"[^>]*>`))![0];
    const width = (s: string) => Number(s.match(/stroke-width="([\d.]+)"/)![1]);
    expect(width(element("v2-hive-edge-a-b")))
      .toBeGreaterThan(width(element("v2-hive-edge-a-c")));
  });

  it("edges are curves, not straight lines", () => {
    // The reference offsets the control point 28px perpendicular to the chord. Straight lines
    // read as a mesh; these read as flows, and beside the real thing the difference is the
    // whole character of the diagram.
    const out = hiveHtml(wf({
      agents: [agent({ id: "a" }), agent({ id: "b" })],
      handoffs: [{ source: "a", target: "b", times: 2, last_action: null, last_at: null }],
    }));
    const path = out.match(/<path[^>]*data-testid="v2-hive-edge-a-b"[^>]*>/)![0];
    const d = path.match(/d="M([\d.-]+) ([\d.-]+) Q([\d.-]+) ([\d.-]+) ([\d.-]+) ([\d.-]+)"/);
    expect(d, "edge should be a quadratic path").toBeTruthy();
    const [x1, y1, cx, cy, x2, y2] = d!.slice(1).map(Number);

    // The CONTROL POINT must sit off the chord. Checking only that a "Q" is present passes a
    // degenerate curve whose control point is the midpoint — which draws a straight line, and
    // which survived an earlier version of this test.
    const mx = (x1 + x2) / 2;
    const my = (y1 + y2) / 2;
    const offset = Math.hypot(cx - mx, cy - my);
    expect(offset, "control point is on the chord — this draws a straight line").toBeGreaterThan(20);

    expect(out).not.toMatch(/<line[^>]*v2-hive-edge/);
  });

  it("every node has a halo", () => {
    // A ring with a halo, not a filled disc. The halo is what makes status readable at a
    // glance across the whole graph.
    const out = hiveHtml(wf({ agents: [agent({ id: "a" }), agent({ id: "b" })] }));
    expect(out).toContain('data-testid="v2-hive-halo-a"');
    expect(out).toContain('data-testid="v2-hive-halo-b"');
  });

  it("the busiest agent takes the middle", () => {
    // Position means something: the hub is earned by traffic, not assigned alphabetically.
    const out = hiveHtml(wf({
      agents: [agent({ id: "quiet", name: "Quiet" }), agent({ id: "hub", name: "Hub" })],
      handoffs: [{ source: "quiet", target: "hub", times: 9, last_action: null, last_at: null }],
    }));
    const hub = out.match(/<g[^>]*data-testid="v2-hive-node-hub"[\s\S]*?<circle[^>]*class="v2-hive-node"[^>]*>/)![0];
    expect(hub).toContain('data-ring="hub"');
  });

  it("agents sit on two rings, not one circle", () => {
    // The outer ring is offset 0.35 rad from the inner one, which is why the reference reads
    // as scattered rather than concentric. One circle of evenly spaced dots is a different
    // picture.
    const many = Array.from({ length: 9 }, (_, i) => agent({ id: `a${i}`, name: `A${i}` }));
    const out = hiveHtml(wf({ agents: many }));
    expect(out).toMatch(/data-ring="inner"/);
    expect(out).toMatch(/data-ring="outer"/);
  });

  it("an edge to an agent that is not on the graph is dropped, not drawn to nowhere", () => {
    const out = hiveHtml(wf({
      agents: [agent({ id: "a" })],
      handoffs: [{ source: "a", target: "ghost", times: 2, last_action: null, last_at: null }],
    }));
    expect(out).not.toContain('data-testid="v2-hive-edge-a-ghost"');
  });

  it("no handoffs says what that means", () => {
    // "Quiet window" and "pipeline is not running" look identical; the page says both are
    // possible rather than implying the first.
    const out = hiveHtml(wf());
    expect(out).toContain('data-testid="v2-hive-no-handoffs"');
    expect(out).toMatch(/not running/);
  });
});

describe("Org Chart", () => {
  const chart = (over: Partial<OrgChart> = {}): OrgChart => ({
    project_id: "boss-dogfood",
    window_hours: 168,
    nodes: [{ ...agent({ id: "a" }), handoffs_in: 2, handoffs_out: 1, connected: true }],
    edges: [{ source: "b", target: "a", times: 2, last_action: "review", last_at: null }],
    derived_from: "observed handoffs",
    note: "BOSS has no declared reporting line. This is who actually passed work to whom.",
    isolated: [],
    apiReachable: true,
    ...over,
  });
  const html = (c: OrgChart) => renderToStaticMarkup(<OrgScreen chart={c} />);

  it("says on the page that it is derived, not declared", () => {
    // A derived shape presented as a declared structure is a lie that looks like a diagram.
    const out = html(chart());
    expect(out).toContain('data-testid="v2-org-provenance"');
    expect(out).toMatch(/no declared reporting line/);
  });

  it("shows who work comes from and goes to, with counts", () => {
    const out = html(chart());
    expect(out).toMatch(/receives from/);
    expect(out).toMatch(/b \(2\)/);
  });

  it("agents nobody works with get their own section", () => {
    // Invisible on a tree; each is idle by design or forgotten.
    const out = html(chart({
      nodes: [
        { ...agent({ id: "a" }), handoffs_in: 1, handoffs_out: 0, connected: true },
        { ...agent({ id: "hermit", name: "Hermit" }), handoffs_in: 0, handoffs_out: 0, connected: false },
      ],
      isolated: ["hermit"],
    }));
    expect(out).toContain('data-testid="v2-org-isolated-hermit"');
    expect(out).toMatch(/forgotten/);
  });

  it("no observed flow says so rather than drawing nothing", () => {
    const out = html(chart({
      nodes: [{ ...agent({ id: "a" }), handoffs_in: 0, handoffs_out: 0, connected: false }],
      edges: [],
      isolated: ["a"],
    }));
    expect(out).toContain('data-testid="v2-org-no-flow"');
  });

  it("an unreachable API is not an empty company", () => {
    const out = html(chart({ apiReachable: false, blockedReason: "boom", nodes: [], edges: [] }));
    expect(out).toContain('data-testid="v2-org-unreachable"');
  });
});
