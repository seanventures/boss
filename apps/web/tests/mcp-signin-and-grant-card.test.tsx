// @vitest-environment happy-dom
/**
 * BOSSAPP-846..848 in the console: an OAuth server's drawer offers Sign in (and Reconnect when its
 * sign-in needs it), "Use my own account" when the server uses each person's account, the
 * credential policy as one control; the tools granted on request are listed with Revoke; and the
 * Grant card lists exactly the tools an agent asked for, with their tiers and why.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, cleanup, click, render } from "./_helpers/kit-render";
import { clone, installKitApi, KITS, SERVERS } from "./_helpers/kit-api";

const { McpDrawer } = await import("../app/skills/mcp-drawer");
const { AddMcpSheet } = await import("../app/skills/add-mcp-sheet");
const { GrantAccessCard, grantOf } = await import("../app/skills/grant-card");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const OAUTH_SERVER = { ...SERVERS[1], id: "mcps_lin", name: "linear", auth: { type: "oauth", header_name: null, set: false,
  last4: null, oauth: { authorization_server: "https://linear.app", client_source: "dynamic" } },
  auth_status: "needs_reauth", credential_policy: "per_user_fallback" };

function credentials(over: Record<string, unknown> = {}) {
  return { server_id: "mcps_lin", credential_policy: "per_user_fallback", auth_status: "needs_reauth",
           credentials: [{ id: "ccred_1", subject_kind: "org", subject_id: "org_1", kind: "oauth",
                           connected_as: "bot@acme.com", status: "needs_reauth", updated_at: null }],
           you: { ok: true, subject_kind: "org" }, ...over };
}

describe("signing in to an MCP server", () => {
  it("shows the shared sign-in needs a reconnect and opens the provider on Reconnect", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const calls = installKitApi(clone(KITS.fresh), (url, body) => {
      if (url.includes("view=credentials")) return credentials();
      if (url.includes("view=access_grants")) return { grants: [] };
      if (body?.action === "oauth_start") return { redirect: "https://linear.app/oauth/authorize?state=s" };
      if (body?.action === "oauth_mine") return { redirect: "https://linear.app/oauth/authorize?state=m" };
      return undefined;
    });
    await render(<McpDrawer projectId="p1" actor="ceo" server={OAUTH_SERVER as never} crew={[]} onClose={() => {}}
                            onChanged={() => {}} onRemoved={() => {}} />);
    expect($('[data-testid="kit-mcp-auth-status"]')!.textContent).toBe("Needs reconnect");
    expect($('[data-testid="kit-mcp-secret"]')).toBeNull();               // no token field for OAuth
    expect($('[data-testid="kit-mcp-policy-per_user_fallback"]')!.getAttribute("aria-checked")).toBe("true");
    const reconnect = $('[data-testid="kit-mcp-signin-shared"]')!;
    expect(reconnect.textContent).toBe("Reconnect");
    await click('[data-testid="kit-mcp-signin-shared"]');
    expect(calls.find((c) => c.body?.action === "oauth_start")!.body).toMatchObject({ server_id: "mcps_lin", subject: "org" });
    expect(open).toHaveBeenCalledWith("https://linear.app/oauth/authorize?state=s", "_blank", "noopener,noreferrer");
    // The server uses each person's account (else the shared one): "Use my own account".
    await click('[data-testid="kit-mcp-signin-mine"]');
    expect(calls.some((c) => c.body?.action === "oauth_mine")).toBe(true);
  });

  it("the credential policy is one control that writes the server's policy", async () => {
    const calls = installKitApi(clone(KITS.fresh), (url, body) => {
      if (url.includes("view=credentials")) return credentials();
      if (url.includes("view=access_grants")) return { grants: [] };
      if (body?.action === "credential_policy") return { credential_policy: body.policy };
      return undefined;
    });
    await render(<McpDrawer projectId="p1" actor="ceo" server={OAUTH_SERVER as never} crew={[]} onClose={() => {}}
                            onChanged={() => {}} onRemoved={() => {}} />);
    await click('[data-testid="kit-mcp-policy-per_agent"]');
    expect(calls.find((c) => c.body?.action === "credential_policy")!.body).toMatchObject({ policy: "per_agent" });
  });

  it("tools granted on request are listed where the agents are, with Revoke", async () => {
    const calls = installKitApi(clone(KITS.fresh), (url, body) => {
      if (url.includes("view=credentials")) return credentials();
      if (url.includes("view=access_grants")) return { grants: [{ id: "mag_1", server_id: "mcps_lin", role_key: "pm",
        tool: "create_issue", tier: 2, policy: "ask", status: "active", decision_id: "dec_1", reason: "file the bug",
        granted_by: "ceo", created_at: null }] };
      if (body?.action === "grant_revoke") return { id: "mag_1", status: "revoked" };
      return undefined;
    });
    await render(<McpDrawer projectId="p1" actor="ceo" server={OAUTH_SERVER as never} crew={[]} onClose={() => {}}
                            onChanged={() => {}} onRemoved={() => {}} />);
    expect($('[data-testid="kit-mcp-grant-pm-create_issue"]')!.textContent).toContain("Ask");
    await click('[data-testid="kit-mcp-grant-pm-create_issue"] button');
    expect(calls.find((c) => c.body?.action === "grant_revoke")!.body).toMatchObject({ grant_id: "mag_1" });
  });

  it("adding a server with OAuth registers it and opens the sign-in, with nothing to test first", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const calls = installKitApi(clone(KITS.fresh), (_url, body) => {
      if (body?.action === "register") return { ...OAUTH_SERVER, id: "mcps_new" };
      if (body?.action === "oauth_start") return { redirect: "https://linear.app/authorize?x=1" };
      return undefined;
    });
    const added = vi.fn();
    await render(<AddMcpSheet projectId="p1" actor="ceo" onClose={() => {}} onAdded={added} />);
    await click('[data-testid="kit-add-custom"]');
    const { type } = await import("./_helpers/kit-render");
    await type('[data-testid="kit-custom-name"]', "linear");
    await type('[data-testid="kit-custom-url"]', "https://mcp.linear.app/mcp");
    await click('[data-testid="kit-custom-auth-oauth"]');
    expect($('[data-testid="kit-custom-secret"]')).toBeNull();
    await click('[data-testid="kit-custom-save"]');
    expect(calls.some((c) => c.body?.action === "test")).toBe(false);
    expect(calls.find((c) => c.body?.action === "register")!.body!.server).toMatchObject({ auth: { type: "oauth" } });
    expect(calls.find((c) => c.body?.action === "oauth_start")!.body).toMatchObject({ server_id: "mcps_new" });
    expect(open).toHaveBeenCalled();
    expect(added).toHaveBeenCalledWith("mcps_new");
  });
});

describe("the Grant card", () => {
  const rec = { action: "grant_tool_access", server: "linear", role_key: "pm", reason: "I need to file the bug I found.",
                tools: [{ tool: "create_issue", tier: 2, policy: "ask" }, { tool: "read_issue", tier: 0, policy: "allow" }] };

  it("lists exactly the requested tools with their tiers, policies and the reason", async () => {
    const card = grantOf(rec)!;
    await render(<GrantAccessCard card={card} />);
    expect($('[data-testid="decision-grant-card"]')!.textContent).toContain("I need to file the bug I found.");
    expect($('[data-testid="decision-grant-tool-create_issue"]')!.textContent).toContain("Writes");
    expect($('[data-testid="decision-grant-tool-create_issue"]')!.textContent).toContain("Ask each call");
    expect($('[data-testid="decision-grant-tool-read_issue"]')!.textContent).toContain("Allow");
    expect(document.querySelectorAll('[data-testid^="decision-grant-tool-"]').length).toBe(2);
  });

  it("is drawn only for a tool request", () => {
    expect(grantOf({ action: "review_tool_change", tools: [] })).toBeNull();
    expect(grantOf(rec)!.tools.map((t) => t.tool)).toEqual(["create_issue", "read_issue"]);
  });
});
