/**
 * Organization with the longest real names we can think of (boss-1, 2026-10-04: a Waiting row's
 * run name ran out of the Leadership card and over the Architecture card). This renders Teams,
 * the phone list, Flow's nodes and Stuck now, and the Agent panel with long agent, team, model,
 * ability, step and run names.
 *
 * The GEOMETRY is checked in a real browser by tests/e2e/selftest/test_org_boxes_never_overflow.py
 * (no element outside its card, no two text boxes on top of each other, at 1440, 1280 and 390):
 * it runs this file with ORG_LAYOUT_OUT set and loads the pages it writes. Here, the structure
 * that makes that true: every wait is two lines, its action apart from its text.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OrgAgentPanel } from "../app/agents/org-agent-panel";
import { OrgFlow } from "../app/agents/org-flow";
import { OrgTeams } from "../app/agents/org-teams";
import { parseOrgBoard } from "../lib/agents-org-board";
import { agentWire, edgeWire, orgWire, teamWire } from "./_helpers/org-board-fixtures";

const LONG_AGENT = "release_notes_and_changelog_coordinator_for_the_enterprise_platform";
const LONG_TEAM = "platform_reliability_and_developer_experience_guild";
const LONG_LABEL = "Create the tickets · Meeting to tickets — Q4 planning offsite follow-ups for the platform team";
const ago = (m: number) => new Date(Date.now() - m * 60_000).toISOString();
const wait = (role: string, id: string, attention: string | null, label = LONG_LABEL) => ({
  role_key: role, act_id: id, status: attention ? "awaiting_input" : "active", label, since: ago(1560),
  run_id: "wfr_1", attention_id: attention,
});

const LONG_AGENT_OVER = {
  open: [wait(LONG_AGENT, "w1", "dec_1")], open_total: 1, busy_seconds: 32400,
  spend: { metered_usd: 99.12, plan_calls: 4321 },
  models: [{ model: "claude-sonnet-4-6-20251001-extended-context-preview", calls: 1234 }],
  named_on: [{ workflow_key: "w", workflow_name: "Meeting to tickets for every planning offsite",
               step_id: "s", step_title: "Create the tickets in the enterprise tracker" }],
  gaps: { abilities: [{ ability: "a1", label: "Read the enterprise ticket tracker and its custom fields", needs_setup: false }],
          step_gaps: [] },
};

function board() {
  const wire = orgWire({
    teams: {
      leadership: { members: ["coo", "cto", "pm", "program_manager", "metatron", LONG_AGENT],
                    work: { done: 12, failed: 3, open: 4 }, needs_you: 2, waiting_you: 2,
                    waiting: [wait(LONG_AGENT, "w1", "dec_1"), wait("pm", "w2", "dec_2"), wait("cto", "w3", null)],
                    waiting_total: 3,
                    members_load: [{ role_key: LONG_AGENT, busy_seconds: 9 * 3600 }, { role_key: "pm", busy_seconds: 40 }],
                    spend: { metered_usd: 1234.56, plan_calls: 98765 },
                    models: [{ model: "claude-sonnet-4-6-20251001-extended-context-preview", calls: 1234 },
                             { model: "gpt-5-mini-2026-08-07-reasoning-high", calls: 99 }, { model: "x", calls: 1 }],
                    gaps: { abilities: [{ ability: "a1", label: "Read the enterprise ticket tracker and its custom fields",
                                          roles: [LONG_AGENT], needs_setup: false },
                                        { ability: "a2", label: "Team chat", roles: ["pm"], needs_setup: true }],
                            step_gaps: [{ workflow_key: "w", workflow_name: "A workflow with a very long name indeed", step_id: "s",
                                          step_title: "A step whose title goes on and on", role_key: "pm", ability: "a3",
                                          label: "Open pull requests on the monorepo", needs_setup: false }],
                            missing_roles: 2, kit_ok: true, kit_reason: null } },
      architecture: { waiting: [wait("chief_architect", "w4", "dec_3")], waiting_total: 1, needs_you: 1, waiting_you: 1,
                      work: { done: 3, failed: 0, open: 1 } },
    },
    agents: {},
    team_edges: [edgeWire("person", "leadership", 6), edgeWire("leadership", "architecture", 19),
                 edgeWire("architecture", LONG_TEAM, 12, { failed: 3, waiting: 2 }),
                 edgeWire(LONG_TEAM, "architecture", 4)],
    agent_edges: [edgeWire(LONG_AGENT, "chief_architect", 19), edgeWire("pm", LONG_AGENT, 6)],
    failures: [{ role_key: LONG_AGENT, act_id: "f1", label: LONG_LABEL, at: ago(5), run_id: "wfr_2", team: "leadership" }],
    missing_roles: [{ role_key: "data_engineer_for_the_analytics_warehouse_migration", team: "leadership",
                      steps: [{ workflow_key: "w", workflow_name: "W", step_id: "s", step_title: "S" }] }],
  }) as Record<string, unknown> & { teams: Record<string, unknown>[]; agents: Record<string, unknown>[] };
  wire.teams.push(teamWire(LONG_TEAM, { label: "Platform Reliability and Developer Experience Guild", members: ["sre"],
                                        flow_col: 2, waiting: [wait("sre", "w5", null)], waiting_total: 1,
                                        work: { done: 1, failed: 1, open: 1 } }));
  wire.teams = wire.teams.map((t) => (t.key === "leadership" ? { ...t, flow_col: 0 } : t.key === "architecture" ? { ...t, flow_col: 1 } : t));
  wire.agents.push(agentWire(LONG_AGENT, "leadership", LONG_AGENT_OVER));
  return parseOrgBoard(wire);
}

const noop = () => {};
function teams(narrow: boolean): string {
  const b = board();
  return renderToStaticMarkup(
    <OrgTeams projectId="p1" board={b} live={new Map()} search="" days={7} narrow={narrow} selected={null}
              onSelect={noop} onWorksWith={noop} onMakeAgent={noop} onRegister={noop} actions={null}
              focusTeam={narrow ? "leadership" : null} />);
}
function flow(): string {
  const b = board();
  return renderToStaticMarkup(
    <OrgFlow projectId="p1" board={b} live={new Map()} search="" days={7} narrow={false} isolate={null} onIsolate={noop}
             selected={null} onSelect={noop} onOpenEdge={noop} onWindow={noop} mode="map" onMode={noop} />);
}
function panel(): string {
  return renderToStaticMarkup(
    <OrgAgentPanel board={board()} live={new Map()} roleKey={LONG_AGENT} days={7} actions={null} onClose={noop} />);
}

function page(body: string): string {
  const web = resolve(__dirname, "..");
  return `<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="file://${join(web, "app/globals.css")}">
<link rel="stylesheet" href="file://${join(web, "app/agents/agent-world.css")}">
<style>body{margin:0;padding:16px} .frame{box-sizing:border-box;width:var(--frame)} @media (min-width:1024px){:root{--frame:calc(100vw - 340px)}}
@media (max-width:1023px){:root{--frame:100%}}</style></head>
<body><div class="frame"><div class="ag-org">${body}</div></div></body></html>`;
}

describe("Organization with very long names", () => {
  it("renders every wait as two lines, its action apart from its text", () => {
    const html = teams(false);
    expect(html).toContain("org-wait-what");
    expect(html).toContain("org-wait-act");
    expect(panel()).toContain("org-wait-what");
    expect(flow()).toContain("org-wait-what");
  });

  it("writes the pages the browser check measures (ORG_LAYOUT_OUT)", () => {
    const out = process.env.ORG_LAYOUT_OUT;
    if (!out) return;
    mkdirSync(out, { recursive: true });
    writeFileSync(join(out, "wide.html"), page(`${teams(false)}${flow()}${panel()}`));
    // The phone: the list, opened, and the panel as a bottom sheet.
    writeFileSync(join(out, "narrow.html"), page(`${teams(true)}${panel()}`));
  });
});
