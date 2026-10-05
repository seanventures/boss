// @vitest-environment happy-dom
/**
 * Repo and models › Capacity holds what System Configuration › AI Capacity did (spec §2.3,
 * P-S-46, P-S-47, P-S-49; operator decision §11.2), without a second key form beside the Model
 * row: every source with its state, `Store credential` (with the secret backend) for a source
 * with no key, `Replace ↗` into the Model row for one that has a row there, `Start official
 * session` / `Disconnect session` for a plan login, and `Complete session` from FIELDS — the
 * observed capacity is assembled, never typed as JSON. Broker skills are a link into Skills.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { entitlementSessionsWire, entitlementSourcesWire } from "./_helpers/obs-fixtures";
import { board } from "./_helpers/setup-fixtures";
import { click, mountBoard, q, type, type Mounted } from "./_helpers/setup-board";
import { completeBody } from "../app/setup/capacity-row";
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
    if (url.startsWith("/v1/entitlements/sources?")) return json(entitlementSourcesWire());
    if (url.startsWith("/v1/entitlement-connection-sessions?")) return json(entitlementSessionsWire());
    if (method !== "GET") return json({ ok: true });
    return json(view);
  }));
  return calls;
}
function withAnthropicRow(): SetupView {
  const view = board();
  view.models = [{ ...view.models[0], source_id: "anthropic_api", provider: "anthropic", label: "Anthropic", kind: "metered" }];
  return view;
}
const src = (id: string) => document.querySelector(`[data-testid="setup-cap-source"][data-source="${id}"]`) as HTMLElement;

describe("Capacity on Repo and models", () => {
  it("lists every source with its state, and links Broker skills into Skills", async () => {
    api(withAnthropicRow());
    mounted = await mountBoard(withAnthropicRow());
    expect(q(document.body, "setup-entitlements")).not.toBeNull();
    const ids = [...document.querySelectorAll('[data-testid="setup-cap-source"]')].map((e) => e.getAttribute("data-source"));
    expect(ids).toEqual(["anthropic_api", "openai_gateway", "claude_max", "chatgpt_pro"]);
    expect(src("anthropic_api").querySelector(".su-pill")?.textContent).toBe("Active");
    expect(src("openai_gateway").querySelector(".su-pill")?.textContent).toBe("Blocked");
    expect(src("claude_max").querySelector(".su-pill")?.textContent).toBe("Needs login");
    expect(q(document.body, "setup-entitlement-session")?.textContent).toContain("1 · 1 waiting");
    expect(q(document.body, "setup-broker-skills-link")?.getAttribute("href")).toBe("?area=skills");
    expect(overBudget(q(document.body, "setup-entitlements") as HTMLElement)).toEqual([]);
    expect((document.body.textContent ?? "").toLowerCase()).not.toContain("entitlement");
  });

  it("a key already on the Model row is replaced there, not in a second form", async () => {
    api(withAnthropicRow());
    mounted = await mountBoard(withAnthropicRow());
    const row = src("anthropic_api");
    expect(row.querySelector('[data-testid="setup-cap-key-open"]')).toBeNull();
    expect(row.querySelector('[data-testid="setup-cap-replace"]')?.getAttribute("href")).toBe("?area=setup&source=anthropic_api");
  });

  it("a source with no key stores one into the backend you pick", async () => {
    const calls = api(board());
    mounted = await mountBoard(board());
    await click(src("openai_gateway").querySelector('[data-testid="setup-cap-key-open"]'));
    await type(q(document.body, "setup-cap-key"), "sk-test");
    const backend = q(document.body, "setup-cap-backend") as HTMLSelectElement;
    backend.value = "vault";
    backend.dispatchEvent(new Event("change", { bubbles: true }));
    await click(q(document.body, "setup-cap-key-save"));
    const w = calls.filter((c) => c.method === "POST");
    expect(w).toHaveLength(1);
    expect(w[0].url).toBe("/v1/entitlements/sources/openai_gateway/credential");
    expect(w[0].body).toEqual({ project_id: "p", value: "sk-test", backend: "vault", actor: "ceo" });
  });

  it("a plan login starts, completes from fields, and disconnects after asking", async () => {
    const calls = api(board());
    mounted = await mountBoard(board());
    await click(src("claude_max").querySelector('[data-testid="setup-cap-start-open"]'));
    await click(q(document.body, "setup-cap-start"));
    expect(calls.filter((c) => c.method === "POST").at(-1)).toMatchObject({
      url: "/v1/entitlements/sources/claude_max/connection-sessions",
      body: { project_id: "p", connection_mode: "claude_cli_login", requested_scopes: ["user:inference"], actor: "ceo" },
    });

    await type(q(document.body, "setup-cap-ref"), "secret://claude-max");
    await click(q(document.body, "setup-cap-complete"));
    expect(calls.filter((c) => c.method === "POST").at(-1)).toMatchObject({
      url: "/v1/entitlement-connection-sessions/ecs_1/complete",
      body: { connection_ref: "secret://claude-max", oauth_scopes: ["user:inference"], observed_capacity: { capacity_state: "available" } },
    });

    const before = calls.filter((c) => c.method === "POST").length;
    await click(src("chatgpt_pro").querySelector('[data-testid="setup-cap-disconnect"]'));
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(before);
    await click(q(document.body, "setup-cap-disconnect-yes"));
    expect(calls.filter((c) => c.method === "POST").at(-1)?.url).toBe("/v1/entitlements/sources/chatgpt_pro/disconnect");
  });

  it("Complete session refuses what is not a reference or a real time", () => {
    expect(completeBody({ ref: " ", scopes: "", state: "available", resetAt: "" })).toBeNull();
    expect(completeBody({ ref: "r", scopes: "", state: "plenty", resetAt: "" })).toBeNull();
    expect(completeBody({ ref: "r", scopes: "a, b", state: "limited", resetAt: "2026-10-05T09:00" })).toMatchObject({
      connection_ref: "r", oauth_scopes: ["a", "b"], observed_capacity: { capacity_state: "limited" },
    });
  });
});
