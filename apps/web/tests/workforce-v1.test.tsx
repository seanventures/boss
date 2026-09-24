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
  it("shows the current state and recent outcome without turning every role into a scorecard", () => {
    const out = html(wf({ agents: [agent({ ok_calls: 565, success_rate: 0.96, turns: 17474 })] }));
    expect(out).toContain("Software Engineer");
    expect(out).toContain("Idle");
    expect(out).toContain("Last: solo");
    expect(out).toContain("Details");
    expect(out).not.toContain("96%");
    expect(out).not.toContain("17,474");
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

  it("puts detail metrics behind each role's detail view", () => {
    expect(html(wf())).not.toContain("trust 50");
    expect(html(wf())).toContain('data-testid="wf-open-software_engineer"');
  });

  it("flags an agent running work with no registered role", () => {
    const out = html(wf({ agents: [agent({ registered: false })] }));
    expect(out).toContain("unregistered role");
    expect(out).toContain("Review authority before another run.");
  });

  it("says so rather than rendering an empty roster when the API is unreachable", () => {
    const out = html(wf({ apiReachable: false, agents: [] }));
    expect(out).toContain("would read as a company with no agents");
  });

  it("avoids summary bars and exposes search plus navigation", () => {
    const src = codeOnly("app/workforce-ui.tsx");
    expect(src).not.toContain("roster-scorebar");
    expect(src).toContain('aria-label="Find an agent role"');
    expect(src).toContain('openArea("org_chart")');
    expect(src).toContain('openArea("studio")');
  });

  it("is styled, not a bare list", () => {
    // Shipping an unstyled table once was enough.
    const css = cssOnly("app/globals.css");
    for (const cls of [".roster-card", ".roster-actions", ".roster-search", ".org-node"]) {
      expect(css, `${cls} has no styling`).toContain(cls);
    }
  });

  it("does not reuse class names another component already owns", () => {
    // I built these screens on a `wf-` prefix that capabilities, meeting-room, reports,
    // workflow and InfraScreen were already using — six collisions including .wf-card and
    // .wf-bar — so my rules silently restyled five other screens and theirs broke my layout
    // (the filter row collapsed to zero height under a foreign .wf-bar rule).
    const src = codeOnly("app/workforce-ui.tsx");
    const mine = new Set(
      [...src.matchAll(/className="([^"{}]+)"/g)]
        .flatMap((m) => m[1].split(/\s+/))
        .filter((c) => c && !c.startsWith("cap-") && !c.startsWith("detail")
                    && !c.startsWith("eyebrow") && !c.startsWith("section-")
                    && !c.startsWith("ct-")),
    );
    // Every class this file introduces must be namespaced to it.
    const stray = [...mine].filter((c) => !/^(roster|org)-/.test(c) && c !== "roster" && c !== "org");
    expect(stray, "classes outside this screen's namespace").toEqual([]);
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
    const node = { id: "cto", name: "CTO", role: "cto", depth: 0 } as never;
    const out = renderToStaticMarkup(<OrgChartArea projectId="boss-dogfood" actor="ceo" chart={chart({
      nodes: [node], tree: [node], declared: { cto: { reports_to: null } },
    })} />);
    expect(out.match(/CTO/g)?.length).toBe(1);
  });

  it("still shows a role that adds something", () => {
    const node = { id: "pm", name: "Nova", role: "product_manager", depth: 0 } as never;
    const out = renderToStaticMarkup(<OrgChartArea projectId="boss-dogfood" actor="ceo" chart={chart({
      nodes: [node], tree: [node], declared: { pm: { reports_to: null } },
    })} />);
    expect(out).toContain("Nova");
    expect(out).toContain("Product Manager");
  });

  it("keeps parent context and paths to work and definition on each role", () => {
    // Planned tree rows carry depth, while their parent lives in `declared`.
    const captain = { id: "captain", name: "Captain", role: "captain", depth: 0 } as never;
    const engineer = { id: "engineer", name: "Engineer", role: "engineer", depth: 1 } as never;
    const out = renderToStaticMarkup(<OrgChartArea projectId="p" actor="ceo" chart={chart({
      nodes: [captain, engineer], tree: [captain, engineer],
      declared: { captain: { reports_to: null }, engineer: { reports_to: "captain" } },
    })} />);
    expect(out.indexOf('data-testid="org-node-captain"')).toBeLessThan(
      out.indexOf('data-testid="org-node-engineer"'));
    expect(out).toContain("Reports to");
    expect(out).toContain('class="org-node-parent">Captain');
    expect(out).toContain('data-testid="org-work-engineer"');
    expect(out).toContain('data-testid="org-definition-engineer"');
  });

  it("separates planned structure from read-only observed handoffs", () => {
    const out = renderToStaticMarkup(<OrgChartArea projectId="boss-dogfood" actor="ceo" chart={chart({ derived_from: "handoffs" })} />);
    expect(out).toContain("Planned roles");
    expect(out).toContain("Actual handoffs");
    expect(out).toContain("This plan does not change task routing.");
    expect(codeOnly("app/workforce-ui.tsx")).toContain("Read-only work handoffs");
  });

  it("says nothing rather than drawing an empty tree", () => {
    const out = renderToStaticMarkup(
      <OrgChartArea chart={chart()} projectId="p" actor="ceo" />);
    expect(out).toContain("No planned roles yet.");
  });

  it("does not call an unreachable structure empty", () => {
    const out = renderToStaticMarkup(
      <OrgChartArea chart={chart({ apiReachable: false, blockedReason: "API returned 503" })}
        projectId="p" actor="ceo" />);
    expect(out).toContain("Team structure is unavailable");
    expect(out).not.toContain("No planned roles yet.");
  });

  it("invites the operator to place somebody", () => {
    // The chart was derived from observed handoffs and could not be edited, so it had no
    // effect anywhere in the product: a measurement nobody can act on is a report.
    const node = { id: "pm", name: "Product Manager", role: "product_manager", depth: 0 } as never;
    const out = renderToStaticMarkup(
      <OrgChartArea chart={chart({ nodes: [node] })} projectId="p" actor="ceo" />);
    expect(out).toContain("Add agent from template");
    expect(out).toContain("Choose a role to place");
  });
});
