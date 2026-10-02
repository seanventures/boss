/**
 * An agent is one thing: you create it with what it holds, edit it, give it MCP servers, and
 * delete it — or are told why you cannot yet.
 *
 * THE INCIDENTS. "Create agent" opened a five-step Studio wizard that demanded prompts, rules,
 * an artifact type and A2A topics, then left the new agent holding NOTHING: its skills went to
 * two lists the runtime never reads, so its own page said "This agent holds no skills" and every
 * step naming it was refused. There was no way at all to give an agent an MCP server, no edit
 * for its mission or boundaries, and no delete. These render the screens that now do each.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { codeOnly } from "./_helpers/source";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
}));

const { AgentMcp, AgentEdit, AgentDelete } = await import("../app/agent-detail-ui");
const { AgentQuickCreate } = await import("../app/agent-quick-create");
const { errorSentence } = await import("../lib/agents");

const render = (node: React.ReactElement) => renderToStaticMarkup(node);

const detail = (over: Record<string, unknown> = {}) => ({
  role_key: "release_auditor", name: "Release Auditor", registered: true,
  reports_to: "cto", spec: { mission: "Checks releases.", boundaries: { must: [], must_not: [] } },
  named_on_steps: [], studio_profiles: [], ...over,
}) as never;

describe("creating an agent is one screen that grants what it holds", () => {
  it("asks for name, purpose, reports-to, skills and MCP servers — and nothing else", () => {
    const html = render(<AgentQuickCreate projectId="p" actor="ceo" onClose={() => {}}
                                          onCreated={() => {}} onOpenStudio={() => {}} />);
    for (const id of ["aq-name", "aq-purpose", "aq-reports-to", "aq-skills", "aq-mcp", "aq-create"]) {
      expect(html).toContain(`data-testid="${id}"`);
    }
    // The prompts-and-rules envelope is still one press away, not mandatory.
    expect(html).toContain("full Studio wizard");
    expect(html).not.toContain("System prompt");
  });

  it("a name with no English letters gets a key field instead of an empty-key refusal", async () => {
    // Review round 1: 'காணொளி தொகுப்பாளர்' slugged to '', quick create had no key field, and
    // the API refused with "'' cannot be an agent key". Now the key field appears exactly when
    // the name makes no key, and left empty the server makes one.
    const { isAgentKey } = await import("../lib/agents");
    expect(isAgentKey("")).toBe(false);
    expect(isAgentKey("x")).toBe(false);
    expect(isAgentKey("video_editor")).toBe(true);
    const source = codeOnly("app/agent-quick-create.tsx");
    expect(source).toContain("const nameMakesNoKey = !!name.trim() && !isAgentKey(derived);");
    expect(source).toContain('data-testid="aq-key"');
    expect(source).toMatch(/nameMakesNoKey && key \? \{\s*roleKey: key\s*\}/);
  });

  it("writes through the agent resource as the actor it was handed", () => {
    const source = codeOnly("app/agent-quick-create.tsx");
    expect(source).toContain("createAgent(projectId, actor,");
    const lib = codeOnly("lib/agents.ts");
    expect(lib).toContain('send("/v1/agents", "POST"');
    expect(lib).toContain("mcp_servers:");
  });
});

describe("an agent holds MCP servers", () => {
  it("lists what it holds, narrowed or whole, and what holding it costs", () => {
    const html = render(<AgentMcp projectId="p" roleKey="publisher" actor="ceo" onChanged={() => {}}
      panel={{ role_key: "publisher", available: [], holds: [
        { mcp_server_id: "mcp_1", server: "mattermost", tools: ["post"], enabled: true, risk_tier: 2 },
        { mcp_server_id: "mcp_2", server: "github", tools: [], enabled: false, risk_tier: 1 },
      ] }} />);
    expect(html).toContain("2 servers held");
    expect(html).toContain("only post");
    expect(html).toContain("every tool it offers");
    expect(html).toContain("its calls wait for approval (tier 2)");
    // A disabled server is not offered, and the page says where to turn it on.
    expect(html).toContain("disabled in this project");
  });

  it("holding none says what that means, and where to register one when none exist", () => {
    const html = render(<AgentMcp projectId="p" roleKey="x" actor="ceo" onChanged={() => {}}
      panel={{ role_key: "x", holds: [], available: [] }} />);
    expect(html).toContain("offered no MCP tool");
    expect(html).toContain("register one on the Skills");
  });

  it("offers the grant when a server is available", () => {
    const html = render(<AgentMcp projectId="p" roleKey="x" actor="ceo" onChanged={() => {}}
      panel={{ role_key: "x", holds: [], available: [
        { mcp_server_id: "mcp_3", server: "drive", url: "http://d", enabled: true, risk_tier: 1 }] }} />);
    expect(html).toContain('data-testid="agent-mcp-open"');
  });
});

describe("an agent can be edited", () => {
  it("offers name, mission, boundaries and manager", () => {
    const html = render(<AgentEdit projectId="p" actor="ceo" detail={detail()} onChanged={() => {}} />);
    expect(html).toContain("change name, mission, boundaries or manager");
  });

  it("picks its manager from the project's agents, as quick create does", () => {
    // Review round 1: Agent detail's "Reports to" was free text expecting an agent key while
    // quick create offered a picker, so a typo was caught only by the API's refusal.
    const source = codeOnly("app/agent-detail-ui.tsx");
    expect(source).toMatch(/<select value=\{current\} data-testid="agent-edit-reports-to"/);
    expect(source).toContain("loadGrantable(projectId)");
  });

  it("and sends only what changed", () => {
    // POST /v1/roles as an update reset autonomy and reporting line whenever they were left
    // out; the edit sends only the fields the operator changed, through PATCH.
    const source = codeOnly("app/agent-detail-ui.tsx");
    expect(source).toContain("if (!Object.keys(changes).length)");
    expect(codeOnly("lib/agents.ts")).toContain('"PATCH"');
  });
});

describe("an agent can be deleted, or you are told why not", () => {
  it("a step naming it blocks the delete, and the steps are listed before anyone presses", () => {
    const html = render(<AgentDelete projectId="p" actor="ceo" onDeleted={() => {}}
      detail={detail({ named_on_steps: [{
        workflow_key: "ship", workflow: "Ship it", version: 3, step_id: "audit",
        step: "Audit the release", how: "runs it", because: "latest" }] })} />);
    expect(html).toContain("cannot be deleted while 1 workflow step names it");
    expect(html).toContain("Audit the release");
    expect(html).toMatch(/data-testid="agent-delete-open"[^>]*disabled|disabled[^>]*data-testid="agent-delete-open"/);
  });

  it("says which version names it, so the version that runs is never a bare number", () => {
    // Review of milestone/agents, round 2: the guard read only the newest version, so the
    // version that actually ran could lose its agent. The page now names the version's role.
    const html = render(<AgentDelete projectId="p" actor="ceo" onDeleted={() => {}}
      detail={detail({ named_on_steps: [{
        workflow_key: "ship", workflow: "Ship it", version: 1, step_id: "audit",
        step: "Audit the release", how: "runs it", because: "active", retirable: false }] })} />);
    expect(html).toContain("v1, the version it runs");
    expect(html).toMatch(/data-testid="agent-delete-open"[^>]*disabled|disabled[^>]*data-testid="agent-delete-open"/);
  });

  it("an older version alone does not block: the page says it is retired with the agent", () => {
    // Every startable version is guarded; without this way through, an agent once named in v1
    // of a workflow now at v5 could never be deleted.
    const html = render(<AgentDelete projectId="p" actor="ceo" onDeleted={() => {}}
      detail={detail({ named_on_steps: [{
        workflow_key: "ship", workflow: "Ship it", version: 1, step_id: "audit",
        step: "Audit the release", how: "runs it", because: "older", retirable: true }] })} />);
    expect(html).not.toContain("agent-delete-blocked");
    expect(html).toContain('data-testid="agent-delete-retires"');
    expect(html).toContain("Ship it v1");
    expect(html).not.toMatch(/data-testid="agent-delete-open"[^>]*disabled|disabled[^>]*data-testid="agent-delete-open"/);
    // And the delete it sends asks for exactly that.
    expect(codeOnly("app/agent-detail-ui.tsx")).toContain("retireOlderVersions: older.length > 0");
    expect(codeOnly("lib/agents.ts")).toContain("retire_older_versions=true");
  });

  it("otherwise says what deleting does before it is confirmed", () => {
    const html = render(<AgentDelete projectId="p" actor="ceo" onDeleted={() => {}}
      detail={detail({ studio_profiles: [{ id: "a", agent_key: "k", name: "K", status: "active" }] })} />);
    expect(html).toContain("skills and MCP servers are released");
    expect(html).toContain("Studio profile is retired");
    expect(html).toContain("An upgrade will not bring");
  });
});

describe("a refusal reaches the operator as a sentence", () => {
  it("flattens FastAPI's pydantic detail instead of printing [object Object]", () => {
    expect(errorSentence({ detail: [{ loc: ["body", "name"], msg: "Field required" }] }, 422))
      .toBe("name: Field required");
    expect(errorSentence({ detail: "an agent 'qa' already exists" }, 409))
      .toBe("an agent 'qa' already exists");
    expect(errorSentence({}, 403)).toContain("role_review");
  });
});
