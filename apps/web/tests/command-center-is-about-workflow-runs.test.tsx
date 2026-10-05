/**
 * The Command Center shows what needs the operator, what is running and how it went — in runs.
 *
 * The old landing page read `tasks`. A run started by Build creates no task, so the thing the
 * operator had just started never appeared; "Live coordination" animated idle orbs from a
 * field nothing populates; "Pending decisions" linked nowhere; two buttons were dead links.
 *
 * PORTED to the bridge (command-center spec §10.2): the page reads GET /v1/command-center/view,
 * which IS Agents › Live's board plus its own reads, so these fixtures are the view's wire shape.
 * The assertions are the same ones, plus the truth bug the port fixed: a card the server allows
 * to be decided in place IS decided in place here (this page used to force it off).
 */
import { readFileSync } from "node:fs";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { CommandCenter } from "../app/command-center-ui";
import {
  agentPhrase,
  decisionHref,
  elapsedLabel,
  needsYouLabel,
  runHref,
} from "../lib/command-center";
import { parseCommandCenterView } from "../lib/command-center-view";
import { failed, freshView, gate, view } from "./_helpers/command-center-view-fixture";

const refusal = {
  id: "dec_9", kind: "approval", decision_kind: "refusal", decision_id: "dec_9",
  title: "Model capacity refused", detail: "No model had credit.", what_to_do: "Add credit, then Retry the step.",
  role_key: null, actions: [{ action: "reject", label: "Dismiss", tone: "reject", effect: "e", needs: [] }],
  answers: [], decide_inline: false, fix: { area: "setup", label: "Fix in Repo and models" }, at: null,
};
const setupGap = {
  id: "ready:github", kind: "readiness", title: "Connect a repository", detail: "",
  fix: { area: "setup", label: "Fix in Repo and models", focus: "repository" }, actions: [], answers: [],
  decide_inline: false, at: null,
};
const DATA = parseCommandCenterView(view({
  attention: [
    failed({ answers: [{ key: "retry_step", label: "Try build again", enabled: true, reason: null, step_id: "build" }] }),
    gate({ decide_inline: false }),
    refusal,
    setupGap,
  ],
}));

const render = (v = DATA) => renderToStaticMarkup(
  <CommandCenter projectId="p" orgId="org_1" actor="ceo" initial={v} />);
const html = render();

describe("what is running", () => {
  it("shows the run, its current step, the agent on it and how long it has been there", () => {
    expect(html).toContain("Ship it #182");
    expect(html).toContain("Build");
    expect(html).toContain("12m");
    // The shared run row (agents-home §3.3): steps done, never a percent.
    expect(html).toContain("step 4 of 7");
    // In what the page SAYS, not its markup: an SVG gradient's `offset="50%"` is geometry.
    const said = html.replace(/<[^>]*>/g, " ");
    expect(said).not.toMatch(/\d+%/);
  });

  it("links straight into the run", () => {
    expect(html).toContain(`href="${runHref("wfr_182", "org_1").replace(/&/g, "&amp;")}"`);
  });
});

describe("what needs you", () => {
  it("a gate the server keeps for Approvals links to its decision", () => {
    expect(html).toContain(decisionHref("dec_gate", "org_1").replace(/&/g, "&amp;"));
    expect(html).toContain("Review ↗");
    expect(html).toContain("Publish");
  });

  it("a gate the server allows in place is decided in place (truth bug 4)", () => {
    const inline = render(parseCommandCenterView(view({ attention: [gate({ decide_inline: true })] })));
    expect(inline).toContain('data-testid="ag-att-approve"');
    expect(inline).toContain("Publish it");
  });

  it("a failed step says why and offers Retry", () => {
    expect(html).toContain("The sandbox ran out of disk.");
    // The run's own answer, with its own label.
    expect(html).toContain('data-testid="ag-att-answer-retry_step"');
    expect(html).toContain("Try build again");
  });

  it("offers Retry only when the run itself offers it", () => {
    const without = render(parseCommandCenterView(view({ attention: [failed({ answers: [
      { key: "retry_step", label: "Try build again", enabled: false,
        reason: "a newer attempt is running", step_id: "build" }] })] })));
    expect(without).toContain("The sandbox ran out of disk.");
    // Drawn, but not pressable, and it says why.
    expect(without).toMatch(/data-testid="ag-att-answer-retry_step"[^>]*disabled/);
    expect(without).toContain('title="a newer attempt is running"');
  });

  it("a refusal is reviewed in Approvals, never approved from here; setup links to its row", () => {
    expect(html).toContain("No model had credit.");
    expect(html).toContain(decisionHref("dec_9", "org_1").replace(/&/g, "&amp;"));
    expect(html).not.toMatch(/data-att-id="dec_9"[\s\S]*?data-testid="ag-att-approve"[\s\S]*?data-att-id/);
    expect(html).toContain('data-testid="cc2-setup"');
    expect(html).toContain("Fix in Repo and models");
    expect(html).toContain("focus=repository");
    expect(html).toContain("area=setup");
  });

  it("a setup gap is listed, and lands on its row in Repo and models", () => {
    expect(html).toContain("Connect a repository");
    expect(html).toContain("area=setup&amp;focus=repository");
  });
});

it("recent outcomes link to their runs", () => {
  expect(html).toContain("Ship it #179");
  expect(html).toContain("wfr_169");
});

it("an empty factory says so, and how to start", () => {
  const empty = render(parseCommandCenterView(freshView()));
  expect(empty).toContain('data-testid="cc2-needs-you-empty"');
  expect(empty).toContain('data-testid="cc2-running-empty"');
  expect(empty).toContain("area=delivery_workflows");
});

it("an unreachable API is said, not shown as an empty factory", () => {
  const down = renderToStaticMarkup(
    <CommandCenter projectId="p" actor="ceo" initial={null} initialError="Postgres is not reachable." />);
  expect(down).toContain('data-testid="cc2-load-error"');
  expect(down).toContain("Can&#x27;t reach BOSS");
  // The reason is in the tooltip, with the time.
  expect(down).toContain("Postgres is not reachable.");
  expect(down).not.toContain('data-testid="cc2-needs-you-empty"');
});

describe("the words", () => {
  it("elapsed reads like a person would say it", () => {
    expect(elapsedLabel(42)).toBe("42s");
    expect(elapsedLabel(12 * 60)).toBe("12m");
    expect(elapsedLabel(65 * 60)).toBe("1h 05m");
    expect(elapsedLabel(3 * 86400 + 4 * 3600)).toBe("3d 4h");
    expect(elapsedLabel(null)).toBe("");
  });

  it("the agent says how it came to be on the step", () => {
    expect(agentPhrase({ role_key: "qa", name: "QA", how: "picked" })).toContain("picked by BOSS");
    expect(agentPhrase({ role_key: null, name: null, how: "auto" })).toContain("BOSS picks");
    expect(agentPhrase({ role_key: null, name: "You", how: "person" })).toBe("Waiting on you");
  });

  it("each approval type has its own label", () => {
    const base = { kind: "approval" as const, title: "", detail: "", actions: [], at: null };
    expect(needsYouLabel({ ...base, decision_kind: "pull_request" })).toBe("PR merge");
    expect(needsYouLabel({ ...base, decision_kind: "refusal" })).toBe("Refused");
    expect(needsYouLabel({ ...base, decision_kind: "deploy_gate" })).toBe("Deploy gate");
  });
});

it("the page mounts it with its one read in place of the task-era panels", () => {
  const src = readFileSync("app/page.tsx", "utf8");
  expect(src).toContain("<CommandCenter");
  expect(src).toContain('fetchCommandCenterView(activeProjectId, ccZone, undefined, needs("overview"))');
  expect(src).not.toContain("fetchCommandCenter(");
  expect(src).not.toContain('area="apps"');
  expect(src).not.toContain('<div className="cc-kpis">');
  expect(src).not.toContain("Pending decisions");
});
