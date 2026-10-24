// @vitest-environment happy-dom
/**
 * Email (SMTP) is ONE connector in Skills & MCP (operator decision 2026-10-22): a card with a
 * status pill and Connect / Send test, a drawer section that takes one connection URL or the
 * separate fields, Edit · Send test (the server's own reply) · Disconnect once connected, and the
 * local test inbox offered as a local-only choice. The old Email form is gone: one surface.
 */
import fs from "node:fs";
import path from "node:path";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, cleanup, click, render, type } from "./_helpers/kit-render";
import { ability, clone, installKitApi, KITS } from "./_helpers/kit-api";
import type { Ability } from "../lib/kit-types";

const { ConnectionSection } = await import("../app/skills/connection-section");
const { AbilityCard } = await import("../app/skills/ability-card");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const REPLY = "250 2.0.0 Ok: queued as 4ZxY · to b…@example.com";

function email(kit: keyof typeof KITS = "fresh"): Ability {
  return ability(clone(KITS[kit]), "email");
}

function connected(): Ability {
  const a = email("fresh");
  a.status = "ready";
  a.connection = {
    ...a.connection!, provider: "smtp", connected: true, acts_as: "boss@example.com",
    set_fields: { url: "smtps://login%40example.com@smtp.example.com:465", host: "smtp.example.com",
                  port: "465", security: "ssl", username: "login@example.com",
                  from_address: "boss@example.com", from_name: "", password: "set" },
  };
  a.last_test = { ok: true, at: new Date().toISOString(), latency_ms: 120, fact: REPLY, error: null };
  return a;
}

describe("the card", () => {
  it("is one Email (SMTP) card with a status pill and Connect", async () => {
    const kit = clone(KITS.fresh);
    expect(kit.abilities.filter((a) => a.key.includes("email")).map((a) => a.label)).toEqual(["Email (SMTP)"]);
    await render(<AbilityCard ability={email()} onOpen={() => {}} onAction={() => {}} />);
    expect($('[data-testid="kit-card-email"] .kit-card-label')!.textContent).toBe("Email (SMTP)");
    expect($('[data-testid="kit-card-email"] .kit-pill')!.getAttribute("data-status")).toBe("setup");
    expect($('[data-testid="kit-action-email"]')!.textContent).toBe("Connect");
  });

  it("says Send test once connected", async () => {
    await render(<AbilityCard ability={connected()} onOpen={() => {}} onAction={() => {}} />);
    expect($('[data-testid="kit-card-email"] .kit-pill')!.getAttribute("data-status")).toBe("ready");
    expect($('[data-testid="kit-card-email"]')!.textContent).toContain("Send test");
  });
});

describe("the URL field", () => {
  it("leads, with the separate fields folded under Fields", async () => {
    installKitApi();
    await render(<ConnectionSection ability={email()} projectId="p1" actor="ceo" onChanged={() => {}} />);
    const url = $('[data-testid="kit-field-url-input"]') as HTMLInputElement;
    expect(url).not.toBeNull();
    expect(url.placeholder).toMatch(/^smtps:\/\/user:key@smtp\.example\.com:465$/);
    expect($('[data-testid="kit-field-url"] .kit-field-label')!.getAttribute("data-tip")).toMatch(/smtp:\/\/.*smtps:\/\//);
    const alt = $('[data-testid="kit-fields-alt"]') as HTMLDetailsElement;
    expect(alt.open).toBe(false);
    for (const name of ["host", "port", "username", "password"]) {
      expect(alt.querySelector(`[data-testid="kit-field-${name}"]`)).not.toBeNull();
    }
    expect(alt.querySelector('[data-testid="kit-field-from_address"]')).toBeNull();
  });

  it("sends the typed URL with the From address", async () => {
    const calls = installKitApi();
    await render(<ConnectionSection ability={email()} projectId="p1" actor="ceo" onChanged={() => {}} />);
    await type('[data-testid="kit-field-url-input"]', "smtps://login%40example.com:k%23y@smtp.example.com:465");
    await type('[data-testid="kit-field-from_address-input"]', "boss@example.com");
    await click('[data-testid="kit-save-test"]');
    const sent = calls.find((c) => c.body?.action === "connect")!;
    expect(sent.body!.provider).toBe("smtp");
    expect(sent.body!.fields).toMatchObject({
      url: "smtps://login%40example.com:k%23y@smtp.example.com:465", from_address: "boss@example.com" });
    expect(sent.body!.fields).not.toHaveProperty("password");
  });

  it("puts a URL error under the URL", async () => {
    installKitApi(undefined, (url, body) => body?.action === "connect" ? new Response(
      JSON.stringify({ error: "The URL must start with smtp:// (STARTTLS, port 587) or smtps:// (TLS, port 465).", field: "url" }),
      { status: 422, headers: { "Content-Type": "application/json" } }) : undefined);
    await render(<ConnectionSection ability={email()} projectId="p1" actor="ceo" onChanged={() => {}} />);
    await type('[data-testid="kit-field-url-input"]', "https://x");
    await type('[data-testid="kit-field-from_address-input"]', "boss@example.com");
    await click('[data-testid="kit-save-test"]');
    expect($('[data-testid="kit-field-url-error"]')!.getAttribute("data-tip")).toContain("smtp://");
  });
});

describe("connected", () => {
  it("shows Edit, Send test and Disconnect, and the last reply", async () => {
    installKitApi();
    await render(<ConnectionSection ability={connected()} projectId="p1" actor="ceo" onChanged={() => {}} />);
    expect($('[data-testid="kit-field-url-input"]')).toBeNull();
    expect($('[data-testid="kit-edit"]')).not.toBeNull();
    expect($('[data-testid="kit-send-test"]')!.textContent).toBe("Send test");
    expect($('[data-testid="kit-disconnect"]')).not.toBeNull();
    expect($('[data-testid="kit-acts-as"]')!.textContent).toContain("boss@example.com");
    expect($('[data-testid="kit-test-row"]')!.textContent).toContain("250 2.0.0 Ok: queued as 4ZxY");
  });

  it("Send test asks the server and shows its reply", async () => {
    const after = connected();
    after.last_test = { ok: true, at: new Date().toISOString(), latency_ms: 90, fact: "250 2.0.0 Ok: queued as NEW1 · to b…@example.com", error: null };
    const calls = installKitApi(undefined, (url, body) => body?.action === "test"
      ? { ok: true, at: after.last_test!.at, latency_ms: 90, fact: after.last_test!.fact, error: null, ability: after }
      : undefined);
    let shown: Ability | null = null;
    await render(<ConnectionSection ability={connected()} projectId="p1" actor="ceo" onChanged={(a) => { shown = a; }} />);
    await click('[data-testid="kit-send-test"]');
    expect(calls.some((c) => c.body?.action === "test" && c.body?.ability === "email")).toBe(true);
    expect(shown!.last_test!.fact).toContain("queued as NEW1");
  });

  it("Edit shows the URL without its key, the key only as Set, and keeps it when not retyped", async () => {
    const calls = installKitApi();
    await render(<ConnectionSection ability={connected()} projectId="p1" actor="ceo" onChanged={() => {}} />);
    await click('[data-testid="kit-edit"]');
    const url = $('[data-testid="kit-field-url-input"]') as HTMLInputElement;
    expect(url.value).toBe("smtps://login%40example.com@smtp.example.com:465");
    expect($('[data-testid="kit-field-password-set"]')!.textContent).toBe("Set");
    expect($('[data-testid="kit-field-password-replace"]')!.textContent).toBe("Edit");
    await type('[data-testid="kit-field-from_name-input"]', "Ops");
    await click('[data-testid="kit-save-test"]');
    const fields = calls.find((c) => c.body?.action === "connect")!.body!.fields as Record<string, string>;
    // The shown URL is not sent back (it has no key); the stored fields say the same server.
    expect(fields).not.toHaveProperty("url");
    expect(fields).not.toHaveProperty("password");
    expect(fields).toMatchObject({ host: "smtp.example.com", port: "465", security: "ssl", from_name: "Ops" });
  });
});

describe("the local test inbox", () => {
  it("is a local-only choice when the install offers it", async () => {
    const calls = installKitApi();
    const a = email();
    a.connection!.providers = [...a.connection!.providers,
      { key: "dev_inbox", label: "Local test inbox", local_only: true, fields: [],
        tip: "Mailpit on this machine. Local only: nothing is delivered." }];
    await render(<ConnectionSection ability={a} projectId="p1" actor="ceo" onChanged={() => {}} />);
    expect($('[data-testid="kit-provider-dev_inbox-local"]')!.textContent).toBe("Local only");
    await click('[data-testid="kit-provider-dev_inbox"]');
    expect($('[data-testid="kit-local-only"]')).not.toBeNull();
    expect($('[data-testid="kit-save-test"]')!.textContent).toBe("Use local test inbox");
    await click('[data-testid="kit-save-test"]');
    expect(calls.find((c) => c.body?.action === "connect")!.body!.provider).toBe("dev_inbox");
  });

  it("is not offered when the install did not turn it on", async () => {
    installKitApi();
    await render(<ConnectionSection ability={email()} projectId="p1" actor="ceo" onChanged={() => {}} />);
    expect($('[data-testid="kit-provider-dev_inbox"]')).toBeNull();
  });
});

describe("one surface", () => {
  it("the old Email form and its proxy are gone, and the checklist links to the connector", () => {
    const app = path.resolve(__dirname, "../app");
    expect(fs.existsSync(path.join(app, "email-setup-ui.tsx"))).toBe(false);
    expect(fs.existsSync(path.join(app, "api/email-config"))).toBe(false);
    const readiness = fs.readFileSync(path.join(app, "workflow-readiness-ui.tsx"), "utf8");
    expect(readiness).not.toMatch(/EmailSetup|email-setup/);
  });
});
