// @vitest-environment happy-dom
/**
 * Connection (skills-mcp spec §5.3, test 4): a secret is never prefilled (a stored one shows only
 * as `Set · …a1f3`), Save & test sends what was typed and puts a 422's sentence under the field
 * the provider named, and Disconnect asks first.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, cleanup, click, render, type } from "./_helpers/kit-render";
import { ability, clone, installKitApi, KITS } from "./_helpers/kit-api";

const { ConnectionSection } = await import("../app/skills/connection-section");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("a connection", () => {
  it("a stored secret shows as its last four and is never in an input", async () => {
    installKitApi(clone(KITS.failing));
    const email = ability(KITS.failing, "email");
    await render(<ConnectionSection ability={email} projectId="p1" actor="ceo" onChanged={() => {}} />);
    expect($('[data-testid="kit-field-password-set"]')!.textContent).toBe("Set · …a1f3");
    expect($('[data-testid="kit-field-password-input"]')).toBeNull();
    await click('[data-testid="kit-field-password-replace"]');
    const input = $('[data-testid="kit-field-password-input"]') as HTMLInputElement;
    expect(input.type).toBe("password");
    expect(input.value).toBe("");
    expect(($('[data-testid="kit-field-host-input"]') as HTMLInputElement).value).toBe("smtp.example.com");
  });

  it("Save & test sends what was typed, and a 422 lands under the field it names", async () => {
    const calls = installKitApi(clone(KITS.fresh), (url, body) => (body?.action === "connect"
      ? new Response(JSON.stringify({ error: "401 · Token rejected", field: "api_token" }), { status: 422 })
      : undefined));
    const changed = vi.fn();
    await render(<ConnectionSection ability={ability(KITS.fresh, "file_tickets")} projectId="p1" actor="ceo" onChanged={changed} />);
    await click('[data-testid="kit-provider-linear"]');
    await type('[data-testid="kit-field-api_token-input"]', "lin_bad_0000");
    await type('[data-testid="kit-field-team_key-input"]', "ENG");
    await click('[data-testid="kit-save-test"]');
    const sent = calls.find((c) => c.body?.action === "connect")!;
    expect(sent.body).toMatchObject({ ability: "file_tickets", provider: "linear",
                                      fields: { api_token: "lin_bad_0000", team_key: "ENG" } });
    expect($('[data-testid="kit-field-api_token-error"]')!.textContent).toBe("Token rejected");
    expect(changed).not.toHaveBeenCalled();
  });

  it("Disconnect asks before it forgets anything", async () => {
    const calls = installKitApi(clone(KITS.failing));
    await render(<ConnectionSection ability={ability(KITS.failing, "email")} projectId="p1" actor="ceo" onChanged={() => {}} />);
    await click('[data-testid="kit-disconnect"]');
    expect($('[data-testid="kit-disconnect-confirm"]')!.textContent).toContain("Disconnect SMTP?");
    expect(calls.some((c) => c.body?.action === "disconnect")).toBe(false);
    await click('[data-testid="kit-disconnect-yes"]');
    expect(calls.some((c) => c.body?.action === "disconnect")).toBe(true);
  });

  it("an ability with nothing to connect says so in one row", async () => {
    installKitApi();
    await render(<ConnectionSection ability={ability(KITS.fresh, "web_research")} projectId="p1" actor="ceo" onChanged={() => {}} />);
    expect($('[data-testid="kit-conn-none"]')!.textContent).toContain("Built in, Nothing to connect");
  });
});
