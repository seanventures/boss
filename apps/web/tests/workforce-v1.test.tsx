import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OrgChartArea, WorkforceArea } from "../app/workforce-ui";
import { codeOnly, cssOnly } from "./_helpers/source";
import type { OrgChart, V2Agent, Workforce } from "../lib/v2/workforce";

const agent = (over: Partial<V2Agent> = {}): V2Agent => ({
  id: "software_engineer", name: "Software Engineer", role: "Software Engineer",
  status: "IDLE", task: "last: solo", model: "claude-sonnet-5",
  tokens: 1000, spend_usd: 651, calls: 590, autonomy_level: 1, trust_score: 50,
  successful_runs: 0, violations: 0, hidden_failures: 0, last_seen_seconds: 60,
  run_id: null, project: null, last_touched: null, registered: true,
  ...over,
} as V2Agent);

const wf = (over: Partial<Workforce> = {}): Workforce => ({
  project_id: "p", window_hours: 24, apiReachable: true,
  counts: { WORKING: 0, STUCK: 0, IDLE: 1, "AT GATE": 0, total: 1 },
  agents: [agent()], handoffs: [], ...over,
} as Workforce);

const html = (w: Workforce) => renderToStaticMarkup(<WorkforceArea workforce={w} />);

describe("v1 Workforce", () => {
  it("shows what an agent produced, not just that it exists", () => {
    const out = html(wf({ agents: [agent({ ok_calls: 565, success_rate: 0.96, turns: 17474 })] }));
    expect(out).toContain("96%");
    expect(out).toContain("565/590 ok");   // the denominator travels with the rate
    expect(out).toContain("17,474");
  });

  it("groups digits without toLocaleString", () => {
    // Server and browser can disagree about locale, so toLocaleString renders "17,474" on one
    // and "17 474" on the other and React tears the tree down on hydration. A test already in
    // this repo forbids it, and it caught this file.
    expect(codeOnly("app/workforce-ui.tsx")).not.toContain("toLocaleString");
  });

  it("gives an agent that has done nothing no score rather than a zero", () => {
    // A row of zeros reads as "bad at its job" when it means "has not been asked".
    const out = html(wf({ agents: [agent({ calls: null, spend_usd: null, steps: null })] }));
    expect(out).not.toContain("wf-score-");
    expect(out).not.toContain("0%");
  });

  it("keeps the configured trust score visibly apart from the observed rate", () => {
    // trust starts at 50 and has never moved; unlabelled beside an observed rate it reads as
    // something the agent earned.
    expect(html(wf())).toContain("set");
  });

  it("flags an agent running work with no registered role", () => {
    const out = html(wf({ agents: [agent({ registered: false })] }));
    expect(out).toContain("no registered role");
    expect(out).toContain("nothing governs them");
  });

  it("says so rather than rendering an empty roster when the API is unreachable", () => {
    const out = html(wf({ apiReachable: false, agents: [] }));
    expect(out).toContain("would read as a company with no agents");
  });

  it("makes bar length volume and colour quality", () => {
    // Success rate barely varies — nearly everyone is at 100% — so length carrying it would
    // draw a column of identical full bars saying nothing.
    const src = codeOnly("app/workforce-ui.tsx");
    expect(src).toContain("volumeOf(a) / busiest");
    expect(src).toContain('data-tone={tone}');
  });

  it("is styled, not a bare list", () => {
    // Shipping an unstyled table once was enough.
    const css = cssOnly("app/globals.css");
    for (const cls of [".wf-card", ".wf-figs", ".wf-chip", ".org-node", ".org-chip"]) {
      expect(css, `${cls} has no styling`).toContain(cls);
    }
  });
});

const chart = (over: Partial<OrgChart> = {}): OrgChart => ({
  project_id: "p", window_hours: 168, apiReachable: true,
  derived_from: "observed handoffs", note: "", root_inferred: false,
  nodes: [], tree: [], roots: [], ...over,
} as OrgChart);

describe("v1 Org Chart", () => {
  it("does not print the role twice when it says the same as the name", () => {
    // "Product Manager / Product Manager" and "CTO / CTO" — the same duplication flagged
    // elsewhere on these screens.
    const out = renderToStaticMarkup(<OrgChartArea chart={chart({
      tree: [{ id: "cto", name: "CTO", role: "cto", depth: 0 } as never],
    })} />);
    expect(out.match(/CTO/g)?.length).toBe(1);
  });

  it("still shows a role that adds something", () => {
    const out = renderToStaticMarkup(<OrgChartArea chart={chart({
      tree: [{ id: "pm", name: "Nova", role: "product_manager", depth: 0 } as never],
    })} />);
    expect(out).toContain("Nova");
    expect(out).toContain("Product Manager");
  });

  it("says the hierarchy is observed rather than declared", () => {
    // BOSS has no reporting line. Presenting a derived shape as a declared one is a lie that
    // looks like a diagram.
    const out = renderToStaticMarkup(<OrgChartArea chart={chart({ derived_from: "handoffs" })} />);
    expect(out).toContain("Derived from handoffs");
  });

  it("says nothing rather than drawing an empty tree", () => {
    const out = renderToStaticMarkup(<OrgChartArea chart={chart()} />);
    expect(out).toContain("no observed structure to draw yet");
  });
});
