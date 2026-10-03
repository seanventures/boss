/**
 * A run says, per step, which agent ran it and why — and the builder offers the project's
 * agents, not a constant list.
 *
 * THE INCIDENTS. When BOSS picked the agent for a step, nothing in the console said which one
 * ran or on what grounds: the engine returned `agent`, `chosen_because` and `skills_used`, and
 * no file under apps/web read them. And the step-agent picker offered a hard-coded set of 29
 * role keys — five of which no project has — while an agent made on the Agents page never
 * appeared, and `human` was offered on steps a person cannot run.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

// The lane and the editor are other tests' business; they get stand-ins here.
vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));

import {
  AgentStepScope,
  HOW_CHOSEN, NOT_AN_AGENT, RanAs, StepAgentPicker, stalePick, stepAgentChip,
} from "../app/step-agents-ui";
import { codeOnly } from "./_helpers/source";

(globalThis as Record<string, unknown>).React = React;

describe("the run view says who ran each step, how they were chosen and why", () => {
  it("names the agent, the reason, and the skills offered against those used", () => {
    const html = renderToStaticMarkup(
      <RanAs stepId="edit" output={{ ran_as: {
        agent: "video_editor", how: "model", why: "it holds the trim and reframe skills",
        skills_offered: ["video.trim", "video.reframe"], skills_used: ["video.trim"],
        mcp_offered: ["drive"] } }} />);
    expect(html).toContain("Ran as <strong>video_editor</strong> — chosen by BOSS");
    expect(html).toContain("it holds the trim and reframe skills");
    expect(html).toContain("offered video.trim, video.reframe, MCP drive");
    expect(html).toContain("used video.trim");
  });

  it("says so when a built-in step ran as whoever started the run", () => {
    const html = renderToStaticMarkup(<RanAs stepId="build" output={{ ran_as: {
      agent: "ceo", how: "run_actor", why: "a built-in step runs BOSS's own tool" } }} />);
    expect(html).toContain("Ran as <strong>ceo</strong> — ran as whoever started the run");
  });

  it("draws nothing for a step that has not run", () => {
    expect(renderToStaticMarkup(<RanAs stepId="x" output={{}} />)).toBe("");
  });

  it("is drawn on every step of a run", () => {
    expect(codeOnly("app/delivery-workflows-ui.tsx"))
      .toContain("<RanAs output={step.output} stepId={step.step_id} />");
  });

  it("has words for every way the engine can say an agent was chosen", () => {
    // ENUMERATED FROM THE ENGINE, so a new `how` added there without words here fails now
    // instead of showing an operator a bare code.
    const engine = [
      codeOnly("../../engine/boss_engine/services/step_agents.py"),
      codeOnly("../../engine/boss_engine/services/agent_steps.py"),
      codeOnly("../../engine/boss_engine/services/workflow_capability_runner.py"),
    ].join("\n");
    const recorded = new Set<string>();
    for (const pattern of [/"how": "(\w+)"/g, /single_how="(\w+)"/g,
                           /_record\(\s*[\w"]+,\s*"(\w+)"/g]) {
      for (const match of engine.matchAll(pattern)) recorded.add(match[1]);
    }
    expect(recorded.size).toBeGreaterThanOrEqual(6);
    const missing = [...recorded].filter((how) => !(how in HOW_CHOSEN));
    expect(missing).toEqual([]);
  });
});

describe("the step-agent picker offers the project's agents", () => {
  const render = (isGate: boolean, value?: string) => renderToStaticMarkup(
    <StepAgentPicker value={value} onChange={() => undefined}
                     roles={["docs_writer", "video_editor", "human"]}
                     holdings={{ video_editor: ["video.trim"] }} isGate={isGate}
                     emptyLabel="whichever agent fits — BOSS picks" testId="t" ariaLabel="a" />);

  it("lists them with what each holds, and an empty choice that says what it means", () => {
    const html = render(false);
    expect(html).toContain("whichever agent fits — BOSS picks");
    expect(html).toContain("docs_writer · 0 skills");
    expect(html).toContain("video_editor · 1 skill");
  });

  it("offers a person on a gate, and only on a gate", () => {
    expect(render(true)).toContain("a person (human)");
    expect(render(false)).not.toContain('value="human"');
  });

  it("keeps a name the project no longer has visible, and says so", () => {
    expect(render(false, "engineering_captain"))
      .toContain("engineering_captain — not an agent on this project");
  });

  it("is fed from the capabilities payload, which now carries the project's roles", () => {
    const api = codeOnly("../../apps/api/app/main.py");
    // Integration: the roles are the project's roster (workflows.project_roster — its agents
    // plus the bundled roles it has not deleted), the same list publish accepts, so the picker
    // never offers what publish refuses nor hides what it accepts. No constant fallback.
    expect(api).toContain('"roles": ([r for r in sorted(roster) if r != step_agents.HUMAN] + [step_agents.HUMAN]');
    expect(api).toContain('"agents": [{"role_key": a["role_key"]');
    expect(api).not.toContain('"roles": sorted(REGISTERED_ROLES)');
    expect(api).not.toContain("roster or REGISTERED_ROLES");
  });
});

describe("a loaded 'human' on a step that is not a gate says why it cannot stay", () => {
  it("shows it with the reason instead of as the empty choice", () => {
    // THE INCIDENT: `human` was valid on any step before a person could only answer a gate.
    // Such a spec loaded as "whichever agent fits", and Save returned a 422 nothing on the
    // canvas explained.
    const html = renderToStaticMarkup(
      <StepAgentPicker value="human" onChange={() => undefined} roles={["docs_writer", "human"]}
                       holdings={{}} isGate={false} emptyLabel="BOSS picks" testId="t"
                       ariaLabel="a" />);
    expect(html).toContain("human — only a gate can wait for a person");
    expect(html).toMatch(/<option value="human" selected="">/);
    expect(stalePick("human", ["docs_writer"], true)).toBeNull();
  });
});

describe("the chip beside a run step never contradicts who ran it", () => {
  it("shows no default captain once a step has run as something other than an agent", () => {
    // THE INCIDENT: "engineering_captain — answers for this step" directly above "Ran as ceo —
    // ran as whoever started the run", on a project with no engineering_captain at all.
    for (const how of NOT_AN_AGENT) {
      expect(stepAgentChip(undefined, { ran_as: { agent: "ceo", how } },
                           "engineering_captain")).toBeNull();
    }
  });

  it("shows the agent that ran it, the one the workflow named, or someone to ask", () => {
    expect(stepAgentChip(undefined, { ran_as: { agent: "video_editor", how: "model" } },
                         "engineering_captain"))
      .toEqual({ role: "video_editor", note: "ran this step" });
    expect(stepAgentChip("publisher", { ran_as: { agent: "publisher", how: "named" } }, "x"))
      .toEqual({ role: "publisher", note: "named by this workflow" });
    expect(stepAgentChip(undefined, {}, "engineering_captain"))
      .toEqual({ role: "engineering_captain", note: "can answer questions about this step" });
  });

  it("is the one function the run view uses", () => {
    const view = codeOnly("app/delivery-workflows-ui.tsx");
    expect(view).toContain("stepAgentChip(step.captain, step.output, capability?.captain)");
    expect(view).not.toContain('"answers for this step"');
  });
});

describe("a step's MCP list narrows what its agent holds, and never grants", () => {
  // INTEGRATION of the agents stream (role_mcp_servers is THE grant the runtime reads) with the
  // steps stream (a step may name MCP servers): the runtime offers held ∩ named, so the builder
  // offers only servers an agent holds, and an empty list means the agent's own servers — it
  // said "no MCP servers", which stopped being true when agents could hold servers.
  it("says what an empty list means", () => {
    const html = renderToStaticMarkup(
      <AgentStepScope stepId="s" skills={[]} mcp={[]} available={[]}
                      servers={[{ id: "m1", name: "mattermost" }]} onChange={() => undefined} />);
    expect(html).toContain("may use every MCP server its agent holds");
    expect(html).not.toContain("no MCP servers");
  });
  // (Which MCP servers a step may reach is chosen in the inspector now, from what the step's
  // agent holds — tests/inspector.test.tsx.)
;
});
