// @vitest-environment happy-dom
/**
 * Agents › Outside agents (KG P6b console): render the list, add one (secret never echoed
 * back), test it in words, and refuse suspend/remove without a named confirm.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, cleanup, click, render, type } from "./_helpers/kit-render";

const { OutsideAgentsPanel } = await import("../app/agents/outside-agents-panel");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const AGENT = {
  id: "ext_1", project_id: "p1", display_name: "Partner Bot", endpoint_url: "https://partner.example.com/agent",
  secret_ref_id: "sref_1", sponsor_principal_id: "alice", status: "active",
  created_by: "alice", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

function mockFetch(over: Record<string, (init: RequestInit | undefined) => { status?: number; body: unknown }> = {}) {
  const calls: string[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://console.test");
    calls.push(`${(init?.method ?? "GET").toUpperCase()} ${url.pathname}`);
    for (const [path, answer] of Object.entries(over)) {
      if (url.pathname === path) {
        const r = answer(init);
        return new Response(JSON.stringify(r.body), { status: r.status ?? 200, headers: { "content-type": "application/json" } });
      }
    }
    if (url.pathname === "/v1/external-agents") return new Response(JSON.stringify({ items: [AGENT] }), { status: 200 });
    return new Response(JSON.stringify({ detail: `no fixture for ${url.pathname}` }), { status: 404 });
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}

describe("the outside agents list", () => {
  it("renders every registered agent by its host, never a raw secret", async () => {
    mockFetch();
    await render(<OutsideAgentsPanel projectId="p1" actor="alice" />);
    expect($('[data-testid="ag-outside-row-ext_1"]')!.textContent).toContain("Partner Bot");
    expect($('[data-testid="ag-outside-row-ext_1"]')!.textContent).toContain("partner.example.com");
    expect(document.body.innerHTML).not.toContain("sref_1");
  });

  it("an empty project says so in words", async () => {
    mockFetch({ "/v1/external-agents": () => ({ body: { items: [] } }) });
    await render(<OutsideAgentsPanel projectId="p1" actor="alice" />);
    expect($('[data-testid="ag-outside-empty"]')).not.toBeNull();
  });
});

describe("adding an outside agent", () => {
  it("writes the secret through the secret store, and only hands the reference to register — never the value itself", async () => {
    const { calls } = mockFetch({
      "/v1/secrets": () => ({ body: { secret_ref_id: "sref_new" } }),
      "/v1/external-agents": () => ({ body: { items: [] } }),
    });
    await render(<OutsideAgentsPanel projectId="p1" actor="alice" />);
    await click('[data-testid="ag-outside-open-form"]');
    await type('[data-testid="ag-outside-name"]', "Partner Bot");
    await type('[data-testid="ag-outside-endpoint"]', "https://partner.example.com/agent");
    await type('[data-testid="ag-outside-secret"]', "sk-super-secret-value");
    await type('[data-testid="ag-outside-sponsor"]', "alice");
    await click('[data-testid="ag-outside-submit"]');

    const secretsCall = calls.find((c) => c.startsWith("POST /v1/secrets"));
    expect(secretsCall).toBeTruthy();
    expect(document.body.innerHTML).not.toContain("sk-super-secret-value");

    const registerCall = calls.find((c) => c.startsWith("POST /v1/external-agents"));
    expect(registerCall).toBeTruthy();
  });

  it("refuses to submit without every field, in words", async () => {
    mockFetch();
    await render(<OutsideAgentsPanel projectId="p1" actor="alice" />);
    await click('[data-testid="ag-outside-open-form"]');
    await click('[data-testid="ag-outside-submit"]');
    expect($('[data-testid="ag-outside-form-error"]')!.textContent).toContain("required");
  });
});

describe("testing an outside agent", () => {
  it("says reachable or unreachable, in words — never a reason", async () => {
    mockFetch({ "/v1/external-agents/ext_1/test": () => ({ body: { reachable: true } }) });
    await render(<OutsideAgentsPanel projectId="p1" actor="alice" />);
    await click('[data-testid="ag-outside-test-ext_1"]');
    expect($('[data-testid="ag-outside-test-word-ext_1"]')!.textContent).toContain("Reachable");
  });

  it("unreachable reads as unreachable, not an error code", async () => {
    mockFetch({ "/v1/external-agents/ext_1/test": () => ({ body: { reachable: false } }) });
    await render(<OutsideAgentsPanel projectId="p1" actor="alice" />);
    await click('[data-testid="ag-outside-test-ext_1"]');
    expect($('[data-testid="ag-outside-test-word-ext_1"]')!.textContent).toContain("Unreachable");
  });
});

describe("suspend and remove", () => {
  it("suspend asks a named confirm before it revokes anything", async () => {
    mockFetch();
    await render(<OutsideAgentsPanel projectId="p1" actor="alice" />);
    await click('[data-testid="ag-outside-suspend-ext_1"]');
    expect($('[data-testid="ag-outside-confirm-ext_1"]')!.textContent).toContain("Suspend Partner Bot?");
    expect($$('[data-testid="ag-outside-row-ext_1"] [data-testid^="ag-outside-suspend-do"]').length).toBe(0);
  });

  it("cancelling the confirm sends nothing", async () => {
    const { calls } = mockFetch();
    await render(<OutsideAgentsPanel projectId="p1" actor="alice" />);
    await click('[data-testid="ag-outside-remove-ext_1"]');
    await click('[data-testid="ag-outside-confirm-cancel-ext_1"]');
    expect(calls.some((c) => c.includes("/remove"))).toBe(false);
  });

  it("confirming remove calls the API and the row's error surfaces in words on refusal", async () => {
    mockFetch({ "/v1/external-agents/ext_1/remove": () => ({ status: 422, body: { detail: "agent already suspended" } }) });
    await render(<OutsideAgentsPanel projectId="p1" actor="alice" />);
    await click('[data-testid="ag-outside-remove-ext_1"]');
    await click('[data-testid="ag-outside-confirm-do-ext_1"]');
    expect($('[data-testid="ag-outside-row-error-ext_1"]')!.textContent).toContain("agent already suspended");
  });
});
