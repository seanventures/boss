// @vitest-environment happy-dom
/**
 * BOSSAPP-873 in the console: the MCP drawer's upstream-headers controls read the server's
 * headers policy (secret values never shown, last four only) and Save sends the whole policy back
 * — forwarded metadata, the allowlist, kept secrets as {keep: true}, a new secret as typed, and a
 * removed one left out.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { headersPolicyInput } from "../lib/kit-api";
import { $, cleanup, click, render } from "./_helpers/kit-render";
import { clone, installKitApi, KITS, SERVERS } from "./_helpers/kit-api";

const { McpDrawer } = await import("../app/skills/mcp-drawer");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const VIEW = { server_id: SERVERS[0].id, forward_choices: [
  { key: "run_id", header: "X-BOSS-Run-Id" }, { key: "agent", header: "X-BOSS-Agent" },
  { key: "traceparent", header: "traceparent" }],
  headers_policy: { allow: null, forward: ["agent"],
                    secret_headers: { "X-Tenant-Key": { set: true, last4: "0042" }, "X-Old": { set: true, last4: "9999" } } } };

function type(sel: string, value: string) {
  const box = $(sel) as HTMLInputElement;
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(box, value);
  box.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("the headers policy input", () => {
  it("keeps, removes and adds secrets and reads the allowlist", () => {
    expect(headersPolicyInput(VIEW.headers_policy as never, { allow: "X-A, X-B", forward: ["run_id"],
      remove: ["X-Old"], add: { name: "X-New", value: "v" } })).toEqual({
      forward: ["run_id"], allow: ["X-A", "X-B"],
      secret_headers: { "X-Tenant-Key": { keep: true }, "X-New": "v" } });
    expect(headersPolicyInput({ allow: null, forward: [], secret_headers: {} },
                              { allow: " ", forward: [], remove: [], add: null })).toEqual({ forward: [] });
  });
});

describe("the drawer's upstream headers", () => {
  it("shows last four only and saves the whole policy", async () => {
    const calls = installKitApi(clone(KITS.fresh), (url, body) => {
      if (url.includes("view=headers_policy")) return VIEW;
      if (url.includes("view=credentials")) return { server_id: SERVERS[0].id, credential_policy: "shared",
        auth_status: null, credentials: [], you: null };
      if (url.includes("view=access_grants")) return { grants: [] };
      if (body?.action === "headers_policy") return { headers_policy: VIEW.headers_policy };
      return undefined;
    });
    await render(<McpDrawer projectId="p1" actor="ceo" server={SERVERS[0] as never} crew={[]} onClose={() => {}}
                            onChanged={() => {}} onRemoved={() => {}} />);
    expect($('[data-testid="kit-mcp-headers-secret-X-Tenant-Key"]')!.textContent).toContain("••••0042");
    expect(($('[data-testid="kit-mcp-headers-forward-agent"]') as HTMLInputElement).checked).toBe(true);
    await click('[data-testid="kit-mcp-headers-forward-run_id"]');
    await click('[data-testid="kit-mcp-headers-remove-X-Old"]');
    type('[data-testid="kit-mcp-headers-allow"]', "X-Tenant-Key");
    type('[data-testid="kit-mcp-headers-name"]', "X-Region");
    type('[data-testid="kit-mcp-headers-value"]', "eu-west");
    await click('[data-testid="kit-mcp-headers-save"]');
    const sent = calls.find((c) => c.body?.action === "headers_policy")!.body;
    expect(sent).toMatchObject({ project_id: "p1", server_id: SERVERS[0].id, headers_policy: {
      forward: ["agent", "run_id"], allow: ["X-Tenant-Key"],
      secret_headers: { "X-Tenant-Key": { keep: true }, "X-Region": "eu-west" } } });
  });
});
