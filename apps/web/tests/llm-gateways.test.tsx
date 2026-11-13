// @vitest-environment happy-dom
/**
 * SETUP › ADVANCED › GATEWAYS (BOSSAPP-887): the gateways the API lists (never a key), Test sends
 * one call and says the answer, Disable sends one PATCH, Add sends the key once and clears it.
 * BOSSAPP-1042 (d): Save with an empty or malformed URL says why and sends nothing — never a
 * silent no-op.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GatewaysRow } from "../app/setup/gateways-row";
import { gatewayFormError, parseGateways } from "../lib/llm-gateways";
import { overBudget } from "./_helpers/copy-budget";
import { $, mount, settle, unmount } from "./_helpers/live-render";

const LIST = { gateways: [
  { id: "llmgw_1", scope: "project", name: "LiteLLM", protocol: "anthropic", base_url: "https://gw.example.com",
    routes: ["claude_code"], status: "active", secret_set: true,
    last_test: { ok: true, status: 200, detail: "the gateway answered and offers 3 models", at: "2026-11-13T00:00:00Z" } },
] };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => { unmount(); vi.restoreAllMocks(); });

describe("LLM gateways", () => {
  it("parses defensively", () => {
    const [g] = parseGateways({ gateways: [{ id: "x", protocol: "weird", status: "nope" }] });
    expect(g).toMatchObject({ protocol: "openai", status: "active", secret_set: false, last_test: null });
  });

  it("lists, tests, disables and adds with the key sent once", async () => {
    const calls: { url: string; method: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = String(init?.method ?? "GET");
      calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : {} });
      if (method === "GET") return json(LIST);
      if (String(url).endsWith("/test")) return json({ ok: false, status: 401, detail: "the gateway refused the key" });
      return json({ gateway: {} });
    }));
    await mount(<GatewaysRow projectId="p1" />);
    await settle();
    expect($('[data-testid="gateway-routes-llmgw_1"]')?.textContent).toContain("Claude Code");
    expect($('[data-testid="gateway-test-llmgw_1"]')?.textContent).toContain("Works");
    expect(overBudget(document.body)).toEqual([]);
    ($('[data-testid="gateway-test-run-llmgw_1"]') as HTMLButtonElement).click();
    await settle();
    expect($('[data-testid="gateways-said"]')?.textContent).toBe("Failed: the gateway refused the key");
    ($('[data-testid="gateway-toggle-llmgw_1"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ project_id: "p1", status: "disabled" });

    ($('[data-testid="gateways-add"]') as HTMLButtonElement).click();
    await settle();
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    for (const [label, value] of [["Gateway URL", "https://portkey.example.com"], ["Gateway key", "sk-test"]]) {
      const box = $(`[aria-label="${label}"]`) as HTMLInputElement;
      setter.call(box, value);
      box.dispatchEvent(new Event("input", { bubbles: true }));
    }
    await settle();
    ($('[data-testid="gateways-save"]') as HTMLButtonElement).click();
    await settle();
    const posts = calls.filter((c) => c.method === "POST" && c.url === "/v1/llm-gateways");
    expect(posts).toHaveLength(1);
    expect(posts[0].body).toEqual({ project_id: "p1", name: "LiteLLM", protocol: "anthropic",
                                    base_url: "https://portkey.example.com", secret: "sk-test", scope: "project" });
  });

  it("gatewayFormError says what is wrong, mirroring the API's URL rule", () => {
    const ok = { name: "LiteLLM", url: "https://gw.example.com/prefix", secret: "k" };
    expect(gatewayFormError(ok)).toBeNull();
    expect(gatewayFormError({ ...ok, url: "http://10.0.0.5:4000" })).toBeNull();
    expect(gatewayFormError({ ...ok, url: "  " })).toMatch(/Enter the gateway's URL/);
    expect(gatewayFormError({ ...ok, url: "gw.example.com" })).toMatch(/not a URL/);
    expect(gatewayFormError({ ...ok, url: "ftp://gw.example.com" })).toMatch(/https:\/\/ or http:\/\//);
    expect(gatewayFormError({ ...ok, url: "https://user:pw@gw.example.com" })).toMatch(/never in the URL/);
    expect(gatewayFormError({ ...ok, url: "https://gw.example.com/?a=1" })).toMatch(/no \?query/);
    expect(gatewayFormError({ ...ok, secret: "" })).toMatch(/key/);
    expect(gatewayFormError({ ...ok, name: " " })).toMatch(/Name/);
  });

  it("Save with an empty or invalid URL says why and sends nothing", async () => {
    const calls: { method: string }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
      calls.push({ method: String(init?.method ?? "GET") });
      return json({ gateways: [] });
    }));
    await mount(<GatewaysRow projectId="p1" />);
    ($('[data-testid="gateways-add"]') as HTMLButtonElement).click();
    await settle();
    const save = $('[data-testid="gateways-save"]') as HTMLButtonElement;
    expect(save.disabled).toBe(false);
    save.click();
    await settle();
    expect($('[data-testid="gateways-form"] [role="alert"]')?.textContent).toMatch(/Enter the gateway's URL/);
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    const box = $('[aria-label="Gateway URL"]') as HTMLInputElement;
    setter.call(box, "not a url");
    box.dispatchEvent(new Event("input", { bubbles: true }));
    await settle();
    save.click();
    await settle();
    expect($('[data-testid="gateways-form"] [role="alert"]')?.textContent).toMatch(/not a URL/);
    expect(calls.filter((c) => c.method !== "GET")).toEqual([]);
  });
});
