// @vitest-environment happy-dom
/**
 * A signed-in person approves the code a terminal shows (BOSSAPP-862).
 * The code is fake: nothing here talks to the OAuth server.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DeviceApprove } from "../app/connected-apps/device/device-approve";
import { identityCopyAudit } from "./_helpers/identity-copy";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const FAKE_CODE = "FAKE-CODE";
let root: Root | null = null;
let host: HTMLElement | null = null;
let calls: { method: string; url: string; body: unknown }[] = [];

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  vi.unstubAllGlobals();
});

function routes(answer: { status?: number; body: unknown }) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url: String(url), body });
    return new Response(JSON.stringify(answer.body), {
      status: answer.status ?? 200,
      headers: { "content-type": "application/json" },
    });
  }));
}

async function settle() {
  for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

async function mount(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  await settle();
  return host!;
}

function type(el: Element | null, value: string) {
  if (!el) throw new Error("no field");
  const input = el as HTMLInputElement;
  act(() => {
    const proto = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    proto?.set?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function click(el: Element | null) {
  if (!el) throw new Error("nothing to click");
  await act(async () => { (el as HTMLElement).click(); });
  await settle();
}

describe("approve an MCP device code", () => {
  it("sends a fake code the person typed, and says the terminal can continue", async () => {
    routes({ body: { user_code: FAKE_CODE, status: "approved", client_id: "cli_fake" } });
    const el = await mount(<DeviceApprove initialCode="" />);
    expect(el.querySelectorAll("h1").length).toBe(1);
    expect(identityCopyAudit(el)).toEqual({ labels: [], prose: [], tips: [] });
    type(el.querySelector("#device-code"), FAKE_CODE);
    await click(el.querySelector("[data-testid='device-approve']"));
    expect(calls).toEqual([{
      method: "POST",
      url: "/v1/mcp-oauth/device/approve",
      body: { user_code: FAKE_CODE, approve: true },
    }]);
    expect(el.querySelector("[role='status']")?.textContent).toMatch(/Approved/);
    expect(el.textContent).not.toContain("cli_fake");
    expect(identityCopyAudit(el)).toEqual({ labels: [], prose: [], tips: [] });
  });

  it("denies a fake code instead of connecting the terminal", async () => {
    routes({ body: { user_code: FAKE_CODE, status: "denied", client_id: "cli_fake" } });
    const el = await mount(<DeviceApprove initialCode={FAKE_CODE} />);
    await click(el.querySelector("[data-testid='device-deny']"));
    expect(calls).toEqual([{
      method: "POST",
      url: "/v1/mcp-oauth/device/approve",
      body: { user_code: FAKE_CODE, approve: false },
    }]);
    expect(el.querySelector("[role='status']")?.textContent).toMatch(/Denied/);
    expect(el.textContent).not.toContain("cli_fake");
  });

  it("shows the API's sentence when a fake code is unknown", async () => {
    routes({ status: 400, body: { error: "invalid_request", error_description: "That code is unknown, used or expired." } });
    const el = await mount(<DeviceApprove initialCode={FAKE_CODE} />);
    await click(el.querySelector("[data-testid='device-approve']"));
    expect(calls[0]?.body).toEqual({ user_code: FAKE_CODE, approve: true });
    expect(el.querySelector("[role='alert']")?.textContent).toBe("That code is unknown, used or expired.");
    expect(el.querySelector("[role='status']")).toBeNull();
  });
});
