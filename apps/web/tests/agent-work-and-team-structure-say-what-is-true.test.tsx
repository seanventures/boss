/**
 * Agent work and Team structure say what is true.
 *
 *  - "22 roles with recorded work" counted every registered role, idle ones included.
 *  - "Project spend is on the Projects screen" named a screen the Community rail calls Factory.
 *  - Team structure's "Reports to" offered roles that were not in the plan (it fell back to the
 *    observed tree), so a role could be saved reporting to somebody the chart did not contain.
 */
import { readFileSync } from "node:fs";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OrgChartArea, WorkforceArea } from "../app/workforce-ui";
import type { OrgChart, V2Agent, Workforce } from "../lib/v2/workforce";

const agent = (over: Partial<V2Agent> = {}): V2Agent => ({
  id: "qa", name: "QA", role: "QA", status: "IDLE", task: "", model: "m",
  tokens: 0, spend_usd: 0, calls: 0, steps: 0, autonomy_level: 0, trust_score: 50,
  successful_runs: 0, violations: 0, hidden_failures: 0, last_seen_seconds: null,
  run_id: null, project: null, last_touched: null, registered: true, ...over,
} as V2Agent);

describe("Agent work", () => {
  it("counts the roles that have done work, not every role", () => {
    const workforce = {
      project_id: "p", window_hours: 24, apiReachable: true,
      counts: { WORKING: 0, STUCK: 0, IDLE: 3, "AT GATE": 0, total: 3 },
      agents: [agent({ id: "a", calls: 4 }), agent({ id: "b" }), agent({ id: "c" })],
      handoffs: [],
    } as unknown as Workforce;
    const out = renderToStaticMarkup(<WorkforceArea workforce={workforce} actor="ceo" initialView="directory" />);
    // The Directory (the roster) counts every role; "Has done work" counts the ones with work.
    expect(out).toContain("Directory <em data-testid=\"wf-roster-count\">(3)</em>");
    expect(out).toContain("Has done work 1");
  });

  it("the spend note names the screens the rail has", () => {
    const engine = readFileSync(
      "../../engine/boss_engine/services/console_v2_workforce.py", "utf8");
    expect(engine).not.toContain("Projects screen");
    expect(engine).toContain("on its card in Factory, and the total is in Govern");
  });
});

describe("Team structure", () => {
  const chart = (over: Partial<OrgChart>): OrgChart => ({
    project_id: "p", window_hours: 168, apiReachable: true, derived_from: "", note: "",
    root_inferred: false, nodes: [], tree: [], roots: [], ...over,
  } as OrgChart);

  it("Reports to offers only roles in the plan", () => {
    const placed = { id: "cto", name: "CTO", role: "cto", depth: 0 } as never;
    const observedOnly = { id: "eng", name: "Engineer", role: "eng", depth: 1 } as never;
    const bench = { id: "qa", name: "QA", role: "qa", depth: 0, connected: false } as never;
    const out = renderToStaticMarkup(
      <OrgChartArea projectId="p" actor="ceo" chart={chart({
        nodes: [placed, observedOnly, bench],
        tree: [placed, observedOnly],
        declared: { cto: { reports_to: null } },
      })} />);
    const select = out.slice(out.indexOf('data-testid="org-bench-parent-qa"'),
                             out.indexOf("</select>", out.indexOf('data-testid="org-bench-parent-qa"')));
    expect(select).toContain('value="cto"');
    expect(select).not.toContain('value="eng"');
  });

  it("each list's buttons have their own testids", () => {
    const src = readFileSync("app/workforce-ui.tsx", "utf8");
    expect(src).toContain("data-testid={`org-bench-work-${n.id}`}");
    expect(src).toContain("data-testid={`org-alone-work-${n.id}`}");
    // The tree keeps the plain ones.
    expect(src.match(/data-testid=\{`org-work-\$\{n\.id\}`\}/g)?.length).toBe(1);
  });
});
