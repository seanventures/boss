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
  it("shows the run, what its agent is doing, the step it is on and how long it has been there", () => {
    expect(html).toContain("Ship it #182");
    expect(html).toContain("Coding");
    expect(html).toContain("12m");
    // Steps done, never a percent.
    expect(html).toContain("step 4/7");
    // In what the page SAYS, not its markup: an SVG gradient's `offset="50%"` is geometry.
    const said = html.replace(/<style[\s\S]*?<\/style>/g, "").replace(/<[^>]*>/g, " ");
    expect(said).not.toMatch(/\d+%/);
  });

  it("links straight into the run", () => {
    expect(html).toContain(`href="${runHref("wfr_182", "org_1").replace(/&/g, "&amp;")}"`);
  });
});

describe("what needs you", () => {
  // Each item is a sentence with a Review button that opens the shared AttentionCard in place
  // (the server's buttons: approve/reject inline when it allows, Review ↗ into Approvals, the
  // run's own Retry) — pressed and checked in tests/command-center-v2.test.tsx.
  it("every item is listed as a sentence, with Review", () => {
    expect(html).toContain("“Build” failed on Ship it #180");
    expect(html).toContain("“Publish” is waiting for you on Weekly video");
    expect(html).toContain("Model capacity refused");
    expect(html).toContain('data-testid="cc-need-review"');
  });

  it("a failed step says why, in a line", () => {
    expect(html).toContain("The sandbox ran out of disk.");
  });

  it("nothing is approved from the page's first paint: a decision is one Review press away", () => {
    expect(html).not.toContain('data-testid="ag-att-approve"');
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
  expect(empty).toContain("Nothing running right now");
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
