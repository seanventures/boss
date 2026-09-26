/**
 * An agent is where you say what it can do.
 *
 * WHAT WAS WRONG. `role_skills` has always been many-to-many — an agent holds many skills, a
 * skill is held by many agents — but the only place to WRITE it was the Skills page: pick a
 * skill, choose an agent from a dropdown, one skill at a time. Nobody builds a team that way.
 * They open the agent and say what it can do.
 *
 * AND THE AGENT PAGE SHOWED THE WRONG LIST. It rendered `spec.allowed_skills`, which is prose in
 * a spec pack meaning "may be given these". `skill_runtime` does not read that; it reads
 * `role_skills`. So an agent with eleven allowed skills and no grants looked fully capable and
 * would be refused by every step that named it — which is exactly the state five installed
 * example workflows were found in.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
}));

const { AgentSkills } = await import("../app/agent-detail-ui");
const { CaptainCanRunIt } = await import("../app/delivery-workflows-ui");

const skill = (id: string, over: Record<string, unknown> = {}) => ({
  skill_id: id, label: id, summary: "", source: "builtin",
  behaviour_kind: "builtin", risk_tier: 0, enabled: true, ...over,
});

function panel(holds: unknown[], available: unknown[]) {
  return { role_key: "video_editor", holds, available } as never;
}

const render = (node: React.ReactElement) => renderToStaticMarkup(node);

describe("what an agent can do is on the agent", () => {
  it("lists the skills it actually holds, with what holding them costs", () => {
    const html = render(
      <AgentSkills projectId="p" roleKey="video_editor" onChanged={() => {}}
                   spec={{}}
                   panel={panel([skill("video.apply_plan"),
                                 skill("publish_youtube", { risk_tier: 3 })], [])} />);
    expect(html).toContain("video.apply_plan");
    expect(html).toContain("2 skills held");
    // A tier at or above the approval floor stops a run and waits for a person. That is a
    // property of GIVING the skill out, so it belongs where the giving happens.
    expect(html).toContain("needs approval each time (tier 3)");
  });

  it("an agent holding nothing is told what that means, not shown an empty box", () => {
    // The state every installed example was in. "No skills" is not a cosmetic gap: every step
    // naming this agent is refused at the gate.
    const html = render(
      <AgentSkills projectId="p" roleKey="video_editor" onChanged={() => {}}
                   spec={{ allowed_skills: ["video.cut", "video.reframe"] }}
                   panel={panel([], [skill("video.cut")])} />);
    expect(html).toContain("holds no skills");
    expect(html).toContain("refused at the gate");
    // And it names the ones its own spec says it may have, because that is the next click.
    expect(html).toContain("video.cut");
  });

  it("a grant left behind by a deleted skill is shown as broken, not hidden", () => {
    const html = render(
      <AgentSkills projectId="p" roleKey="video_editor" onChanged={() => {}} spec={{}}
                   panel={panel([skill("gone", { missing: true })], [])} />);
    expect(html).toContain("this grant does");
  });

  it("offers a skill to give, and says so when there is nothing left to give", () => {
    const withMore = render(
      <AgentSkills projectId="p" roleKey="video_editor" onChanged={() => {}} spec={{}}
                   panel={panel([], [skill("video.cut")])} />);
    expect(withMore).toContain("Give this agent a skill");

    const exhausted = render(
      <AgentSkills projectId="p" roleKey="video_editor" onChanged={() => {}} spec={{}}
                   panel={panel([skill("video.cut")], [])} />);
    expect(exhausted).toContain("already holds every skill");
    expect(exhausted).not.toContain("Give this agent a skill");
  });

  it("a skill the spec forbids is not offered, and the reason is stated", () => {
    // Offering a forbidden skill and refusing it later is a control that lies about itself.
    const html = render(
      <AgentSkills projectId="p" roleKey="video_editor" onChanged={() => {}}
                   spec={{ forbidden_skills: ["deploy_production"] }}
                   panel={panel([], [skill("deploy_production"), skill("video.cut")])} />);
    expect(html).toContain("Its spec forbids deploy_production");
    // The picker itself must not carry it as an option.
    const options = html.split("<select")[1] ?? "";
    expect(options.split("</select>")[0]).not.toContain("deploy_production");
  });
});

describe("a step names an agent, and the builder checks it may run it", () => {
  const step = { id: "publish", kind: "capability", captain: "media_publisher" } as never;
  const mine = { skill_id: "publish_youtube" } as never;

  it("says nothing when the agent holds the skill — beyond confirming it", () => {
    const html = render(
      <CaptainCanRunIt projectId="p" step={step} mine={mine} onGranted={() => {}}
                       holdings={{ media_publisher: ["publish_youtube"] }} />);
    expect(html).toContain("media_publisher holds publish_youtube");
    expect(html).not.toContain("does not hold");
  });

  it("warns when it does not, and offers the grant in the row", () => {
    // Without this, the failure surfaces AFTER the run has waited on a human approval — the
    // worst possible moment to learn the agent was never permitted to publish.
    const html = render(
      <CaptainCanRunIt projectId="p" step={step} mine={mine} onGranted={() => {}}
                       holdings={{ media_publisher: ["video.cut"] }} />);
    expect(html).toContain("does not hold publish_youtube");
    expect(html).toContain("will be refused when it runs");
    expect(html).toContain("Give publish_youtube to media_publisher");
  });

  it("is silent with no agent named, or on a step that runs an MCP tool", () => {
    // An MCP tool's authority comes from the connection, not from a grant on the agent, so
    // there is nothing here to check and nothing to say.
    expect(render(
      <CaptainCanRunIt projectId="p" step={{ id: "s", kind: "capability" } as never}
                       mine={mine} holdings={{}} onGranted={() => {}} />)).toBe("");
    expect(render(
      <CaptainCanRunIt projectId="p" step={step} mine={{ skill_id: null } as never}
                       holdings={{}} onGranted={() => {}} />)).toBe("");
  });
});
