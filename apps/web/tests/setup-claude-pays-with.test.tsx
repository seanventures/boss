// @vitest-environment happy-dom
/**
 * Claude Code pays with: subscription first or API key first — a control on the Claude Code row.
 *
 * boss-1, 2026-11-03: "API key first" had been set with no way to see or undo it in the console;
 * a day of builds billed the Anthropic key ($112) while the Claude subscription sat idle. The
 * row now says which credential Claude Code runs on and offers the order as two presses (a press,
 * so a stale second factor opens "Confirm it's you" instead of a silent refusal).
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { entitlementSessionsWire, entitlementSourcesWire } from "./_helpers/obs-fixtures";
import { board } from "./_helpers/setup-fixtures";
import { click, mountBoard, q, type Mounted } from "./_helpers/setup-board";
import type { SetupView } from "../lib/setup-types";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.unstubAllGlobals(); });

type Rec = { url: string; method: string; body: Record<string, unknown> };
function api(view: SetupView) {
  const calls: Rec[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    let body: Record<string, unknown> = {};
    try { body = init?.body ? JSON.parse(String(init.body)) : {}; } catch { /* none */ }
    calls.push({ url, method, body });
    const json = (b: unknown) => new Response(JSON.stringify(b), { status: 200, headers: { "Content-Type": "application/json" } });
    if (url.startsWith("/v1/entitlements/sources?")) {
      // The Claude Code subscription row (source anthropic_claude_code) beside the API key.
      const wire = entitlementSourcesWire() as { items: Record<string, unknown>[] };
      const sub = { ...wire.items.find((x) => x.source_id === "claude_max"), id: "es_cc",
                    source_id: "anthropic_claude_code" };
      return json({ ...wire, items: [...wire.items, sub] });
    }
    if (url.startsWith("/v1/entitlement-connection-sessions?")) return json(entitlementSessionsWire());
    if (method !== "GET") {
      return json({ ...view, claude_credential: { ...view.claude_credential, order: body.order, active: "subscription" } });
    }
    return json(view);
  }));
  return calls;
}
function onTheKey(): SetupView {
  const view = board();
  view.claude_credential = { order: "api_key_first", active: "stored_key", billing: "metered" };
  return view;
}
const pays = () => document.querySelector('[data-testid="setup-claude-pays"]') as HTMLElement | null;

describe("Claude Code pays with", () => {
  it("says the key is paying, and one press puts the subscription first", async () => {
    const calls = api(onTheKey());
    mounted = await mountBoard(onTheKey());
    await vi.waitFor(() => expect(pays()).not.toBeNull());
    const group = pays() as HTMLElement;
    expect(q(group, "setup-claude-pays-now")?.textContent).toContain("API key");
    expect(q(group, "setup-claude-pays-api_key_first")?.getAttribute("aria-pressed")).toBe("true");
    await click(q(group, "setup-claude-pays-subscription_first"));
    const write = calls.find((c) => c.method !== "GET" && c.body.op === "claude_credential");
    expect(write?.body.order).toBe("subscription_first");
  });

  it("API key first waits for a key: no Anthropic key connected, it cannot be pressed", async () => {
    const view = board();
    view.claude_credential = { order: "subscription_first", active: "subscription", billing: null };
    view.models = view.models.filter((m) => m.provider !== "anthropic");
    const calls = api(view);
    mounted = await mountBoard(view);
    await vi.waitFor(() => expect(pays()).not.toBeNull());
    const key = q(pays() as HTMLElement, "setup-claude-pays-api_key_first") as HTMLButtonElement;
    expect(key.disabled).toBe(true);
    expect(key.getAttribute("data-tip")).toContain("Add an Anthropic API key first");
    await click(key);
    expect(calls.filter((c) => c.method !== "GET")).toHaveLength(0);
  });
});
