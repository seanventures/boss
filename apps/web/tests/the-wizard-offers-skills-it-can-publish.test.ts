import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * A picker must not offer what publishing will refuse.
 *
 * `GET /v1/skills` returns every skill file on disk when it is called without a project — 71 on
 * this install. The entitlement broker filters by the project's edition bundles, so a project is
 * entitled to 23, and that smaller set is what Agent Studio validates a new agent against.
 *
 * The client called it unscoped, so the commission wizard listed all 71. Ticking any of the
 * other 48 got you to the last step and then failed with "Requested skills are not installed"
 * — naming a skill the wizard had just offered. Verified live against the running API before
 * and after: `/v1/skills` returns 71, `/v1/skills?project_id=boss-dogfood` returns 23, and a
 * publish with an entitled skill succeeds.
 */
const CLIENT = codeOnly("../../packages/api-client/src/community.ts");
const FEED = codeOnly("lib/skills.ts");
const PAGE = codeOnly("app/page.tsx");
const WORKFORCE = codeOnly("app/workforce-ui.tsx");

describe("the skill list is scoped to the project", () => {
  it("the client sends project_id", () => {
    expect(CLIENT).toContain("listSkills(projectId?: string)");
    expect(CLIENT).toContain("`?project_id=${encodeURIComponent(projectId)}`");
  });

  it("the feed takes a project and forwards it", () => {
    expect(FEED).toContain("fetchSkillFeed(projectId?: string)");
    expect(FEED).toContain("client.listSkills(projectId)");
  });

  it("the page passes the active project", () => {
    expect(PAGE).toContain("fetchSkillFeed(activeProjectId)");
  });
});

describe("the one New agent door offers the project's skills", () => {
  // REWRITTEN: there were two doors into the commission wizard (Agents and Agent Studio) and the
  // Agents one passed `skills={[]}`. There is one door now, New agent, and it reads what THIS
  // project's agents may be given, scoped server-side.
  const DIALOG = codeOnly("app/agent-quick-create.tsx");

  it("reads the grantable skills for the active project", () => {
    expect(DIALOG).toContain("loadGrantable(projectId)");
    expect(codeOnly("lib/agents.ts")).toContain("/v1/agents/grantable?project_id=${enc(projectId)}");
  });

  it("and nothing hands it an empty list", () => {
    expect(WORKFORCE).not.toContain("skills={[]}");
    expect(WORKFORCE).toContain("<AgentQuickCreate projectId={workforce.project_id}");
  });
});
