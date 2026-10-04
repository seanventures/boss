// @vitest-environment happy-dom
/**
 * The drawer's Connection and Tools sections (tester K7, K10, K16, K22).
 *
 * K7  a choice field (Email's TLS: None · STARTTLS · SSL) is a segmented control, so a plain relay
 *     on port 25/1025 connects; SSL is the default on port 465.
 * K10 Acts as and the last test belong to the STORED provider: picking another provider's tab
 *     does not leave GitHub's result on screen.
 * K16 a locked row's "always asks a person" is on something that can be hovered — a disabled
 *     button never shows its tooltip — and Ask carries it too.
 * K22 Mark reviewed's popover names the server's URL, its tool count and its highest risk.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, cleanup, click, render, type } from "./_helpers/kit-render";
import { ability, clone, installKitApi, KITS } from "./_helpers/kit-api";
import type { Ability } from "../lib/kit-types";

const { ConnectionSection } = await import("../app/skills/connection-section");
const { ToolsSection } = await import("../app/skills/tools-section");
const { TIP } = await import("../app/skills/kit-copy");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function emailWithSecurity(stored: Record<string, string> = {}): Ability {
  const a = ability(clone(KITS.fresh), "email");
  a.connection = {
    providers: [{ key: "smtp", label: "SMTP", fields: [
      { name: "host", label: "Host" }, { name: "port", label: "Port" },
      { name: "security", label: "Security", optional: true,
        options: [{ value: "none", label: "None" }, { value: "starttls", label: "STARTTLS" }, { value: "ssl", label: "SSL" }] },
      { name: "from_address", label: "From" },
    ] }],
    provider: "smtp", set_fields: stored, acts_as: null, connected: false,
  };
  return a;
}

describe("a choice field is a segmented control (K7)", () => {
  it("renders None · STARTTLS · SSL and sends the one picked", async () => {
    const calls = installKitApi();
    await render(<ConnectionSection ability={emailWithSecurity()} projectId="p1" actor="ceo" onChanged={() => {}} />);
    expect($('[data-testid="kit-field-security-input"]')).toBeNull();
    expect($('[data-testid="kit-field-security-starttls"]')!.getAttribute("aria-checked")).toBe("true");
    await type('[data-testid="kit-field-host-input"]', "mailpit");
    await type('[data-testid="kit-field-port-input"]', "1025");
    await click('[data-testid="kit-field-security-none"]');
    await click('[data-testid="kit-save-test"]');
    const sent = calls.find((c) => c.body?.action === "connect")!;
    expect(sent.body!.fields).toMatchObject({ host: "mailpit", port: "1025", security: "none" });
  });

  it("defaults to SSL on port 465", async () => {
    const calls = installKitApi();
    await render(<ConnectionSection ability={emailWithSecurity()} projectId="p1" actor="ceo" onChanged={() => {}} />);
    await type('[data-testid="kit-field-port-input"]', "465");
    expect($('[data-testid="kit-field-security-ssl"]')!.getAttribute("aria-checked")).toBe("true");
    await click('[data-testid="kit-save-test"]');
    expect(calls.find((c) => c.body?.action === "connect")!.body!.fields).toMatchObject({ security: "ssl" });
  });
});

describe("what is shown belongs to the stored provider (K10)", () => {
  it("switching from GitHub Issues to Linear clears GitHub's Acts as and result", async () => {
    installKitApi();
    const a = ability(clone(KITS.fresh), "file_tickets");
    a.connection = { ...a.connection!, provider: "github_issues", connected: true, acts_as: "@octo-bot", set_fields: {} };
    a.last_test = { ok: true, at: new Date().toISOString(), latency_ms: 40, fact: "GitHub Issues · @octo-bot", error: null };
    await render(<ConnectionSection ability={a} projectId="p1" actor="ceo" onChanged={() => {}} />);
    expect($('[data-testid="kit-acts-as"]')!.textContent).toContain("@octo-bot");
    expect($('[data-testid="kit-test-row"]')).not.toBeNull();
    await click('[data-testid="kit-provider-linear"]');
    expect($('[data-testid="kit-acts-as"]')).toBeNull();
    expect($('[data-testid="kit-test-row"]')).toBeNull();
    expect($('[data-testid="kit-disconnect"]')).toBeNull();
  });
});

describe("Tools: the locked rule can be read, and review shows what is reviewed (K16, K22)", () => {
  it("a tier-2 row's Allow sits in a hoverable wrapper with the lock sentence, and Ask says it too", async () => {
    installKitApi();
    const a = ability(clone(KITS.fresh), "open_prs");
    await render(<ToolsSection ability={a} projectId="p1" actor="ceo" onChanged={() => {}} />);
    const ref = "skill:github.create_pr_real";
    const allow = $(`[data-testid="kit-policy-${ref}-allow"]`) as HTMLButtonElement;
    expect(allow.disabled).toBe(true);
    expect($(`[data-testid="kit-policy-${ref}-allow-tip"]`)!.dataset.tip).toBe(TIP.locked);
    expect(allow.parentElement!.dataset.tip).toBe(TIP.locked);
    expect($(`[data-testid="kit-policy-${ref}-ask"]`)!.dataset.tip).toBe(TIP.locked);
  });

  it("Mark reviewed lists the URL, the tool count and the highest risk", async () => {
    installKitApi();
    const a = ability(clone(KITS.failing), "web_research");
    a.mcp = a.mcp.map((m) => ({ ...m, trusted: false, url: "http://mcp-tools:8770/mcp" }));
    await render(<ToolsSection ability={a} projectId="p1" actor="ceo" onChanged={() => {}} />);
    await click('[data-testid="kit-review-open-web-intel"]');
    expect($('[data-testid="kit-review-url"]')!.textContent).toBe("http://mcp-tools:8770/mcp");
    expect($('[data-testid="kit-review-facts"]')!.textContent).toBe("2 tools · Highest: Reads");
  });
});
