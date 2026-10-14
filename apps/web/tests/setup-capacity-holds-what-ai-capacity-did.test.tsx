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
import { act } from "react";

import { overBudget } from "./_helpers/copy-budget";
import { entitlementSessionsWire, entitlementSourcesWire } from "./_helpers/obs-fixtures";
import { board, firstRun } from "./_helpers/setup-fixtures";
import { click, mountBoard, q, type, type Mounted } from "./_helpers/setup-board";
import { completeBody, stateOf, type CapSource } from "../app/setup/capacity-row";
import type { SetupView } from "../lib/setup-types";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.unstubAllGlobals(); });

type Rec = { url: string; method: string; body: Record<string, unknown> };
function api(view: SetupView, sources: unknown = entitlementSourcesWire(), sessions: unknown = entitlementSessionsWire(), sessionStatus = 200, sourceStatus = 200) {
  const calls: Rec[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    let body: Record<string, unknown> = {};
    try { body = init?.body ? JSON.parse(String(init.body)) : {}; } catch { /* none */ }
    calls.push({ url, method, body });
    const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } });
    if (url.startsWith("/v1/entitlements/sources?")) return json(sources, sourceStatus);
    if (url.startsWith("/v1/entitlement-connection-sessions?")) {
      if (sessionStatus === 0) throw new Error("Network failure");
      return json(sessions, sessionStatus);
    }
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
    expect(src("claude_max").querySelector(".su-pill")?.textContent).toBe("Needs sign-in");
    expect(q(document.body, "setup-entitlement-session")?.textContent).toContain("1 · 1 waiting");
    expect(q(document.body, "setup-broker-skills-link")?.getAttribute("href")).toBe("?area=skills");
    expect(overBudget(q(document.body, "setup-entitlements") as HTMLElement)).toEqual([]);
    expect((document.body.textContent ?? "").toLowerCase()).not.toContain("entitlement");
  });

  it("uses source names for disclosure while keeping exact identity and every model available", async () => {
    const sources = entitlementSourcesWire();
    sources.items[0].supported_models = ["claude-sonnet", "claude-opus"];
    sources.items[1].source_id = "constructor";
    api(withAnthropicRow(), sources);
    mounted = await mountBoard(withAnthropicRow());
    const row = src("anthropic_api");
    const details = row.querySelector("details")!;
    expect(details.open).toBe(false);
    expect(details.querySelector("summary")?.textContent).toBe("Anthropic API details");
    expect(details.textContent).toContain("Source ID: anthropic_api");
    expect(details.textContent).toContain("Provider: anthropic");
    expect(details.textContent).toContain("Models: claude-sonnet, claude-opus");
    expect(row.querySelector('[role="status"]')?.closest("details")).toBeNull();
    expect(row.querySelector('[data-testid="setup-cap-replace"]')?.closest("details")).toBeNull();
    // Unknown sources keep their actual names; they are never mapped to a different source.
    expect(src("constructor").querySelector("summary")?.textContent).toBe("constructor details");
  });

  it("keeps blocked, limited, exhausted and session controls outside optional metadata", async () => {
    const sources = entitlementSourcesWire();
    sources.items[0].observed_capacity = { capacity_state: "limited" };
    sources.items[1].observed_capacity = { capacity_state: "exhausted" };
    sources.items[3].effective_status = "unsupported";
    api(board(), sources);
    mounted = await mountBoard(board());
    for (const [id, text] of [["anthropic_api", "Limited"], ["openai_gateway", "Exhausted"]]) {
      const row = src(id);
      const signal = [...row.querySelectorAll("span")].find(el => el.textContent === text)!;
      expect(signal).toBeDefined();
      expect(signal.closest("details")).toBeNull();
    }
    expect(src("openai_gateway").querySelector('[role="status"]')?.textContent).toBe("Blocked");
    expect(src("chatgpt_pro").querySelector('[role="status"]')?.textContent).toBe("Unsupported");
    expect(q(src("chatgpt_pro"), "setup-cap-disconnect")?.closest("details")).toBeNull();
    const session = q(document.body, "setup-cap-session-row")!;
    expect(session.closest("details")).toBeNull();
    expect(session.textContent).toContain("Waiting on you");
    expect(session.textContent).toContain("claude login");
    expect(q(session, "setup-cap-complete")).not.toBeNull();
    await click(src("openai_gateway").querySelector('[data-testid="setup-cap-key-open"]'));
    expect(q(document.body, "setup-cap-key-form")?.closest("details")).toBeNull();
  });

  it("friendly names preserve official login mode choices and the source bound to writes", async () => {
    const sources = entitlementSourcesWire();
    sources.items[2].source_id = "xai_grok_build";
    sources.items[2].provider = "xai";
    sources.items[2].connection_metadata.supported_connection_modes = ["browser_oidc", "device_oauth"];
    const calls = api(board(), sources);
    mounted = await mountBoard(board());
    const row = src("xai_grok_build");
    expect(row.querySelector("summary")?.textContent).toBe("Grok Build details");
    await click(row.querySelector('[data-testid="setup-cap-start-open"]'));
    const form = q(row, "setup-cap-start-form")!;
    expect(form.closest("details")).toBeNull();
    const mode = form.querySelector("select")!;
    expect([...mode.options].map(x => x.value)).toEqual(["browser_oidc", "device_oauth"]);
    mode.value = "device_oauth";
    await act(async () => { mode.dispatchEvent(new Event("change", { bubbles: true })); });
    await click(q(row, "setup-cap-start"));
    expect(calls.filter(c => c.method === "POST")).toEqual([{
      url: "/v1/entitlements/sources/xai_grok_build/connection-sessions", method: "POST",
      body: { project_id: "p", connection_mode: "device_oauth", requested_scopes: ["user:inference"], actor: "ceo" },
    }]);
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
    await act(async () => { backend.dispatchEvent(new Event("change", { bubbles: true })); });
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

  it("says which Anthropic credential runs Claude, and who pays (BOSSAPP-530)", async () => {
    const onKey = { active: "stored_key", order: "subscription_first", billing: "metered", source_id: "anthropic_api", subscription_disconnected: true };
    const sources = entitlementSourcesWire();
    // After Store credential: the key source is connected; the subscription was disconnected.
    Object.assign(sources.items[0], { connection_status: "connected", claude_credential: onKey });
    sources.items.push({ ...sources.items[2], id: "es_5", source_id: "anthropic_claude_code", auth_method: "official_cli_oauth",
      effective_status: "missing_oauth_connection", connection_status: "revoked", claude_credential: onKey } as never);
    api(board(), sources, { items: [] });
    mounted = await mountBoard(board());
    const key = src("anthropic_api");
    expect(key.querySelector('[role="status"]')?.textContent).toBe("Connected");
    expect(q(key, "setup-cap-claude")?.textContent).toBe("Used for: builds, agents, meetings · Billed to your API account");
    expect(q(src("anthropic_claude_code"), "setup-cap-claude")?.textContent).toBe("Claude runs on: Anthropic API key");
    expect(src("openai_gateway").querySelector('[data-testid="setup-cap-claude"]')).toBeNull();
    expect(overBudget(q(document.body, "setup-entitlements") as HTMLElement)).toEqual([]);
    mounted.unmount(); mounted = null;

    // On the subscription the key row says nothing it is not doing; the plan row says so.
    const onPlan = { ...onKey, active: "subscription", billing: "subscription", source_id: "anthropic_claude_code", subscription_disconnected: false };
    const again = entitlementSourcesWire();
    Object.assign(again.items[0], { connection_status: "connected", claude_credential: onPlan });
    again.items.push({ ...again.items[2], id: "es_5", source_id: "anthropic_claude_code", auth_method: "official_cli_oauth",
      effective_status: "active", connection_status: "connected", claude_credential: onPlan } as never);
    api(board(), again, { items: [] });
    mounted = await mountBoard(board());
    expect(src("anthropic_api").querySelector('[data-testid="setup-cap-claude"]')).toBeNull();
    expect(q(src("anthropic_claude_code"), "setup-cap-claude")?.textContent).toBe("Claude runs on: this subscription");
  });

  it("Complete session refuses what is not a reference or a real time", () => {
    expect(completeBody({ ref: " ", scopes: "", state: "available", resetAt: "" })).toBeNull();
    expect(completeBody({ ref: "r", scopes: "", state: "plenty", resetAt: "" })).toBeNull();
    expect(completeBody({ ref: "r", scopes: "a, b", state: "limited", resetAt: "2026-10-05T09:00" })).toMatchObject({
      connection_ref: "r", oauth_scopes: ["a", "b"], observed_capacity: { capacity_state: "limited" },
    });
  });
});

// Exact-ed providers.yaml auth/source metadata, projected into an absent-credential read fixture.
// Catalog metadata is fixture data; no live provider or API is contacted.
// A hosted gateway row is conditional on the catalog returned by the install, not Community capacity.
const FRESH_CATALOG = [
  {"source_id": "openai_codex_chatgpt", "source_kind": "official_cli", "auth_method": "official_cli_oauth", "connection_ref": "BOSS_OPENAI_CODEX_OAUTH_SESSION_REF", "multi_tenant_allowed": false, "supported_models": ["codex"], "provider": "openai", "oauth_scopes": ["codex.local", "codex.agentic_usage"], "connection_metadata": {"login_command": "codex login", "supported_connection_modes": ["browser_oauth"], "official_auth_surface": "Codex CLI Sign in with ChatGPT"}, "credential_ref": "BOSS_OPENAI_CODEX_OAUTH_SESSION_REF"},
  {"source_id": "openai_api", "source_kind": "official_api", "auth_method": "api_key", "connection_ref": "OPENAI_API_KEY", "multi_tenant_allowed": true, "supported_models": ["gpt-5.2-codex", "gpt-5.2-codex-coder"], "provider": "openai", "oauth_scopes": [], "connection_metadata": {}, "credential_ref": "OPENAI_API_KEY"},
  {"source_id": "anthropic_claude_code", "source_kind": "official_cli", "auth_method": "official_cli_oauth", "connection_ref": "BOSS_ANTHROPIC_CLAUDE_OAUTH_SESSION_REF", "multi_tenant_allowed": false, "supported_models": ["claude-sonnet", "claude-opus"], "provider": "anthropic", "oauth_scopes": ["claude_code.local", "claude_subscription_usage"], "connection_metadata": {"login_command": "claude login", "supported_connection_modes": ["browser_oauth"], "official_auth_surface": "Claude Code Pro or Max plan login"}, "credential_ref": "BOSS_ANTHROPIC_CLAUDE_OAUTH_SESSION_REF"},
  {"source_id": "anthropic_api", "source_kind": "official_api", "auth_method": "api_key", "connection_ref": "ANTHROPIC_API_KEY", "multi_tenant_allowed": true, "supported_models": ["claude-sonnet", "claude-opus"], "provider": "anthropic", "oauth_scopes": [], "connection_metadata": {}, "credential_ref": "ANTHROPIC_API_KEY"},
  {"source_id": "typesafe_jev", "source_kind": "official_api", "auth_method": "api_key", "connection_ref": "TYPESAFE_API_KEY", "multi_tenant_allowed": false, "supported_models": ["jev"], "provider": "typesafe", "oauth_scopes": [], "connection_metadata": {"decisions_only": true}, "credential_ref": "TYPESAFE_API_KEY"},
  {"source_id": "google_gemini_cli", "source_kind": "official_cli", "auth_method": "official_cli_oauth", "connection_ref": "GOOGLE_GEMINI_CLI_SESSION", "multi_tenant_allowed": false, "supported_models": ["gemini-cli"], "provider": "google", "oauth_scopes": [], "connection_metadata": {"login_command": "gemini", "supported_connection_modes": ["browser_oauth"], "official_auth_surface": "Gemini CLI Sign in with Google prompt"}, "credential_ref": null},
  {"source_id": "google_api", "source_kind": "official_api", "auth_method": "api_key", "connection_ref": "GOOGLE_API_KEY", "multi_tenant_allowed": true, "supported_models": ["gemini-pro", "gemini-flash"], "provider": "google", "oauth_scopes": [], "connection_metadata": {}, "credential_ref": "GOOGLE_API_KEY"},
  {"source_id": "cursor_cli", "source_kind": "official_coding_agent", "auth_method": "official_cli_oauth", "connection_ref": "BOSS_CURSOR_CLI_ENABLED", "multi_tenant_allowed": false, "supported_models": ["cursor-auto", "openai", "anthropic", "google", "xai"], "provider": "cursor", "oauth_scopes": [], "connection_metadata": {"login_command": "cursor-agent login", "status_command": "cursor-agent status", "supported_connection_modes": ["browser_oauth"], "official_auth_surface": "Cursor Agent CLI browser login"}, "credential_ref": "BOSS_CURSOR_CLI_ENABLED"},
  {"source_id": "cursor_api", "source_kind": "official_coding_agent", "auth_method": "api_key", "connection_ref": "CURSOR_API_KEY", "multi_tenant_allowed": false, "supported_models": ["cursor-auto"], "provider": "cursor", "oauth_scopes": [], "connection_metadata": {}, "credential_ref": null},
  {"source_id": "xai_grok_build", "source_kind": "official_cli", "auth_method": "official_device_oauth", "connection_ref": "BOSS_XAI_GROK_OAUTH_SESSION_REF", "multi_tenant_allowed": false, "supported_models": ["grok-4.3", "grok-code-fast"], "provider": "xai", "oauth_scopes": ["grok_build.local", "grok_code_proxy"], "connection_metadata": {"login_command": "grok login --device-auth", "supported_connection_modes": ["browser_oidc", "device_oauth"], "official_auth_surface": "Grok Build auth.x.ai OAuth2/OIDC"}, "credential_ref": "BOSS_XAI_GROK_OAUTH_SESSION_REF"},
  {"source_id": "xai_api", "source_kind": "official_api", "auth_method": "api_key", "connection_ref": "XAI_API_KEY", "multi_tenant_allowed": true, "supported_models": ["grok-4.3", "grok-code-fast"], "provider": "xai", "oauth_scopes": [], "connection_metadata": {}, "credential_ref": "XAI_API_KEY"},
  {"source_id": "approved_local_model_host", "source_kind": "approved_local_model", "auth_method": "local_host", "connection_ref": "BOSS_LOCAL_MODEL_ID", "multi_tenant_allowed": false, "supported_models": ["approved-local"], "provider": "local", "oauth_scopes": [], "connection_metadata": {}, "credential_ref": "BOSS_LOCAL_MODEL_ID"},
  {"source_id": "managed_cloud_model_gateway", "source_kind": "managed_cloud_model_gateway", "auth_method": "account_connect", "connection_ref": "BOSS_ACCOUNT_CONNECT_URL", "multi_tenant_allowed": true, "supported_models": ["managed-cloud-gateway-auto"], "provider": "boss", "oauth_scopes": [], "connection_metadata": {"required_env": ["BOSS_MANAGED_CLOUD_MODEL_GATEWAY_ENABLED", "BOSS_ACCOUNT_CONNECT_URL", "BOSS_ACCOUNT_CONNECT_TOKEN"], "execution_boundary": "boss_cloud_managed_gateway_adapter"}, "credential_ref": "BOSS_ACCOUNT_CONNECT_TOKEN"},
];
function freshCatalogSources(includeHosted = true) {
  return { project_id: "p", items: FRESH_CATALOG
    .filter((x) => includeHosted || x.source_id !== "managed_cloud_model_gateway")
    .map((x) => ({ ...x, id: `catalog:${x.source_id}`, project_id: "p",
      status: ["openai_api", "typesafe_jev"].includes(x.source_id) ? "missing_credentials" : "declared",
      effective_status: ["openai_api", "typesafe_jev"].includes(x.source_id) ? "missing_credentials" : "declared",
      connection_status: "not_connected", credential_secret_ref_id: null,
      observed_capacity: { credential_present: false, capacity_state: "unknown" },
    })) };
}

// Synthetic key-only records exercise absence/error cases without claiming catalog coverage.
const unusedKeySources = (ids: string[]) => ({ project_id: "p", items: ids.map((id) => ({
  ...entitlementSourcesWire().items[0], source_id: id, effective_status: "missing_credentials",
  connection_status: "not_connected", observed_capacity: { credential_present: false, capacity_state: "unknown" },
  credential_ref: "OPENAI_API_KEY", connection_ref: "OPENAI_API_KEY", credential_secret_ref_id: null as string | null,
})) });
async function choose(id: string) {
  const select = q(document.body, "setup-cap-chooser") as HTMLSelectElement;
  select.value = id;
  await act(async () => { select.dispatchEvent(new Event("change", { bubbles: true })); });
}

describe("unused model sources", () => {
  it("offers eleven unselected actions from the thirteen-source catalog, preserving local and account rows without a write", async () => {
    const sources = freshCatalogSources();
    const ids = sources.items.slice(0, 11).map(x => x.source_id);
    const calls = api(firstRunView(), sources, { items: [] });
    mounted = await mountBoard(firstRunView());
    expect(q(document.body, "su-row-capacity")?.getAttribute("aria-label")).toBe("Source connections");
    expect([...document.querySelectorAll('[data-testid="setup-cap-source"]')].map(x => x.getAttribute("data-source")))
      .toEqual(["approved_local_model_host", "managed_cloud_model_gateway"]);
    expect((q(document.body, "setup-cap-add") as HTMLDetailsElement).open).toBe(false);
    const chooser = q(document.body, "setup-cap-chooser") as HTMLSelectElement;
    expect(chooser.value).toBe("");
    expect([...chooser.options].map(x => x.value)).toEqual(["", ...ids]);
    expect(chooser.options[1].textContent).toBe("Codex (ChatGPT)");
    await click(q(document.body, "setup-cap-add")?.querySelector("summary") ?? null);
    await choose("openai_api");
    expect([...document.querySelectorAll('[data-testid="setup-cap-source"]')].map(x => x.getAttribute("data-source"))).toEqual(["openai_api", "approved_local_model_host", "managed_cloud_model_gateway"]);
    expect(q(src("openai_api"), "setup-cap-key-open")?.textContent).toBe("Add key");
    expect(calls.filter(x => x.method !== "GET")).toEqual([]);
    await click(q(src("openai_api"), "setup-cap-key-open"));
    await type(q(src("openai_api"), "setup-cap-key"), "entered-value");
    const field = q(src("openai_api"), "setup-cap-key");
    await click(q(document.body, "setup-cap-add")?.querySelector("summary") ?? null);
    await click(q(document.body, "setup-cap-add")?.querySelector("summary") ?? null);
    await choose("anthropic_api");
    expect(q(src("openai_api"), "setup-cap-key")).toBe(field);
    expect((field as HTMLInputElement).value).toBe("entered-value");
    expect(calls.filter(x => x.method !== "GET")).toEqual([]);
  });

  for (const id of ["openai_codex_chatgpt", "anthropic_claude_code", "google_gemini_cli", "cursor_cli", "xai_grok_build"]) {
    it(`catalog OAuth chooser preserves ${id} sign-in modes and scope controls without incidental writes`, async () => {
      const sources = freshCatalogSources();
      const expected = sources.items.find(x => x.source_id === id)!;
      const calls = api(firstRunView(), sources, { items: [] });
      mounted = await mountBoard(firstRunView());
      await click(q(document.body, "setup-cap-add")?.querySelector("summary") ?? null);
      await choose(id);
      const row = src(id);
      expect(q(row, "setup-cap-key-open")).toBeNull();
      expect(q(row, "setup-cap-start-open")?.textContent).toBe("Sign in");
      expect(calls.filter(x => x.method !== "GET")).toEqual([]);
      await click(q(row, "setup-cap-start-open"));
      const form = q(row, "setup-cap-start-form")!;
      const modes = form.querySelector('[aria-label="Login"]') as HTMLSelectElement;
      const scopes = form.querySelector('[aria-label="Scopes"]') as HTMLInputElement;
      expect([...modes.options].map(x => x.value)).toEqual(expected.connection_metadata.supported_connection_modes);
      expect(scopes.value).toBe(expected.oauth_scopes.join(", "));
      expect(calls.filter(x => x.method !== "GET")).toEqual([]);
      if (id === "xai_grok_build") {
        modes.value = "device_oauth";
        await act(async () => { modes.dispatchEvent(new Event("change", { bubbles: true })); });
      }
      expect(calls.filter(x => x.method !== "GET")).toEqual([]);
      await click(q(row, "setup-cap-start"));
      expect(calls.filter(x => x.method !== "GET")).toEqual([{
        url: `/v1/entitlements/sources/${id}/connection-sessions`, method: "POST",
        body: { project_id: "p", connection_mode: id === "xai_grok_build" ? "device_oauth" : "browser_oauth",
          requested_scopes: expected.oauth_scopes, actor: "ceo" },
      }]);
    });
  }

  it("does not add hosted capability when the returned catalog omits its gateway", async () => {
    api(firstRunView(), freshCatalogSources(false), { items: [] });
    mounted = await mountBoard(firstRunView());
    const chooser = q(document.body, "setup-cap-chooser") as HTMLSelectElement;
    expect(chooser.options).toHaveLength(12); // Empty choice plus eleven key/OAuth actions.
    expect([...chooser.options].map(x => x.value)).not.toContain("managed_cloud_model_gateway");
    expect([...document.querySelectorAll('[data-testid="setup-cap-source"]')].map(x => x.getAttribute("data-source")))
      .toEqual(["approved_local_model_host"]);
  });

  it("keeps unknown fields, connection states, credentials, models, blocked status and reported capacity visible", async () => {
    const ids = ["missing-field", "unknown", "connected", "key", "model", "blocked", "unsupported", "limited", "failed", "stored-secret", "no-action", "normal"];
    const sources = unusedKeySources(ids);
    sources.items[0].observed_capacity = {} as typeof sources.items[0]["observed_capacity"];
    sources.items[1].effective_status = "unknown";
    sources.items[2].connection_status = "connected";
    sources.items[3].observed_capacity.credential_present = true;
    sources.items[5].effective_status = "blocked";
    sources.items[6].effective_status = "unsupported";
    sources.items[7].observed_capacity.capacity_state = "limited";
    sources.items[8].connection_status = "failed";
    sources.items[9].credential_secret_ref_id = "secret-id";
    sources.items[10].auth_method = "none";
    const view = firstRunView();
    view.models = [{ ...board().models[0], source_id: "model" }];
    api(view, sources, { items: [] });
    mounted = await mountBoard(view);
    expect([...document.querySelectorAll('[data-testid="setup-cap-source"]')].map(x => x.getAttribute("data-source"))).toEqual(ids.slice(0, -1));
    expect(q(src("model"), "setup-cap-replace")).not.toBeNull();
  });

  for (const failure of ["failed", "network failure", "malformed", "full page"] as const) {
    it(`${failure} session reads never hide sources or claim None on failure`, async () => {
      const sources = unusedKeySources(["unused"]);
      const sessions = failure === "full page" ? { items: Array.from({ length: 20 }, (_, i) => ({
        ...entitlementSessionsWire().items[0], id: `s${i}`, source_id: "other",
      })) } : failure === "malformed" ? {} : { items: [] };
      api(firstRunView(), sources, sessions, failure === "failed" ? 503 : failure === "network failure" ? 0 : 200);
      mounted = await mountBoard(firstRunView());
      expect(src("unused")).not.toBeNull();
      expect(q(document.body, "setup-cap-add")).toBeNull();
      if (failure !== "full page") expect(q(document.body, "setup-entitlement-session")?.textContent).toContain("Can't load sessions");
    });
  }

  it("a failed source read cannot create an unused chooser", async () => {
    api(firstRunView(), unusedKeySources(["unused"]), { items: [] }, 200, 503);
    mounted = await mountBoard(firstRunView());
    expect(q(document.body, "setup-entitlements")?.textContent).toContain("Can't load model sources");
    expect(q(document.body, "setup-cap-add")).toBeNull();
  });

  it("keeps pending, failed, expired and completion sessions beside their sources", async () => {
    const states = ["pending", "failed", "expired", "operator_action_required"];
    const sources = unusedKeySources(states);
    api(firstRunView(), sources, { items: states.map(status => ({ ...entitlementSessionsWire().items[0], id: status, source_id: status, status })) });
    mounted = await mountBoard(firstRunView());
    expect(document.querySelectorAll('[data-testid="setup-cap-source"]')).toHaveLength(4);
    expect(document.querySelectorAll('[data-testid="setup-cap-session-row"]')).toHaveLength(4);
    expect(q(src("operator_action_required"), "setup-cap-complete")).not.toBeNull();
    expect(q(document.body, "setup-cap-add")).toBeNull();
  });
});
function firstRunView(): SetupView { const view = board(); view.models = []; return view; }

// Disconnected is a transport state, not evidence that every source needs an OAuth login.
describe("source verdict precedence", () => {
  const base: CapSource = {
    sourceId: "source", provider: "provider", kind: "official_api", effective: "declared",
    auth: "api_key", connection: "not_connected", credentialPresent: false, credentialAbsent: true,
    capacity: "unknown", models: [], modes: [], scopes: [], pooled: false,
  };
  const cases: [Partial<CapSource>, string][] = [
    [{}, "Needs key"],
    [{ auth: "gateway_token" }, "Needs key"],
    [{ auth: "official_cli_oauth" }, "Needs sign-in"],
    [{ auth: "official_device_oauth" }, "Needs sign-in"],
    [{ auth: "local_host" }, "Not configured"],
    [{ auth: "account_connect" }, "Not connected"],
    [{ credentialAbsent: false }, "Not connected"],
    [{ credentialPresent: true, credentialAbsent: false }, "Not connected"],
    [{ effective: "unknown" }, "Unknown"],
    [{ effective: "" }, "Unknown"],
    [{ effective: "unsupported" }, "Unsupported"],
    [{ effective: "runtime_unavailable" }, "Runtime unavailable"],
    [{ effective: "disabled" }, "Disabled"],
    [{ effective: "failed" }, "Failed"],
    [{ effective: "blocked" }, "Blocked"],
    [{ connection: "failed" }, "Failed"],
    [{ connection: "expired", auth: "official_cli_oauth" }, "Expired"],
    [{ connection: "revoked", auth: "official_cli_oauth" }, "Revoked"],
    // A stored key reads Connected (BOSSAPP-530); a connected plan login stays Active.
    [{ effective: "active", connection: "connected", credentialPresent: true, credentialAbsent: false }, "Connected"],
    [{ effective: "active", connection: "connected", auth: "official_cli_oauth" }, "Active"],
    [{ effective: "active", connection: "configured", credentialPresent: true, credentialAbsent: false }, "Active"],
  ];
  for (const [patch, label] of cases) {
    it(`${JSON.stringify(patch)} retains ${label}`, () => {
      expect(stateOf({ ...base, ...patch }).label).toBe(label);
    });
  }
});

describe("model and source anchors share the model section", () => {
  for (const focus of ["models", "capacity"] as const) {
    it(`${focus} preserves its own target and source controls before repository setup`, async () => {
      const view = board();
      view.models = [];
      view.repository.repos = [];
      view.repository.connection = null;
      view.next = "repository";
      const calls = api(view, freshCatalogSources(), { items: [] });
      const landed: HTMLElement[] = [];
      const scroll = vi.spyOn(HTMLElement.prototype, "scrollIntoView").mockImplementation(function (this: HTMLElement) { landed.push(this); });
      try {
        mounted = await mountBoard(view, `?area=setup&focus=${focus}`);
        const models = q(document.body, "su-row-models")!;
        const capacity = q(document.body, "su-row-capacity")!;
        expect(capacity.closest('[data-testid="su-row-models"]')).toBe(models);
        expect(landed[0]).toBe(focus === "models" ? models : capacity);
        expect(models.getAttribute("data-focused")).toBe("true");
        await click(q(document.body, "setup-cap-add")?.querySelector("summary") ?? null);
        await choose("openai_codex_chatgpt");
        expect(q(src("openai_codex_chatgpt"), "setup-cap-start-open")?.textContent).toBe("Sign in");
        expect(calls.filter(x => x.method !== "GET")).toEqual([]);
      } finally { scroll.mockRestore(); }
    });
  }
});

// The CSS hides only the generic waiting summary when a rendered source states absence.
// Happy-dom ignores :has in querySelector; these cases prove only marker/state preservation.
// Actual CSS visibility and phone reflow require the final browser render.
describe("compact Models waiting summary", () => {
  it("a shown disconnected source replaces the duplicate while keeping its name, state and action", async () => {
    const calls = api(firstRun(), freshCatalogSources(), { items: [] });
    mounted = await mountBoard(firstRun());
    const fallback = document.querySelector(".su-model-empty");
    expect(fallback?.textContent).toBe("Not connected");
    expect(document.querySelector(".su-model-sources [data-connection-summary]")).not.toBeNull();
    const local = src("approved_local_model_host");
    expect(local.querySelector('[role="status"]')?.textContent).toBe("Not configured");
    expect(local.querySelector(".su-cap-head")?.firstElementChild?.tagName).toBe("DETAILS");
    await click(q(document.body, "setup-cap-add")?.querySelector("summary") ?? null);
    await choose("anthropic_api");
    expect(src("anthropic_api").querySelector('[role="status"]')?.textContent).toBe("Needs key");
    expect(q(src("anthropic_api"), "setup-cap-key-open")?.closest("details")).toBeNull();
    expect(calls.filter(x => x.method !== "GET")).toEqual([]);
  });

  for (const state of ["unknown", "blocked", "unsupported", "failed"] as const) {
    it(`${state} alone keeps the waiting fallback and its actual source status`, async () => {
      const sources = unusedKeySources([state]);
      if (state === "failed") sources.items[0].connection_status = "failed";
      else sources.items[0].effective_status = state;
      api(firstRun(), sources, { items: [] });
      mounted = await mountBoard(firstRun());
      expect(document.querySelector(".su-model-sources [data-connection-summary]")).toBeNull();
      expect(document.querySelector(".su-model-empty")?.textContent).toBe("Not connected");
      expect(src(state).querySelector('[role="status"]')?.textContent).toBe(state[0].toUpperCase() + state.slice(1));
    });
  }

  for (const failed of [false, true]) {
    it(`${failed ? "failed" : "empty"} source reads keep the waiting fallback and visible explanation`, async () => {
      api(firstRun(), { items: [] }, { items: [] }, 200, failed ? 503 : 200);
      mounted = await mountBoard(firstRun());
      expect(document.querySelector(".su-model-sources [data-connection-summary]")).toBeNull();
      expect(document.querySelector(".su-model-empty")?.textContent).toBe("Not connected");
      expect(q(document.body, "setup-entitlements")?.textContent).toContain(failed ? "Can't load model sources" : "No sources yet");
    });
  }

  it("an unfamiliar long source state keeps every word and never marks confirmed absence", async () => {
    const sources = unusedKeySources(["future-state"]);
    const state = "a very long unrecognized connection state ".repeat(6).trim();
    sources.items[0].effective_status = state;
    api(firstRun(), sources, { items: [] });
    mounted = await mountBoard(firstRun());
    expect(src("future-state").querySelector('[role="status"]')?.textContent?.trim().toLowerCase()).toBe(state);
    expect(document.querySelector(".su-model-sources [data-connection-summary]")).toBeNull();
    expect(document.querySelector(".su-model-empty")?.textContent).toBe("Not connected");
  });

  it("loading source reads keep the waiting fallback until a specific source state exists", async () => {
    api(firstRun(), { items: [] }, { items: [] });
    const readyFetch = globalThis.fetch;
    vi.stubGlobal("fetch", vi.fn((input: string | URL, init?: RequestInit) =>
      String(input).startsWith("/v1/entitlements/sources?") ? new Promise<Response>(() => {}) : readyFetch(input, init)));
    mounted = await mountBoard(firstRun());
    expect(document.querySelector(".su-model-sources [data-connection-summary]")).toBeNull();
    expect(document.querySelector(".su-model-empty")?.textContent).toBe("Not connected");
    expect(q(document.body, "setup-entitlements")?.textContent).toContain("Loading");
  });
});
