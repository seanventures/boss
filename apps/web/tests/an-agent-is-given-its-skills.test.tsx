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

const { LoadoutGrants, offerSkills } = await import("../app/skills/agent-abilities");
const { CaptainCanRunIt } = await import("../app/delivery-workflows-ui");

const skill = (id: string, over: Record<string, unknown> = {}) => ({
  skill_id: id, label: id, summary: "", source: "builtin",
  behaviour_kind: "builtin", risk_tier: 0, enabled: true, ...over,
});

const render = (node: React.ReactElement) => renderToStaticMarkup(node);

/** The Passport's Loadout › Skills row (agent-loadout spec §3.2), holding `holds`. */
function loadout(holds: unknown[], available: unknown[] = [], canEdit = true) {
  return render(
    <LoadoutGrants projectId="p" actor="ceo" roleKey="video_editor" canEdit={canEdit}
                   skills={holds as never} available={available as never} servers={[]} availableServers={[]}
                   allowed={[]} forbidden={[]} onChanged={() => {}} onFromFile={() => {}} initialAbilities={[]} />);
}

describe("what an agent can do is on the agent, first", () => {
  it("lists the skills it actually holds, with what holding them costs", () => {
    const html = loadout([skill("video.apply_plan"), skill("publish_youtube", { risk_tier: 3 })]);
    expect(html).toContain('data-testid="agent-skill-video.apply_plan"');
    expect(html).toContain('data-testid="agent-skill-publish_youtube"');
    // A tier at or above the approval floor stops a run and waits for a person: said on the chip.
    const chip = html.split('data-testid="agent-skill-publish_youtube"')[1].split("</span></span>")[0];
    expect(chip).toContain('aria-label="Asks a person"');
  });

  it("an agent holding nothing is told what that means, not shown an empty box", () => {
    // The state every installed example was in: every step naming it is refused at the gate.
    const html = loadout([]);
    expect(html).toContain("No skills");
    expect(html).toContain("Steps naming it are refused until it holds one");
  });

  it("a grant left behind by a deleted skill is shown as off, not hidden", () => {
    const html = loadout([skill("gone", { missing: true })]);
    expect(html).toMatch(/data-off="true"[^>]*data-tip="Off, not in this project"[^>]*data-testid="agent-skill-gone"/);
  });

  it("offers + Add to an editor and nothing to a reader", () => {
    expect(loadout([], [skill("video.cut")])).toContain('data-testid="loadout-skills-add"');
    const reader = loadout([skill("video.cut")], [], false);
    expect(reader).not.toContain('data-testid="loadout-skills-add"');
    expect(reader).not.toContain('data-testid="agent-skill-video.cut-more"');
  });

  it("offers the role's own skills first and never a skill its spec forbids", () => {
    // Offering a forbidden skill and refusing it later is a control that lies about itself.
    const { itsRole, others } = offerSkills(
      [skill("video.cut"), skill("deploy_production"), skill("web.search")] as never,
      ["video.cut"], ["deploy_production"], [], "video_editor");
    expect(itsRole.map((s) => s.skill_id)).toEqual(["video.cut"]);
    expect(others.map((s) => s.skill_id)).toEqual(["web.search"]);
  });
});

describe("a step names an agent, and the builder checks it may run it", () => {
  const step = { id: "publish", kind: "capability", captain: "media_publisher" } as never;
  const mine = { skill_id: "publish_youtube" } as never;

  it("says nothing when the agent holds the skill — beyond confirming it", () => {
    const html = render(
      <CaptainCanRunIt projectId="p" actor="ceo" step={step} mine={mine} onGranted={() => {}}
                       holdings={{ media_publisher: ["publish_youtube"] }} />);
    expect(html).toContain("media_publisher holds publish_youtube");
    expect(html).not.toContain("does not hold");
  });

  it("warns when it does not, and offers the grant in the row", () => {
    // Without this, the failure surfaces AFTER the run has waited on a human approval — the
    // worst possible moment to learn the agent was never permitted to publish.
    const html = render(
      <CaptainCanRunIt projectId="p" actor="ceo" step={step} mine={mine} onGranted={() => {}}
                       holdings={{ media_publisher: ["video.cut"] }} />);
    expect(html).toContain("does not hold publish_youtube");
    expect(html).toContain("will be refused when it runs");
    expect(html).toContain("Give publish_youtube to media_publisher");
  });

  it("is silent with no agent named, or on a step that runs an MCP tool", () => {
    // An MCP tool's authority comes from the connection, not from a grant on the agent, so
    // there is nothing here to check and nothing to say.
    expect(render(
      <CaptainCanRunIt projectId="p" actor="ceo" step={{ id: "s", kind: "capability" } as never}
                       mine={mine} holdings={{}} onGranted={() => {}} />)).toBe("");
    expect(render(
      <CaptainCanRunIt projectId="p" actor="ceo" step={step} mine={{ skill_id: null } as never}
                       holdings={{}} onGranted={() => {}} />)).toBe("");
  });
});
