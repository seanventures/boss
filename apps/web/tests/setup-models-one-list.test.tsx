// @vitest-environment happy-dom
/**
 * Repo and models › Models, redesigned (2026-10-25). The operator: "it is not easy to detect Add
 * source and choose source — it's not self intuitive to add the model". Held here:
 *
 * - "+ Add a model" is a primary button in the section header; it opens the one drawer
 *   (Subscription / API key / Local), and the catalogue's unused sources live in that drawer.
 * - ONE list: a Models row and its source are one row — name, pill, one sentence, at most one
 *   primary action on its line; ids and raw fields only behind Details, in words.
 * - ONE status: the drawer's provider chips read the rows' own pills, and the list re-reads its
 *   sources whenever the board changes (Connect said Working above while the row below still
 *   said Needs sign-in until a reload).
 * - A subscription whose CLI is signed in is offered Connect — never a Sign in that signs it out.
 * - A key out of credit says so, with Top up.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { click, fakeApi, flush, mountBoard, q, type FakeApi, type Mounted } from "./_helpers/setup-board";
import { board, PILLS } from "./_helpers/setup-fixtures";
import type { SetupView } from "../lib/setup-types";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; document.body.innerHTML = ""; vi.unstubAllGlobals(); });

const row = (id: string) => document.querySelector(`[data-testid="setup-cap-source"][data-source="${id}"]`) as HTMLElement;
const drawer = () => q(document.body, "su-model-drawer") as HTMLElement;

const CLAUDE_SOURCE = (pill: unknown, connection = "connected") => ({
  id: "es_cc", project_id: "p", source_id: "anthropic_claude_code", provider: "anthropic",
  source_kind: "official_cli", auth_method: "official_cli_oauth", status: "declared",
  effective_status: connection === "connected" ? "active" : "missing_oauth_connection",
  connection_status: connection, observed_capacity: { credential_present: connection === "connected", capacity_state: "available" },
  supported_models: ["claude-sonnet", "claude-opus"], connection_metadata: {}, oauth_scopes: [],
  pill, sign_in_provider: "claude_code",
});

/** Sources answered from a function, so a test can change them between reads. */
function withSources(api: FakeApi, items: () => unknown[]) {
  const before = api.answer;
  let reads = 0;
  api.answer = (call) => {
    if (call.url.startsWith("/v1/entitlements/sources?")) { reads += 1; return { status: 200, body: { items: items() } }; }
    if (call.url.startsWith("/v1/entitlement-connection-sessions?")) return { status: 200, body: { items: [] } };
    return before(call);
  };
  return { reads: () => reads };
}

describe("the header says what works and offers + Add a model", () => {
  it("is a primary button in the header that opens the Add a model drawer", async () => {
    fakeApi(board());
    mounted = await mountBoard(board());
    const head = q(document.body, "su-models-head")!;
    const add = q(head, "su-add-model-open") as HTMLButtonElement;
    expect(add.textContent).toBe("Add a model");
    expect(add.className).toContain("su-btn-primary");
    expect(q(head, "su-models-summary")!.textContent).toContain("2 working");
    await click(add);
    expect(drawer().getAttribute("aria-label")).toBe("Add a model");
    expect(["subscription", "api_key", "local"].every((t) => q(drawer(), `su-tile-${t}`))).toBe(true);
    expect(overBudget(q(document.body, "su-row-models")!)).toEqual([]);
  });
});

describe("one list, one line per source", () => {
  it("a Models row and its source are one row; details only behind Details, in words", async () => {
    const api = fakeApi(board());
    withSources(api, () => [CLAUDE_SOURCE({ state: "working", label: "Working", tone: "green", checked_at: null })]);
    mounted = await mountBoard(board());
    await flush();
    expect(document.querySelectorAll('[data-source="anthropic_claude_code"]')).toHaveLength(1);
    const claude = row("anthropic_claude_code");
    const line = claude.querySelector(".su-src-line")!;
    expect(line.querySelector(".su-src-name")!.textContent).toBe("Claude Code");
    expect(q(line as HTMLElement, "su-model-pill")!.textContent).toBe("Working");
    // Working: nothing to do, so no primary button; everything else is behind ⋯.
    expect(line.querySelectorAll(".su-btn").length).toBe(0);
    expect(claude.textContent).not.toMatch(/Source ID|Provider:|Official cli|anthropic_claude_code/);
    await click(q(claude, "setup-cap-details"));
    const details = q(claude, "setup-cap-details-panel")!;
    expect(details.textContent).toContain("Subscription");
    expect(details.getAttribute("title")).toContain("anthropic_claude_code");
  });

  it("each line has at most one primary action", async () => {
    const view = board();
    view.models = [{ ...view.models[1], pill: PILLS.keyRejected }];
    fakeApi(view);
    mounted = await mountBoard(view);
    const line = row("setup-openrouter").querySelector(".su-src-line")!;
    expect([...line.querySelectorAll(".su-btn")].map((b) => b.textContent)).toEqual(["Replace key"]);
  });
});

describe("one status everywhere", () => {
  it("the drawer's Codex chip reads the row's Working, not the CLI's 'sign in'", async () => {
    const view = board();
    view.models = [...view.models, { ...view.models[0], source_id: "openai_codex_chatgpt", provider: "codex", label: "Codex" }];
    view.detected = view.detected.map((d) => d.provider === "codex" ? { ...d, found: "needs_sign_in" } : d);
    fakeApi(view);
    mounted = await mountBoard(view, "?area=setup&add=codex");
    const chip = q(q(drawer(), "su-provider-codex")!, "su-provider-status")!;
    expect(chip.textContent).toBe("Working");
    // And the drawer agrees: Working, Connect ready, no sign-in started.
    expect(q(drawer(), "su-signed-in-on-server")!.textContent).toBe("Working");
    expect((q(drawer(), "su-model-connect") as HTMLButtonElement).disabled).toBe(false);
    expect(q(drawer(), "su-signin-start")).toBeNull();
  });

  it("after Connect the row is re-read: no 'Needs sign-in' under a Working header", async () => {
    const before = board();
    before.models = before.models.filter((m) => m.provider !== "claude_code");
    before.detected = before.detected.map((d) => d.provider === "claude_code" ? { ...d, found: "signed_in" } : d);
    const after: SetupView = { ...before, models: [...board().models] };
    const api = fakeApi(before);
    let connected = false;
    const sources = withSources(api, () => [connected
      ? CLAUDE_SOURCE({ state: "working", label: "Working", tone: "green", checked_at: null })
      : CLAUDE_SOURCE(null, "not_connected")]);
    const answer = api.answer;
    api.answer = (call) => {
      if (call.op === "models") { connected = true; api.view = after; return { status: 200, body: after }; }
      return answer(call);
    };
    mounted = await mountBoard(before);
    expect(q(row("anthropic_claude_code"), "su-model-pill")!.textContent).toBe("Needs sign-in");
    const readsBefore = sources.reads();
    // Signed in on the server: the row offers Connect, never Sign in.
    expect(q(row("anthropic_claude_code"), "setup-cap-sign-in")).toBeNull();
    await click(q(row("anthropic_claude_code"), "su-model-connect-row"));
    // The drawer: Signed in, left of Connect, on one line; nothing signed out.
    const status = q(drawer(), "su-signed-in-on-server")!;
    const connect = q(drawer(), "su-model-connect") as HTMLButtonElement;
    expect(status.textContent).toBe("Signed in");
    expect(status.parentElement).toBe(connect.parentElement);
    expect(status.parentElement!.className).toContain("su-connect-bar");
    await click(connect);
    await flush();
    expect(api.calls.some((c) => c.op === "sign_in_start")).toBe(false);
    expect(sources.reads()).toBeGreaterThan(readsBefore);
    expect(q(row("anthropic_claude_code"), "su-model-pill")!.textContent).toBe("Working");
    expect(document.body.textContent).not.toContain("Needs sign-in");
  });

  it("closing the drawer re-reads the board", async () => {
    const api = fakeApi(board());
    mounted = await mountBoard(board());
    await click(q(document.body, "su-add-model-open"));
    const reads = api.calls.filter((c) => c.method === "GET" && c.url.startsWith("/api/setup")).length;
    await click(drawer().querySelector('[aria-label="Close"]'));
    expect(api.calls.filter((c) => c.method === "GET" && c.url.startsWith("/api/setup")).length).toBeGreaterThan(reads);
  });
});

describe("a key out of credit", () => {
  it("says Out of credit with Top up at the provider", async () => {
    const view = board();
    view.models = [{ ...view.models[1], pill: { state: "no_credit", tone: "red", label: "Out of credit",
      tip: "The provider refused for credit. Top up at the provider, then Test.", action: "fix_link",
      fix_url: "https://openrouter.ai/settings/credits" } }];
    fakeApi(view);
    mounted = await mountBoard(view);
    const r = row("setup-openrouter");
    expect(q(r, "su-model-pill")!.textContent).toBe("Out of credit");
    expect(q(r, "su-model-said")!.textContent).toBe("Top up at the provider.");
    const top = q(r, "su-model-fix") as HTMLAnchorElement;
    expect(top.textContent).toBe("Top up");
    expect(top.getAttribute("href")).toBe("https://openrouter.ai/settings/credits");
  });
});

describe("parity: every action is still reachable", () => {
  it("Test, Replace key, Default model…, Remove in the menu of a key row", async () => {
    fakeApi(board());
    mounted = await mountBoard(board());
    const r = row("setup-openrouter");
    await click(q(r, "su-model-menu"));
    for (const id of ["su-menu-test", "su-menu-replace", "su-menu-default-model", "su-menu-remove"]) {
      expect(q(r, id), id).not.toBeNull();
    }
  });

  it("Sign in again and Disconnect session in the menu of a signed-in subscription", async () => {
    const api = fakeApi(board());
    withSources(api, () => [CLAUDE_SOURCE({ state: "working", label: "Working", tone: "green", checked_at: null })]);
    mounted = await mountBoard(board());
    await flush();
    const r = row("anthropic_claude_code");
    await click(q(r, "su-model-menu"));
    expect(q(r, "su-menu-sign-in")!.textContent).toBe("Sign in again");
    await click(q(r, "setup-cap-disconnect"));
    expect(api.calls.filter((c) => c.method === "POST" && c.url.includes("/disconnect"))).toHaveLength(0);
    await click(q(document.body, "setup-cap-disconnect-yes"));
    expect(api.calls.find((c) => c.url.includes("/disconnect"))!.url)
      .toBe("/v1/entitlements/sources/anthropic_claude_code/disconnect");
  });
});
